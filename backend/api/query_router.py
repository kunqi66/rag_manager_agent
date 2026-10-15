"""检索问答路由（be-05）。7 个端点，全部只要求**登录**。

**关于权限码**：本模块**不挂任何功能权限码**。PRD §8.3 把这 7 条列在登录态下，
「能问到什么」由**数据权限**（四维 ACL，在引擎里落）决定，不由功能权限决定。
这不违反安全红线 7 —— 那条管的是**写接口**，本模块不对外暴露任何写端点
（落库是问答的副作用，不是端点）。

🔴 **安全红线 1（PRD §11.3 R3）已完成收尾**：原先这里的 `class UserContext`
   与 `SearchRequest.user_context` / `AskRequest.user_context` 是**客户端传参**，
   攻击者把 `department_ids` 填成任意值即可扩权。它们已**整体删除**，
   身份一律来自 `Depends(get_current_user)` 解析的 JWT。
   这是「删 `user_context`」这一步的实际落点 —— 不是「覆盖」，是删除。

🔴 **分层铁律**：本文件只做「声明端点 → 挂登录依赖 → 转发引擎 → 序列化」。
   不写 SQL、不做权限判断、不拼 Prompt。SSE 的**线格式**拼装留在本层，
   因为 `engines/` 不允许 import FastAPI（引擎只产出 `SseEvent` 对象）。

⚠️ **为什么不用 `sse-starlette`**：它确实更省事（自带心跳、断连处理），
   但落地时环境装不上（字典 §9 #104 记的备选路径）。本实现手写帧，
   需要自己保证三件事，缺任何一件都会表现为「流式不生效」：
   ① `media_type="text/event-stream"`；
   ② `X-Accel-Buffering: no` —— **不设它的话 Nginx 会把整个流缓冲到结束才吐**，
      前端看到的是「转很久，然后一次性全出来」；
   ③ 每帧以空行结尾（`\\n\\n`），否则客户端的事件解析器不会派发。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.deps import get_current_user, get_db
from backend.core.response import ok, page_result
from backend.engines import search_engine
from backend.schemas.auth import AuthContext
from backend.schemas.query import AskRequest, SearchRequest, SseEvent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/query", tags=["检索问答"])


def _sse_frame(event: SseEvent) -> str:
    """一条 SSE 帧。

    ⚠️ `ensure_ascii=False`：中文回答若不关掉它，会以 `\\uXXXX` 形式传输 ——
       前端解析后仍能显示，但抓包与日志里全是转义码，排查体验极差。
       响应是 UTF-8 编码的，直接发原字符是安全的。

    ⚠️ 帧尾必须是**两个**换行：SSE 协议以空行作为事件分隔符，
       少一个换行会让相邻两帧被合并成一条（症状是「事件名变成 JSON 的一部分」）。
    """
    return f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


async def _event_source(events: AsyncIterator[SseEvent]) -> AsyncIterator[str]:
    """把引擎的事件流转成 SSE 字节流。"""
    async for event in events:
        yield _sse_frame(event)


# ═══════════════════════════════════════════════════════════════
# 检索与问答
# ═══════════════════════════════════════════════════════════════


@router.post("/search", summary="权限感知检索", response_model=None)
async def search(
    payload: SearchRequest,
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """只检索不生成。不落会话、不落问答日志（见引擎 `search` 的注释）。"""
    data = await search_engine.search(
        db, query=payload.query, top_k=payload.top_k, ctx=ctx
    )
    return ok(data)


@router.post("/ask", summary="鉴权问答（非流式）", response_model=None)
async def ask(
    payload: AskRequest,
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """非流式问答。**仅**供浏览器不支持流时的降级路径使用。"""
    data = await search_engine.ask(
        db,
        query=payload.query,
        session_id=payload.session_id,
        top_k=payload.top_k,
        ctx=ctx,
    )
    return ok(data)


@router.post("/ask/stream", summary="鉴权问答（SSE 流式）", response_model=None)
async def ask_stream(
    payload: AskRequest,
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """流式问答。事件序列：`start → delta* → citations → blocked? → done`。

    ⚠️ **不套统一响应结构** `{code, message, data}` —— 它是流，没有单一的响应体。
       失败通过 `error` 事件下发（`engines` 里已保证），
       因此前端必须处理 `error` 事件而不是只看 HTTP 状态码。
       这是既定契约（字典 §7 / PRD §6.2.5），不是对规范的违反。

    ⚠️ `db` 会话的生命周期：FastAPI 的 `Depends(yield)` 清理发生在
       **响应发送完毕之后** —— 对 `StreamingResponse` 就是流结束之后。
       所以引擎在流的末尾（以及客户端断开时）仍能正常落库。
    """
    events = search_engine.ask_stream(
        db,
        query=payload.query,
        session_id=payload.session_id,
        top_k=payload.top_k,
        ctx=ctx,
    )
    return StreamingResponse(
        _event_source(events),
        media_type="text/event-stream",
        headers={
            # 🔴 见模块头部 ⚠️②：不设这一条，Nginx 会把整个流缓冲住
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ═══════════════════════════════════════════════════════════════
# 会话
# ═══════════════════════════════════════════════════════════════


@router.get("/sessions", summary="会话列表", response_model=None)
async def list_sessions(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """当前用户的会话列表，按最近更新倒序。"""
    items, total = await search_engine.list_sessions(db, ctx, page, page_size)
    return ok(
        page_result(
            [
                {
                    "id": item.id,
                    "title": item.title or "",
                    "message_count": item.message_count,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                }
                for item in items
            ],
            total,
            page,
            page_size,
        )
    )


@router.get("/sessions/{session_id}", summary="会话详情", response_model=None)
async def session_detail(
    session_id: str,
    limit: int = Query(20, ge=1, le=100, description="最近 N 条消息"),
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """取会话的最近 N 条消息（正序返回）。

    ⚠️ `limit` 限的是「**最近**多少条」，不是「最早多少条」——
       多轮对话里给最早的 N 条等于让前端看不到刚说的话。
    """
    data = await search_engine.session_detail(db, session_id, ctx, limit)
    return ok(data)


@router.delete("/sessions/{session_id}", summary="删除会话", response_model=None)
async def delete_session(
    session_id: str,
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """**删除**会话及其消息（不是「清空上下文」）。

    🔴 **语义已在 be-05 §10-105 拍板为真删除**，原 docstring 的
       「清空指定会话的上下文记忆」作废：前端 fe-03 的侧边栏条目要能被移除，
       且确认框文案是「删除后不可恢复」。两者不一致会让用户点了删除却发现条目还在。

    ⚠️ **`qa_log` 不随之删除** —— 它是 be-07 看板与 be-08/be-09 挖掘的数据源。
       跟着删的后果是「用户删掉自己的会话，平台的提问量指标凭空下跌」。

    ⚠️ 归属校验由引擎负责，且「不存在」与「不属于你」返回**同一个** 3001
       —— 用 403 区分等于给攻击者一个可枚举的探针。
    """
    await search_engine.delete_session(db, session_id, ctx)
    return ok(None)


@router.get("/suggestions", summary="智能联想", response_model=None)
async def suggestions(
    limit: int = Query(6, ge=1, le=20, description="返回条数"),
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """联想问题。**P1 只返回当前用户自己的高频问题**（be-05 §10-23）。

    🔴 不做全局热词：那会把他人的提问文本展示给所有人（含敏感信息），
       是一条 PRD 未评估的泄漏面。全局源要等 be-07 有明确脱敏口径。
    """
    return ok(await search_engine.suggestions(db, ctx, limit))
