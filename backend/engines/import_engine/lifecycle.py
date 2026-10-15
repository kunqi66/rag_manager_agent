"""知识单元的生命周期（be-04 §4.1 的 12 个端点背后的业务）。

职责边界：**只做「读写 MySQL 权威状态 + 派发任务」，不做解析与向量化** ——
后者在 `pipeline.py`（worker 侧）。这样划界是因为两者的运行环境不同：
本文件跑在 API 进程，`pipeline.py` 跑在 worker 进程。

落点依据：`vibecoding_doc/specs/P1-10-be-kb-import.md`。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.clients import milvus_client
from backend.core.exceptions import ConflictError, NotFoundError, ParamError
from backend.core.ids import new_id
from backend.core.settings import settings
from backend.models.kb_chunk import KbChunk
from backend.models.kb_document import KbDocument, KbDocumentPermission
from backend.models.kb_import_task import KbImportTask
from backend.models.sys_department import SysDepartment
from backend.models.sys_role import SysRole
from backend.models.sys_user import SysUser
from backend.schemas.auth import AuthContext
from backend.service import audit_service
from backend.schemas.kb_import import (
    DocumentDetail,
    DocumentListItem,
    DocumentListQuery,
    DocumentStatus,
    DocumentUpdatePayload,
    ImportTaskDetail,
    ImportTaskListQuery,
    ImportTaskListItem,
    ImportTaskRef,
    PermissionPayload,
    PermissionSummary,
    PermissionView,
    UploadAccepted,
)
from backend.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

__all__ = [
    "list_documents",
    "get_document",
    "get_permissions",
    "update_permissions",
    "set_document_status",
    "update_document",
    "delete_document",
    "upload_document",
    "upload_documents_batch",
    "list_tasks",
    "get_task",
    "retry_import_task",
    "create_supplement_task",
    "list_visible_document_ids",
    "derive_acl",
]

_DOC_PREFIX = "doc"
_TASK_PREFIX = "task"
_BATCH_PREFIX = "batch"
_PERM_PREFIX = "perm"

#: 空 ACL —— 新导入文档的默认值：没有任何权限行 = 除知识管理员外不可检索
#: （字典 §2.2 判定规则）。这不是 bug，是刻意的安全默认。
EMPTY_ACL: dict[str, Any] = {
    "acl_global": False,
    "acl_departments": [],
    "acl_roles": [],
    "acl_users": [],
}


# ═══════════════════════════════════════════════════════════════
# 公共小工具
# ═══════════════════════════════════════════════════════════════


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _dispatch(task_name: str, *args: Any) -> None:
    """派发 Celery 任务。

    🔴 **只能在事务提交之后调用**。事务内派活，worker 可能抢在提交前跑到，
       读不到任务行 → 直接 3001 失败，且现象是「偶发」——
       本地连打几十次都不复现，压测时才出现。
       把派发收进这一个函数，就是为了让这条约束只有一个入口。
    """
    celery_app.send_task(task_name, args=list(args))
    logger.info("已派发任务：%s args=%s", task_name, args)


async def _load_document(db: AsyncSession, document_id: str) -> KbDocument:
    """按 id 取**未删除**的文档。

    🔴 软删除的文档一律按 `NotFoundError`(3001) 处理，而不是 3002 ——
       对调用方来说它就是「不存在」，且这样读接口与写接口的行为一致。
       若读报 3001、写报 3002，前端就得为同一个「文档没了」写两条分支。
    """
    doc = await db.get(KbDocument, document_id)
    if doc is None or doc.deleted_at is not None:
        raise NotFoundError("知识单元不存在")
    return doc


async def _load_task(db: AsyncSession, task_id: str) -> KbImportTask:
    task = await db.get(KbImportTask, task_id)
    if task is None:
        raise NotFoundError("导入任务不存在")
    return task


async def _has_active_task(db: AsyncSession, document_id: str) -> bool:
    """该文档是否有未结束的导入/重建任务。"""
    stmt = (
        select(func.count())
        .select_from(KbImportTask)
        .where(
            KbImportTask.document_id == document_id,
            KbImportTask.status.in_(KbImportTask.ACTIVE_STATUSES),
        )
    )
    return bool(await db.scalar(stmt))


async def _record_read(document_id: str, operator: AuthContext) -> None:
    """管理员读正文留痕（be-06 §4.3 / 字典 §9 #101）。

    ✅ **be-06 已落地（2026-10-14）**，原先的「运行时探测 + 缺失时降级放行」已按期收口。
    探针那种写法有个隐蔽代价：`except ImportError` 在模块存在之后**永远不会触发**，
    于是「有人把调用改坏了」与「一切正常」在日志上长得一模一样 ——
    一条 `debug` 都没有。读正文是最该留痕的操作（`P2-12` §5.4 里唯一的读取类动作），
    它的失效必须是**响的**。

    因此现在直连 `audit_service.record_read`：调用签名错、函数被改名，
    都会在**导入期/启动时**炸出来，而不是安静地少记一批日志。

    🔴 仍然**不 await 它的结果、也不检查异常** —— `record_read` 内部已吞掉全部异常
    （读路径是热路径，留痕是旁路，不能让审计写失败阻断管理员看文档）。
    这是刻意的不对称：**调用关系**必须硬，**运行时失败**必须软。
    """
    await audit_service.record_read(
        operator,
        audit_service.AuditAction.DOCUMENT_READ,
        target_type=audit_service.AuditTargetType.DOCUMENT,
        target_id=document_id,
    )


# ═══════════════════════════════════════════════════════════════
# 四维权限
# ═══════════════════════════════════════════════════════════════


def derive_acl(rows: list[KbDocumentPermission]) -> dict[str, Any]:
    """把权限行折算成 Milvus 的四个 `acl_*` 字段。

    🔴 **这是 ACL 的唯一计算入口**（spec §8.2）。写入切片时与刷新权限时
       都必须走它 —— 两处各算一遍，迟早会出现「新写入的片带新权限、
       旧的片带旧权限」，而那正是 be-05 后置兜底要花力气关掉的窗口。
    """
    acl = dict(EMPTY_ACL)
    acl["acl_departments"] = []
    acl["acl_roles"] = []
    acl["acl_users"] = []
    for row in rows:
        if row.entity_type == KbDocumentPermission.ENTITY_GLOBAL:
            acl["acl_global"] = True
        elif row.entity_type == KbDocumentPermission.ENTITY_DEPARTMENT:
            acl["acl_departments"].append(row.entity_id)
        elif row.entity_type == KbDocumentPermission.ENTITY_ROLE:
            acl["acl_roles"].append(row.entity_id)
        elif row.entity_type == KbDocumentPermission.ENTITY_USER:
            acl["acl_users"].append(row.entity_id)
    return acl


async def _load_permission_rows(
    db: AsyncSession, document_id: str
) -> list[KbDocumentPermission]:
    stmt = select(KbDocumentPermission).where(
        KbDocumentPermission.document_id == document_id
    )
    return list((await db.scalars(stmt)).all())


def _expand_payload(document_id: str, payload: PermissionPayload) -> list[KbDocumentPermission]:
    """把入参展开成权限行。`global` 的 `entity_id` 固定 `*`（字典 §5.3）。"""
    rows: list[KbDocumentPermission] = []

    def add(entity_type: str, entity_ids: list[str]) -> None:
        for entity_id in entity_ids:
            rows.append(
                KbDocumentPermission(
                    id=new_id(_PERM_PREFIX),
                    document_id=document_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                )
            )

    if payload.global_public:
        add(KbDocumentPermission.ENTITY_GLOBAL, [KbDocumentPermission.GLOBAL_ENTITY_ID])
    add(KbDocumentPermission.ENTITY_DEPARTMENT, payload.departments)
    add(KbDocumentPermission.ENTITY_ROLE, payload.roles)
    add(KbDocumentPermission.ENTITY_USER, payload.users)
    return rows


async def _assert_references_exist(db: AsyncSession, payload: PermissionPayload) -> None:
    """校验授权对象的 ID 真实存在（字典 §9 #98）。

    🔴 **不能省**：传了不存在的部门/角色/用户 ID 时若静默写入，
       那一行 ACL **永远不会命中任何人** —— 管理员以为授权成功，
       实际是空授权，而且**没有任何报错**。这类「静默无效」比报错难查得多。

    报 1001（`ParamError`）而不是 3001：错在**请求内容**里的 ID 不存在，
       不是「你请求的那个资源没有」。
    """
    checks: list[tuple[type, list[str], str]] = [
        (SysDepartment, payload.departments, "部门"),
        (SysRole, payload.roles, "角色"),
        (SysUser, payload.users, "用户"),
    ]
    for model, ids, label in checks:
        wanted = [i for i in dict.fromkeys(ids) if i]
        if not wanted:
            continue
        found = set(
            (await db.scalars(select(model.id).where(model.id.in_(wanted)))).all()
        )
        missing = [i for i in wanted if i not in found]
        if missing:
            raise ParamError(f"{label}不存在：{'、'.join(missing)}")


async def get_permissions(db: AsyncSession, document_id: str) -> PermissionView:
    """权限回显（§4.1 #11）。

    🔴 这个接口**不是可选项**：`PUT` 的语义是**全量替换**，
       没有回显的话弹窗每次打开都是空的，用户一保存就把原权限覆盖成全空。
       验收 A3 的原话就是「4 种权限类型各配一次，**权限弹窗回显正确**」。
    """
    await _load_document(db, document_id)
    rows = await _load_permission_rows(db, document_id)
    acl = derive_acl(rows)
    return PermissionView(
        global_public=acl["acl_global"],
        departments=acl["acl_departments"],
        roles=acl["acl_roles"],
        users=acl["acl_users"],
    )


async def update_permissions(
    db: AsyncSession,
    document_id: str,
    payload: PermissionPayload,
    operator: AuthContext,
) -> None:
    """覆盖配置四维权限，并刷新 Milvus 的 ACL 副本（spec §5.6）。"""
    await _load_document(db, document_id)
    await _assert_references_exist(db, payload)

    await db.execute(
        delete(KbDocumentPermission).where(
            KbDocumentPermission.document_id == document_id
        )
    )
    new_rows = _expand_payload(document_id, payload)
    if new_rows:
        db.add_all(new_rows)
    # 🔴 **只记每个维度的条数，不记 entity_id 集合** —— `P2-12` §5.2 的
    #    「授权类操作只记数量」。id 集合等价于把 `kb_document_permission`
    #    这张权限表复制进审计表，而审计页对管理员是全量可见的。
    counts: dict[str, int] = {}
    for row in new_rows:
        counts[row.entity_type] = counts.get(row.entity_type, 0) + 1
    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.DOCUMENT_PERMISSION_UPDATE,
        target_type=audit_service.AuditTargetType.DOCUMENT,
        target_id=document_id,
        detail={"grant_entity_counts": counts},
    )
    await db.commit()

    # 提交后才刷 Milvus。失败**不向上抛**（spec §5.6）：ACL 同步允许最终一致，
    # 前提是 be-05 的后置兜底一定在 —— 过宽的 ACL 只会暂时漏召（不越权），
    # 过窄的 ACL 造成的越权窗口由 be-05 用 MySQL 权威权限复核关掉。
    acl = derive_acl(new_rows)
    try:
        await milvus_client.refresh_document_acl(document_id, acl)
    except Exception:  # noqa: BLE001 —— 见上
        logger.error(
            "ACL 副本刷新失败，等待 be-05 后置兜底兜住：document_id=%s", document_id,
            exc_info=True,
        )


# ═══════════════════════════════════════════════════════════════
# 台账读
# ═══════════════════════════════════════════════════════════════


async def _permission_summaries(
    db: AsyncSession, document_ids: list[str]
) -> dict[str, PermissionSummary]:
    """批量取权限摘要 —— 一条 GROUP BY 查完，**不要**在循环里逐条查。"""
    if not document_ids:
        return {}

    stmt = (
        select(
            KbDocumentPermission.document_id,
            KbDocumentPermission.entity_type,
            func.count().label("cnt"),
        )
        .where(KbDocumentPermission.document_id.in_(document_ids))
        .group_by(KbDocumentPermission.document_id, KbDocumentPermission.entity_type)
    )
    buckets: dict[str, dict[str, int]] = {}
    for doc_id, entity_type, cnt in (await db.execute(stmt)).all():
        buckets.setdefault(doc_id, {})[entity_type] = int(cnt)

    out: dict[str, PermissionSummary] = {}
    for doc_id in document_ids:
        counts = buckets.get(doc_id, {})
        out[doc_id] = PermissionSummary(
            global_public=counts.get(KbDocumentPermission.ENTITY_GLOBAL, 0) > 0,
            department_count=counts.get(KbDocumentPermission.ENTITY_DEPARTMENT, 0),
            role_count=counts.get(KbDocumentPermission.ENTITY_ROLE, 0),
            user_count=counts.get(KbDocumentPermission.ENTITY_USER, 0),
        )
    return out


def _to_list_item(doc: KbDocument, summary: PermissionSummary) -> DocumentListItem:
    """文档行 + 权限摘要 → 台账列表项。

    ⚠️ `permission_summary` 传的是 `summary.text`（**字符串**），不是 `summary` 本身 ——
       前端把它当字符串渲染，传对象会显示成 `[object Object]`。
       `permission_scopes` 同理：前端 `PermissionTag` 靠它渲染，缺了会抛异常。
    """
    return DocumentListItem(
        id=doc.id,
        title=doc.title,
        file_name=doc.file_name,
        file_type=doc.file_type,
        category=doc.category,
        status=doc.status,  # type: ignore[arg-type]
        chunk_count=doc.chunk_count,
        permission_scopes=summary.scopes,
        permission_summary=summary.text,
        uploaded_by=doc.uploaded_by,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


async def list_documents(
    db: AsyncSession, query: DocumentListQuery
) -> tuple[list[DocumentListItem], int]:
    """台账分页列表。

    ⚠️ 本接口按 PRD §3.3 **不受数据权限约束**，返回全量（调用方已校验
       `kb:document:list`）。因此**绝不能**把 `kb:document:list` 授予 `staff` ——
       那等于让全公司看到所有文档标题（含受限内容），违反安全红线 4。
       详见字典 §5.6.1。
    """
    conditions = [KbDocument.deleted_at.is_(None)]
    if query.keyword:
        # autoescape：关键词里的 `%` `_` 是**字面量**而非通配符。
        # 不转义的话搜 `_` 会命中全部文档 —— 与 be-02 踩过的
        # 「id 里的下划线是 LIKE 通配符」是同一个坑，只是入口在用户输入侧。
        conditions.append(
            or_(
                KbDocument.title.contains(query.keyword, autoescape=True),
                KbDocument.file_name.contains(query.keyword, autoescape=True),
            )
        )
    if query.category:
        conditions.append(KbDocument.category == query.category)
    if query.status:
        conditions.append(KbDocument.status == query.status)

    total = int(
        await db.scalar(
            select(func.count()).select_from(KbDocument).where(*conditions)
        )
        or 0
    )

    stmt = (
        select(KbDocument)
        .where(*conditions)
        .order_by(KbDocument.updated_at.desc(), KbDocument.id.desc())
        .offset((query.page - 1) * query.page_size)
        .limit(query.page_size)
    )
    docs = list((await db.scalars(stmt)).all())
    summaries = await _permission_summaries(db, [d.id for d in docs])
    items = [
        _to_list_item(d, summaries.get(d.id) or PermissionSummary(
            global_public=False, department_count=0, role_count=0, user_count=0
        ))
        for d in docs
    ]
    return items, total


async def get_document(
    db: AsyncSession, document_id: str, operator: AuthContext
) -> DocumentDetail:
    """文档详情（含正文）。

    🔴 这是**读正文**的路径，必须留审计（be-06 §4.3 / §5.9）。
    """
    doc = await _load_document(db, document_id)
    summaries = await _permission_summaries(db, [doc.id])
    item = _to_list_item(
        doc,
        summaries.get(doc.id)
        or PermissionSummary(
            global_public=False, department_count=0, role_count=0, user_count=0
        ),
    )
    await _record_read(doc.id, operator)
    return DocumentDetail(**item.model_dump(), source_markdown=doc.source_markdown)


# ═══════════════════════════════════════════════════════════════
# 写：状态 / 编辑 / 删除
# ═══════════════════════════════════════════════════════════════


async def set_document_status(
    db: AsyncSession, document_id: str, status: DocumentStatus, operator: AuthContext
) -> None:
    """启用 / 停用。

    非法流转（回到 `draft`）在**类型层**已被 `DocumentStatusPayload` 挡掉，
    走不到这里 —— 所以本函数不需要再判一次。
    """
    doc = await _load_document(db, document_id)
    if doc.status == status:
        return  # 幂等：重复点同一个按钮不该报错
    # 旧状态要在赋值**之前**取。审计里「从哪来、到哪去」两个值缺一不可：
    # 只记新状态的话，「谁把这份文档停用了」永远查不出来（停用与启用写的是同一个新值）。
    old_status = doc.status
    doc.status = status
    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.DOCUMENT_STATUS_UPDATE,
        target_type=audit_service.AuditTargetType.DOCUMENT,
        target_id=document_id,
        detail={"old_status": old_status, "new_status": status},
    )
    await db.commit()
    logger.info("文档状态变更：%s -> %s（操作人 %s）", document_id, status, operator.user_id)


async def update_document(
    db: AsyncSession,
    document_id: str,
    payload: DocumentUpdatePayload,
    operator: AuthContext,
) -> None:
    """编辑标题 / 分类 / 正文。

    传了 `source_markdown` 才触发切片与向量重建 —— 那是本模块最重的操作。
    """
    doc = await _load_document(db, document_id)
    fields = payload.model_fields_set
    applied: set[str] = set()

    if "title" in fields:
        if not payload.title or not payload.title.strip():
            raise ParamError("标题不能为空")
        doc.title = payload.title.strip()
        applied.add("title")
    if "category" in fields:
        doc.category = payload.category
        applied.add("category")

    rebuild = "source_markdown" in fields and payload.source_markdown is not None
    if rebuild:
        # 已有任务在跑时不允许改正文：两个 worker 会同时往同一文档写切片，
        # 而切片是全量重建（先删后插）—— 后完成的那个会把先完成的成果抹掉，
        # 且最终留下的可能是旧正文切出来的片。
        if await _has_active_task(db, document_id):
            raise ConflictError("该文档有导入任务正在进行，请稍后再试")
        doc.source_markdown = payload.source_markdown

    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.DOCUMENT_UPDATE,
        target_type=audit_service.AuditTargetType.DOCUMENT,
        target_id=document_id,
        detail={"changed_fields": sorted(applied), "title": doc.title},
    )
    if rebuild:
        # 正文重建单独一条动作，不合并进 `document.update`：它的代价（重切片 +
        # 全量重向量化）与「改个标题」差两个数量级，运维排查时要能单独筛出来。
        # 不传 detail：`chunk_count` 此刻**真的不知道** —— 切片要等 worker 跑完。
        # 白名单里有这个键是为了给 worker 侧的 `import.rebuild_document_index`
        # 回调留位；在这里硬塞一个 `None` 只会让审计页显示「切片数：—」，
        # 与「这条记录本来就没测过切片数」在视觉上无法区分。
        audit_service.record(
            db,
            operator,
            audit_service.AuditAction.DOCUMENT_REINDEX,
            target_type=audit_service.AuditTargetType.DOCUMENT,
            target_id=document_id,
        )

    await db.commit()

    if rebuild:
        # 重建走独立任务：整份文档重新切片 + 重新向量化，耗时与首次导入同级
        _dispatch("import.rebuild_document_index", document_id)
    logger.info("文档已编辑：%s（操作人 %s，重建=%s）", document_id, operator.user_id, rebuild)


async def delete_document(
    db: AsyncSession, document_id: str, operator: AuthContext
) -> None:
    """软删除文档，并清理 MySQL / Milvus 的切片（PRD §6.5）。

    软删除的是 `kb_document`（留档可追溯），**物理删除**的是 `kb_chunk` ——
    切片的唯一价值就是被检索，留着只会让「已删除的文档仍被检索到」。
    """
    doc = await _load_document(db, document_id)
    if await _has_active_task(db, document_id):
        # 删除时 worker 可能正在写切片，写完就成了「已删除文档的孤儿切片」，
        # 而它们**仍然是可被检索的**（Milvus 侧刚被删掉，随后又被写回）。
        raise ConflictError("该文档有导入任务正在进行，无法删除")

    # 标题在软删之后仍读得到（`deleted_at` 只标记），但审计行要能被**人**看懂，
    # 不能只留一个 `doc_xxxx` —— 而文档一旦被删，列表页就再也查不到它叫什么了。
    title = doc.title
    doc.deleted_at = _now()
    await db.execute(delete(KbChunk).where(KbChunk.document_id == document_id))
    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.DOCUMENT_DELETE,
        target_type=audit_service.AuditTargetType.DOCUMENT,
        target_id=document_id,
        detail={"title": title},
    )
    await db.commit()

    # 提交后清 Milvus。失败只告警：重试载体尚未定义（字典 §9 #13），
    # 当前靠运维手工确认 —— 但 be-05 的后置兜底会用 MySQL 复核 document_id，
    # 而 `kb_document` 已软删，那次复核会把残留的片判定为无权 → 剔除。
    try:
        await milvus_client.delete_by_document(document_id)
    except Exception:  # noqa: BLE001
        logger.error(
            "Milvus 切片清理失败，需人工确认（字典 §9 #13）：document_id=%s",
            document_id,
            exc_info=True,
        )
    logger.info("文档已删除：%s（操作人 %s）", document_id, operator.user_id)


# ═══════════════════════════════════════════════════════════════
# 上传受理
# ═══════════════════════════════════════════════════════════════


def _file_ext(file_name: str) -> str:
    return file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""


def _assert_upload_ok(file_name: str, size: int) -> str:
    """校验扩展名与单文件大小，返回扩展名。"""
    from backend.clients import mineru_client  # noqa: PLC0415 —— 避免模块级循环

    ext = _file_ext(file_name)
    if not ext or not mineru_client.is_supported(ext):
        raise ParamError(f"不支持的文件类型：{file_name}")
    if size <= 0:
        raise ParamError(f"文件为空：{file_name}")
    if size > settings.upload_max_file_mb * 1024 * 1024:
        raise ParamError(f"文件超过 {settings.upload_max_file_mb} MB 上限：{file_name}")
    return ext


async def _submit_one(file_name: str, content: bytes) -> str | None:
    """把一个文件交给 MinerU，返回 job_id；`md`/`txt` 不需要 MinerU，返回 None。

    🔴 提交放在**受理阶段**（同步）而不是 worker 里，理由见
       `mineru_client.submit_document` —— 原始文件没有任何持久化位置，
       worker 拿不到字节。
    """
    from backend.clients import mineru_client  # noqa: PLC0415

    if not mineru_client.needs_mineru(_file_ext(file_name)):
        return None
    return await mineru_client.submit_document(file_name, content)


def _decode_plain(content: bytes, file_name: str) -> str:
    """`md` / `txt` 直接解码为 markdown 正文。"""
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ParamError(f"无法识别文件编码（请转成 UTF-8）：{file_name}")


async def upload_documents(
    db: AsyncSession,
    files: list[tuple[str, bytes]],
    category: str | None,
    operator: AuthContext,
) -> UploadAccepted:
    """受理一批上传（单个即 len==1）。**单篇与批量走同一条实现**。

    顺序（spec §5.1）：

    1. 逐文件校验扩展名 / 大小 / 数量；
    2. **先提交 MinerU**（同步）—— 失败就整批拒绝，此时还没有任何落库，
       用户拿到的是一个干净的 HTTP 错误，而不是一条必然失败的任务行；
    3. 一个事务内写 `kb_document` + `kb_import_task`（**同事务**，字典 §9 #93）；
    4. 提交**之后**才派发 Celery。

    🔴 红线 1：`operator` 只能是 `Depends(get_current_user)` 的产物。
       现有壳子里的 `uploaded_by: str = Form("")` 必须删除 ——
       那是客户端传参，攻击者可以填成别人。
    """
    if not files:
        raise ParamError("未选择文件")
    if len(files) > settings.upload_batch_max_files:
        raise ParamError(f"单次最多上传 {settings.upload_batch_max_files} 个文件")

    batch_id = new_id(_BATCH_PREFIX) if len(files) > 1 else None

    # —— 第 1、2 步：先全部校验并提交，任何一步失败都整批拒绝（#97）——
    prepared: list[tuple[str, str, int, str | None, str | None]] = []
    for file_name, content in files:
        ext = _assert_upload_ok(file_name, len(content))
        job_id = await _submit_one(file_name, content)
        markdown = None if job_id else _decode_plain(content, file_name)
        prepared.append((file_name, ext, len(content), job_id, markdown))

    # —— 第 3 步：一个事务写两组行 ——
    documents: list[KbDocument] = []
    tasks: list[KbImportTask] = []
    for file_name, ext, size, job_id, markdown in prepared:
        document_id = new_id(_DOC_PREFIX)
        task_id = new_id(_TASK_PREFIX)
        documents.append(
            KbDocument(
                id=document_id,
                # 标题先取文件名（去扩展名），管理员可在台账里改
                title=file_name.rsplit(".", 1)[0][:255] or file_name[:255],
                file_name=file_name,
                file_type=ext,
                category=category,
                status=KbDocument.STATUS_DRAFT,
                source_markdown=markdown,
                chunk_count=0,
                uploaded_by=operator.user_id,
            )
        )
        tasks.append(
            KbImportTask(
                id=task_id,
                batch_id=batch_id,
                document_id=document_id,
                file_name=file_name,
                file_size=size,
                type=KbImportTask.TYPE_UPLOAD,
                status=KbImportTask.STATUS_PENDING,
                progress=0,
                mineru_task_id=job_id,
                uploaded_by=operator.user_id,
            )
        )

    db.add_all(documents)
    db.add_all(tasks)
    # 一批多篇时记**一条** `document.import`（带 `batch_id` + `file_count`），
    # 不逐篇记：上传 20 个文件会产生 20 条同秒同批的记录，把审计页刷满，
    # 而这 20 条的信息量等于 1 条。
    #
    # ⚠️ 单篇才有 `target_id` / `title` / `file_name`。多篇时**不塞 None** ——
    #    白名单里留着这几个键是为了单篇路径，多篇路径填 None 会让审计页显示
    #    「标题：—」，与「这条记录本来就没有标题这个概念」无法区分。
    single = len(documents) == 1
    detail: dict[str, Any] = {
        "batch_id": batch_id,
        "file_count": len(documents),
        "category": category,
    }
    if single:
        detail["title"] = documents[0].title
        detail["file_name"] = documents[0].file_name
    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.DOCUMENT_IMPORT,
        target_type=audit_service.AuditTargetType.DOCUMENT,
        target_id=documents[0].id if single else None,
        detail=detail,
    )
    await db.commit()

    # —— 第 4 步：提交之后派发 ——
    for task in tasks:
        _dispatch("import.parse_and_index", task.id)

    return UploadAccepted(
        task_id=tasks[0].id if len(tasks) == 1 else None,
        document_id=documents[0].id if len(documents) == 1 else None,
        batch_id=batch_id,
    )


async def upload_document(
    db: AsyncSession,
    content: bytes,
    file_name: str,
    category: str | None,
    operator: AuthContext,
) -> UploadAccepted:
    """单篇上传。门面函数，转发给 `upload_documents`（同一实现）。"""
    return await upload_documents(db, [(file_name, content)], category, operator)


# ═══════════════════════════════════════════════════════════════
# 任务读与重试
# ═══════════════════════════════════════════════════════════════


def _to_task_dto(task: KbImportTask) -> ImportTaskListItem:
    """任务行 → DTO。

    返回的是 `ImportTaskListItem`（`ImportTaskDetail` 的子类）——
    两者字段完全相同（spec §4.2「暂不分化」），详情接口返回它同样成立。

    🔴 **逐字段手抄 ORM 列，是本文件最容易出错的地方**（漏一个是必然，不是意外）：
    这里原先漏了 `created_at` 与 `batch_id`。后果不是「少显示一个时间」——
    前端 `store.sortedTasks` 写的是 `b.created_at.localeCompare(...)`，
    拿到 `undefined` 直接抛 `TypeError`，**整个进度列表一张卡片都渲染不出来**，
    而 upload 本身完全成功（文档进了库、Celery 也在跑）。
    用户的观感是「上传了但什么都没出现」，与「上传失败」无从区分。
    → `verify_import.py` 有一条**前端类型 ↔ 本 DTO 逐字段对账**的静态用例守它。
    """
    return ImportTaskListItem(
        id=task.id,
        document_id=task.document_id,
        batch_id=task.batch_id,
        file_name=task.file_name,
        file_size=task.file_size,
        type=task.type,  # type: ignore[arg-type]
        status=task.status,  # type: ignore[arg-type]
        progress=task.progress,
        total_chunks=task.total_chunks,
        error_msg=task.error_msg,
        created_at=task.created_at,
        finished_at=task.finished_at,
    )


async def get_task(db: AsyncSession, task_id: str) -> ImportTaskDetail:
    """任务详情。前端按它轮询进度。"""
    return _to_task_dto(await _load_task(db, task_id))


async def list_tasks(
    db: AsyncSession, query: ImportTaskListQuery
) -> tuple[list[ImportTaskListItem], int]:
    """任务列表。批量导入后前端用 `batch_id` 拉全批进度（#100）。"""
    conditions = []
    if query.batch_id:
        conditions.append(KbImportTask.batch_id == query.batch_id)
    if query.status:
        conditions.append(KbImportTask.status == query.status)

    total = int(
        await db.scalar(
            select(func.count()).select_from(KbImportTask).where(*conditions)
        )
        or 0
    )
    stmt = (
        select(KbImportTask)
        .where(*conditions)
        .order_by(KbImportTask.created_at.desc(), KbImportTask.id.desc())
        .offset((query.page - 1) * query.page_size)
        .limit(query.page_size)
    )
    rows = list((await db.scalars(stmt)).all())
    return [_to_task_dto(t) for t in rows], total


async def retry_import_task(db: AsyncSession, task_id: str, operator: AuthContext) -> None:
    """人工重试一个失败的任务（spec §5.3b）。"""
    task = await _load_task(db, task_id)
    if task.status != KbImportTask.STATUS_FAILED:
        raise ConflictError("只有失败的任务才能重试")

    # 🔴 必须有**可重放输入**，否则重试必然再失败一次，白烧三次配额。
    #    两条可重放的路径：① 已有 source_markdown（解析成功过）；
    #    ② 已有 MinerU job_id（提交过但没轮询完）—— 后者是本次实现新增的，
    #    它把 #96 的残差从「所有解析阶段失败」缩小到「MinerU 提交失败」，
    #    而提交失败发生在同步受理阶段，压根不会留下失败任务。
    if task.document_id:
        doc = await db.get(KbDocument, task.document_id)
        replayable = bool(doc and doc.source_markdown) or bool(task.mineru_task_id)
    else:
        replayable = bool(task.mineru_task_id)
    if not replayable:
        raise ConflictError("该任务在解析阶段失败且无可用输入，请重新上传")

    # ⚠️ 不先删 kb_chunk：Milvus 侧靠**同 id upsert** 幂等（spec §5.5）。
    #    先删会在第二次失败时把第一次的成果也丢掉。
    # 「第几次人工重试」得从审计流水自己数 —— `kb_import_task` 没有这个列
    # （worker 侧的退避计数是 Celery 内存里的 `self.request.retries`，重启即归零，
    # 而且它与「人工重试」根本不是一回事）。必须**先数后记**，顺序反了会把本次算进去。
    retry_count = await audit_service.count_action(
        db,
        audit_service.AuditAction.IMPORT_TASK_RETRY,
        target_type=audit_service.AuditTargetType.IMPORT_TASK,
        target_id=task_id,
    )

    task.status = KbImportTask.STATUS_PENDING
    task.error_msg = None
    task.progress = 0
    task.finished_at = None
    audit_service.record(
        db,
        operator,
        audit_service.AuditAction.IMPORT_TASK_RETRY,
        target_type=audit_service.AuditTargetType.IMPORT_TASK,
        target_id=task_id,
        detail={"batch_id": task.batch_id, "retry_count": retry_count},
    )
    await db.commit()

    _dispatch("import.parse_and_index", task.id)
    logger.info("任务已重试：%s（操作人 %s）", task_id, operator.user_id)


# ═══════════════════════════════════════════════════════════════
# 跨模块门面（be-05 / be-09）
# ═══════════════════════════════════════════════════════════════


async def create_supplement_task(
    db: AsyncSession, gap_id: str, category: str | None, operator: AuthContext
) -> ImportTaskRef:
    """be-09「缺口一键转建」：为一条知识缺口建一个待上传的导入任务。

    本函数建的任务：`type='supplement'`、`status='pending'`、
    **`document_id` 为 `NULL`**（此时还没有文档）、`file_name`/`file_size` 为空
    —— 见字典 §9 #64。

    ⚠️ **本函数仍不校验 `gap_id` 的存在性**：知识缺口表归 be-09，
       本模块不 import 它的模型（反向依赖会让两个模块互相锁死）。
       **校验由 be-09 自己在调用前做**（`gap.convert_gap_to_task` 先 `db.get` 再进来），
       这正是 §3.6「同一个语义只准有一份实现」的落点：存在性判定只有一处。
       ✅ 2026-10-14 起 `gap_id` **会落库**（迁移 `0007` 加的列，字典 §9 #64 已定）——
       此前它被当成不落库的入参丢弃，于是「这条任务补的是哪个缺口」无从回查。

    ⚠️ 两次写（本函数建任务 + be-09 置缺口 `tasked`）**不在同一事务** ——
       本函数自己 `commit`。建任务成功、置 `tasked` 失败会留下一条
       `gap_id` 指向仍为 `open` 的缺口。
       ✅ **be-09 侧的收敛策略**：它用 `gap_id + ACTIVE_STATUSES` 查「是否已有在跑的
       补充任务」，重试时**复用**已有任务而不是再建一条（见 `gap._find_open_task`）。
       所以这条非原子性最坏的结果是「一条孤儿任务」，而不是「一条缺口补两次料」。

    🔵 **本函数刻意不留审计痕**（`P2-12` §5.4 的调用点表里没有它）：
       这个动作的业务含义是「为某条知识缺口建了一个补充任务」，
       它的审计动作是 `gap.task.create`，**归 be-09 记**（`target_type='gap'`）。
       在这里再记一条 `document.import` 是错的 —— 此刻**还没有任何文档**，
       `target_id` 无从落；而且同一次操作出现两条动作会让
       「按动作统计缺口闭环率」重复计数。be-09 落地时补上它那一侧。
    """
    task = KbImportTask(
        id=new_id(_TASK_PREFIX),
        batch_id=None,
        document_id=None,
        # ✅ 2026-10-14 起落库（迁移 0007）。见 docstring 与字典 §9 #64。
        gap_id=gap_id,
        file_name=None,
        file_size=0,
        type=KbImportTask.TYPE_SUPPLEMENT,
        status=KbImportTask.STATUS_PENDING,
        progress=0,
        uploaded_by=operator.user_id,
    )
    db.add(task)
    await db.commit()
    # ⚠️ 刻意**不在这里派发** `parse_and_index`：还没有文件，
    #    worker 跑起来必然失败。等用户真正上传了文件，由上传路径派发。
    logger.info("已创建缺口补充任务：%s（category=%s）", task.id, category)
    return ImportTaskRef(task_id=task.id)


async def list_visible_document_ids(db: AsyncSession) -> set[str]:
    """`enabled` 且未软删的文档 ID 集合（be-05 §8 的契约）。

    be-05 的后置兜底用它复核 Milvus 命中结果的 `document_id`：
    不在这个集合里的候选一律剔除 —— 这是防越权的**硬保证**，
    不依赖 Milvus 的标量过滤是否算对。
    """
    stmt = select(KbDocument.id).where(
        KbDocument.deleted_at.is_(None),
        KbDocument.status == KbDocument.SEARCHABLE_STATUS,
    )
    return set((await db.scalars(stmt)).all())
