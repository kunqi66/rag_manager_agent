"""审计服务（be-06）。

**本模块的价值不在这些函数，而在调用点** —— 一个没有任何人调用的审计服务
等于没有审计。调用点清单冻结在 `specs/P2-12-be-audit.md` §5.4（28 个动作），
评审与验收都对照那张表逐个核。

两条留痕路径**语义完全不同**，用错会破坏业务事务或阻断读路径：

| | `record()` | `record_read()` |
|---|---|---|
| 场景 | 写操作 | 管理员读正文 |
| 事务 | 与业务同 session，**自己不 commit** | **自开独立短事务**并 commit |
| 失败 | 抛错 → 业务一并回滚 | 仅告警，**绝不回滚调用方** |

🔴 **`record()` 绝不 commit**：它一 commit，就把调用方尚未完成的业务写
一起提交了，业务后续失败时回滚不掉 —— 审计与业务的一致性反而被它破坏。
事务边界完全归调用方。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.db import SessionLocal
from backend.core.exceptions import ParamError
from backend.core.ids import new_id
from backend.core.settings import settings
from backend.models.audit_log import ANONYMOUS_USER_ID, SysAuditLog
from backend.models.sys_user import SysUser
from backend.schemas.audit import AuditLogItem, AuditLogQuery
from backend.schemas.auth import AuthContext

logger = logging.getLogger(__name__)

__all__ = [
    "AuditAction",
    "AuditTargetType",
    "list_audit_logs",
    "record",
    "record_read",
    "sanitize_detail",
]


class AuditAction:
    """动作全集（字典 §5.5，28 个）。

    用类常量而非 `enum.Enum`：值要直接落库成字符串，Enum 在每个调用点
    都要多写一层 `.value`，而漏写的那次会把 `<AuditAction.LOGIN: 'auth.login'>`
    这种 repr 写进库 —— 字符串列照收不误，不报错。

    🔴 **禁止在调用点写裸字符串**。动作名是「审计页筛选下拉的选项来源」
    与「统计口径」之间的隐式契约，拼错的表现是那一类操作在审计页**筛不出来**
    （行还在，只是没人能按动作找到它）。
    """

    # ---- be-01 认证 ----
    LOGIN = "auth.login"
    LOGIN_FAILED = "auth.login_failed"
    LOGOUT = "auth.logout"
    # ---- be-02 组织 ----
    DEPARTMENT_CREATE = "department.create"
    DEPARTMENT_UPDATE = "department.update"
    DEPARTMENT_DELETE = "department.delete"
    USER_CREATE = "user.create"
    USER_UPDATE = "user.update"
    USER_DELETE = "user.delete"
    USER_GRANT_ROLE = "user.grant_role"
    USER_RESET_PASSWORD = "user.reset_password"
    ROLE_CREATE = "role.create"
    ROLE_UPDATE = "role.update"
    ROLE_DELETE = "role.delete"
    ROLE_GRANT_PERMISSION = "role.grant_permission"
    # ---- be-04 知识导入 ----
    DOCUMENT_IMPORT = "document.import"
    DOCUMENT_UPDATE = "document.update"
    DOCUMENT_REINDEX = "document.reindex"
    DOCUMENT_STATUS_UPDATE = "document.status.update"
    DOCUMENT_PERMISSION_UPDATE = "document.permission.update"
    DOCUMENT_DELETE = "document.delete"
    DOCUMENT_READ = "document.read"
    IMPORT_TASK_RETRY = "import.task.retry"
    # ---- be-08 FAQ / be-09 缺口（P3 落地）----
    FAQ_PUBLISH = "faq.publish"
    FAQ_REJECT = "faq.reject"
    FAQ_CACHE_TOGGLE = "faq.cache.toggle"
    GAP_TASK_CREATE = "gap.task.create"
    GAP_STATUS_UPDATE = "gap.status.update"


class AuditTargetType:
    """目标类型全集（字典 §2.1 `target_type` 列的取值）。"""

    USER = "user"
    ROLE = "role"
    DEPARTMENT = "department"
    DOCUMENT = "document"
    IMPORT_TASK = "import_task"
    FAQ = "faq"
    GAP = "gap"


# 🔴 **按 `action` 收敛的白名单**（`P2-12` §5.2）。
# 不用 `target_type` 做键：同一 target_type 下多个动作的键集被迫取并集，
# `user.delete` 的调用点写 `changed_fields` 也能通过 —— 默认拒绝就被架空了。
# 键的语义本来就依附于动作。
_DETAIL_WHITELIST: dict[str, frozenset[str]] = {
    # ---- be-01 ----
    AuditAction.LOGIN: frozenset(),
    # `username` 必须在白名单里：登录失败时 `user_id` 可能落成 `anonymous`
    # （账号根本不存在），不留登录名的话这条记录**完全无法追溯**
    # —— 而「谁在暴力试哪个账号」正是这类记录唯一的价值。
    AuditAction.LOGIN_FAILED: frozenset({"reason", "username"}),
    AuditAction.LOGOUT: frozenset(),
    # ---- be-02 ----
    AuditAction.DEPARTMENT_CREATE: frozenset({"name", "parent_id", "sort"}),
    AuditAction.DEPARTMENT_UPDATE: frozenset({"changed_fields", "parent_id", "sort"}),
    AuditAction.DEPARTMENT_DELETE: frozenset({"name", "child_count", "user_count"}),
    AuditAction.USER_CREATE: frozenset({"username", "department_id", "role_ids_count"}),
    AuditAction.USER_UPDATE: frozenset({"changed_fields", "department_id"}),
    AuditAction.USER_DELETE: frozenset({"username"}),
    AuditAction.USER_GRANT_ROLE: frozenset({"role_ids_count", "added_count", "removed_count"}),
    AuditAction.USER_RESET_PASSWORD: frozenset(),
    AuditAction.ROLE_CREATE: frozenset({"code", "name"}),
    AuditAction.ROLE_UPDATE: frozenset({"changed_fields"}),
    AuditAction.ROLE_DELETE: frozenset({"code", "name", "user_count"}),
    AuditAction.ROLE_GRANT_PERMISSION: frozenset(
        {"permission_count", "added_count", "removed_count"}
    ),
    # ---- be-04 ----
    AuditAction.DOCUMENT_IMPORT: frozenset(
        {"title", "file_name", "batch_id", "file_count", "category"}
    ),
    AuditAction.DOCUMENT_UPDATE: frozenset({"changed_fields", "title"}),
    AuditAction.DOCUMENT_REINDEX: frozenset({"chunk_count"}),
    AuditAction.DOCUMENT_STATUS_UPDATE: frozenset({"old_status", "new_status"}),
    AuditAction.DOCUMENT_PERMISSION_UPDATE: frozenset({"grant_entity_counts"}),
    AuditAction.DOCUMENT_DELETE: frozenset({"title"}),
    AuditAction.DOCUMENT_READ: frozenset({"title"}),
    AuditAction.IMPORT_TASK_RETRY: frozenset({"batch_id", "retry_count"}),
    # ---- be-08 / be-09（P3 落地，此处先冻结键集）----
    AuditAction.FAQ_PUBLISH: frozenset({"candidate_id", "faq_id", "frequency"}),
    AuditAction.FAQ_REJECT: frozenset({"candidate_id", "reason"}),
    AuditAction.FAQ_CACHE_TOGGLE: frozenset({"faq_id", "cache_enabled"}),
    AuditAction.GAP_TASK_CREATE: frozenset(
        {"gap_id", "frequency", "suggested_category", "task_id"}
    ),
    AuditAction.GAP_STATUS_UPDATE: frozenset({"gap_id", "from_status", "to_status"}),
}

#: 子串兜底黑名单。防的是「白名单本身写错」——例如把 `new_password` 写进了
#: 某个动作的允许集。**这是兜底不是主防线**：主防线是上面按 action 收敛的白名单。
#: ⚠️ 匹配的是**子串**（`any(s in key.lower())`），因为敏感键的命名前缀五花八门
#: （`new_password` / `old_password` / `password_hash` / `access_token` …），
#: 逐个枚举必漏。
_DENY_SUBSTRINGS: tuple[str, ...] = (
    "password",
    "passwd",
    "token",
    "jwt",
    "secret",
    "hash",
    "salt",
)

#: 读取去重键的 Redis 命名空间（沿用全项目 `rag:*` 前缀）。
_READ_DEDUP_KEY = "rag:audit:read:{user_id}:{target_id}"


def sanitize_detail(action: str, detail: dict[str, Any] | None) -> dict[str, Any]:
    """按 `action` 白名单剔除键；非白名单键一律丢弃（**默认拒绝**）。

    `action` 不在白名单表里 → 返回 `{}` 并告警。**失败向安全侧倒**：
    新增动作忘了登记时，结果是「详情是空的」而不是「敏感字段落库了」。
    前者是可见的缺陷，后者是不可见的泄漏。
    """
    if not detail:
        return {}

    allowed = _DETAIL_WHITELIST.get(action)
    if allowed is None:
        logger.warning("审计 detail 白名单命中失败（动作未登记），已清空：action=%s", action)
        return {}

    cleaned: dict[str, Any] = {}
    for key, value in detail.items():
        lowered = key.lower()
        if any(bad in lowered for bad in _DENY_SUBSTRINGS):
            # 记键名不记值 —— 值本身可能就是那个敏感串。
            logger.warning("审计 detail 剔除敏感键：action=%s key=%s", action, key)
            continue
        if key in allowed:
            cleaned[key] = value
        # else: 静默丢弃非白名单键。这是**预期行为**（默认拒绝），
        #       不是异常，所以不打日志 —— 否则每个动作都会刷一堆噪音，
        #       反而把上面两条真正该看的告警淹没。
    return cleaned


def _assert_known_action(action: str) -> None:
    """未知动作当场报错。

    ⚠️ 刻意**不**像 `sanitize_detail` 那样静默降级。动作名拼错是**开发期错误**：
    若只是清空 detail，症状会是「审计行在、详情永远为空」——
    没有任何人会在几百条审计里注意到某几行少了详情，于是错误活到线上。
    """
    if action not in _DETAIL_WHITELIST:
        raise ParamError(f"未知审计动作：{action}")


def record(
    db: AsyncSession,
    ctx: AuthContext,
    action: str,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """写操作留痕：**塞进调用方的 session，自己不 commit**。

    同步函数、无 `await` —— 只做 `db.add()`，不发 SQL，所以调用方
    不必为了留痕多一次 `await`，也不会因为这里的 flush 打乱自己的语句顺序。

    🔴 **不 commit** 是本函数的硬约束（见模块 docstring）。
    """
    _assert_known_action(action)
    db.add(_build_row(ctx, action, target_type, target_id, detail))


async def record_detached(
    ctx: AuthContext,
    action: str,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
    dedup_seconds: int | None = None,
) -> None:
    """**自开独立短事务**落痕，失败只告警。用于「没有业务事务可搭」的场合。

    🔴 这个函数的存在本身就是一处设计结论（`P2-12` §4.2 的实现期更正）：

    `record()` **自己不 commit**，靠调用方提交。可有些留痕点**根本没有**调用方事务：
    `auth.login_failed`（失败分支直接 `raise`，session 从不提交）、
    `auth.logout`（函数签名里连 `db` 都没有）、`document.read`（纯读路径）。
    这些地方若用 `record()`，行会被 `add` 进 session 然后随 session 关闭**静默蒸发** ——
    审计看起来「代码写了」，实际一条都没落库，且不报任何错。

    | | `record()` | `record_detached()` |
    |---|---|---|
    | 事务 | 搭调用方的 | **自开一个并 commit** |
    | 失败 | 抛错，业务一并回滚 | **吞掉**，只告警 |
    | 用在 | 有业务写的路径 | 失败分支 / 纯读路径 / 无 session 的路径 |

    `dedup_seconds` 给定时，窗口内同 `(user_id, target_id)` 只落一条。
    ⚠️ **默认不去重** —— 去重是 `record_read()` 的专属需求。登录失败**绝不能去重**：
    连续 20 次失败正是最该被看见的信号，合并成一条等于把攻击痕迹抹平。
    """
    try:
        if dedup_seconds is not None:
            redis = await _get_redis()
            key = _READ_DEDUP_KEY.format(user_id=ctx.user_id, target_id=target_id or "-")
            # SET NX EX：抢到键才写。抢不到说明窗口内已经记过，直接返回。
            if not await redis.set(key, "1", nx=True, ex=dedup_seconds):
                return

        async with SessionLocal() as db:
            db.add(_build_row(ctx, action, target_type, target_id, detail))
            await db.commit()
    except Exception:
        # 故意用宽泛的 except：这里**任何**失败都不该冒泡到调用方。
        # 但必须 logger.exception 留全栈，否则「审计静默失效」将无人知晓。
        logger.exception(
            "审计写入失败（已忽略，不影响主流程）：action=%s target_id=%s",
            action,
            target_id,
        )


async def record_read(
    ctx: AuthContext,
    action: str,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """读正文留痕 = `record_detached` + 去重窗口。

    去重的是**翻页 / 预览 / 重复打开**产生的同质记录：知识管理员翻一份长文档
    会触发几十次读取，逐条落库会让审计表被同一个人同一份文档刷满，
    真正的异常行为反而被淹没。
    """
    await record_detached(
        ctx,
        action,
        target_type=target_type,
        target_id=target_id,
        detail=detail,
        dedup_seconds=settings.audit_read_dedup_seconds,
    )


async def _get_redis() -> Redis:
    """延迟取 Redis 单例。

    延迟 import：本模块在 `service/` 层，而 `clients.redis_client` 会建连接池 ——
    放在模块顶层会让「只是 import audit_service」也建池（`scripts/` 下的
    离线验收脚本正是这种用法）。
    """
    from backend.clients.redis_client import get_client

    return get_client()


def _build_row(
    ctx: AuthContext,
    action: str,
    target_type: str | None,
    target_id: str | None,
    detail: dict[str, Any] | None,
) -> SysAuditLog:
    """构造审计行。写入路径的唯一入口 —— 两条路径共用，防字段漂移。"""
    _assert_known_action(action)
    return SysAuditLog(
        id=new_id("aud"),
        user_id=ctx.user_id or ANONYMOUS_USER_ID,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=sanitize_detail(action, detail) or None,
        ip=ctx.ip,
    )


async def count_action(
    db: AsyncSession, action: str, *, target_type: str, target_id: str
) -> int:
    """数某个目标上某个动作**已经发生过几次**（不含本次）。

    存在的理由只有一个：有些动作的「第几次」本身就是信号，而业务表里
    **没有**对应的计数器列。首个用例是 `import.task.retry` ——
    `kb_import_task` 没有 `retry_count` 列（`celery_app` 的退避计数是
    worker 内存里的 `self.request.retries`，重启即归零），
    所以「这个任务被人工重试了 7 次」只能从审计流水本身读出来。

    这带来一个**必须知道的语义边界**：保留期清理（`audit.purge_audit_logs`，
    默认 180 天）会把这些历史行删掉，计数随之归零。对「重试次数」这个用途
    无影响（一个任务不可能挂 180 天还在重试），但**不要**拿它做长期累计。

    ⚠️ 读的是已提交的行 —— `record()` 在同一事务里 add 的行还没进这次 COUNT，
       这正是我们要的（「本次之前」的语义）。
    """
    stmt = (
        select(func.count())
        .select_from(SysAuditLog)
        .where(
            SysAuditLog.action == action,
            SysAuditLog.target_type == target_type,
            SysAuditLog.target_id == target_id,
        )
    )
    return int(await db.scalar(stmt) or 0)


async def list_audit_logs(
    db: AsyncSession, q: AuditLogQuery
) -> tuple[list[AuditLogItem], int]:
    """按条件分页查询审计流水。

    时间范围：`start_time` 缺省时回退到「`end_time`（或现在）往前
    `AUDIT_DEFAULT_RANGE_DAYS` 天」。**必须有这个默认** —— 不传参时若不加范围，
    就是一次全表扫，而这个端点面向的是管理员随手点开。
    """
    conditions = []
    if q.user_id:
        conditions.append(SysAuditLog.user_id == q.user_id)
    if q.action:
        conditions.append(SysAuditLog.action == q.action)
    if q.target_type:
        conditions.append(SysAuditLog.target_type == q.target_type)
    if q.target_id:
        conditions.append(SysAuditLog.target_id == q.target_id)

    end = q.end_time or datetime.now(UTC).replace(tzinfo=None)
    start = q.start_time or (end - timedelta(days=settings.audit_default_range_days))
    conditions.append(SysAuditLog.created_at >= start)
    conditions.append(SysAuditLog.created_at <= end)

    total = await db.scalar(
        select(func.count()).select_from(SysAuditLog).where(*conditions)
    )

    # ⚠️ **左连接**（不是 join）：用户被删后审计行仍要能列出来，
    # 只是 `username` 为 None。内连接会把「离职员工做过什么」整段抹掉 ——
    # 而那段恰恰是最需要追溯的。
    stmt = (
        select(SysAuditLog, SysUser.username)
        .outerjoin(SysUser, SysUser.id == SysAuditLog.user_id)
        .where(*conditions)
        # 次级键 id：created_at 精度只到秒，同一秒内的多行若只按时间排序，
        # 分页会在页边界上重复或漏行（同一个经典陷阱，见 fe 侧的游标约定）。
        .order_by(SysAuditLog.created_at.desc(), SysAuditLog.id.desc())
        .offset((q.page - 1) * q.page_size)
        .limit(q.page_size)
    )
    rows = (await db.execute(stmt)).all()

    items = []
    for row, username in rows:
        item = AuditLogItem(**row.to_dict())
        item.username = username
        items.append(item)
    return items, int(total or 0)
