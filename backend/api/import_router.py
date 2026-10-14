"""知识导入路由。

对应引擎：backend/engines/import_engine
职责：多格式文档（PDF / Markdown / Word / TXT）单篇上传与批量拖拽导入，
      异步完成文档清洗、文本分块（Chunking）、语义向量化索引入库，
      以及知识单元生命周期与四维数据权限配置。
"""

from fastapi import APIRouter, File, Form, Query, UploadFile
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/import", tags=["知识导入"])


class PermissionPayload(BaseModel):
    """知识单元的四维数据权限配置，任意一维命中即放行（OR 判定）。"""

    global_public: bool = Field(False, description="是否全局公开")
    departments: list[str] = Field(default_factory=list, description="可见部门 ID 列表")
    roles: list[str] = Field(default_factory=list, description="可见角色 ID 列表")
    users: list[str] = Field(default_factory=list, description="可见用户 ID 列表")


@router.post("/documents", summary="上传单个文档")
async def upload_document(
    file: UploadFile = File(..., description="待导入的文档"),
    category: str = Form("", description="所属分类"),
    uploaded_by: str = Form("", description="上传人用户 ID"),
):
    """上传单个文档，触发解析 → 切片 → 向量化入库。

    返回任务 ID，前端据此轮询解析进度。
    """
    # TODO: 交给 import_engine 处理文件，落库知识单元并回写向量检索库
    return {"code": 0, "message": "接口待实现", "data": None}


@router.post("/documents/batch", summary="批量导入文档或文件夹")
async def upload_documents_batch(
    files: list[UploadFile] = File(..., description="批量文档，支持拖拽文件夹"),
    category: str = Form("", description="所属分类"),
    uploaded_by: str = Form("", description="上传人用户 ID"),
):
    """批量导入，逐篇异步解析切片，返回批次任务 ID。"""
    # TODO: 交给 import_engine 并发解析，按文件维度上报进度与失败原因
    return {"code": 0, "message": "接口待实现", "data": None}


@router.get("/documents", summary="知识单元台账列表")
async def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    keyword: str = Query("", description="标题关键词"),
    category: str = Query("", description="所属分类"),
    enabled: bool | None = Query(None, description="启用状态"),
):
    """分页查询知识单元台账（编号、标题、格式、分类、权限标签、更新时间、状态）。"""
    # TODO: 交给 import_engine 查询知识单元台账
    return {"code": 0, "message": "接口待实现", "data": None}


@router.get("/tasks/{task_id}", summary="查询解析与向量化进度")
async def get_import_task(task_id: str):
    """查询导入任务的解析进度、成功切片数与失败原因。"""
    # TODO: 交给 import_engine 读取任务进度
    return {"code": 0, "message": "接口待实现", "data": None}


@router.put("/documents/{document_id}/permissions", summary="配置四维数据权限")
async def update_document_permissions(document_id: str, payload: PermissionPayload):
    """为知识单元配置全局 / 部门 / 角色 / 个人四类数据权限，配置后即时生效。"""
    # TODO: 交给 import_engine 落库权限，并同步刷新鉴权引擎缓存
    return {"code": 0, "message": "接口待实现", "data": None}


@router.delete("/documents/{document_id}", summary="删除知识单元")
async def delete_document(document_id: str):
    """删除知识单元，同时清理其切片与向量索引。"""
    # TODO: 交给 import_engine 执行删除与索引清理
    return {"code": 0, "message": "接口待实现", "data": None}
