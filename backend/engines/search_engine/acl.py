"""权限过滤：前置表达式构造 + **后置兜底复核**（be-05 的安全核心）。

两层防线的分工（PRD §6.2.2 / §6.2.3）：

| | 前置过滤（`build_acl_expr`） | 后置兜底（`post_verify`） |
|---|---|---|
| 位置 | Milvus 标量过滤，进索引层 | MySQL 权威权限复核 |
| 作用 | 让无权内容**进不了候选集**，顺带省算力 | 抓住 Milvus 侧 ACL **陈旧/算错**的情况 |
| 数据源 | Milvus 的 `acl_*` 副本 | `kb_document_permission` + `kb_document` |

🔴 **后置兜底不可省略**。Milvus 的 ACL 是冗余副本（be-04 写入时同步），
   与 MySQL 之间存在同步窗口：权限刚被收回、或同步失败时，
   Milvus 里那一份仍是旧值。此时**只有后置复核能挡住越权**。
   所以本文件的两个函数是「互补」关系，不是「重复」。

⚠️ 本模块**不 import FastAPI**（分层铁律），也不 import 任何 client ——
   它是纯逻辑 + SQLAlchemy 查询，方便单测。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.kb_document import KbDocument, KbDocumentPermission
from backend.schemas.auth import AuthContext

logger = logging.getLogger(__name__)

__all__ = ["AclExpr", "PostVerifyResult", "build_acl_expr", "post_verify"]

#: ID 白名单 —— 与 `clients/milvus_client.py` 的 `_SAFE_ID_RE` **同一条规则**。
#:
#: 🔴 Milvus 的 filter 表达式**不支持绑定参数**，只能拼字符串，
#:    所以白名单是这里唯一的防线。正常路径上这些 id 全部由后端生成
#:    （`前缀_12位hex`，见 `core/ids.py`）或来自 JWT（be-03 签发），
#:    绝不会含特殊字符 —— 它挡的是「将来某个调用方把用户输入当 id 传进来」。
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _sanitize_ids(values: list[str] | None, label: str) -> list[str]:
    """过滤掉不合法的 id，**剔除而不是抛异常**。

    为什么剔除：一个畸形的部门 id 只应该导致「该维度不扩权」，
    不应该让整次问答 500 —— 后者会把一个数据脏点放大成一次服务中断。
    但**必须留日志**：静默剔除会让「授权了但检索不到」无从排查。
    """
    out: list[str] = []
    for value in values or []:
        if isinstance(value, str) and _ID_RE.match(value):
            out.append(value)
        else:
            logger.warning("ACL 维度 %s 含非法 id，已剔除：%r", label, value)
    return out


@dataclass(slots=True)
class AclExpr:
    """前置过滤表达式及其构造过程的可观测信息。"""

    expr: str
    #: 实际参与拼装的各维度 id 数量 —— 便于在「检索结果为空」时快速判断
    #: 是「权限没配上」还是「文档没有」。
    dept_count: int = 0
    role_count: int = 0
    user_count: int = 0
    doc_count: int = 0


def build_acl_expr(ctx: AuthContext, visible_document_ids: set[str]) -> AclExpr:
    """构造 Milvus 标量过滤表达式（字典 §3 / PRD §6.2.2 的口径）。

    形态：

    ```
    (acl_global == true
     or array_contains_any(acl_departments, [...])
     or array_contains_any(acl_roles, [...])
     or array_contains_any(acl_users, ['<uid>']))
    and document_id in [...]
    ```

    🔴 **`ctx.depts` 必须是「自身 + 全部祖先」的有效部门集合**，不是 `department_id`。
       部门权限向下继承（PRD §6.1.2）：授权给「财务部」自动覆盖其子孙部门。
       少一层祖先 = 该部门下所有文档**静默不可见**，不报错、不打日志，
       只是「怎么搜都搜不到」—— 这是本项目最难归因的失效模式之一。
       该集合由 be-03 的 `load_user_identity` 计算并写进 JWT，本模块**信任**它。

    🔴 **`document_id in [...]` 这一段的由来**：字典 §3 的 Milvus schema
       **没有 `status` 字段**，而 PRD §6.5 要求过滤掉非 `enabled` 的文档。
       本设计选择「把可见文档 id 拼进表达式」（be-05 §10-18），
       口径唯一、不依赖 Milvus 侧状态字段的正确性。
       代价是表达式随文档数线性膨胀 —— 已登记为字典 §9 #112，
       P1 文档量小可接受，量级上来后需换方案。

    Raises:
        ValueError: `visible_document_ids` 为空。调用方应**在此之前**短路
            （没有可见文档就不必检索），而不是构造一个「匹配空集」的表达式 ——
            Milvus 对 `in []` 的行为随版本而异，不能依赖。
    """
    if not visible_document_ids:
        # 空集合在这里是**调用方的编排错误**，不是正常业务态：
        # 「用户一篇文档都无权看」应当在检索前就返回空结果，省掉三次 Milvus 调用。
        raise ValueError("visible_document_ids 为空，应在调用前短路")

    depts = _sanitize_ids(ctx.depts, "acl_departments")
    roles = _sanitize_ids(ctx.roles, "acl_roles")
    users = _sanitize_ids([ctx.user_id], "acl_users")
    docs = sorted(_sanitize_ids(list(visible_document_ids), "document_id"))

    # 全局公开是恒真的一维，永远保留 —— 漏掉它会让所有公开文档消失。
    parts = ["acl_global == true"]
    if depts:
        parts.append(f"array_contains_any(acl_departments, {json.dumps(depts)})")
    if roles:
        parts.append(f"array_contains_any(acl_roles, {json.dumps(roles)})")
    if users:
        parts.append(f"array_contains_any(acl_users, {json.dumps(users)})")

    # `json.dumps` 产出的就是 Milvus 需要的 `["a", "b"]` 字面量，
    # 且它会对引号/反斜杠做转义 —— 比手工 join 更安全（虽然上面已过白名单）。
    expr = f"({' or '.join(parts)}) and document_id in {json.dumps(docs)}"

    return AclExpr(
        expr=expr,
        dept_count=len(depts),
        role_count=len(roles),
        user_count=len(users),
        doc_count=len(docs),
    )


@dataclass(slots=True)
class PostVerifyResult:
    """后置复核结果。"""

    #: 复核通过、可进入上下文与引用列表的 document_id
    allowed: set[str] = field(default_factory=set)
    #: 复核**未通过**的 document_id —— 即前置过滤放行了但权威权限不认的
    blocked: set[str] = field(default_factory=set)
    #: `document_id -> title`，**只含 allowed**。未授权文档连标题都不取（红线 4）。
    titles: dict[str, str] = field(default_factory=dict)

    @property
    def desynced(self) -> set[str]:
        """前置放行但后置拒绝的集合 —— 即「Milvus ACL 与 MySQL 不一致」的证据。

        非空即说明存在同步窗口，必须告警（PRD §6.2.3「不一致的剔除 + 告警」）。
        """
        return set(self.blocked)


async def post_verify(
    db: AsyncSession,
    candidate_document_ids: set[str],
    ctx: AuthContext,
    visible_document_ids: set[str],
) -> PostVerifyResult:
    """用 MySQL 的**权威**权限数据复核候选文档（PRD §6.2.3，防越权硬保证）。

    判据与 `kb_document_permission` 的四维 OR 完全一致（字典 §6.1.2）：

    1. 文档必须**可见** —— 在 `visible_document_ids` 里（`enabled` 且未软删）；
    2. 且满足四维 OR 之一：`global` / 命中 `ctx.depts` / 命中 `ctx.roles` / 等于 `ctx.user_id`。

    ⚠️ **没有任何权限行的文档 = 不可检索**（字典 §2.2 判定规则）。
       这是刻意的安全默认：新导入的文档在管理员显式授权前，谁都不能查。
       实现上体现为「第 2 步没有任何行命中」。

    Args:
        visible_document_ids: 由 `import_engine.list_visible_document_ids(db)` 得到。
            **由调用方传入而不是在这里再查一次** —— 检索那边为拼表达式已经查过，
            再查一遍既是浪费，更糟的是两次查询之间文档可能被停用，
            造成「表达式允许但复核拒绝」的假不一致告警。

    Returns:
        复核结果。**本函数不抛业务异常** —— 复核失败只是少几条候选，
        不该让整次问答失败。
    """
    if not candidate_document_ids:
        return PostVerifyResult()

    candidates = set(candidate_document_ids) & set(visible_document_ids)
    invisible = set(candidate_document_ids) - candidates

    result = PostVerifyResult()
    # 不在可见集合里的（已停用 / 已软删）直接算未通过 ——
    # 它们没有标题可查，也不该出现在引用里。
    result.blocked |= invisible

    if not candidates:
        return result

    # ---- 第 1 步：一次性取回候选文档的全部权限行 ----
    # 🔴 一条 SQL 取回而不是「逐个文档查一次」：候选有 top_k*3 条，
    #    逐个查就是 N+1，在问答热路径上直接体现为响应变慢。
    perm_stmt = select(
        KbDocumentPermission.document_id,
        KbDocumentPermission.entity_type,
        KbDocumentPermission.entity_id,
    ).where(KbDocumentPermission.document_id.in_(candidates))
    perm_rows = (await db.execute(perm_stmt)).all()

    #: `document_id -> {entity_type: set(entity_id)}`
    grants: dict[str, dict[str, set[str]]] = {}
    for document_id, entity_type, entity_id in perm_rows:
        grants.setdefault(document_id, {}).setdefault(entity_type, set()).add(entity_id)

    # ---- 第 2 步：取标题 ----
    # 只查候选，且只取复核通过后才用得到的列。标题**不来自 Milvus**
    # （字典 §3 的 schema 里没有 title），必须回权威表取。
    title_stmt = select(KbDocument.id, KbDocument.title).where(
        KbDocument.id.in_(candidates)
    )
    title_by_id = {row[0]: row[1] for row in (await db.execute(title_stmt)).all()}

    depts = set(ctx.depts)
    roles = set(ctx.roles)

    for document_id in candidates:
        doc_grants = grants.get(document_id, {})
        authorized = (
            KbDocumentPermission.GLOBAL_ENTITY_ID
            in doc_grants.get(KbDocumentPermission.ENTITY_GLOBAL, set())
            or bool(depts & doc_grants.get(KbDocumentPermission.ENTITY_DEPARTMENT, set()))
            or bool(roles & doc_grants.get(KbDocumentPermission.ENTITY_ROLE, set()))
            or ctx.user_id in doc_grants.get(KbDocumentPermission.ENTITY_USER, set())
        )
        if authorized:
            result.allowed.add(document_id)
            # 标题缺失时回退成 id 而不是丢弃：文档行还在（可见集合已确认），
            # 只是标题查询没命中，这种情况罕见但真发生时应留下线索。
            result.titles[document_id] = title_by_id.get(document_id, document_id)
        else:
            result.blocked.add(document_id)

    if result.desynced:
        # 🔴 **结构化告警**（be-05 §10-26）。这条日志是「Milvus ACL 与 MySQL 不同步」
        #    的唯一可见证据 —— 不一致本身不阻断请求，所以没有它就没有任何信号。
        #    ⚠️ 日志里**只记 id 与数量，不记标题**：标题属未授权内容（红线 4）。
        logger.warning(
            "ACL 不一致：前置过滤放行了 %d 个文档，MySQL 权威权限复核未通过",
            len(result.desynced),
            extra={
                "event": "acl_desync",
                "user_id": ctx.user_id,
                "blocked_document_ids": sorted(result.desynced),
                "count": len(result.desynced),
            },
        )

    return result
