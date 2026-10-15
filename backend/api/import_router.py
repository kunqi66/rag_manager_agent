"""知识导入路由（be-04）。

12 个端点：6 个沿用既有壳子、6 个按 spec §4.1 补齐（PRD §8.2 的 10 个 ＋
A3 成立所必需的 2 个，见 spec §10 #70/#71）。权限码 6 个**全部出自字典 §5.6**
（2026-10-14 冻结），本模块**不引入任何新码**。

分层铁律：本文件只做「声明端点 → 挂权限依赖 → 读文件字节 → 转发引擎 → `ok()` 包装」，
**不写 SQL、不做业务判断**。校验、事务、派发全在 `import_engine`。

🔴 安全红线 1（PRD §11.3 R3）：上传人等身份一律来自 `Depends(get_current_user)`
   解析的 JWT。壳子里原有的 `uploaded_by: str = Form("")` **已删除** ——
   那是客户端传参，攻击者可以填成别人，是实打实的越权漏洞。

🔴 安全红线 7：**每个写接口独立挂自己的权限码**。注意 `PUT .../permissions`
   挂的是 `kb:permission:update` 而**不是** `kb:document:update` ——
   能改标题的人不一定能改权限，这是 PRD §3.2 明确分开的两件事。

🔴 安全红线 4：列表与详情**只回权限的数量与维度**（`PermissionSummary`），
   不回 `entity_id` 集合。回显单个文档的四维权限（`GET .../permissions`）
   是另一回事 —— 那是管理员显式发起、且持有 `kb:document:list` 的前提下的配置回显。

⚠️ 文件字节在**本层**读（`await file.read()`）后再传给引擎：
   `engines/` 铁律禁止 import FastAPI，所以 `UploadFile` 不能跨过这条线。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.deps import get_db, require_permission
from backend.core.exceptions import ParamError
from backend.core.response import ok, page_result
from backend.engines import import_engine
from backend.schemas.auth import AuthContext
from backend.schemas.kb_import import (
    DocumentListQuery,
    DocumentStatusPayload,
    DocumentUpdatePayload,
    ImportTaskListQuery,
    PermissionPayload,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/import", tags=["知识导入"])


async def _read_files(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    """把 `UploadFile` 读成 `(文件名, 字节)` 列表。

    ⚠️ 体积上限在引擎里校验（`settings.upload_max_file_mb`），
       但那是在**读完之后** —— 恶意客户端仍可以先传 10 GB 把内存吃满。
       彻底的做法是边读边计数、超限即断（`await file.read(chunk)` 循环），
       当前依赖 Nginx 的 `client_max_body_size` 兜第一道。
       两者的分工已写进 spec §5.1，不要以为引擎里校验了就万事大吉。
    """
    out: list[tuple[str, bytes]] = []
    for file in files:
        name = file.filename or ""
        if not name:
            raise ParamError("存在没有文件名的上传项")
        out.append((name, await file.read()))
    return out


# ═══════════════════════════════════════════════════════════════
# 上传
# ═══════════════════════════════════════════════════════════════


@router.post("/documents", summary="上传单个文档", response_model=None)
async def upload_document(
    file: UploadFile = File(..., description="待导入的文档"),
    category: str = Form("", description="所属分类"),
    ctx: AuthContext = Depends(require_permission("kb:document:upload")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """上传单个文档，受理后返回任务 ID，前端据此轮询解析进度。

    受理时会**同步**把文件交给 MinerU（`md`/`txt` 不需要），
    详见 `mineru_client.submit_document` 的注释：原始文件没有持久化位置，
    worker 拿不到字节，只能在这里提交。
    """
    name, content = (await _read_files([file]))[0]
    data = await import_engine.upload_document(
        db, content, name, category or None, ctx
    )
    return ok(data)


@router.post("/documents/batch", summary="批量导入文档或文件夹", response_model=None)
async def upload_documents_batch(
    files: list[UploadFile] = File(..., description="批量文档，支持拖拽文件夹"),
    category: str = Form("", description="所属分类"),
    ctx: AuthContext = Depends(require_permission("kb:document:upload")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """批量导入，逐篇异步解析切片，返回**批次 ID**（前端按它拉全批进度）。

    采用**整批拒绝**（字典 §9 #97）：任一文件扩展名不支持或超限，
    整批都不受理、不落任何行。理由是部分成功会让前端拿到一批
    「有的成了、有的根本没建」的结果，而失败的那些没有任务行可轮询 ——
    用户看到的是「传了 10 个，只有 7 个出现在列表里」，无从补救。
    """
    items = await _read_files(files)
    data = await import_engine.upload_documents(db, items, category or None, ctx)
    return ok(data)


# ═══════════════════════════════════════════════════════════════
# 台账
# ═══════════════════════════════════════════════════════════════


@router.get("/documents", summary="知识单元台账列表", response_model=None)
async def list_documents(
    query: DocumentListQuery = Depends(),
    ctx: AuthContext = Depends(require_permission("kb:document:list")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """分页查询知识单元台账。

    🔴 本接口按 PRD §3.3 对知识管理员**不受数据权限约束**，返回全量。
       因此 `kb:document:list` **绝不授予 `staff`**（字典 §5.6.1）——
       那等于全公司都能看到所有文档标题，违反红线 4。
    """
    items, total = await import_engine.list_documents(db, query)
    return ok(page_result(items, total, query.page, query.page_size))


@router.get("/documents/{document_id}", summary="知识单元详情", response_model=None)
async def get_document(
    document_id: str,
    ctx: AuthContext = Depends(require_permission("kb:document:list")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """知识单元详情，**含正文** `source_markdown`。

    正文单独走详情而不进列表：它是 LONGTEXT，进列表会让分页接口
    一次拖出几 MB。编辑正文（`PUT /documents/{id}`）前必须先调本接口取值。

    本接口读正文，会留审计（be-06 §4.3）。
    """
    return ok(await import_engine.get_document(db, document_id, ctx))


@router.put("/documents/{document_id}", summary="编辑知识单元", response_model=None)
async def update_document(
    document_id: str,
    payload: DocumentUpdatePayload,
    ctx: AuthContext = Depends(require_permission("kb:document:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """编辑标题 / 分类 / 正文。

    ⚠️ 传了 `source_markdown` 会触发**全量重建切片与向量**（PRD §6.5，本期不做增量），
       耗时与首次导入同级 —— 前端要给出「重建中」的反馈。
    """
    await import_engine.update_document(db, document_id, payload, ctx)
    return ok(None)


@router.put("/documents/{document_id}/status", summary="启用 / 停用知识单元", response_model=None)
async def set_document_status(
    document_id: str,
    payload: DocumentStatusPayload,
    ctx: AuthContext = Depends(require_permission("kb:document:status")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """启用 / 停用。

    只有 `enabled` 的文档会被检索命中（`KbDocument.SEARCHABLE_STATUS`），
    停用是**下架**而非删除，切片与向量都保留，随时可重新启用。

    ⚠️ `DocumentStatusPayload` 刻意不含 `draft` —— 回到草稿是非法流转。
    """
    await import_engine.set_document_status(db, document_id, payload.status, ctx)
    return ok(None)


@router.delete("/documents/{document_id}", summary="删除知识单元", response_model=None)
async def delete_document(
    document_id: str,
    ctx: AuthContext = Depends(require_permission("kb:document:delete")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删除知识单元，同时清理其切片与向量索引。

    文档行**软删除**（留档可追溯），切片**物理删除**（留着只会被检索到）。
    """
    await import_engine.delete_document(db, document_id, ctx)
    return ok(None)


# ═══════════════════════════════════════════════════════════════
# 四维权限
# ═══════════════════════════════════════════════════════════════


@router.get("/documents/{document_id}/permissions", summary="读取四维数据权限", response_model=None)
async def get_document_permissions(
    document_id: str,
    ctx: AuthContext = Depends(require_permission("kb:document:list")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """回显该文档当前的四维权限配置。

    🔴 **不是可选项**：`PUT` 的语义是全量替换，没有回显的话权限弹窗
       每次打开都是空的，管理员一保存就把原权限覆盖成全空。
       验收 A3 的原话就是「4 种权限类型各配一次，**权限弹窗回显正确**」。
    """
    return ok(await import_engine.get_permissions(db, document_id))


@router.put("/documents/{document_id}/permissions", summary="配置四维数据权限", response_model=None)
async def update_document_permissions(
    document_id: str,
    payload: PermissionPayload,
    ctx: AuthContext = Depends(require_permission("kb:permission:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """覆盖配置全局 / 部门 / 角色 / 个人四类数据权限。

    - 语义是**全量替换**（OR 判定：四维任一命中即可见）；
    - 传空数组即「清空该维度」；
    - 幂等：重复提交同一份配置不报错，MySQL 侧重建权限行、Milvus 侧重刷 ACL。

    配置后即时生效：MySQL 权威权限即刻更新，Milvus 标量字段同步刷新。
    """
    await import_engine.update_permissions(db, document_id, payload, ctx)
    return ok(None)


# ═══════════════════════════════════════════════════════════════
# 导入任务
# ═══════════════════════════════════════════════════════════════


@router.get("/tasks", summary="导入任务列表", response_model=None)
async def list_import_tasks(
    query: ImportTaskListQuery = Depends(),
    ctx: AuthContext = Depends(require_permission("kb:document:list")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """导入任务分页列表。

    批量导入后前端传 `batch_id` 即可拉出**整批**每篇的进度与失败原因
    （字典 §9 #100）—— 这是「按文件维度上报进度」的落地方式。
    """
    items, total = await import_engine.list_tasks(db, query)
    return ok(page_result(items, total, query.page, query.page_size))


@router.get("/tasks/{task_id}", summary="查询解析与向量化进度", response_model=None)
async def get_import_task(
    task_id: str,
    ctx: AuthContext = Depends(require_permission("kb:document:list")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """查询导入任务的解析进度、成功切片数与失败原因。前端轮询本接口。"""
    return ok(await import_engine.get_task(db, task_id))


@router.post("/tasks/{task_id}/retry", summary="重试失败的导入任务", response_model=None)
async def retry_import_task(
    task_id: str,
    ctx: AuthContext = Depends(require_permission("kb:document:upload")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """人工重试一个失败的任务。

    只对 `failed` 状态开放（其余状态报 3002）。重试前会校验**是否存在可重放的输入**
    ——见 `lifecycle.retry_import_task`：解析阶段失败且没有留下 markdown 或
    MinerU job_id 的任务无法重放，会明确报错让用户重新上传，
    而不是让他等三次必然失败的重试。
    """
    await import_engine.retry_import_task(db, task_id, ctx)
    return ok(None)
