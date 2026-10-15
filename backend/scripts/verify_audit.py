"""be-06 审计的验收脚本（spec `P2-12-be-audit` §8 的可执行版本）。

跑法（项目根目录）：

    uv run python -m backend.scripts.verify_audit

脚本分四层，**能跑的就跑，跑不了的明确报 SKIP 并说明原因** ——
绝不把「环境不具备」伪装成「通过」（那是验收脚本最坏的失败模式）。

| 层 | 需要什么 | 覆盖 |
|---|---|---|
| **T1 离线** | 什么都不需要 | 动作全集与白名单、脱敏器、`record()` 的「不 commit」契约、迁移/模型列与索引一致、**调用点静态核查**、路由与调度注册、配置项齐备 |
| **T1.5 假会话** | 什么都不需要 | 用**假 AsyncSession** 驱动 `list_audit_logs` / `count_action`：筛选条件拼装、分页 offset/limit、`username` 回填 |
| **T2 接口** | MySQL + **Redis** | 端点鉴权（无 token / 无权限码）、分页结构、**「登录」这个动作自己留下的审计痕**、时间范围默认值 |
| **T3 链路** | ＋ worker ＋ beat | 保留期清理任务真跑一次、过期行真被删 |

⚠️ **T1 里最有价值的一类是「调用点静态核查」**（T1-40 ~ T1-43）。
   本模块的核心风险不是 `audit_service` 写错，而是**某个写操作忘了调它** ——
   那种缺陷在运行时完全无声：接口正常返回 200，只是审计表里少了一行，
   而且没人会去数。静态核查把一个「靠人评审」的约定变成了脚本能红的东西。
   这正是 spec §5.4 那句「漏留痕按缺陷处理」的可执行版本。

⚠️ Windows 控制台是 GBK —— 输出一律 **ASCII**，断言**布尔值与具体值**，
不靠肉眼读中文。与 `verify_org.py` / `verify_import.py` / `verify_search.py` 同一约定。

⚠️ 会**真的**连 `.env` 里的 MySQL / Redis（开发期直连 ECS），
   只读 + 只增自己造的数据（标记前缀 `ZZVFYAUDIT`）。**绝不动种子数据**，
   也**绝不删除**任何审计行 —— 删审计行本身就是本模块要防的事。
"""

from __future__ import annotations

import asyncio
import ast
import inspect
import re
import sys
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects import mysql

import main
from backend.core.db import engine
from backend.core.exceptions import ErrorCode, ParamError
from backend.core.settings import settings
from backend.models.audit_log import ANONYMOUS_USER_ID, SYSTEM_USER_ID, SysAuditLog
from backend.schemas.audit import AuditLogQuery
from backend.schemas.auth import AuthContext
from backend.service import audit_service
from backend.service.audit_service import AuditAction, AuditTargetType

PASSWORD = "Rag@12345"
#: 自造数据的标记前缀。**不含下划线** —— `LIKE 'zzvfy_%'` 里的 `_` 是通配符，
#: 会连带命中兄弟数据（CLAUDE.md 已知坑第一条）。
MARK = "ZZVFYAUDIT"

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"

# 🔴 Windows 控制台默认是 GBK，本脚本的标签里有 🔴 —— 不重配的话
#    `print` 直接抛 UnicodeEncodeError，脚本当场崩，而前面已打的 OK 行
#    看起来像「跑到一半莫名退出」。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 —— 重配失败不该让脚本起不来
        pass

_results: list[bool] = []
_skipped: list[str] = []


def check(passed: bool, label: str) -> None:
    _results.append(bool(passed))
    print(f"  [{'OK  ' if passed else 'FAIL'}] {label}")


def skip(label: str, reason: str) -> None:
    """环境不具备 —— 记一笔，**不计入通过数**。"""
    _skipped.append(label)
    print(f"  [SKIP] {label} —— {reason}")


def _src(rel: str) -> str:
    return (BACKEND / rel).read_text(encoding="utf-8")


def _code_only(src: str) -> str:
    """去掉注释与 docstring，只留代码本体。

    用于「源码里不许出现 X」这类静态断言。**必须去注释与 docstring**：
    本项目的注释会大段引用被禁的东西（例如「原先有 `user_context`，已删除」
    或「不要用 print」），直接子串匹配会把解释本身当成违规，
    于是用例永远红着，最后被人注释掉 —— 那才是真损失。

    ⚠️ docstring 按 `ast` 给的**行列位置**挖空，不能用
       `src.replace(docstring_value, "")`：源文件里写的是转义后的 `\\n\\n`，
       而 docstring 的**值**是真换行 —— 两者不相等，替换静默失效。
       （`verify_search.py` 首跑就是这么错的。）
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:  # pragma: no cover —— 源码本身语法错该在别处暴露
        return src

    blanked = src.splitlines()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            and first.end_lineno is not None
        ):
            for i in range(first.lineno - 1, min(first.end_lineno, len(blanked))):
                blanked[i] = ""

    for i, line in enumerate(blanked):
        if not line:
            continue
        # 只处理行注释；本项目不用 `#` 做字符串拼接，不必处理引号内的 `#`
        hash_at = line.find("#")
        if hash_at >= 0:
            blanked[i] = line[:hash_at]
    return "\n".join(blanked)


# ═══════════════════════════════════════════════════════════════
# 假会话：不需要数据库就能驱动 list_audit_logs / count_action
# ═══════════════════════════════════════════════════════════════


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows

    def __iter__(self):
        return iter(self._rows)

    def one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """只实现 `list_audit_logs` / `count_action` 用到的那几个方法。

    ⚠️ 不做事务语义模拟 —— 那会变成一个半吊子 ORM，测的是假实现而不是真代码。
       这里只关心「构造了什么语句」与「拿到结果后怎么回填」。
    """

    def __init__(self, rows: list | None = None) -> None:
        self._rows = rows or []
        self.last_stmt = None
        self.scalar_calls = 0

    async def scalars(self, stmt):
        self.last_stmt = stmt
        return _FakeResult(self._rows)

    async def execute(self, stmt):
        self.last_stmt = stmt
        return _FakeResult(self._rows)

    async def scalar(self, stmt):
        self.last_stmt = stmt
        self.scalar_calls += 1
        return 0


def _sql(stmt) -> str:
    """把语句编译成 MySQL 方言的字符串（不连接数据库）。"""
    return str(stmt.compile(dialect=mysql.dialect()))


def _ctx(
    user_id: str = "u_audit",
    *,
    username: str = "u_audit",
    ip: str | None = "10.0.0.1",
) -> AuthContext:
    return AuthContext(
        user_id=user_id,
        username=username,
        department_id="dept_a",
        depts=["dept_root", "dept_a"],
        roles=["role_staff"],
        perms=[],
        ip=ip,
    )


# ═══════════════════════════════════════════════════════════════
# T1 离线
# ═══════════════════════════════════════════════════════════════


def tier1_offline() -> None:
    print("\n── T1 离线（动作全集 / 脱敏 / 契约 / 迁移 / 调用点）──")

    # ---- T1-1 动作全集与命名规则 ----
    actions = {
        name: value
        for name, value in vars(AuditAction).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    check(len(actions) == 28, f"T1-1  动作全集 = 28 个（实测 {len(actions)}）")
    check(
        len(set(actions.values())) == 28,
        "T1-2  28 个动作的**值**互不重复（重名会让两类操作筛在一起）",
    )
    # ⚠️ 段内允许下划线：`auth.login_failed` 是既有动作（字典 §5.5 的清单里有），
    #    用 `^[a-z.]+$` 这种更紧的写法会把它误判成违规。
    _SEG = r"[a-z]+(?:_[a-z]+)*"
    check(
        all(re.fullmatch(rf"{_SEG}(\.{_SEG})+", v) for v in actions.values()),
        "T1-3  命名规则 1/2：全部带域前缀、多级对象继续加点，无裸名",
    )
    domains = {v.split(".")[0] for v in actions.values()}
    check(
        domains == {"auth", "department", "user", "role", "document", "import", "faq", "gap"},
        f"T1-4  域前缀全集符合字典 §5.5 规则 1（实测 {sorted(domains)}）",
    )
    check(
        actions["DOCUMENT_STATUS_UPDATE"] == "document.status.update",
        "T1-5  §5.5 规则 4 的例外写法保留：document.status.update 用单动作 + detail",
    )

    # ---- T1-6 target_type 全集 ----
    targets = {
        name: value
        for name, value in vars(AuditTargetType).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    check(len(targets) == 7, f"T1-6  target_type 全集 = 7 个（实测 {len(targets)}）")

    # ---- T1-7~T1-11 白名单结构 ----
    wl = audit_service._DETAIL_WHITELIST
    check(set(wl) == set(actions.values()), "T1-7  白名单按 action 逐条登记，无遗漏无多余")
    check(
        all(isinstance(v, frozenset) for v in wl.values()),
        "T1-8  白名单的值一律 frozenset（可变集合会被调用点就地改掉）",
    )
    check(wl[AuditAction.LOGIN] == frozenset(), "T1-9  auth.login 无 detail（没什么可记的）")
    check(
        wl[AuditAction.LOGIN_FAILED] == frozenset({"reason", "username"}),
        "T1-10 auth.login_failed 必须留 username（user_id 可能是 anonymous，不留就完全无法追溯）",
    )
    check(
        wl[AuditAction.USER_RESET_PASSWORD] == frozenset(),
        "T1-11 user.reset_password 的 detail 必须为空集（白名单里没有键 = 全丢）",
    )

    # ---- T1-12~T1-20 脱敏器 ----
    s = audit_service.sanitize_detail
    check(s("no.such.action", {"a": 1}) == {}, "T1-12 未登记动作 → 清空（失败向安全侧倒）")
    check(s(AuditAction.LOGIN, {"anything": 1}) == {}, "T1-13 非白名单键 → 丢弃（默认拒绝）")
    check(
        s(AuditAction.USER_CREATE, {"username": "u", "role_ids": ["r1"]})
        == {"username": "u"},
        "T1-14 user.create 收 role_ids_count、拒 role_ids（id 集合 = 权限快照）",
    )
    check(
        s(AuditAction.USER_CREATE, {"new_password": "x", "username": "u"})
        == {"username": "u"},
        "T1-15 子串兜底：new_password 被剥离，但**其余键保留**（不是整包清空）",
    )
    check(
        s(AuditAction.LOGIN_FAILED, {"reason": "bad_password", "access_token": "x"})
        == {"reason": "bad_password"},
        "T1-16 子串兜底：access_token 被剥离",
    )
    check(s(AuditAction.LOGIN, {}) == {} and s(AuditAction.LOGIN, None) == {},
          "T1-17 空 detail / None → {}（不抛错）")
    check(
        s(AuditAction.ROLE_GRANT_PERMISSION, {"permission_ids": ["p1"], "permission_count": 3})
        == {"permission_count": 3},
        "T1-18 role.grant_permission 收数量、拒 id 集合（提权的集中入口）",
    )
    check(
        s(AuditAction.DOCUMENT_PERMISSION_UPDATE, {"grant_entity_counts": {"dept": 2}})
        == {"grant_entity_counts": {"dept": 2}},
        "T1-19 document.permission.update 收各维度条数（dict 值原样保留）",
    )
    check(
        all(
            not any(bad in k.lower() for bad in audit_service._DENY_SUBSTRINGS)
            for keys in wl.values()
            for k in keys
        ),
        "T1-20 🔴 白名单自身不含任何敏感子串键（T1-15 的兜底不能靠白名单写错来兜）",
    )

    # ---- T1-21~T1-24 record() 的契约 ----
    check(
        not inspect.iscoroutinefunction(audit_service.record),
        "T1-21 record() 是**同步**函数（调用点不必为留痕多一次 await）",
    )
    check(
        inspect.iscoroutinefunction(audit_service.record_detached)
        and inspect.iscoroutinefunction(audit_service.record_read),
        "T1-22 record_detached / record_read 是协程（自开事务必须 await）",
    )
    rec_src = _code_only(inspect.getsource(audit_service.record))
    check(".commit(" not in rec_src, "T1-23 🔴 record() 源码里不含 commit —— 事务边界归调用方")
    # 全仓核查：`record(` 的调用点里不能有人替它 commit（那会提前提交业务写）
    check(
        ".rollback(" not in rec_src and "flush(" not in rec_src,
        "T1-24 record() 不发 flush / rollback（不插进调用方的语句顺序）",
    )
    det_src = _code_only(inspect.getsource(audit_service.record_detached))
    check("except Exception" in det_src and "logger.exception" in det_src,
          "T1-25 record_detached 吞掉全部异常并留全栈（审计失效必须看得见）")
    rr_src = _code_only(inspect.getsource(audit_service.record_read))
    check(
        "record_detached" in rr_src and "dedup" in inspect.getsource(audit_service.record_read),
        "T1-26 record_read = record_detached + 去重窗口（读路径唯一的差异）",
    )
    check(
        "{user_id}" in audit_service._READ_DEDUP_KEY
        and "{target_id}" in audit_service._READ_DEDUP_KEY
        and audit_service._READ_DEDUP_KEY.startswith("rag:"),
        "T1-27 去重键同时含 user_id 与 target_id，且带 rag: 前缀",
    )

    # ---- T1-28~T1-32 _build_row ----
    check(SYSTEM_USER_ID == "system", "T1-28 系统动作的 user_id 字面量 = 'system'")
    check(ANONYMOUS_USER_ID == "anonymous", "T1-29 匿名 user_id 字面量 = 'anonymous'")
    row = audit_service._build_row(_ctx(user_id=""), AuditAction.LOGOUT, "user", None, None)
    check(row.user_id == ANONYMOUS_USER_ID, "T1-30 user_id 空串 → 落 anonymous（不是空串）")
    check(row.detail is None, "T1-31 空 detail → 落 NULL（不是空 JSON 对象）")
    check(row.id.startswith("aud"), "T1-32 审计行 id 前缀 = aud")
    row2 = audit_service._build_row(
        _ctx(), AuditAction.LOGIN_FAILED, "user", "u1", {"reason": "x"}
    )
    check(
        row2.detail == {"reason": "x"} and row2.ip == "10.0.0.1",
        "T1-33 _build_row 透传 ip、并已过脱敏（detail 只剩白名单键）",
    )
    try:
        audit_service._build_row(_ctx(), "bogus.action", None, None, None)
        check(False, "T1-34 未登记动作 → 必须抛错")
    except ParamError as exc:
        # 🔴 检查的是**业务码**：裸 BizError 默认 5000，前端会当成服务端故障。
        check(
            exc.code == ErrorCode.PARAM_ERROR,
            f"T1-34 未登记动作抛 ParamError 且码为 1001（实测 {exc.code}）",
        )
    except Exception as exc:  # noqa: BLE001
        check(False, f"T1-34 抛了非 ParamError：{type(exc).__name__}")

    # ---- T1-35 假会话验证「record 只 add 不 commit」 ----
    # commit() 直接抛 AssertionError：这是**结构性**守门 ——
    # 若哪天有人往 record() 里加了 commit，这里不是「断言失败」而是当场炸，
    # 连「先提交了业务写再报错」的中间态都不会出现。
    class _NoCommitSession(_FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.added: list = []
            self.committed = 0

        def add(self, obj) -> None:
            self.added.append(obj)

        async def commit(self) -> None:
            self.committed += 1
            raise AssertionError("record() 不该 commit")

    ns = _NoCommitSession()
    audit_service.record(ns, _ctx(), AuditAction.LOGOUT, target_type="user", target_id="u1")
    check(
        len(ns.added) == 1 and ns.committed == 0,
        "T1-35 record() 只 add 一行、**一次都没 commit**（假会话里 commit 会直接炸）",
    )
    ns2 = _NoCommitSession()
    try:
        audit_service.record(ns2, _ctx(), "bogus.action")
        check(False, "T1-36 未登记动作 → record() 必须抛错")
    except ParamError:
        check(len(ns2.added) == 0, "T1-36 抛错时**一行都没写**（不是写一半）")

    # ---- T1-37 静态：无 os.getenv / 无 print / 无 text() ----
    svc_src = _src("service/audit_service.py")
    svc_code = _code_only(svc_src)
    check("os.getenv" not in svc_code, "T1-37 audit_service 无 os.getenv（配置一律走 settings）")
    check(
        not re.search(r"^\s*print\(", svc_code, re.M),
        "T1-38 audit_service 无 print（日志一律走 logging）",
    )
    check(
        "text(" not in svc_code,
        "T1-39 audit_service 全 ORM、无 text() 裸 SQL（安全红线 8）",
    )

    # ---- T1-40~T1-43 调用点静态核查（本层最有价值的一类） ----
    call_sites = _scan_call_sites()
    # 🔴 **唯一**允许没有调用点的动作。
    #
    # 这里曾经列了 6 个：除了 `USER_RESET_PASSWORD`，还有 be-08/be-09 的 5 个
    # P3 动作（`FAQ_PUBLISH` / `FAQ_REJECT` / `FAQ_CACHE_TOGGLE` /
    # `GAP_TASK_CREATE` / `GAP_STATUS_UPDATE`）—— 那是 be-06 落地时
    # 「P3 还没实现」的**过渡态**，当时用反断言（T1-41）防止误报。
    #
    # ⚠️ P3 落地后这 5 条反断言变成了**掩护**：它们把「P3 的写操作根本没留痕」
    #    一路判成通过，而那个缺陷在运行时完全无声（接口照回 200，
    #    只是审计表少一行，没人会去数）。已实测确认过：这 5 个动作在
    #    be-08/be-09 落地后仍有 3 个（FAQ 族）没有调用点，直到本次补上。
    #    → 反断言的**生命周期与它描述的那个"尚未实现"同长**，
    #      模块落地时必须回来删掉它，否则它就从护栏变成盲区。
    no_site_expected = {
        "USER_RESET_PASSWORD",  # 全项目无密码重置端点（§5.4 实现期更正）
    }
    missing = [
        name
        for name in actions
        if name not in no_site_expected and not call_sites.get(name)
    ]
    check(
        not missing,
        f"T1-40 🔴 每个应有调用点的动作都至少被引用一次（缺失：{missing}）",
    )
    leaked = [name for name in no_site_expected if call_sites.get(name)]
    check(
        not leaked,
        f"T1-41 唯一允许没有调用点的是 reset_password（实测多出：{leaked}）",
    )
    # 🔴 P3 的 5 个动作**逐个**点名，不靠总数。总数相等的组合很多
    #    （少一个 FAQ 的、多一个别的），点名才守得住「be-08/be-09 真的接上了」。
    p3_actions = {
        "FAQ_PUBLISH",
        "FAQ_REJECT",
        "FAQ_CACHE_TOGGLE",
        "GAP_TASK_CREATE",
        "GAP_STATUS_UPDATE",
    }
    p3_missing = sorted(n for n in p3_actions if not call_sites.get(n))
    check(
        not p3_missing,
        f"T1-41b 🔴 be-08/be-09 的 5 个写动作都留痕了（缺：{p3_missing}）",
    )
    wired = sorted(n for n, v in call_sites.items() if v)
    # 27 = 全集 28 个动作 - 唯一没有调用点的 reset_password。
    # 这个数字是**变更绊线**：加动作或加调用点时必须回来改它，改的过程就是复核。
    check(
        len(wired) == 27,
        f"T1-42 有调用点的动作 = 27 个（实测 {len(wired)}；差值 "
        f"{sorted(set(wired) ^ set(n for n in actions if n not in no_site_expected))}）",
    )
    # 至少有一处是 be-04 的读取路径，且它走的是 record_read 而不是 record
    read_src = _code_only(_src("engines/import_engine/lifecycle.py"))
    check(
        "record_read" in read_src,
        "T1-43 document.read 走 record_read（读写两条路径不能混用）",
    )
    check(
        "except ImportError" not in read_src,
        "T1-44 🔴 be-04 的「audit_service 尚不存在」运行时探针已收口（它会让漏审计变静默）",
    )

    # ---- T1-45~T1-49 迁移与模型一致 ----
    mig = _src("../alembic/versions/0004_create_audit_log.py")
    cols = {c.name for c in SysAuditLog.__table__.columns}
    mig_cols = set(re.findall(r'sa\.Column\(\s*"([a-z_]+)"', mig))
    check(
        cols == mig_cols,
        f"T1-45 迁移 0004 的列集 == 模型列集（差异：{cols ^ mig_cols}）",
    )
    check("updated_at" not in cols, "T1-46 🔴 模型无 updated_at（审计只增不改的硬保证）")
    check(
        SysAuditLog.__table__.c.detail.type.__class__.__name__ == "JSON",
        "T1-47 detail 是 JSON 而非 TEXT（筛选要按语义读键）",
    )
    check(
        not SysAuditLog.__table__.c.user_id.nullable,
        "T1-48 user_id 非空（可空会造出一个查不到的隐形分组）",
    )
    model_idx = {
        i.name: [c.name for c in i.columns] for i in SysAuditLog.__table__.indexes
    }
    expected_idx = {
        "ix_sys_audit_log_user_created": ["user_id", "created_at"],
        "ix_sys_audit_log_created_at": ["created_at"],
        "ix_sys_audit_log_target": ["target_type", "target_id", "created_at"],
    }
    check(
        model_idx == expected_idx,
        f"T1-49 三个索引名与列序与字典 §2.1 一致（差异：{model_idx}）",
    )
    mig_all = re.findall(
        r'op\.create_index\(\s*"(ix_sys_audit_log_[a-z_]+)",\s*"sys_audit_log",\s*\[([^\]]+)\]',
        mig,
    )
    mig_idx = {
        name: [p.strip().strip('"') for p in cols_.split(",")] for name, cols_ in mig_all
    }
    check(mig_idx == expected_idx, "T1-50 迁移里的索引与模型逐字一致（两边各写各的会漂移）")
    check(
        'down_revision: str | None = "0003"' in mig,
        "T1-51 迁移 0004 的 down_revision = 0003（迁移链不断）",
    )

    # ---- T1-52 配置项齐备 ----
    # ⚠️ 涉及「真编译一次 SQL」的用例在 T1.5（那一层已经是 async），
    #    不在这里 `asyncio.run` —— `tier1_offline` 本身就是从 `asyncio.run(run())`
    #    里被调用的，再嵌一层会 `RuntimeError: asyncio.run() cannot be called
    #    from a running event loop`（本脚本首跑就是这么挂的）。
    need = [
        "audit_retention_days",
        "audit_purge_batch_size",
        "audit_read_dedup_seconds",
        "audit_default_range_days",
        "audit_page_size_max",
        "audit_trust_proxy_ip",
        "audit_purge_retry_backoff_base",
        "audit_purge_retry_backoff_cap",
    ]
    missing_cfg = [n for n in need if not hasattr(settings, n)]
    check(not missing_cfg, f"T1-52 8 个 audit_* 配置项齐备（缺：{missing_cfg}）")
    schema_code = _code_only(_src("schemas/audit.py"))
    check(
        "le=settings.audit_page_size_max" in schema_code,
        "T1-53 page_size 上限取自 settings.audit_page_size_max（不是硬编码 100）",
    )
    # 上限真的生效：构造一个超限的查询应当被 Pydantic 拦掉
    try:
        AuditLogQuery(page_size=settings.audit_page_size_max + 1)
        check(False, "T1-54 page_size 超上限必须被 DTO 拦住")
    except Exception:  # noqa: BLE001 —— ValidationError
        check(True, "T1-54 page_size 超上限被 DTO 拦住（无过滤即全表，上限就是成本阀）")

    # ---- T1-55~T1-59 路由与调度 ----
    paths = main.app.openapi()["paths"]
    check("/api/stats/audit-logs" in paths, "T1-55 GET /api/stats/audit-logs 已注册")
    router_src = _src("api/stats_router.py")
    check(
        'require_permission("stats:audit:list")' in router_src,
        "T1-56 🔴 端点独立挂 stats:audit:list（安全红线 7，不靠前端隐藏菜单）",
    )
    router_code = _code_only(router_src)
    check(
        "select(" not in router_code and "text(" not in router_code,
        "T1-57 路由层无 SQL / 无业务逻辑（分层铁律）",
    )

    from backend.tasks.celery_app import celery_app

    check(
        "backend.tasks.audit_tasks" in celery_app.conf.include,
        "T1-58 celery include 含 audit_tasks（漏写 = 任务不执行且不报错到业务层）",
    )
    beat = celery_app.conf.beat_schedule
    check("purge-audit-logs-daily" in beat, "T1-59 beat 有 purge-audit-logs-daily")
    sched = beat.get("purge-audit-logs-daily", {}).get("schedule")
    # ⚠️ `crontab(hour=3, minute=17)` 把**整数**存进 `_orig_hour` / `_orig_minute`
    #    （不是集合）。传 `{3}` 来比是错的 —— 这里比的是 `== 3`。
    check(
        getattr(sched, "_orig_hour", None) == 3
        and getattr(sched, "_orig_minute", None) == 17,
        "T1-60 清理计划 = 03:17（刻意避开整点，别撞上 */10 的预聚合）",
    )
    task_src = _code_only(_src("tasks/audit_tasks.py"))
    check(
        "fresh_session" in task_src and "SessionLocal" not in task_src,
        "T1-61 🔴 清理任务用 fresh_session 而非 SessionLocal（异步 engine 不能跨 asyncio.run 复用）",
    )
    # 🔴 查的是 Celery 的**关键字实参**写法，不是裸子串 —— 裸 `"retry_backoff" in src`
    #    会被本模块自己的配置名 `audit_purge_retry_backoff_cap` 命中，
    #    于是一个「退避算对了」的文件被判成违规（本脚本首跑就是这么错的）。
    #    凡是用子串查标识符的地方都要先问一句：这个名字会不会是别人名字的一部分？
    check(
        "countdown=" in task_src
        and "retry_backoff=" not in task_src
        and "retry_backoff_max=" not in task_src,
        "T1-62 🔴 手动 self.retry() 显式算退避（retry_backoff= 对手动重试静默无效）",
    )
    check(
        "LIMIT :batch" in _src("tasks/audit_tasks.py"),
        "T1-63 清理分批删（一次性 DELETE 会长时间持锁并生成巨大 binlog 事务）",
    )

    # ---- T1-64 权限码三处一致 ----
    dict_src = (ROOT / "vibecoding_doc" / "核心数据字典.md").read_text(encoding="utf-8")
    seed_src = _src("scripts/seed_org.py")
    fe_src = (ROOT / "frontend" / "src" / "constants" / "system.ts").read_text(
        encoding="utf-8"
    )
    for code in ("stats:audit:list", "stats:dashboard:view"):
        in_dict = code in dict_src
        in_seed = f'"{code}"' in seed_src
        in_fe = f"'{code}'" in fe_src
        check(
            in_dict and in_seed and in_fe,
            f"T1-64 权限码 {code} 三处同动（字典={in_dict} 种子={in_seed} 前端={in_fe}）"
            "—— 少一处就静默不可见",
        )

    # ---- T1-65 层级铁律：engines 不 import fastapi ----
    eng_src = _src("engines/import_engine/lifecycle.py")
    check(
        not re.search(r"^\s*(from|import)\s+fastapi", eng_src, re.M),
        "T1-65 engines/ 不 import fastapi（分层铁律）",
    )


def _scan_call_sites() -> dict[str, list[str]]:
    """扫 `backend/` 全部源码，统计每个 `AuditAction` 被哪些文件引用。

    只认 `AuditAction.<NAME>` 形式（含 `audit_service.AuditAction.<NAME>`）。
    ⚠️ **排除 `service/audit_service.py` 自身** —— 那里的引用是定义与白名单，
       不是调用点。不排除的话每个动作都「有调用点」，这个核查就永远绿。
    ⚠️ 也排除 `scripts/`：验收脚本自己会写动作名，算进去同样是自我证明。
    """
    found: dict[str, list[str]] = {}
    names = {
        n for n, v in vars(AuditAction).items() if not n.startswith("_") and isinstance(v, str)
    }
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND).as_posix()
        if rel in ("service/audit_service.py",) or rel.startswith("scripts/"):
            continue
        if "__pycache__" in rel:
            continue
        text = _code_only(path.read_text(encoding="utf-8"))
        for name in names:
            if re.search(rf"AuditAction\.{name}\b", text):
                found.setdefault(name, []).append(rel)
    return found


# ═══════════════════════════════════════════════════════════════
# T1.5 假会话
# ═══════════════════════════════════════════════════════════════


async def tier15_fake_session() -> None:
    print("\n── T1.5 假会话（查询形状 / 筛选 / 分页 / 回填）──")

    # T1.5-1 无筛选：WHERE 里只该有时间范围两项
    fake = _FakeSession()
    await audit_service.list_audit_logs(fake, AuditLogQuery())
    sql = _sql(fake.last_stmt)
    check(
        "LEFT OUTER JOIN" in sql.upper(),
        "T1.5-0 🔴 列表查询是 LEFT OUTER JOIN"
        "（内连接会静默抹掉离职员工的操作记录，只是列表少几行）",
    )
    check(
        "ORDER BY sys_audit_log.created_at DESC, sys_audit_log.id DESC" in sql,
        "T1.5-0b 排序带 id 次级键（否则同秒多行在页边界重复或漏行）",
    )
    check(
        sql.count("created_at >=") == 1 and sql.count("created_at <=") == 1,
        "T1.5-1 不传时间 → 仍有默认范围（不传参就全表扫是最坏默认）",
    )
    check(
        "sys_audit_log.user_id =" not in sql and "sys_audit_log.action =" not in sql,
        "T1.5-2 不传筛选维度 → 不拼对应条件（None = 不过滤，不是 IS NULL）",
    )

    # T1.5-3 全筛选：五个维度各拼一个条件
    fake = _FakeSession()
    await audit_service.list_audit_logs(
        fake,
        AuditLogQuery(
            user_id="u1", action="auth.login", target_type="user", target_id="u2"
        ),
    )
    sql = _sql(fake.last_stmt)
    check(
        all(
            frag in sql
            for frag in (
                "sys_audit_log.user_id =",
                "sys_audit_log.action =",
                "sys_audit_log.target_type =",
                "sys_audit_log.target_id =",
            )
        ),
        "T1.5-3 四个维度都能拼进 WHERE（互相独立、可任意组合）",
    )

    # T1.5-4 分页 offset
    fake = _FakeSession()
    await audit_service.list_audit_logs(fake, AuditLogQuery(page=3, page_size=20))
    sql = _sql(fake.last_stmt)
    check(
        "LIMIT %s OFFSET %s" in sql or "LIMIT" in sql.upper(),
        "T1.5-4 分页参数走绑定参数而不是拼字符串",
    )
    params = fake.last_stmt.compile(dialect=mysql.dialect()).params
    check(
        params.get("param_1") == 20 or 20 in params.values(),
        "T1.5-5 limit 参数 = page_size",
    )
    check(
        params.get("param_2") == 40 or 40 in params.values(),
        "T1.5-6 offset 参数 = (page-1) * page_size = 40（不是 page*page_size）",
    )

    # T1.5-7 username 回填
    row = SysAuditLog(
        id="aud_x",
        user_id="u1",
        action="auth.login",
        target_type="user",
        target_id="u1",
        detail=None,
        ip=None,
    )
    row.created_at = __import__("datetime").datetime(2026, 10, 14, 12, 0, 0)
    fake = _FakeSession(rows=[(row, "alice")])
    items, total = await audit_service.list_audit_logs(fake, AuditLogQuery())
    check(
        items[0].username == "alice" and items[0].action == "auth.login",
        "T1.5-7 username 从 join 结果回填到 DTO",
    )
    fake = _FakeSession(rows=[(row, None)])
    items, _ = await audit_service.list_audit_logs(fake, AuditLogQuery())
    check(
        items[0].username is None,
        "T1.5-8 用户已删 → username 为 None，而**这一行仍然列得出来**（审计的意义）",
    )

    # T1.5-9 count_action
    fake = _FakeSession()
    n = await audit_service.count_action(
        fake, AuditAction.IMPORT_TASK_RETRY, target_type="import_task", target_id="t1"
    )
    sql = _sql(fake.last_stmt)
    check(
        "count(" in sql.lower()
        and "sys_audit_log.action =" in sql
        and "sys_audit_log.target_id =" in sql,
        "T1.5-9 count_action 按 (action, target_type, target_id) 计数",
    )
    check(n == 0, "T1.5-10 count_action 返回 int（scalar 的 None 也转成 0）")

    # T1.5-11 record_read 的去重窗口与去重语义
    # 两侧都换成假的：Redis 用假客户端、DB 用假 session。
    # **不连真库**是有意的 —— 这一层的前提就是「什么都不需要」。
    # 若让真 SessionLocal 去连，写入失败会被 record_detached 吞掉（T1-25 已验证），
    # 于是这个用例会「通过」，但它其实什么也没验证到，只在日志里甩一大段连接栈。
    seen: dict[str, object] = {}

    class _FakeRedis:
        def __init__(self, acquired: bool = True) -> None:
            self._acquired = acquired

        async def set(self, key, value, nx=False, ex=None):
            seen["key"] = key
            seen["ex"] = ex
            return self._acquired

    class _RecordingSession(_FakeSession):
        def __init__(self) -> None:
            super().__init__()
            self.added: list = []
            self.committed = 0

        def add(self, obj) -> None:
            self.added.append(obj)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def commit(self) -> None:
            self.committed += 1

    import backend.clients.redis_client as rc

    orig_get, orig_local = rc.get_client, audit_service.SessionLocal
    sess = _RecordingSession()
    rc.get_client = lambda: _FakeRedis()  # type: ignore[assignment]
    audit_service.SessionLocal = lambda: sess  # type: ignore[assignment]
    try:
        await audit_service.record_read(
            _ctx(user_id="u9"),
            AuditAction.DOCUMENT_READ,
            target_type="document",
            target_id="doc_1",
        )
    finally:
        rc.get_client = orig_get  # type: ignore[assignment]
        audit_service.SessionLocal = orig_local  # type: ignore[assignment]

    check(
        seen.get("ex") == settings.audit_read_dedup_seconds,
        "T1.5-11 读去重窗口取自 settings.audit_read_dedup_seconds",
    )
    check(
        seen.get("key") == "rag:audit:read:u9:doc_1",
        f"T1.5-12 去重键按 (user_id, target_id) 展开（实测 {seen.get('key')}）",
    )
    check(
        len(sess.added) == 1 and sess.committed == 1,
        "T1.5-13 record_read 抢到去重键 → 自开事务写一行并 commit",
    )
    row = sess.added[0] if sess.added else None
    check(
        row is not None
        and row.action == AuditAction.DOCUMENT_READ
        and row.user_id == "u9"
        and row.target_id == "doc_1"
        and row.detail is None,
        "T1.5-14 落库行的动作/操作人/目标正确；document.read 无 detail 时为 NULL",
    )

    # T1.5-15 抢不到去重键 → 一行都不写（翻页/重复打开不该刷满审计表）
    sess2 = _RecordingSession()
    audit_service.SessionLocal = lambda: sess2  # type: ignore[assignment]
    rc.get_client = lambda: _FakeRedis(acquired=False)  # type: ignore[assignment]
    try:
        await audit_service.record_read(
            _ctx(user_id="u9"),
            AuditAction.DOCUMENT_READ,
            target_type="document",
            target_id="doc_1",
        )
    finally:
        rc.get_client = orig_get  # type: ignore[assignment]
        audit_service.SessionLocal = orig_local  # type: ignore[assignment]
    check(
        not sess2.added and sess2.committed == 0,
        "T1.5-15 🔴 去重窗口内 → **一行都不写、也不开事务**（否则长文档翻页会刷满表）",
    )

    # T1.5-16 登录失败**绝不**去重（record_detached 不传 dedup_seconds）
    sess3 = _RecordingSession()
    audit_service.SessionLocal = lambda: sess3  # type: ignore[assignment]
    seen.clear()
    try:
        await audit_service.record_detached(
            _ctx(user_id=ANONYMOUS_USER_ID),
            AuditAction.LOGIN_FAILED,
            target_type="user",
            target_id=None,
            detail={"reason": "bad_password", "username": "victim"},
        )
    finally:
        audit_service.SessionLocal = orig_local  # type: ignore[assignment]
    check(
        "key" not in seen and len(sess3.added) == 1,
        "T1.5-16 🔴 登录失败**不去重**（连续 20 次失败正是最该被看见的信号，"
        "合并成一条等于把爆破痕迹抹平）",
    )
    check(
        sess3.added[0].user_id == ANONYMOUS_USER_ID
        and sess3.added[0].detail == {"reason": "bad_password", "username": "victim"},
        "T1.5-17 登录失败落 anonymous 且 detail 保住 reason + username",
    )


# ═══════════════════════════════════════════════════════════════
# T2 接口层
# ═══════════════════════════════════════════════════════════════


async def _login(client: AsyncClient, username: str) -> str | None:
    try:
        resp = await client.post(
            "/api/auth/login", json={"username": username, "password": PASSWORD}
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [note] 登录请求失败：{type(exc).__name__}")
        return None
    if resp.status_code != 200:
        return None
    return (resp.json().get("data") or {}).get("access_token")


async def tier2_http() -> bool:
    print("\n── T2 接口层（MySQL + Redis）──")
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        # 先探一次连通性：连不上就整层 SKIP，不让它变成 FAIL
        try:
            async with engine.connect() as conn:
                await conn.execute(select(1))
        except Exception as exc:  # noqa: BLE001
            skip("T2 全部", f"MySQL 不可达（{type(exc).__name__}）")
            return False

        admin = await _login(client, "admin")
        if admin is None:
            skip("T2 全部", "admin 登录失败（Redis 不可达或账号/密码不符）")
            return False
        check(True, "T2-1  admin 登录成功")

        # ---- T2-2 「登录」这个动作自己留下的审计痕 ----
        resp = await client.get(
            "/api/stats/audit-logs",
            params={"action": "auth.login", "page_size": 5},
            headers={"Authorization": f"Bearer {admin}"},
        )
        check(resp.status_code == 200, f"T2-2  审计列表 200（实测 {resp.status_code}）")
        body = resp.json()
        check(
            set(body.get("data", {})) == {"list", "total", "page", "page_size"},
            "T2-3  分页结构 = {list,total,page,page_size}",
        )
        lst = body["data"]["list"]
        check(
            body["data"]["total"] >= 1 and len(lst) >= 1,
            "T2-4  🔴 刚才那次登录**自己**留下了一条 auth.login（端到端自证：留痕真的落库了）",
        )
        if lst:
            item = lst[0]
            # 🔴 `user_id` 是**用户 id**（`u_1001` 这种）而不是登录名 —— 登录名在
            #    `username`（由 LEFT JOIN 回填，见 T2-6）。原断言写的是
            #    `user_id == "admin"`，于是它红了很久而代码是对的：
            #    把「日志里的身份」和「账号名」当成同一回事。
            #    这里改成拿 **JWT 自己的身份**去对：`/auth/me` 是另一个来源，
            #    对上了才说明「这一行确实是这次登录留下的」，而不只是"有个 id"。
            me = (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {admin}"})).json()
            # ⚠️ 字段名是 **`user_id`**，不是 `id`（`schemas/auth.py::UserDetail`）。
            #    写成 `id` 会恒得 None，于是这条断言看起来"跑过了"其实没在比。
            me_id = (me.get("data") or {}).get("user_id")
            check(
                item["action"] == "auth.login" and me_id is not None and item["user_id"] == me_id,
                f"T2-5  审计行的 action/user_id 与 JWT 身份一致"
                f"（实测 action={item['action']} user_id={item['user_id']} / me.user_id={me_id}）",
            )
            check(
                item["username"] == "admin",
                "T2-6  username 由 LEFT JOIN 回填",
            )
            check(
                item["created_at"] is not None and item["detail"] is None,
                "T2-7  created_at 有值；auth.login 的 detail 为 NULL（白名单是空集）",
            )

        # ---- T2-8 登录失败落 anonymous ----
        bad = await client.post(
            "/api/auth/login", json={"username": MARK, "password": "definitely-wrong"}
        )
        check(bad.status_code == 401, "T2-8  错误密码 → 401")
        resp = await client.get(
            "/api/stats/audit-logs",
            params={"action": "auth.login_failed", "page_size": 5},
            headers={"Authorization": f"Bearer {admin}"},
        )
        rows = resp.json()["data"]["list"]
        mine = [r for r in rows if r["target_id"] is None and r["detail"]]
        check(
            any(r["user_id"] == "anonymous" for r in mine),
            "T2-9  🔴 账号不存在时 user_id 落 anonymous，而 detail.username 留住登录名（可追溯）",
        )
        check(
            any((r["detail"] or {}).get("reason") == "user_not_found" for r in mine),
            "T2-10 detail.reason 记下真实原因（对外仍是统一文案，审计侧才看得到）",
        )

        # ---- T2-11 鉴权 ----
        resp = await client.get("/api/stats/audit-logs")
        check(resp.status_code in (401, 403), f"T2-11 无 token → 401/403（实测 {resp.status_code}）")

        # 🔴 登录名是 **`kb_hq`**（种子里的普通用户），`kb_admin` 是**角色码**不是账号。
        #    原写法 `_login(client, "kb_admin")` 永远登不上，于是这条**守门用例**
        #    （知识管理员不得读审计）从落地起一直在静默 SKIP ——
        #    而 SKIP 看起来和通过一样是绿的。`kb_hq` 是种子里那个 kb_admin 角色的用户。
        kb = await _login(client, "kb_hq")
        if kb is None:
            skip("T2-12", "kb_hq（kb_admin 角色用户）登录不可用")
        else:
            resp = await client.get(
                "/api/stats/audit-logs", headers={"Authorization": f"Bearer {kb}"}
            )
            check(
                resp.status_code == 403,
                f"T2-12 🔴 kb_admin 无 stats:audit:list → 403（实测 {resp.status_code}）："
                "审计含全组织操作轨迹，看板权限不等于审计权限",
            )

        # ---- T2-13 超限 page_size ----
        resp = await client.get(
            "/api/stats/audit-logs",
            params={"page_size": settings.audit_page_size_max + 1},
            headers={"Authorization": f"Bearer {admin}"},
        )
        # 🔴 断言业务码 1001，而不是 FastAPI 的默认 422：
        #    `core/exceptions.py` 刻意把校验失败统一成 `400 + PARAM_ERROR`。
        check(
            resp.status_code in (400, 422) and resp.json().get("code") == 1001,
            f"T2-13 page_size 超上限 → 被拒且业务码 1001"
            f"（实测 {resp.status_code} / code={resp.json().get('code')}）",
        )

        # ---- T2-14 默认时间范围真的收窄了结果 ----
        resp_big = await client.get(
            "/api/stats/audit-logs",
            params={"page_size": 1, "start_time": "2000-01-01T00:00:00"},
            headers={"Authorization": f"Bearer {admin}"},
        )
        resp_def = await client.get(
            "/api/stats/audit-logs",
            params={"page_size": 1},
            headers={"Authorization": f"Bearer {admin}"},
        )
        if resp_big.status_code == 200 and resp_def.status_code == 200:
            check(
                resp_big.json()["data"]["total"] >= resp_def.json()["data"]["total"],
                "T2-14 不给时间 → 默认只查最近若干天（total 不大于全量）",
            )
        else:
            skip("T2-14", "两次查询未都成功")

        # ---- T2-15 审计**只增不改**：库里面没有 updated_at 列 ----
        try:
            from sqlalchemy import text as _text

            async with engine.connect() as conn:
                res = await conn.execute(
                    _text(
                        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sys_audit_log'"
                    )
                )
                live_cols = {r[0] for r in res}
            if not live_cols:
                skip("T2-15", "sys_audit_log 表尚不存在（迁移 0004 未应用）")
            else:
                check(
                    live_cols == {c.name for c in SysAuditLog.__table__.columns},
                    f"T2-15 库里的列与模型一致（库={sorted(live_cols)}）",
                )
                check(
                    "updated_at" not in live_cols,
                    "T2-16 🔴 库里无 updated_at（审计行不可 UPDATE 的结构保证）",
                )
        except Exception as exc:  # noqa: BLE001
            skip("T2-15/16", f"information_schema 查询失败（{type(exc).__name__}）")

    return True


# ═══════════════════════════════════════════════════════════════
# T3 链路层
# ═══════════════════════════════════════════════════════════════


def tier3_chain() -> None:
    print("\n── T3 链路层（worker + beat + MySQL）──")
    skip(
        "T3-1  保留期清理任务真跑一次",
        "需要 Celery worker 与真实过期数据（与 Docker 无关）。"
        "在 ECS 上跑法：往 sys_audit_log 插一行 created_at 为 300 天前的记录，"
        "再 `celery -A backend.tasks call audit.purge_audit_logs`，断言该行消失、较新的行仍在",
    )
    skip(
        "T3-2  beat 到点自动触发",
        "需要 beat 进程连续运行跨过 03:17；另行确认 `celery inspect registered` "
        "含 audit.purge_audit_logs",
    )
    skip(
        "T3-3  其它模块的写操作端到端留痕",
        "be-02/be-04 的写接口需要 MySQL；其中 be-04 的 `document.read` 还需 Milvus 侧"
        "有真实切片，本机不具备",
    )


# ═══════════════════════════════════════════════════════════════


async def run() -> tuple[bool, bool]:
    """返回 `(全部通过, T2 是否真的跑过)`。

    ⚠️ 三层在**同一个事件循环**里跑 —— 这正是 `core/db.py` 的模块级 engine
       能跨层复用的前提。若各自塞进一个 `asyncio.run()`，第二层会复用到
       上个循环遗留的连接，报 `'NoneType' object has no attribute 'send'`
       （CLAUDE.md 已知坑），而且症状是「单跑某一层都过、合起来必挂」。

    ⚠️ 但 **T1 是纯同步**，所以它跑在 `asyncio.run()` 之外 ——
       它里面有自己的一次 `asyncio.run(_run_list())` 用于驱动假会话的
       `list_audit_logs`，那个假会话不碰真 DB，不会污染真 engine。
    """
    tier1_offline()
    await tier15_fake_session()
    ran = await tier2_http()
    tier3_chain()
    await engine.dispose()
    return all(_results), ran


def main_() -> None:
    ok_, ran = asyncio.run(run())
    passed = sum(_results)
    total = len(_results)
    print(f"\n{'=' * 56}")
    # 🔴 标签要说实话：`_results` 里**含 T2 的检查**（T2 真跑过时）。
    #    写死「T1/T1.5」会让人以为接口层一条都没验，把已经跑过的当成没跑
    #    —— 与「把 SKIP 当成通过」是同一类误读，只是方向相反。
    _scope = "T1/T1.5 + T2（含接口层）" if ran else "T1/T1.5（离线+假会话）"
    print(f"{_scope}：{passed}/{total} 通过")
    # 🔴 不要写死「剩下的都是 T2/T3 的依赖问题」——T2 现在真的跑起来了。
    #    写死归类会让人以为接口层一条都没验过，把已经能跑的东西当成没跑。
    print(
        f"SKIP：{len(_skipped)} 项（**不是通过**）—— 逐条原因见上；"
        "缺口通常在 T2（需 MySQL+Redis）或 T3（需 Milvus/BGE/DeepSeek+worker/beat）"
    )
    if not ran:
        print("⚠️ T2 未执行（MySQL 或 Redis 不可用）—— **本次结果不构成完整验收**。")
        print("   依赖恢复后重跑本脚本。")
    if passed != total:
        print("存在失败项，见上方 FAIL 行。")
    sys.exit(0 if (ok_ and ran) else 1)


if __name__ == "__main__":
    main_()
