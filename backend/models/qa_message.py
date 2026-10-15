"""消息表（字典 §2.3 `qa_message`）。

一个会话里按时间排列的 user / assistant 消息。`citations` 只挂在 assistant 那条上。

🔴 **不得 JOIN `qa_session` 来判断可见性**（be-05 §10-105）：
   `qa_log` 是不随会话删除的业务日志（be-07/be-08/be-09 的数据源），
   而 `qa_message` 会随 `DELETE /sessions/{id}` 一起消失。
   需要「某条日志属于谁」时，用 `qa_log.user_id`，不要去 join 一个可能已被删的会话。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TABLE_KWARGS, Base


class QaMessage(Base):
    """一条消息。`role` 是 `user` / `assistant` 两种。"""

    __tablename__ = "qa_message"
    __table_args__ = (
        # 读会话历史恒是「按 session_id 取、按时间排序」，故用复合索引。
        # 只索引 session_id 时，MySQL 还要对这一批做 filesort。
        Index("ix_qa_message_session_created", "session_id", "created_at"),
        TABLE_KWARGS,
    )

    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 纯文本 / Markdown。🔴 **绝不存 HTML** —— 前端渲染前过 DOMPurify（红线 2）。
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: 引用溯源列表（`Citation` 的 dict 形态），只有 assistant 消息有值。
    citations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    #: 本轮被权限挡掉的文档数。**只存数量，不存标题**（红线 4）。
    blocked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
