"""会话表（字典 §2.3 `qa_session`）。

一个用户可以有多个会话；会话是**消息的容器**，本身不含权限语义 ——
「谁能看这个会话」的判据只有一条：`user_id` 是不是当前用户。

🔴 这条判据必须在**每次读/删**时校验（be-05 spec §4 关键契约点 2）。
   少了它，任何人拿到 `session_id` 就能读走他人的全部问答历史 ——
   与 R3 同性质的**横向越权**，且不报错。
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TABLE_KWARGS, Base


class QaSession(Base):
    """一个问答会话。`title` 取首问前若干字（be-05 §5.2-⑧）。"""

    __tablename__ = "qa_session"
    __table_args__ = (TABLE_KWARGS,)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 所属用户。索引是查询必需的：会话列表恒按 user_id 过滤 + 倒序。
    #: ⚠️ **不加外键**，与 `kb_document.uploaded_by` 的处理保持一致 ——
    #: 用户被删时要保留其历史问答（be-06 的审计语义也依赖它）。
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: 可空：首问落库前还没有标题。取首问前 `settings.qa_session_title_chars` 字。
    title: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 消息条数（user + assistant 都算）。与 `qa_message` 行数**同事务**维护，
    #: 因此两者永远一致 —— 见 be-05 §10-106 的拍板。
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
