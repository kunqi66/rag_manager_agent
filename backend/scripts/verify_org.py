"""be-02 组织管理的验收脚本（spec `P1-08-be-org` §8 的可执行版本）。

跑法（项目根目录）：

    uv run python -m backend.scripts.verify_org

覆盖 P1-08 §8 的全部验收项，重点是那**四个最容易写错的地方**：

- §5.1 物化路径三条不变式（I1 首尾斜杠 / I2 前缀性质 / I3 无环）；
- §5.2 移动的**整棵子树**重写，以及「拒绝时不写库」；
- §5.3 删除的三类引用校验（子部门 / 挂靠用户 / 文档授权）；
- §5.4 组织变更 → 会话缓存失效 → **权限下一次请求即生效**。

外加两条其它脚本覆盖不到的：

- 权限码**逐条粒度**的 403 用例（`kb_hq` 有 `:list` 无 `:create`）；
- 运行时核对库里的 13 个权限码 —— 字典 §5.6 / 后端种子 / 前端 `PERM`
  三处一致性里，**这一条是唯一能验证「库里真实长什么样」的**。

⚠️ 说明：
1. 会**真的**连 `.env` 里的 MySQL 与 Redis（开发期直连 ECS），
   用 `seed_org.py` 灌的种子账号（密码 `Rag@12345`）。
2. 只增删**自己造的**数据：部门名 `ZZVFY_*`、登录名 `zzvfy_*`、角色码 `zzvfy_*`。
   **不碰任何种子行**（种子部门名是「集团总部」这类中文名，不会被前缀命中）。
3. 收尾会清理；脚本开头也扫一次上次崩溃的残留，所以可以反复跑。

Windows 控制台是 GBK —— 输出一律 ASCII，断言布尔值，不靠肉眼读中文。
"""

from __future__ import annotations

import asyncio
import sys

from httpx import ASGITransport, AsyncClient
from sqlalchemy import bindparam, text

import main
from backend.clients import redis_client
from backend.core.db import engine
from backend.service.session_store import session_key

PASSWORD = "Rag@12345"

# 自造数据的标记前缀。清理一律靠它，不靠「记住 id」——
# 脚本中途抛异常时，只有前缀能保证残留可被下一次运行扫掉。
DEPT_MARK = "ZZVFY_"
USER_MARK = "zzvfy_"
ROLE_MARK = "zzvfy_"

# 🔴 Windows 控制台默认是 GBK —— 本脚本的提示里有 ⚠️ / ✅ 这类字符，
#    不重配的话 `print` 直接抛 UnicodeEncodeError，而它抛在**报 SKIP 的那一行**，
#    于是症状是「环境不可用时脚本崩了」，看起来像代码坏了。
#    `errors="replace"` 兜底：终端不支持 UTF-8 时显示成问号，不会崩。
#    （与 `verify_search.py` / `verify_import.py` / `verify_audit.py` 同一约定。）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 —— 重配失败不该让脚本起不来
        pass

_results: list[bool] = []


def check(passed: bool, label: str) -> None:
    _results.append(bool(passed))
    print(f"  [{'OK  ' if passed else 'FAIL'}] {label}")


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client: AsyncClient, username: str, password: str) -> tuple[int, dict]:
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    return resp.status_code, resp.json()


def _find(tree: list[dict], dept_id: str) -> dict | None:
    """在部门树里找节点（递归）。"""
    for node in tree:
        if node["id"] == dept_id:
            return node
        hit = _find(node["children"], dept_id)
        if hit is not None:
            return hit
    return None


def _codes(nodes: list[dict]) -> set[str]:
    """展平权限树的全部 code。"""
    out: set[str] = set()
    for n in nodes:
        out.add(n["code"])
        out |= _codes(n["children"])
    return out


# 字典 §5.6 冻结的**全部 28 个**展开码。**逐字手抄**，不从库里反查 ——
# 从库里反查就成了同义反复，验证不了「库里与字典一致」这件事。
#
# 🔴 曾经这里只抄了 `org:*` 的 13 条，于是 H5 的文案也写成「与字典 §5.6 的 13 个
#    完全一致」—— 而 §5.6 早就把 `kb:*`(6) / `stats:*`(2) / `faq:*`(5) / `gap:*`(2)
#    一并冻结了（见 §5.6 表下那句「连同 `org:*` 的 13 条，本节共 **28** 条」）。
#    后果是：库里种满 28 条之后 H5 反而红了，**而红的原因是正确的数据**。
#    这正是「用例写错、代码是对的」—— 改断言，不改库。
FROZEN_CODES = {
    # —— 部门（3，无 `:list` 码）——
    "org:department:create",
    "org:department:update",
    "org:department:delete",
    # —— 用户（5）——
    "org:user:list",
    "org:user:create",
    "org:user:update",
    "org:user:delete",
    "org:user:grant",
    # —— 角色（5）——
    "org:role:list",
    "org:role:create",
    "org:role:update",
    "org:role:delete",
    "org:role:grant",
    # —— 知识（6，§5.6.1）——
    "kb:document:list",
    "kb:document:upload",
    "kb:document:update",
    "kb:document:status",
    "kb:document:delete",
    "kb:permission:update",
    # —— 看板 / 审计（2，§5.6.2 / §5.6.3）——
    "stats:dashboard:view",
    "stats:audit:list",
    # —— FAQ 沉淀（5，§5.6.4）——
    "faq:candidate:mine",
    "faq:candidate:list",
    "faq:candidate:publish",
    "faq:list",
    "faq:cache:update",
    # —— 知识缺口（2，§5.6.5）——
    "gap:list",
    "gap:task:create",
}


async def _sweep() -> None:
    """清掉上次运行的残留。

    🔴 这里用 `SUBSTRING(...) =` 而**不是** `LIKE 'zzvfy_%'`：
       下划线是 SQL LIKE 的单字符通配符，`zzvfy_` 会变成「zzvfy + 任意一个字符」。
       本例中后果只是多删了几行同前缀数据（无害），但同样的写法用在
       部门物化路径上就会误伤兄弟子树 —— 见 `org_service._move_department`。
       统一用一种不会踩通配符的写法，省得下次复制到危险的地方。
    """
    async with engine.begin() as conn:
        user_ids = [
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "SELECT id FROM sys_user WHERE SUBSTRING(username, 1, 6) = :m"
                    ),
                    {"m": USER_MARK},
                )
            ).all()
        ]
        role_ids = [
            r[0]
            for r in (
                await conn.execute(
                    text("SELECT id FROM sys_role WHERE SUBSTRING(code, 1, 6) = :m"),
                    {"m": ROLE_MARK},
                )
            ).all()
        ]

        if user_ids:
            await conn.execute(
                text("DELETE FROM sys_user_role WHERE user_id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"ids": user_ids},
            )
            await conn.execute(
                text("DELETE FROM sys_user WHERE id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"ids": user_ids},
            )
        if role_ids:
            await conn.execute(
                text("DELETE FROM sys_role_permission WHERE role_id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"ids": role_ids},
            )
            await conn.execute(
                text("DELETE FROM sys_role WHERE id IN :ids").bindparams(
                    bindparam("ids", expanding=True)
                ),
                {"ids": role_ids},
            )

        # 部门按「深的先删」排序：子部门有外键指向父部门，顺序反了会失败。
        # 按 path 长度倒序即可 —— 这正是物化路径带来的便利。
        await conn.execute(
            text(
                "DELETE FROM sys_department "
                "WHERE SUBSTRING(name, 1, 6) = :m "
                "ORDER BY CHAR_LENGTH(path) DESC"
            ),
            {"m": DEPT_MARK},
        )

    redis = redis_client.get_client()
    for uid in user_ids:
        await redis.delete(session_key(uid))


async def _lock_holder() -> int | None:
    """返回当前持有部门树命名锁的连接 id；没人持有则 None。

    用 `IS_USED_LOCK` 而不是「再 GET_LOCK 一次看能否拿到」——
    后者会**自己把锁拿住**，测完还得记得放，反而制造泄漏。
    """
    async with engine.connect() as conn:
        return await conn.scalar(
            text("SELECT IS_USED_LOCK(:name)"), {"name": "rag:org:dept_tree"}
        )


async def _env_ready() -> str | None:
    """环境自检：返回**不可用的原因**，全部可用时返回 None。

    ⚠️ 这个函数存在的唯一理由是「别让脚本崩在半路」。
       没有它的话，云端 MySQL/Redis 不可达时本脚本会甩出一大段 aiomysql
       连接栈然后退出 —— 从输出上**完全分不出**「环境没起来」和
       「代码被改坏了」，而这正是验收脚本最坏的失败模式
       （`verify_search.py` / `verify_audit.py` 的第一条纪律：
       `绝不把「环境不具备」伪装成「通过」`，反过来也一样：
       **绝不把「环境不具备」伪装成「失败」**）。
    """
    try:
        from sqlalchemy import text as _text

        async with engine.connect() as conn:
            await conn.execute(_text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        _banner(f"MySQL 不可达（{type(exc).__name__}）")
        return f"MySQL: {type(exc).__name__}"
    try:
        await redis_client.get_client().ping()
    except Exception as exc:  # noqa: BLE001
        _banner(f"Redis 不可达（{type(exc).__name__}）")
        return f"Redis: {type(exc).__name__}"
    return None


def _banner(reason: str) -> None:
    print("\n" + "=" * 56)
    print(f"SKIP：{reason}")
    print("本脚本的全部用例都需要 MySQL + Redis（开发期直连云端 ECS）。")
    print("⚠️ **本次结果不构成验收**，依赖恢复后重跑。")
    print("=" * 56)


async def run() -> bool | None:
    """返回 `(全部通过)`；**返回 `None` 表示环境不具备、一条都没跑**。

    ⚠️ 三态而不是二态：二态无法表达「没跑」，于是一次连接失败会被
       `main_` 报成「验收失败」，与真正的断言失败混在一起。
    """
    if await _env_ready() is not None:
        return None

    transport = ASGITransport(app=main.app)
    redis = redis_client.get_client()

    await _sweep()

    # 本脚本造出来的东西，收尾要删干净。存下来是为了「按依赖顺序」删，
    # 而不是靠前缀一把梭 —— 靠前缀的话，失败时连报告删了什么都做不到。
    depts: list[str] = []
    users: list[str] = []
    roles: list[str] = []
    admin = ""  # 登录若失败保持空串，各删除函数据此跳过（不拿空 token 去请求）


    async with AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            # ── 登录三种权限档位的账号 ────────────────────────────
            _, body = await _login(client, "admin", PASSWORD)
            admin = body["data"]["access_token"]
            _, body = await _login(client, "chenjing", PASSWORD)
            staff_token = body["data"]["access_token"]   # staff：权限为空
            _, body = await _login(client, "kb_hq", PASSWORD)
            kb_token = body["data"]["access_token"]      # kb_admin：仅两个 :list

            print("\n== A. 鉴权：401 / 403 与权限码粒度（红线 1、7）==")

            r = await client.get("/api/org/departments/tree")
            check(r.json().get("code") == 2001, "A1 无 token 取部门树 -> 2001")

            r = await client.get(
                "/api/org/departments/tree", headers=auth("not-a-jwt")
            )
            check(r.json().get("code") == 2001, "A2 非法 token -> 2001")

            r = await client.get(
                "/api/org/departments/tree", headers=auth(staff_token)
            )
            check(
                r.json().get("code") == 0,
                "A3 仅登录即可取部门树（无权限码，PRD §8.1 契约）",
            )

            r = await client.get("/api/org/users", headers=auth(staff_token))
            check(r.json().get("code") == 2002, "A4 staff 取用户列表 -> 2002（无 org:user:list）")

            # A5 是**粒度**用例：kb_hq 有 :list 但没有 :create。
            # 若把权限做成「有任意 org 权限即放行」，这条会挂。
            r = await client.get("/api/org/users", headers=auth(kb_token))
            check(
                r.json().get("code") == 0,
                "A5a kb_hq 有 org:user:list -> 取用户列表成功",
            )
            r = await client.post(
                "/api/org/users",
                headers=auth(kb_token),
                json={"username": "x", "password": "y", "real_name": "z"},
            )
            check(
                r.json().get("code") == 2002,
                "A5b kb_hq 无 org:user:create -> 新建用户 2002（粒度正确）",
            )

            r = await client.put(
                "/api/org/roles/role_03/permissions",
                headers=auth(kb_token),
                json={"permission_ids": []},
            )
            check(r.json().get("code") == 2002, "A6 kb_hq 无 org:role:grant -> 2002")

            r = await client.get("/api/org/users", headers=auth(admin))
            check(r.json().get("code") == 0, "A7 admin 取用户列表成功")

            print("\n== B. 部门与物化路径不变式（§5.1 的 I1/I2）==")

            r = await client.post(
                "/api/org/departments",
                headers=auth(admin),
                json={"name": f"{DEPT_MARK}root", "sort": 10},
            )
            a = r.json()["data"]
            depts.append(a["id"])
            check(r.json().get("code") == 0, "B1 建根部门 A -> 2000")

            check(
                a["path"] == f"/{a['id']}/",
                "B2 I1 根的 path 是 '/{id}/'（首尾都有斜杠）",
            )
            check(a["parent_id"] is None, "B3 根的 parent_id 为 null")

            r = await client.post(
                "/api/org/departments",
                headers=auth(admin),
                json={"name": f"{DEPT_MARK}mid", "parent_id": a["id"]},
            )
            b = r.json()["data"]
            depts.append(b["id"])
            check(
                b["path"] == f"{a['path']}{b['id']}/",
                "B4 子部门 B 的 path = 父path + 自己的id + '/'",
            )

            r = await client.post(
                "/api/org/departments",
                headers=auth(admin),
                json={"name": f"{DEPT_MARK}leaf", "parent_id": b["id"]},
            )
            c = r.json()["data"]
            depts.append(c["id"])
            check(c["path"].startswith(b["path"]), "B5 I2 孙 C 的 path 以 B 的 path 为前缀")
            # ⚠️ 别用 `path.count("/")` 表达「深度」：它数的是斜杠而不是层级，
            # 而首斜杠的存在与否会让它差 1。这里数 **id 段**，与深度一一对应。
            # （本用例首版写成 `count("/") == 3`，恰好在「根缺首斜杠」那个 bug
            #   存在时通过 —— 断言按错误输出校准过，修好 bug 反而挂了。）
            check(
                len(c["path"].strip("/").split("/")) == 3,
                "B6 三层节点的 path 有 3 个 id 段（每级一段）",
            )

            r = await client.get("/api/org/departments/tree", headers=auth(admin))
            tree = r.json()["data"]
            node_a = _find(tree, a["id"])
            check(node_a is not None, "B7 树里能找到 A")
            check(
                node_a is not None
                and any(ch["id"] == b["id"] for ch in node_a["children"]),
                "B8 A 的 children 含 B（树结构正确嵌套）",
            )
            node_b = _find(tree, b["id"])
            check(
                node_b is not None
                and any(ch["id"] == c["id"] for ch in node_b["children"]),
                "B9 B 的 children 含 C",
            )

            print("\n== C. 移动：整棵子树重写（§5.2）==")

            r = await client.post(
                "/api/org/departments",
                headers=auth(admin),
                json={"name": f"{DEPT_MARK}other"},
            )
            d = r.json()["data"]
            depts.append(d["id"])

            old_c_path = c["path"]
            r = await client.put(
                f"/api/org/departments/{b['id']}",
                headers=auth(admin),
                json={"parent_id": d["id"]},
            )
            check(r.json().get("code") == 0, "C1 把 B 移到 D 下 -> 2000")
            check(
                r.json()["data"]["path"] == f"{d['path']}{b['id']}/",
                "C2 B 自己的 path 已更新到新父下",
            )

            tree = (await client.get("/api/org/departments/tree", headers=auth(admin))).json()["data"]
            c_after = _find(tree, c["id"])
            check(
                c_after is not None and c_after["path"].startswith(f"{d['path']}{b['id']}/"),
                "C3 C（B 的子孙）的 path 跟着被重写 —— 整棵子树动",
            )
            check(
                c_after is not None and c_after["path"].endswith(f"{c['id']}/"),
                "C4 重写只换前缀，各节点自己的后半段保持不变",
            )
            check(
                c_after is not None and c_after["path"] != old_c_path,
                "C5 C 的 path 确实变了（不是没生效）",
            )
            node_a = _find(tree, a["id"])
            check(
                node_a is not None and len(node_a["children"]) == 0,
                "C6 旧父 A 已不再含 B（不是只加不减）",
            )

            print("\n== D. 环校验 I3：拒绝且不写库 ==")

            r = await client.put(
                f"/api/org/departments/{d['id']}",
                headers=auth(admin),
                json={"parent_id": c["id"]},
            )
            check(
                r.json().get("code") == 1001,
                "D1 把 D 移到自己的子孙 C 下 -> 1001（环被拒）",
            )

            tree = (await client.get("/api/org/departments/tree", headers=auth(admin))).json()["data"]
            d_after = _find(tree, d["id"])
            check(
                d_after is not None and d_after["path"] == d["path"],
                "D2 拒绝后 D 的 path 未被改动（校验发生在写之前）",
            )

            r = await client.put(
                f"/api/org/departments/{a['id']}",
                headers=auth(admin),
                json={"parent_id": a["id"]},
            )
            check(r.json().get("code") == 1001, "D3 把部门移到自己下面 -> 1001")

            print("\n== E. 移到顶层：model_fields_set 判据 ==")

            # 这一条是 DTO 那个「传 null ≠ 没传」设计的守门测试。
            # 若实现写成 `if payload.parent_id is not None`，本用例会静默不生效：
            # 接口返回 2000，但部门根本没动 —— 且没有任何报错。
            r = await client.put(
                f"/api/org/departments/{b['id']}",
                headers=auth(admin),
                json={"parent_id": None},
            )
            check(r.json().get("code") == 0, "E1 显式传 parent_id=null -> 2000")
            check(
                r.json()["data"]["parent_id"] is None,
                "E2 parent_id 已置空（移到顶层生效了）",
            )
            check(
                r.json()["data"]["path"] == f"/{b['id']}/",
                "E3 移到顶层后 path 退回 '/{id}/'",
            )
            tree = (await client.get("/api/org/departments/tree", headers=auth(admin))).json()["data"]
            c_after = _find(tree, c["id"])
            check(
                c_after is not None
                and c_after["path"] == f"/{b['id']}/{c['id']}/",
                "E4 子孙 C 的 path 也随之回到顶层前缀",
            )

            print("\n== F. 删除的三类引用校验（§5.3）==")

            r = await client.post(
                "/api/org/departments",
                headers=auth(admin),
                json={"name": f"{DEPT_MARK}parent2"},
            )
            e = r.json()["data"]
            depts.append(e["id"])
            r = await client.post(
                "/api/org/departments",
                headers=auth(admin),
                json={"name": f"{DEPT_MARK}child2", "parent_id": e["id"]},
            )
            f = r.json()["data"]
            depts.append(f["id"])

            r = await client.delete(f"/api/org/departments/{e['id']}", headers=auth(admin))
            check(
                r.json().get("code") == 3002,
                "F1 删有子部门的部门 -> 3002（不级联）",
            )

            r = await client.post(
                "/api/org/users",
                headers=auth(admin),
                json={
                    "username": f"{USER_MARK}holder",
                    "password": PASSWORD,
                    "real_name": "验证用",
                    "department_id": f["id"],
                    "role_ids": [],
                },
            )
            holder = r.json()["data"]["id"]
            users.append(holder)

            r = await client.delete(f"/api/org/departments/{f['id']}", headers=auth(admin))
            check(
                r.json().get("code") == 3002,
                "F2 删有用户挂靠的部门 -> 3002",
            )

            r = await client.delete(f"/api/org/users/{holder}", headers=auth(admin))
            check(r.json().get("code") == 0, "F3 删该用户 -> 2000")
            users.remove(holder)

            r = await client.delete(f"/api/org/departments/{f['id']}", headers=auth(admin))
            check(r.json().get("code") == 0, "F4 用户清空后删部门 -> 2000")
            depts.remove(f["id"])

            print("\n== G. 用户 CRUD 与含子孙的部门筛选 ==")

            r = await client.post(
                "/api/org/users",
                headers=auth(admin),
                json={
                    "username": f"{USER_MARK}alpha",
                    "password": PASSWORD,
                    "real_name": "验证甲",
                    "department_id": e["id"],
                    "role_ids": ["role_03"],
                },
            )
            ua = r.json()["data"]
            users.append(ua["id"])
            check(r.json().get("code") == 0, "G1 新建用户 -> 2000")
            check(
                "password_hash" not in ua and "password" not in ua,
                "G2 响应里没有 password_hash / password（字典 §2.1 严禁回传）",
            )
            check(ua["role_ids"] == ["role_03"], "G3 初始角色已写入")
            check(ua.get("department_name"), "G4 department_name 已回填（非 null）")

            r = await client.post(
                "/api/org/users",
                headers=auth(admin),
                json={
                    "username": f"{USER_MARK}alpha",
                    "password": PASSWORD,
                    "real_name": "重复",
                },
            )
            check(r.json().get("code") == 3002, "G5 登录名重复 -> 3002")

            r = await client.get(
                f"/api/org/users?department_id={e['id']}", headers=auth(admin)
            )
            listed = [x["id"] for x in r.json()["data"]["list"]]
            check(
                ua["id"] in listed,
                "G6 按父部门 E 筛选，能查到挂在 E 自身下的用户",
            )

            # 再在 E 的**子部门**下挂一个用户，验证「含子孙」这一条。
            # ⚠️ 不能复用 F 段那个子部门 —— 它已按 F4 用例删掉了，
            # 拿它建用户只会得到 404。那不是权限或逻辑问题，是测试引用了已删数据。
            r = await client.post(
                "/api/org/departments",
                headers=auth(admin),
                json={"name": f"{DEPT_MARK}sub2", "parent_id": e["id"]},
            )
            g = r.json()["data"]
            depts.append(g["id"])

            r = await client.post(
                "/api/org/users",
                headers=auth(admin),
                json={
                    "username": f"{USER_MARK}beta",
                    "password": PASSWORD,
                    "real_name": "验证乙",
                    "department_id": g["id"],
                    "role_ids": [],
                },
            )
            ub = r.json()["data"]["id"]
            users.append(ub)

            r = await client.get(
                f"/api/org/users?department_id={e['id']}", headers=auth(admin)
            )
            listed = [x["id"] for x in r.json()["data"]["list"]]
            check(
                ub in listed,
                "G7 按 E 筛选也含「子孙部门下」的用户（继承语义）",
            )

            r = await client.get(
                f"/api/org/users?keyword=verify-nonexistent-zzz", headers=auth(admin)
            )
            check(
                len(r.json()["data"]["list"]) == 0,
                "G8 无匹配关键词 -> 空列表（不是全表）",
            )

            r = await client.get("/api/org/users?page=1&page_size=1", headers=auth(admin))
            page = r.json()["data"]
            check(
                set(page) == {"list", "total", "page", "page_size"},
                "G9 分页信封字段名正确（list/total/page/page_size）",
            )
            check(len(page["list"]) == 1, "G10 page_size=1 只返回 1 条")

            r = await client.put(
                f"/api/org/users/{ua['id']}",
                headers=auth(admin),
                json={"real_name": "验证甲改"},
            )
            check(
                r.json().get("code") == 0 and r.json()["data"]["real_name"] == "验证甲改",
                "G11 改姓名 -> 2000 且已生效",
            )

            print("\n== H. 角色与权限 ==")

            r = await client.get("/api/org/roles", headers=auth(admin))
            role_ids = {x["id"] for x in r.json()["data"]}
            check(
                {"role_01", "role_02", "role_03"} <= role_ids,
                "H1 角色列表含 3 个内置角色",
            )

            r = await client.post(
                "/api/org/roles",
                headers=auth(admin),
                json={"code": f"{ROLE_MARK}tmp", "name": "验证角色"},
            )
            role = r.json()["data"]
            roles.append(role["id"])
            check(
                r.json().get("code") == 0 and role["is_builtin"] is False,
                "H2 新建角色 -> is_builtin=False",
            )

            r = await client.post(
                "/api/org/roles",
                headers=auth(admin),
                json={"code": f"{ROLE_MARK}tmp", "name": "重复"},
            )
            check(r.json().get("code") == 3002, "H3 角色码重复 -> 3002")

            r = await client.delete("/api/org/roles/role_03", headers=auth(admin))
            check(r.json().get("code") == 3002, "H4 删内置角色 -> 3002")

            r = await client.get("/api/org/permissions/tree", headers=auth(admin))
            codes = _codes(r.json()["data"])
            check(
                codes == FROZEN_CODES,
                f"H5 库里权限码与字典 §5.6 的 {len(FROZEN_CODES)} 个完全一致"
                f"（实得 {len(codes)}；缺 {sorted(FROZEN_CODES - codes)} / "
                f"多 {sorted(codes - FROZEN_CODES)}）",
            )
            check(
                not any("*" in c for c in codes),
                "H6 库里没有通配码（org:user:* 等已作废）",
            )

            r = await client.put(
                f"/api/org/roles/{role['id']}/permissions",
                headers=auth(admin),
                json={"permission_ids": ["perm_02", "perm_03"]},
            )
            check(r.json().get("code") == 0, "H7 角色授权 -> 2000")
            r = await client.get("/api/org/roles", headers=auth(admin))
            granted = next(x for x in r.json()["data"] if x["id"] == role["id"])
            check(
                set(granted["permission_ids"]) == {"perm_02", "perm_03"},
                "H8 授权是整体替换（内容与请求体一致）",
            )

            r = await client.put(
                f"/api/org/roles/{role['id']}/permissions",
                headers=auth(admin),
                json={"permission_ids": ["perm_999"]},
            )
            check(r.json().get("code") == 3001, "H9 授权不存在的权限 -> 3001")

            r = await client.put(
                f"/api/org/roles/{role['id']}/permissions",
                headers=auth(admin),
                json={"permission_ids": []},
            )
            r = await client.delete(f"/api/org/roles/{role['id']}", headers=auth(admin))
            check(r.json().get("code") == 0, "H10 清空授权后删自定义角色 -> 2000")
            roles.remove(role["id"])

            print("\n== I. 组织变更 -> 会话失效 -> 权限即时生效（§5.4/§5.5）==")

            r = await client.post(
                "/api/org/users",
                headers=auth(admin),
                json={
                    "username": f"{USER_MARK}live",
                    "password": PASSWORD,
                    "real_name": "验证丙",
                    "department_id": e["id"],
                    "role_ids": [],
                },
            )
            live = r.json()["data"]["id"]
            users.append(live)

            _, body = await _login(client, f"{USER_MARK}live", PASSWORD)
            live_token = body["data"]["access_token"]

            r = await client.get("/api/auth/me", headers=auth(live_token))
            check(
                r.json().get("code") == 0 and r.json()["data"]["perms"] == [],
                "I1 新用户（无角色）perms 为空",
            )

            await client.put(
                f"/api/org/users/{live}/roles",
                headers=auth(admin),
                json={"role_ids": ["role_01"]},
            )
            exists = await redis.exists(session_key(live))
            check(
                exists == 0,
                "I2 授权后 rag:auth:session 缓存键已被删除（§5.5）",
            )

            # 这正是「缓存失效」要达到的效果：**同一个旧 token**，
            # 权限在下一次请求就变了，不必等 access 自然过期（≤30 分钟）。
            r = await client.get("/api/auth/me", headers=auth(live_token))
            perms = r.json()["data"]["perms"]
            check(
                "org:user:create" in perms,
                "I3 旧 token 下一次请求即拿到新权限（无需重新登录）",
            )

            await client.put(
                f"/api/org/users/{live}",
                headers=auth(admin),
                json={"status": "disabled"},
            )
            exists = await redis.exists(session_key(live))
            check(exists == 0, "I4 停用用户后会话缓存键也被删除")

            r = await client.get("/api/auth/me", headers=auth(live_token))
            check(
                r.json().get("code") == 2001,
                "I5 停用后旧 token 立刻失效 -> 2001",
            )

            r = await client.delete(f"/api/org/users/{live}", headers=auth(admin))
            check(r.json().get("code") == 0, "I6 删用户 -> 2000")
            users.remove(live)

            print("\n== I2. 移动部门：整棵子树的会话失效（§5.5 点名最易错处）==")

            # 正例：用户挂在**孙子**部门里，只把**祖先**搬走，断言该用户被失效。
            # 若实现只失效「直属该部门的人」，本用例必挂 —— 而这正是 spec 说的
            # 「只失效直属是本 spec 最可能写错的一处」。
            r = await client.get("/api/auth/me", headers=auth(staff_token))
            bystander = r.json()["data"]["user_id"]
            check(
                await redis.exists(session_key(bystander)) == 1,
                "I7 无关用户此刻有会话缓存（反例基线）",
            )

            r = await client.post(
                "/api/org/departments",
                headers=auth(admin),
                json={"name": f"{DEPT_MARK}mv_root"},
            )
            mv_root = r.json()["data"]
            depts.append(mv_root["id"])
            r = await client.post(
                "/api/org/departments",
                headers=auth(admin),
                json={"name": f"{DEPT_MARK}mv_child", "parent_id": mv_root["id"]},
            )
            mv_child = r.json()["data"]
            depts.append(mv_child["id"])

            r = await client.post(
                "/api/org/users",
                headers=auth(admin),
                json={
                    "username": f"{USER_MARK}subtree",
                    "password": PASSWORD,
                    "real_name": "验证丁",
                    "department_id": mv_child["id"],
                    "role_ids": [],
                },
            )
            subtree_user = r.json()["data"]["id"]
            users.append(subtree_user)

            await _login(client, f"{USER_MARK}subtree", PASSWORD)
            check(
                await redis.exists(session_key(subtree_user)) == 1,
                "I8 子树内用户登录后有会话缓存（正例基线）",
            )

            r = await client.put(
                f"/api/org/departments/{mv_root['id']}",
                headers=auth(admin),
                json={"parent_id": e["id"]},
            )
            check(r.json().get("code") == 0, "I9 把含该用户的部门树整体移到 E 下 -> 2000")
            check(
                await redis.exists(session_key(subtree_user)) == 0,
                "I10 子孙部门里的用户会话被失效（整棵子树口径）",
            )
            check(
                await redis.exists(session_key(bystander)) == 1,
                "I11 无关用户的会话**未**被误删（防一把全删）",
            )

            # 命名锁必须已释放：泄漏的话此后**所有**部门移动都会被 3002 挡掉，
            # 且因为锁是连接级的，要等那条连接被回收才恢复 —— 现象是「过一会儿自己好了」。
            check(
                await _lock_holder() is None,
                "I12 移动结束后命名锁已释放（IS_USED_LOCK 为 NULL）",
            )

            print("\n== J. 清理自己造的数据 ==")
            await _delete_all(client, admin, depts, users, roles)
            check(
                not users and not depts and not roles,
                "J1 自造数据已全部删除（走接口，不是扫表）",
            )
        finally:
            # 异常路径的兜底：接口删不动的残留按前缀扫表清掉，
            # 保证下次运行不被上次的残留干扰。
            if users or depts or roles:
                await _sweep()

    await engine.dispose()

    passed = sum(_results)
    total = len(_results)
    print(f"\n{'=' * 56}\n结果：{passed}/{total} 通过")
    if passed != total:
        print("存在失败项，见上方 FAIL 行。")
    return passed == total


async def _delete_all(
    client: AsyncClient, token: str, depts: list[str], users: list[str], roles: list[str]
) -> None:
    """用接口把自造数据删干净，删成功的从列表里摘掉。

    反复重试直到**没有进展**，而不是按固定顺序删一次：
    删除有依赖方向（子部门必须在父部门之前删），但手里这三个 id 列表是**无序**的，
    顺序排错一次就永久卡住（每次重跑还会重踩）。
    重试法对顺序不敏感，最多跑「树深 + 1」轮就收敛。
    """
    if not token:
        return
    for _ in range(8):
        progress = False
        for uid in list(users):
            r = await client.delete(f"/api/org/users/{uid}", headers=auth(token))
            if r.json().get("code") == 0:
                users.remove(uid)
                progress = True
        for did in list(depts):
            r = await client.delete(f"/api/org/departments/{did}", headers=auth(token))
            if r.json().get("code") == 0:
                depts.remove(did)
                progress = True
        for rid in list(roles):
            r = await client.delete(f"/api/org/roles/{rid}", headers=auth(token))
            if r.json().get("code") == 0:
                roles.remove(rid)
                progress = True
        if not progress:
            return


def main_() -> None:
    ok_ = asyncio.run(run())
    if ok_ is None:
        # 环境不具备 —— 退出码非 0（不能让人把「没跑」当成功），
        # 但上面的 SKIP 横幅已经把原因说清楚了。
        sys.exit(1)
    passed = sum(_results)
    print(f"\n{passed}/{len(_results)} checks passed")
    sys.exit(0 if ok_ else 1)


if __name__ == "__main__":
    main_()
