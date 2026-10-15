"""检索问答 DTO（be-05）。

与 `frontend/src/types/chat.ts` **一一对应** —— 那份文件在 fe-03 已冻结，
本文件是它的后端镜像。改名即破坏契约，改字段即改前端。

🔴 **身份不在请求体里**（红线 1 / PRD §11.3 R3）。
   `query_router.py` 原有的 `class UserContext` 与 `SearchRequest.user_context` /
   `AskRequest.user_context` 已随本模块**整体删除** —— 它们是客户端传参，
   攻击者可以把 `department_ids` 填成任意值来扩权。
   身份一律由 `Depends(get_current_user)` 解析出的 `AuthContext` 提供。

🔴 **`UserContext` 是删除，不是迁移**。曾有一版设计把它搬进本文件，
   那等于把越权漏洞换个位置继续存在。这里连类定义都不留。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "AskRequest",
    "AskResponse",
    "ChatMessageVo",
    "CitationItem",
    "SearchRequest",
    "SearchResponse",
    "SessionBrief",
    "SessionDetail",
    "SseBlocked",
    "SseCitations",
    "SseDelta",
    "SseDone",
    "SseError",
    "SseEvent",
    "SseStart",
    "SuggestionItem",
]


# ═══════════════════════════════════════════════════════════════
# 请求
# ═══════════════════════════════════════════════════════════════


class SearchRequest(BaseModel):
    """`POST /api/query/search` —— 只检索不生成。

    ⚠️ **没有 `user_context`**，见模块头部红线 1。
    """

    query: str = Field(..., min_length=1, description="检索问题")
    top_k: int = Field(5, ge=1, le=50, description="召回条数")


class AskRequest(BaseModel):
    """`POST /api/query/ask` 与 `/api/query/ask/stream`。

    ⚠️ **没有 `user_context`**，见模块头部红线 1。
    """

    query: str = Field(..., min_length=1, description="用户提问")
    #: 为空 = 新会话，由后端在首问时创建 `qa_session`
    session_id: str | None = Field(None, description="会话 ID，为空则新建会话")
    top_k: int = Field(5, ge=1, le=50, description="召回条数")


# ═══════════════════════════════════════════════════════════════
# 响应元素
# ═══════════════════════════════════════════════════════════════


class CitationItem(BaseModel):
    """一条引用溯源（SSE `citations.items[]`）。

    🔴 `title` 只可能来自**后置兜底复核通过**的文档 —— 未授权的文档
       连标题都不返回（红线 4）。这条由 `acl.post_verify` 保证，不是靠本类。
    """

    document_id: str
    title: str
    chunk_id: str
    #: 标题路径，如 `差旅制度 > 报销标准 > 住宿费`
    heading_path: str
    #: 相似度分数（**余弦**，与 `kb_min_score` 同量纲），展示时保留 2 位小数
    score: float


class ChatMessageVo(BaseModel):
    """历史轮次里的一条消息（`SessionDetail.messages[]`）。

    ⚠️ **只含后端持久化的字段**。前端 `ChatMessageVo` 还有 `status` /
       `latency_ms` / `token_out` / `faq_hit` 等**会话内状态**，
       那些不入库、由前端自己补 —— 本类不得为它们加字段，
       否则前端会以为历史消息也能拿到这些值。
    """

    id: str
    session_id: str
    role: Literal["user", "assistant"]
    #: 纯文本 / Markdown。🔴 前端渲染前过 DOMPurify（红线 2），后端不返回 HTML。
    content: str
    citations: list[CitationItem] | None = None
    #: 本轮被权限挡掉的文档数。**只给数量**（红线 4）。
    blocked_count: int = 0
    created_at: datetime


class SessionBrief(BaseModel):
    """会话摘要（`GET /api/query/sessions` 的分页元素）。"""

    id: str
    #: 取首问前若干字（`settings.qa_session_title_chars`）。新会话尚未落库时为空串。
    title: str
    message_count: int
    created_at: datetime
    updated_at: datetime


class SessionDetail(BaseModel):
    """单会话详情（`GET /api/query/sessions/{id}`）。"""

    id: str
    title: str
    message_count: int
    #: 按时间**正序**（最早的在前）—— 前端直接顺序渲染，不需要再排一次。
    messages: list[ChatMessageVo]


class SuggestionItem(BaseModel):
    """`GET /api/query/suggestions` 的元素。

    P1 只返回**当前用户自己的**高频问题（be-05 §10-23）。
    ⚠️ 不做全局热词：那会把他人的提问文本暴露给所有人（含敏感词），
       是一条 PRD 未评估的泄漏面。
    """

    question: str


class SearchResponse(BaseModel):
    """`POST /api/query/search` 的响应。"""

    items: list[CitationItem]
    #: 受限文档数。**只给数量**（红线 4）。
    blocked_count: int = 0
    #: 仅当 `settings.acl_reveal_blocked_titles` 打开时才有值（默认关闭）
    blocked_titles: list[str] | None = None


class AskResponse(BaseModel):
    """`POST /api/query/ask`（非流式降级路径）的响应。

    🔴 **必须与 SSE 事件同源**：本类由 `/ask/stream` 的事件流**折叠**而来
       （be-05 spec §4 关键契约点 4）。两条路径各写一份实现迟早漂移，
       症状是「流式答对了、非流式少一条引用」。
    """

    session_id: str
    message_id: str
    #: 完整回答（等价于把多帧 delta 拼起来）
    content: str
    citations: list[CitationItem] | None = None
    blocked_count: int = 0
    blocked_titles: list[str] | None = None
    latency_ms: int = 0
    token_in: int = 0
    token_out: int = 0
    faq_hit: bool = False
    #: 是否真的调用过生成模型（字典 §9 #107 新增）。
    #: 有了它前端不必再用 `token_out == 0 && !faq_hit` 推断 —— 那在
    #: 「模型正常应答但恰好产出 0 token」时会误报「未调用模型」。
    llm_called: bool = False


# ═══════════════════════════════════════════════════════════════
# SSE 事件载荷（字典 §7，6 种全给）
# ═══════════════════════════════════════════════════════════════


class SseStart(BaseModel):
    session_id: str
    message_id: str


class SseDelta(BaseModel):
    content: str


class SseCitations(BaseModel):
    items: list[CitationItem]


class SseBlocked(BaseModel):
    count: int
    #: 默认不返回（`ACL_REVEAL_BLOCKED_TITLES=false`）。红线 4：无值即只报数量。
    titles: list[str] | None = None


class SseDone(BaseModel):
    """结束帧。

    `llm_called` 见 `AskResponse.llm_called` 的说明。
    """

    latency_ms: int
    token_in: int
    token_out: int
    faq_hit: bool
    llm_called: bool


class SseError(BaseModel):
    code: int
    message: str


#: 事件名 → 载荷类。`engines/` 产出的每条事件都是 `SseEvent`，
#: 由 `api/` 层序列化成 `event: <name>\ndata: <json>\n\n`。
#: ⚠️ 断言名与前端 `SseEventMap` 的键完全一致 —— 拼错一个字母，
#:    前端的分发器会静默忽略该帧（没有 default 分支报错）。
SseEventName = Literal["start", "delta", "citations", "blocked", "done", "error"]

_PAYLOAD_BY_NAME: dict[str, type[BaseModel]] = {
    "start": SseStart,
    "delta": SseDelta,
    "citations": SseCitations,
    "blocked": SseBlocked,
    "done": SseDone,
    "error": SseError,
}


class SseEvent(BaseModel):
    """一条待发送的 SSE 事件。

    `engines/` 只产出本对象（普通 DTO，**不 import FastAPI**），
    线格式的拼装留在 `api/` 层 —— 这是分层铁律在流式接口上的落法。
    """

    event: SseEventName
    data: dict[str, Any]

    @classmethod
    def make(cls, name: SseEventName, payload: BaseModel) -> "SseEvent":
        """由载荷对象构造，顺带校验名与载荷类匹配。

        ⚠️ 不做这层校验的话，`SseEvent(event="done", data=SseStart(...).model_dump())`
           会一路走到前端才炸，而那时错误信息只剩「字段 undefined」。
        """
        expected = _PAYLOAD_BY_NAME[name]
        if not isinstance(payload, expected):
            raise TypeError(f"事件 {name} 的载荷应为 {expected.__name__}")
        return cls(event=name, data=payload.model_dump(mode="json"))
