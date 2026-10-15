"""组织管理服务（be-02）。

be-02 的全部业务：部门树维护（含**移动**的物化路径重写）、用户与角色 CRUD、
权限树、删除前的引用校验、组织变更后的会话缓存失效。

实现依据 `vibecoding_doc/specs/P1-08-be-org.md`。四大难点各有专节：
  §5.1 物化路径的三条不变式（I1 首尾斜杠 / I2 前缀性质 / I3 无环）
  §5.2 移动部门（子树 path 重写 + 命名锁）
  §5.3 删除的引用校验（拒绝而非级联）
  §5.5 缓存失效的**整棵子树**口径

⚠️ 本文件的 `load_user_identity` / `compute_effective_depts` /
`load_department_names` / `split_materialized_path` 四个函数**先于本模块落地**
（be-01/be-03 的依赖倒逼），不是 be-02 的交付物 —— 但它们**归本文件**，
因为同域语义只准有一份实现。

═══════════════════════════════════════════════════════════════
为什么 `load_user_identity` 必须只有一份实现
═══════════════════════════════════════════════════════════════
它被**两处**调用：
  ① be-01 `login` —— 登录时算出身份快照写进 Redis 与 JWT；
  ② be-03 `get_current_user` —— 会话缓存未命中时重算。

若两边各写一份 `effective_depts` 计算，必然漂移。漂移的表现是
「登录时算出的部门集合 ≠ 依赖注入时算出的部门集合」——
**最难查的越权/漏权 bug**：同一个用户，走登录路径和走缓存路径看到不同文档。

be-02 §8 的契约原话：「不要自己另写一份 `effective_depts` 计算 —— 两份实现必然漂移」。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from redis.asyncio import Redis
from sqlalchemy import delete, distinct, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.clients import redis_client
from backend.core.db import engine
from backend.core.ids import new_id
from backend.core.exceptions import (
    ConflictError,
    NotFoundError,
    ParamError,
    UnauthorizedError,
)
from backend.core.security import hash_password
from backend.core.settings import settings
from backend.models import (
    SysDepartment,
    SysPermission,
    SysRole,
    SysRolePermission,
    SysUser,
    SysUserRole,
)
from backend.schemas.auth import AuthContext, UserIdentity
from backend.schemas.org import (
    DepartmentCreate,
    DepartmentNode,
    DepartmentUpdate,
    PermissionNode,
    RoleCreate,
    RoleItem,
    RoleUpdate,
    UserCreate,
    UserItem,
    UserListQuery,
    UserUpdate,
)
from backend.service import audit_service

logger = logging.getLogger(__name__)


def split_materialized_path(path: str | None) -> list[str]:
    """把物化路径解析成「自身 + 全部祖先」的 id 列表。

    `path` 形如 `/dept_01/dept_02/dept_04/`（PRD §6.1.2），
    `strip('/').split('/')` 一次就得到全部祖先 —— **不做递归**。
    顺序为祖先在前、自身在后。

    这是「授权财务部 = 财务部及其全部子孙可见」的推导基础（PRD 验收 A4，D10 向下继承）。
    """
    if not path:
        return []
    return [seg for seg in path.strip("/").split("/") if seg]


async def compute_effective_depts(
    db: AsyncSession, department_id: str | None
) -> list[str]:
    """算出某部门的有效部门集合（自身 + 全部祖先）。"""
    if not department_id:
        # 未分配部门的用户没有任何部门维度的可见范围。
        # 注意这不等于「看不到任何东西」—— 还有 acl_global / 角色 / 用户三个维度，
        # 那是 be-05 拼 ACL 表达式时的事。
        return []
    path = await db.scalar(
        select(SysDepartment.path).where(SysDepartment.id == department_id)
    )
    return split_materialized_path(path)


async def load_department_names(
    db: AsyncSession, department_ids: list[str]
) -> dict[str, str]:
    """按 id 批量取部门名，返回 `{id: name}`。

    给 `/api/auth/me` 的 `dept_names` 用（be-01 spec §10-86）：侧栏副标题与检索范围 chip
    要显示「财务部 · 成本组」= **父级 + 自身**两段名字，而 `department` 只有自身一级、
    `depts` 只有 id 没有名字，两处都凑不出父级，所以必须再查一次。

    不存在的 id **不会出现在返回的字典里** —— 调用方自己决定回退策略
    （`get_me` 的做法是回退成 id 本身并告警，因为静默丢弃会让列表长度与 `depts` 错位）。

    🔴 **本函数不负责顺序**。`WHERE id IN (...)` 的返回顺序由数据库决定（MySQL 一般按主键，
    但那是实现细节、不是契约），调用方**必须按自己的顺序重排**。
    返回字典而非列表，就是为了让「顺序」这件事在类型上无法被误用 ——
    按字典的迭代顺序拼列表，会产出「名字与 id 对错位」这种不报错、只静默显示错名字的 bug。

    归 be-02：`sys_department` 的读取属组织域，本文件已持有同域的
    `split_materialized_path` / `compute_effective_depts`，同一语义只留一份实现。
    """
    # dict.fromkeys 去重且保序；空串 id 直接滤掉，避免 IN 里混进无意义值
    ids = list(dict.fromkeys(d for d in department_ids if d))
    if not ids:
        return {}
    rows = await db.execute(
        select(SysDepartment.id, SysDepartment.name).where(SysDepartment.id.in_(ids))
    )
    return {dept_id: name for dept_id, name in rows}


async def load_user_identity(db: AsyncSession, user_id: str) -> UserIdentity:
    """一次算出权威身份快照：`effective_depts` + `roles` + `perms`。

    用户不存在或已停用时抛 `UnauthorizedError`（be-02 §3.2 的契约）。
    be-01 登录与 be-03 缓存未命中**共用本函数**，保证两份快照永不漂移。
    """
    row = (
        await db.execute(
            select(
                SysUser.id,
                SysUser.username,
                SysUser.department_id,
                SysUser.status,
                SysDepartment.path,
            )
            # LEFT JOIN：department_id 为 NULL 的用户也要能登录，不能被 INNER JOIN 滤掉
            .outerjoin(SysDepartment, SysDepartment.id == SysUser.department_id)
            .where(SysUser.id == user_id)
        )
    ).first()

    if row is None:
        raise UnauthorizedError("登录状态已失效，请重新登录")

    uid, username, department_id, status, dept_path = row

    if status != SysUser.STATUS_ACTIVE:
        # 停用账号的既有 token 在此失效 —— 这是「停用即踢下线」的落点
        # （access 无黑名单，靠会话缓存未命中后重算发现；见 PRD §6.1.3）
        raise UnauthorizedError("账号已停用")

    roles = list(
        await db.scalars(
            select(SysUserRole.role_id).where(SysUserRole.user_id == uid)
        )
    )

    # 用户 → 角色 → 权限码。三表串联，`DISTINCT` 去掉多角色带来的重复码。
    perms = list(
        await db.scalars(
            select(distinct(SysPermission.code))
            .join(SysRolePermission, SysRolePermission.permission_id == SysPermission.id)
            .join(SysUserRole, SysUserRole.role_id == SysRolePermission.role_id)
            .where(SysUserRole.user_id == uid)
        )
    )

    return UserIdentity(
        user_id=uid,
        username=username,
        department_id=department_id,
        depts=split_materialized_path(dept_path),
        roles=roles,
        perms=perms,
    )


# ═══════════════════════════════════════════════════════════════
# 公共工具
# ═══════════════════════════════════════════════════════════════

# 部门树移动的命名锁名。它是**标识符不是阈值**，故留常量；
# 等待上限（会随环境调整）走 settings。
_DEPT_LOCK = "rag:org:dept_tree"

# `sys_department.path` 的列宽（字典 §2.1），写死在代码里做越界前置校验。
_PATH_MAX = 255

_DOC_PERM_TABLE = "kb_document_permission"


def _new_id(prefix: str) -> str:
    """生成主键：`前缀_12位随机hex`。

    ⚠️ 实现已于 2026-10-14 抽到 `backend/core/ids.py` ——
    be-04 的知识库 id 要用**同一形态**，而两份实现必然漂移。
    这里保留 `_new_id` 这个私有名只是为了不改动本模块内已有的十几处调用点；
    **新代码请直接用 `core.ids.new_id`**。
    """
    return new_id(prefix)


def _assert_path_depth(path: str) -> None:
    """建部门/移动部门的前置校验：物化路径不得超出列宽。

    不校验的后果是 MySQL 报 1406 `Data too long for column 'path'` ——
    一个 5000 内部错误，而不是「层级过深」这种能指导用户的 1001。
    """
    if len(path) > _PATH_MAX:
        raise ParamError(f"部门层级过深（物化路径超过 {_PATH_MAX} 字符）")


def _to_node(dept: SysDepartment) -> DepartmentNode:
    return DepartmentNode(
        id=dept.id,
        name=dept.name,
        parent_id=dept.parent_id,
        path=dept.path,
        sort=dept.sort,
        status=dept.status,
        children=[],
    )


def _sort_tree(nodes: list[DepartmentNode]) -> None:
    """按 (sort, id) 递归排序。

    用 id 做**次序兜底**：`sort` 相同的部门若次序不稳定，
    每次请求返回的顺序都可能不同 —— 前端树会「自己抖动」，
    且 diff 时表现为无意义的变更。
    """
    nodes.sort(key=lambda n: (n.sort, n.id))
    for node in nodes:
        _sort_tree(node.children)


# ═══════════════════════════════════════════════════════════════
# 部门
# ═══════════════════════════════════════════════════════════════


async def _get_department(db: AsyncSession, dept_id: str) -> SysDepartment:
    dept = await db.get(SysDepartment, dept_id)
    if dept is None:
        raise NotFoundError("部门不存在")
    return dept


async def list_departments_tree(db: AsyncSession) -> list[DepartmentNode]:
    """部门树。

    一次 `SELECT` 全量取出后在内存组树 —— 部门量级是十位，逐层查库纯属 N+1。
    """
    rows = list(await db.scalars(select(SysDepartment)))
    return _build_department_tree(rows)


def _build_department_tree(rows: Sequence[SysDepartment]) -> list[DepartmentNode]:
    nodes = {r.id: _to_node(r) for r in rows}

    roots: list[DepartmentNode] = []
    for r in rows:
        node = nodes[r.id]
        # parent_id 指向已删/不存在的部门时，该节点**降级为根**而不是被丢弃 ——
        # 丢弃会让整棵子树从界面上消失，且没有任何报错（静默丢数据）。
        parent = nodes.get(r.parent_id) if r.parent_id else None
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)

    _sort_tree(roots)
    return roots


async def create_department(
    db: AsyncSession, payload: DepartmentCreate, operator: AuthContext
) -> DepartmentNode:
    """新建部门。`path` 由服务端算，不收客户端传值（§5.1 的 I1/I2 不能交给不可信输入）。"""
    # 🔴 顶层默认值是 `"/"` 而**不是** `""`。
    # 父部门的 path 自带**尾**斜杠（`/a/b/`），所以「父path + 自己id + /」天然正确；
    # 而顶层没有父，若默认空串，根就长成 `a/` —— 少了**首**斜杠，I1 直接破。
    # 取 "/" 后根是 `/a/`，与 `seed_org.py` 的 `"/dept_01/"` 一致。
    # 症状很隐蔽：`split_materialized_path` 用 strip('/')，两种写法都能解析，
    # 所以只在「跨根做前缀匹配」或与既有种子数据对比时才暴露。
    parent_path = "/"
    if payload.parent_id:
        parent = await _get_department(db, payload.parent_id)
        parent_path = parent.path

    dept_id = _new_id("dept")
    path = f"{parent_path}{dept_id}/"
    _assert_path_depth(path)

    dept = SysDepartment(
        id=dept_id,
        name=payload.name,
        parent_id=payload.parent_id,
        path=path,
        sort=payload.sort,
        status="active",
    )
    db.add(dept)
    # 留痕与建部门**同事务**（`record` 不 commit，由下面这行一起提交）。
    # 顺序放在 `db.add(dept)` 之后、`commit` 之前 —— 这样「部门没了、审计也没了」
    # 不会出现，反之亦然。
    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.DEPARTMENT_CREATE,
        target_type=audit_service.AuditTargetType.DEPARTMENT,
        target_id=dept_id,
        detail={
            "name": payload.name,
            "parent_id": payload.parent_id,
            "sort": payload.sort,
        },
    )
    await db.commit()
    return _to_node(dept)


async def update_department(
    db: AsyncSession, dept_id: str, payload: DepartmentUpdate, operator: AuthContext
) -> DepartmentNode:
    """改部门。未显式传入的字段一律不动（判据是 `model_fields_set`，见 DTO 注释）。"""
    dept = await _get_department(db, dept_id)
    fields = payload.model_fields_set

    # 记「**实际**改了哪些字段」而非「请求里带了哪些」——
    # 带了 `"name": null` 的请求什么也没改，审计里却会留下 name，
    # 复盘时就成了假证据。
    applied: set[str] = set()

    if "name" in fields and payload.name is not None:
        dept.name = payload.name
        applied.add("name")
    if "sort" in fields and payload.sort is not None:
        dept.sort = payload.sort
        applied.add("sort")
    if "status" in fields and payload.status is not None:
        dept.status = payload.status
        applied.add("status")

    moved = "parent_id" in fields and payload.parent_id != dept.parent_id
    if moved:
        applied.add("parent_id")

    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.DEPARTMENT_UPDATE,
        target_type=audit_service.AuditTargetType.DEPARTMENT,
        target_id=dept_id,
        detail={"changed_fields": sorted(applied), "parent_id": payload.parent_id},
    )

    if moved:
        # `_move_department` 在**另一条连接**上自行提交（提交必须收进锁里，
        # 且必须在提交后重新开快照，两件事的理由都见该函数注释）。
        await _move_department(db, dept, payload.parent_id)
        # ⚠️ 必须**再提交一次**：`_move_department` 提交的是它自己那条连接，
        #    上面 `record()` 塞进本 session 的审计行**不在其中**。
        #    不补这一行的话，只有「移动部门」这个分支不留痕 ——
        #    而移动部门恰好是权限影响最大的一种改动（整棵子树的 path 都变了）。
        await db.commit()
    else:
        await db.commit()
        await db.refresh(dept)

    return _to_node(dept)


async def _move_department(
    db: AsyncSession, dept: SysDepartment, new_parent_id: str | None
) -> None:
    """移动部门：改 `parent_id` 并**重写整棵子树**的物化路径。

    ── 为什么不用 `db` 而自己开一条连接 ──────────────────────────
    🔴 MySQL 的 `GET_LOCK` 是**连接级**的，不是事务级的；而它必须与
       `RELEASE_LOCK`、以及**提交**落在同一条连接上，且释放要在提交**之后**。
       若释放早于提交，第二个移动者会拿到锁、却读到提交前的旧 path 快照，
       据此算出错误的 new_path —— 树被静默写坏，且没有任何报错。
       `AsyncSession` 在 `commit()` 后可能从池里换用另一条连接，
       无法保证这一点，故此处用 `engine.connect()` 取一条独占连接。

    ── 为什么不用 LIKE 做前缀匹配 ────────────────────────────────
    🔴 我们的 id 形如 `dept_a1b2c3d4e5f6`，**含下划线**，
       而 `_` 正是 SQL `LIKE` 的单字符通配符。
       `path LIKE '/dept_01/%'` 会连带匹配 `/deptX01/...` 这类**兄弟部门**，
       把无关子树一起重写。改用 `SUBSTRING` 做精确前缀比较，
       语义上没有任何通配符，不可能误伤。
       （代价是列上的索引用不上；部门表量级十位，可忽略。）
    """
    old_self_path = dept.path
    old_len = len(old_self_path)

    async with engine.connect() as conn:
        got = await conn.scalar(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": _DEPT_LOCK, "timeout": settings.org_dept_lock_timeout_seconds},
        )
        if got != 1:
            raise ConflictError("部门树正在被其他操作修改，请稍后重试")

        try:
            # 同 `create_department`：顶层是 "/" 不是 ""，否则移到顶层会丢掉首斜杠。
            new_parent_path = "/"
            if new_parent_id:
                if new_parent_id == dept.id:
                    raise ParamError("不能把部门移到自己下面")
                row = (
                    await conn.execute(
                        text("SELECT path FROM sys_department WHERE id = :pid"),
                        {"pid": new_parent_id},
                    )
                ).first()
                if row is None:
                    raise NotFoundError("父部门不存在")
                new_parent_path = row[0]
                # I3 环校验：新父的 path 以自己为前缀 ⟺ 新父是自己的子孙。
                # 放在写之前，保证「拒绝时未写库」。
                if new_parent_path.startswith(old_self_path):
                    raise ParamError("不能把部门移到自己的子孙下面")

            new_self_path = f"{new_parent_path}{dept.id}/"
            _assert_path_depth(new_self_path)

            # 一条 SQL 重写整棵子树：只换前缀，保留各自后半段。
            # 自身也命中（其后半段为空串），无需单独处理。
            await conn.execute(
                text(
                    "UPDATE sys_department "
                    "SET path = CONCAT(:new_self_path, SUBSTRING(path, :old_len + 1)) "
                    "WHERE SUBSTRING(path, 1, :old_len) = :old_self_path"
                ),
                {
                    "new_self_path": new_self_path,
                    "old_len": old_len,
                    "old_self_path": old_self_path,
                },
            )
            await conn.execute(
                text("UPDATE sys_department SET parent_id = :pid WHERE id = :did"),
                {"pid": new_parent_id, "did": dept.id},
            )
            await conn.commit()
        finally:
            # finally 是硬性的：任何一条 raise 若跳过释放，
            # 这把锁会一直被那条连接持有到连接归还/超时，期间**所有人都不能改部门树**。
            await conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": _DEPT_LOCK})

    # 🔴 必须先 `db.commit()` 结束本 session 的事务，再 refresh。
    #
    # 上面的写是在**另一条连接**上提交的；而本 session 的事务早在
    # `_get_department` 的 `db.get()` 时就开了，此刻仍停在 REPEATABLE READ 的
    # 旧快照上 —— 直接 `refresh` 会读到**移动前**的 path。
    # 症状极具迷惑性：接口返回 2000，库里也确实改了，但响应体里的
    # `path` / `parent_id` 全是旧值，看起来像「接口静默没生效」；
    # 更糟的是下一行 `_user_ids_in_subtree(dept.path)` 会用旧前缀去挑人，
    # 于是**该失效会话的用户没被失效**（权限收回延迟到 30 分钟后才生效）。
    # commit 掉空事务即可让后续读开新快照。
    await db.commit()
    await db.refresh(dept)

    # 移动改变了整棵子树的祖先链，故受影响的是**整棵子树**的用户，
    # 而不只是「直属该部门的人」—— 只失效直属是本函数最容易写错的一处。
    affected = await _user_ids_in_subtree(db, dept.path)
    logger.info(
        "部门移动：id=%s 新path=%s 受影响用户数=%s", dept.id, dept.path, len(affected)
    )
    await evict_user_sessions(redis_client.get_client(), affected)


async def delete_department(
    db: AsyncSession, dept_id: str, operator: AuthContext
) -> None:
    """删部门。**有引用即拒绝**（3002），不做级联 —— 理由见 `P1-08` §5.3。"""
    dept = await _get_department(db, dept_id)

    child_count = await db.scalar(
        select(func.count())
        .select_from(SysDepartment)
        .where(SysDepartment.parent_id == dept_id)
    )
    if child_count:
        raise ConflictError("该部门下仍有子部门，无法删除")

    user_count = await db.scalar(
        select(func.count()).select_from(SysUser).where(SysUser.department_id == dept_id)
    )
    if user_count:
        raise ConflictError("该部门下仍有用户，无法删除")

    await _assert_no_document_permission(db, "department", dept_id)

    # ⚠️ `name` 必须在 `db.delete(dept)` **之前**读出来：
    #    删除后对象进 deleted 状态，属性读取会触发一次注定失败的补查询。
    #    这也是为什么 detail 里要留名字 —— 行删了之后，审计是唯一还记得它叫什么的地方。
    dept_name = dept.name
    await db.delete(dept)
    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.DEPARTMENT_DELETE,
        target_type=audit_service.AuditTargetType.DEPARTMENT,
        target_id=dept_id,
        detail={"name": dept_name, "child_count": 0, "user_count": 0},
    )
    await db.commit()
    logger.info("部门已删除：id=%s name=%s", dept_id, dept_name)


async def _user_ids_in_subtree(db: AsyncSession, root_path: str) -> list[str]:
    """物化路径前缀匹配，取某棵子树下全部用户的 id。

    用 `SUBSTRING` 而非 `LIKE`，理由同 `_move_department`（id 含 `_` 通配符）。
    """
    rows = await db.scalars(
        select(SysUser.id)
        .join(SysDepartment, SysDepartment.id == SysUser.department_id)
        .where(
            func.substring(SysDepartment.path, 1, len(root_path)) == root_path,
        )
    )
    return list(rows)


# ═══════════════════════════════════════════════════════════════
# 缓存失效
# ═══════════════════════════════════════════════════════════════


async def evict_user_sessions(redis: Redis, user_ids: Iterable[str]) -> int:
    """批量删除 `rag:auth:session:{uid}`，返回删除数。

    这是「组织变更立即生效」的落点：be-03 的 `get_current_user` 在缓存未命中时
    会从 MySQL 重算身份 —— 只要键被删掉，权限收回就在**下一次请求**生效，
    而不必等 access 自然过期（≤30 分钟）。

    ⚠️ 键名**不在此处拼**，走 `session_store.session_key`（`rag:auth:*` 的唯一定义处）。
    """
    from backend.service import session_store

    ids = list(dict.fromkeys(uid for uid in user_ids if uid))
    if not ids:
        return 0
    keys = [session_store.session_key(uid) for uid in ids]
    deleted = await redis_client.guard("DEL session", redis.delete(*keys))
    logger.info("会话缓存已失效：用户数=%s 实删键数=%s", len(ids), deleted)
    return int(deleted)


# ═══════════════════════════════════════════════════════════════
# 用户
# ═══════════════════════════════════════════════════════════════


async def _role_ids_of_users(
    db: AsyncSession, user_ids: Sequence[str]
) -> dict[str, list[str]]:
    """一次查出这一页用户的角色，避免逐行 N+1。"""
    if not user_ids:
        return {}
    rows = await db.execute(
        select(SysUserRole.user_id, SysUserRole.role_id).where(
            SysUserRole.user_id.in_(list(user_ids))
        )
    )
    result: dict[str, list[str]] = {uid: [] for uid in user_ids}
    for user_id, role_id in rows:
        result.setdefault(user_id, []).append(role_id)
    return result


async def _to_user_item(db: AsyncSession, user: SysUser) -> UserItem:
    return (await _to_user_items(db, [user]))[0]


async def _to_user_items(db: AsyncSession, users: Sequence[SysUser]) -> list[UserItem]:
    """把 ORM 行转成 `UserItem`，批量补部门名与角色。

    🔴 结果**按传入顺序**返回（内部用 dict 当查找表，遍历 `users` 回填）——
       不按查询结果顺序拼，否则会出现「名字与 id 错位」这种不报错的静默错误。
    """
    role_map = await _role_ids_of_users(db, [u.id for u in users])

    dept_names: dict[str, str] = {}
    dept_ids = [u.department_id for u in users if u.department_id]
    if dept_ids:
        dept_names = await load_department_names(db, dept_ids)

    items: list[UserItem] = []
    for u in users:
        items.append(
            UserItem(
                id=u.id,
                username=u.username,
                real_name=u.real_name,
                email=u.email,
                department_id=u.department_id,
                department_name=(
                    dept_names.get(u.department_id) if u.department_id else None
                ),
                status=u.status,
                role_ids=role_map.get(u.id, []),
                last_login_at=u.last_login_at,
                created_at=u.created_at,
            )
        )
    return items


async def list_users(
    db: AsyncSession, query: UserListQuery
) -> tuple[list[UserItem], int]:
    """用户分页列表。

    `department_id` 是**含子孙**语义 —— 选「财务部」要能看到「成本组」的人。
    实现走物化路径前缀匹配（I2），不是 `=` 等值：
    「只看到直属」是这类界面最常见、也最容易被当成设计而非 bug 的行为。
    """
    conditions = []

    if query.department_id:
        dept = await _get_department(db, query.department_id)
        sub = select(SysUser.id).join(
            SysDepartment, SysDepartment.id == SysUser.department_id
        ).where(func.substring(SysDepartment.path, 1, len(dept.path)) == dept.path)
        conditions.append(SysUser.id.in_(sub))

    if query.keyword:
        # 参数化绑定，不做 f-string 拼串（安全红线 8）。
        like = f"%{query.keyword}%"
        conditions.append(
            SysUser.username.like(like) | SysUser.real_name.like(like)
        )

    if query.status:
        conditions.append(SysUser.status == query.status)

    total = await db.scalar(
        select(func.count()).select_from(SysUser).where(*conditions)
    )

    rows = await db.scalars(
        select(SysUser)
        .where(*conditions)
        .order_by(SysUser.created_at.desc(), SysUser.id)
        .offset((query.page - 1) * query.page_size)
        .limit(query.page_size)
    )
    return await _to_user_items(db, list(rows)), int(total or 0)


async def create_user(
    db: AsyncSession, payload: UserCreate, operator: AuthContext
) -> UserItem:
    """新建用户。密码经 be-03 `hash_password` —— 本模块不碰哈希算法。"""
    exists = await db.scalar(
        select(SysUser.id).where(SysUser.username == payload.username)
    )
    if exists:
        raise ConflictError(f"登录名已存在：{payload.username}")

    if payload.department_id:
        await _get_department(db, payload.department_id)
    if payload.role_ids:
        await _assert_roles_exist(db, payload.role_ids)

    user = SysUser(
        id=_new_id("u"),
        username=payload.username,
        password_hash=hash_password(payload.password),
        real_name=payload.real_name,
        department_id=payload.department_id,
        email=payload.email,
        status=SysUser.STATUS_ACTIVE,
    )
    db.add(user)
    await db.flush()

    for role_id in dict.fromkeys(payload.role_ids):
        db.add(SysUserRole(user_id=user.id, role_id=role_id))

    # 🔴 detail 里**只记 `role_ids_count`，绝不记 `role_ids` 本身** ——
    #    角色 id 集合等价于一份权限快照，而审计页是管理员可读的，
    #    等于把权限表复制到一张不受权限体系保护的表里（`P2-12` §5.2）。
    #    密码自然更是一个字都不进 —— 白名单里根本没有这个键。
    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.USER_CREATE,
        target_type=audit_service.AuditTargetType.USER,
        target_id=user.id,
        detail={
            "username": user.username,
            "department_id": user.department_id,
            "role_ids_count": len(set(payload.role_ids)),
        },
    )
    await db.commit()
    await db.refresh(user)
    logger.info("用户已创建：id=%s username=%s", user.id, user.username)
    return await _to_user_item(db, user)


async def update_user(
    db: AsyncSession, user_id: str, payload: UserUpdate, operator: AuthContext
) -> UserItem:
    """改用户。停用或换部门都会失效该用户的会话缓存。"""
    user = await db.get(SysUser, user_id)
    if user is None:
        raise NotFoundError("用户不存在")

    fields = payload.model_fields_set
    invalidate = False
    # 记「实际改了的」而非「请求里带的」，理由同 `update_department`。
    applied: set[str] = set()

    if "real_name" in fields and payload.real_name is not None:
        user.real_name = payload.real_name
        applied.add("real_name")
    if "email" in fields:
        user.email = payload.email
        applied.add("email")

    if "department_id" in fields and payload.department_id != user.department_id:
        if payload.department_id:
            await _get_department(db, payload.department_id)
        user.department_id = payload.department_id
        applied.add("department_id")
        # 换部门 = 祖先链变了 = 有效部门集合变了，必须重算（靠删缓存触发）。
        invalidate = True

    if "status" in fields and payload.status is not None:
        if payload.status != user.status:
            # 停用即踢下线：access 无黑名单，靠缓存未命中后重算发现（PRD §6.1.3）。
            invalidate = True
        user.status = payload.status
        applied.add("status")

    roles_changed = "role_ids" in fields and payload.role_ids is not None
    if roles_changed:
        await _assert_roles_exist(db, payload.role_ids)
        await _replace_user_roles(db, user_id, payload.role_ids)
        applied.add("role_ids")
        invalidate = True

    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.USER_UPDATE,
        target_type=audit_service.AuditTargetType.USER,
        target_id=user_id,
        detail={"changed_fields": sorted(applied), "department_id": user.department_id},
    )
    if roles_changed:
        # ⚠️ **额外**记一条 `user.grant_role`，这是刻意的双行，不是重复。
        #    理由是字典 §5.5 命名规则 4 的同一个判据：`action` 是最常用的筛选条件。
        #    「谁给谁提了权」必须能用 `action=user.grant_role` 一次查全 ——
        #    只在 `user.update` 的 `detail.changed_fields` 里留个 `role_ids` 的话，
        #    这条查询要么查不到、要么得全文扫 JSON，而**提权恰恰是最该被审计的动作**。
        #    代价是同一操作有两行；两行的 `target_id` 相同，复盘时能对上。
        audit_service.record(
            db,
            operator,
            audit_service.AuditAction.USER_GRANT_ROLE,
            target_type=audit_service.AuditTargetType.USER,
            target_id=user_id,
            detail={"role_ids_count": len(set(payload.role_ids or []))},
        )

    await db.commit()
    await db.refresh(user)
    if invalidate:
        await evict_user_sessions(redis_client.get_client(), [user_id])
    return await _to_user_item(db, user)


async def _replace_user_roles(
    db: AsyncSession, user_id: str, role_ids: Sequence[str]
) -> None:
    """整体替换 `sys_user_role`：先删后插，语义为「最终状态见请求体」。"""
    await db.execute(delete(SysUserRole).where(SysUserRole.user_id == user_id))
    for role_id in dict.fromkeys(role_ids):
        db.add(SysUserRole(user_id=user_id, role_id=role_id))


async def assign_user_roles(
    db: AsyncSession, user_id: str, role_ids: Sequence[str], operator: AuthContext
) -> None:
    """分配角色 —— 整体替换。角色变了，权限随之变，故必须失效会话。"""
    user = await db.get(SysUser, user_id)
    if user is None:
        raise NotFoundError("用户不存在")
    await _assert_roles_exist(db, role_ids)

    # 先取旧集合，才能算出增删数 —— 只记「最终有几个角色」看不出这次是提权还是降权。
    before = set(
        await db.scalars(select(SysUserRole.role_id).where(SysUserRole.user_id == user_id))
    )
    after = set(role_ids)

    await _replace_user_roles(db, user_id, role_ids)
    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.USER_GRANT_ROLE,
        target_type=audit_service.AuditTargetType.USER,
        target_id=user_id,
        detail={
            "role_ids_count": len(after),
            "added_count": len(after - before),
            "removed_count": len(before - after),
        },
    )
    await db.commit()
    logger.info("用户角色已更新：user_id=%s 角色数=%s", user_id, len(after))
    await evict_user_sessions(redis_client.get_client(), [user_id])


async def delete_user(db: AsyncSession, user_id: str, operator: AuthContext) -> None:
    """删用户。有文档授权引用即拒绝。"""
    user = await db.get(SysUser, user_id)
    if user is None:
        raise NotFoundError("用户不存在")

    await _assert_no_document_permission(db, "user", user_id)

    # 同 `delete_department`：名字要在 `db.delete` **之前**取，删完就读不到了。
    # 用户被删且其审计行是唯一还记得「这个 id 是谁」的地方（本表刻意不加外键，
    # 所以 `username` 也不会随行级联消失，但审计行本身要留得下名字）。
    username = user.username
    await db.execute(delete(SysUserRole).where(SysUserRole.user_id == user_id))
    await db.delete(user)
    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.USER_DELETE,
        target_type=audit_service.AuditTargetType.USER,
        target_id=user_id,
        detail={"username": username},
    )
    await db.commit()
    logger.info("用户已删除：id=%s username=%s", user_id, username)
    await evict_user_sessions(redis_client.get_client(), [user_id])


async def _assert_roles_exist(db: AsyncSession, role_ids: Sequence[str]) -> None:
    """批量校验角色 id 都存在 —— 先整体验证再写，避免写入一半失败。"""
    ids = list(dict.fromkeys(role_ids))
    if not ids:
        return
    found = set(await db.scalars(select(SysRole.id).where(SysRole.id.in_(ids))))
    missing = [r for r in ids if r not in found]
    if missing:
        raise NotFoundError(f"角色不存在：{','.join(missing)}")


# ═══════════════════════════════════════════════════════════════
# 角色与权限
# ═══════════════════════════════════════════════════════════════


async def _permission_ids_of_roles(
    db: AsyncSession, role_ids: Sequence[str]
) -> dict[str, list[str]]:
    if not role_ids:
        return {}
    rows = await db.execute(
        select(SysRolePermission.role_id, SysRolePermission.permission_id).where(
            SysRolePermission.role_id.in_(list(role_ids))
        )
    )
    result: dict[str, list[str]] = {rid: [] for rid in role_ids}
    for role_id, permission_id in rows:
        result.setdefault(role_id, []).append(permission_id)
    return result


async def list_roles(db: AsyncSession) -> list[RoleItem]:
    rows = list(await db.scalars(select(SysRole).order_by(SysRole.created_at, SysRole.id)))
    perm_map = await _permission_ids_of_roles(db, [r.id for r in rows])
    return [
        RoleItem(
            id=r.id,
            code=r.code,
            name=r.name,
            description=r.description,
            is_builtin=r.is_builtin,
            permission_ids=perm_map.get(r.id, []),
            created_at=r.created_at,
        )
        for r in rows
    ]


async def create_role(
    db: AsyncSession, payload: RoleCreate, operator: AuthContext
) -> RoleItem:
    exists = await db.scalar(select(SysRole.id).where(SysRole.code == payload.code))
    if exists:
        raise ConflictError(f"角色码已存在：{payload.code}")

    role = SysRole(
        id=_new_id("role"),
        code=payload.code,
        name=payload.name,
        description=payload.description,
        is_builtin=False,
    )
    db.add(role)
    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.ROLE_CREATE,
        target_type=audit_service.AuditTargetType.ROLE,
        target_id=role.id,
        detail={"code": role.code, "name": role.name},
    )
    await db.commit()
    await db.refresh(role)
    return RoleItem(
        id=role.id,
        code=role.code,
        name=role.name,
        description=role.description,
        is_builtin=role.is_builtin,
        permission_ids=[],
        created_at=role.created_at,
    )


async def update_role(
    db: AsyncSession, role_id: str, payload: RoleUpdate, operator: AuthContext
) -> RoleItem:
    """改角色。`code` 不在 DTO 里，故结构上不可能被改。"""
    role = await db.get(SysRole, role_id)
    if role is None:
        raise NotFoundError("角色不存在")

    fields = payload.model_fields_set
    applied: set[str] = set()
    if "name" in fields and payload.name is not None:
        role.name = payload.name
        applied.add("name")
    if "description" in fields:
        role.description = payload.description
        applied.add("description")

    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.ROLE_UPDATE,
        target_type=audit_service.AuditTargetType.ROLE,
        target_id=role_id,
        detail={"changed_fields": sorted(applied)},
    )
    await db.commit()
    await db.refresh(role)
    perm_map = await _permission_ids_of_roles(db, [role_id])
    return RoleItem(
        id=role.id,
        code=role.code,
        name=role.name,
        description=role.description,
        is_builtin=role.is_builtin,
        permission_ids=perm_map.get(role_id, []),
        created_at=role.created_at,
    )


async def delete_role(
    db: AsyncSession, role_id: str, operator: AuthContext
) -> None:
    role = await db.get(SysRole, role_id)
    if role is None:
        raise NotFoundError("角色不存在")

    if role.is_builtin:
        raise ConflictError("内置角色不可删除")

    holder_count = await db.scalar(
        select(func.count()).select_from(SysUserRole).where(SysUserRole.role_id == role_id)
    )
    if holder_count:
        raise ConflictError("仍有用户持有该角色，无法删除")

    await _assert_no_document_permission(db, "role", role_id)

    role_code = role.code
    role_name = role.name
    await db.execute(
        delete(SysRolePermission).where(SysRolePermission.role_id == role_id)
    )
    await db.delete(role)
    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.ROLE_DELETE,
        target_type=audit_service.AuditTargetType.ROLE,
        target_id=role_id,
        detail={"code": role_code, "name": role_name, "user_count": 0},
    )
    await db.commit()
    logger.info("角色已删除：id=%s code=%s", role_id, role_code)


async def get_permission_tree(db: AsyncSession) -> list[PermissionNode]:
    """功能权限树（两层：`menu` → `button`）。

    返回的是**全量**树，不含「某角色已勾选哪些」—— 后者缺接口（字典 §9 #75），
    前端 fe-04 的授权弹窗回显因此会缺数据，本模块不自创路径。
    """
    rows = list(await db.scalars(select(SysPermission)))
    nodes = {
        r.id: PermissionNode(
            id=r.id,
            parent_id=r.parent_id,
            code=r.code,
            name=r.name,
            type=r.type,
            sort=r.sort,
            children=[],
        )
        for r in rows
    }
    roots: list[PermissionNode] = []
    for r in rows:
        node = nodes[r.id]
        parent = nodes.get(r.parent_id) if r.parent_id else None
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)
    _sort_tree(roots)  # type: ignore[arg-type]  # 与部门节点同形（sort + id 兜底）
    return roots


async def grant_role_permissions(
    db: AsyncSession, role_id: str, permission_ids: Sequence[str], operator: AuthContext
) -> None:
    """角色授权 —— 整体替换。权限变了，持该角色的所有用户都要失效会话。"""
    role = await db.get(SysRole, role_id)
    if role is None:
        raise NotFoundError("角色不存在")

    ids = list(dict.fromkeys(permission_ids))
    if ids:
        found = set(
            await db.scalars(select(SysPermission.id).where(SysPermission.id.in_(ids)))
        )
        missing = [p for p in ids if p not in found]
        if missing:
            raise NotFoundError(f"权限不存在：{','.join(missing)}")

    before = set(
        await db.scalars(
            select(SysRolePermission.permission_id).where(
                SysRolePermission.role_id == role_id
            )
        )
    )
    after = set(ids)

    await db.execute(
        delete(SysRolePermission).where(SysRolePermission.role_id == role_id)
    )
    for permission_id in ids:
        db.add(SysRolePermission(role_id=role_id, permission_id=permission_id))
    # 🔴 **只记数量，不记 `permission_ids` 集合** —— 权限 id 集合等价于一份
    #    权限快照，而审计页是管理员可读的（`P2-12` §5.2 的「授权类操作只记数量」）。
    #    这里是最该守住的一处：角色授权是提权的**集中入口**。
    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.ROLE_GRANT_PERMISSION,
        target_type=audit_service.AuditTargetType.ROLE,
        target_id=role_id,
        detail={
            "permission_count": len(after),
            "added_count": len(after - before),
            "removed_count": len(before - after),
        },
    )
    await db.commit()

    affected = await _user_ids_by_role(db, role_id)
    logger.info("角色授权已更新：role_id=%s 权限数=%s 受影响用户数=%s",
                role_id, len(ids), len(affected))
    await evict_user_sessions(redis_client.get_client(), affected)


async def _user_ids_by_role(db: AsyncSession, role_id: str) -> list[str]:
    rows = await db.scalars(
        select(SysUserRole.user_id).where(SysUserRole.role_id == role_id)
    )
    return list(rows)


# ═══════════════════════════════════════════════════════════════
# 跨模块引用校验（只读 kb_document_permission）
# ═══════════════════════════════════════════════════════════════

_TABLE_EXISTS_SQL = text(
    "SELECT COUNT(*) FROM information_schema.tables "
    "WHERE table_schema = DATABASE() AND table_name = :name"
)


async def _assert_no_document_permission(
    db: AsyncSession, entity_type: str, entity_id: str
) -> None:
    """删除前的第三类校验：`kb_document_permission` 是否有悬挂引用。

    🔴 **该表归 be-04，当前尚未建。** 表不存在时**放行并打 WARNING** ——
       这会在 be-04 开工前留下「有文档授权的部门/用户/角色被误删」的窗口。
       这个缺口是**已知且刻意**的：另两个选择是让本模块去建 be-04 的表（越界），
       或让删除接口整体不可用（更糟）。
       探测是运行时的，be-04 建表后此校验**自动生效，无需改代码**。
    """
    exists = await db.scalar(_TABLE_EXISTS_SQL, {"name": _DOC_PERM_TABLE})
    if not exists:
        logger.warning(
            "跳过文档授权引用校验：%s 尚未建立（归 be-04）。entity_type=%s entity_id=%s"
            " —— 此删除在 be-04 落地后本应被拒绝",
            _DOC_PERM_TABLE,
            entity_type,
            entity_id,
        )
        return

    count = await db.scalar(
        text(
            f"SELECT COUNT(*) FROM {_DOC_PERM_TABLE} "  # noqa: S608 - 表名是模块常量，非用户输入
            "WHERE entity_type = :t AND entity_id = :i"
        ),
        {"t": entity_type, "i": entity_id},
    )
    if count:
        raise ConflictError("存在文档权限引用，无法删除")
