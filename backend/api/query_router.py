"""鉴权检索与问答路由。

对应引擎：backend/engines/search_engine
职责：混合检索（向量召回 + 关键词）、四维数据权限过滤与未授权拦截、
      提示词编排、流式生成、引用溯源组装与多轮上下文管理。
"""

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/query", tags=["鉴权检索与问答"])


class UserContext(BaseModel):
    """会话用户上下文，供鉴权引擎执行四维数据权限判定。"""

    user_id: str = Field(..., description="用户 ID")
    department_ids: list[str] = Field(default_factory=list, description="所属部门 ID（含上级链路）")
    role_ids: list[str] = Field(default_factory=list, description="所属角色 ID")


class SearchRequest(BaseModel):
    """检索请求。"""

    query: str = Field(..., description="检索问题")
    top_k: int = Field(5, ge=1, le=50, description="召回条数")
    user_context: UserContext


class AskRequest(BaseModel):
    """AI 问答请求。"""

    query: str = Field(..., description="用户提问")
    session_id: str | None = Field(None, description="会话 ID，为空则新建会话")
    top_k: int = Field(5, ge=1, le=50, description="召回条数")
    user_context: UserContext


@router.post("/search", summary="混合检索并执行权限过滤")
async def search(payload: SearchRequest):
    """向量召回 + 关键词检索产出候选知识单元，随后执行四维鉴权过滤。

    返回召回列表、鉴权通过列表与鉴权拦截列表（拦截项只回 ID，不回正文）。
    """
    # TODO: 交给 search_engine 执行混合检索与权限隔离分流
    return {"code": 0, "message": "接口待实现", "data": None}


@router.post("/ask", summary="AI 鉴权问答（非流式）")
async def ask(payload: AskRequest):
    """基于有权访问的切片生成回答，并附带知识引用溯源元数据。

    召回内容中包含无权访问的知识单元时，回答中标注「部分参考资料因权限受限无法展示」。
    """
    # TODO: 交给 search_engine 完成检索 → 鉴权裁剪 → 提示词编排 → 生成 → 溯源组装
    return {"code": 0, "message": "接口待实现", "data": None}


@router.post("/ask/stream", summary="AI 鉴权问答（SSE 流式）")
async def ask_stream(payload: AskRequest):
    """流式返回打字机效果的回答分片，末尾推送引用溯源与权限受限提示事件。

    响应类型为 text/event-stream，事件序列：start → delta* → citations → done。
    """
    # TODO: 交给 search_engine 返回 StreamingResponse(media_type="text/event-stream")
    return {"code": 0, "message": "接口待实现", "data": None}


@router.get("/sessions/{session_id}", summary="查询多轮会话上下文")
async def get_session(session_id: str, limit: int = Query(20, ge=1, le=100)):
    """返回指定会话的历史问答轮次，用于前端渲染历史侧边栏。"""
    # TODO: 交给 search_engine 读取会话上下文
    return {"code": 0, "message": "接口待实现", "data": None}


@router.delete("/sessions/{session_id}", summary="清空多轮会话上下文")
async def clear_session(session_id: str):
    """清空指定会话的上下文记忆。"""
    # TODO: 交给 search_engine 清理会话上下文
    return {"code": 0, "message": "接口待实现", "data": None}
