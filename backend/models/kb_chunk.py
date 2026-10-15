"""切片表（字典 §2.2 `kb_chunk`，MySQL 权威侧）。

与 Milvus 的同名 collection（字典 §3）是**同一批 id 的两份表示**：
MySQL 存正文与元数据（权威），Milvus 存向量与 ACL 标量字段（检索副本）。

🔴 **双写的 id 必须完全相同**（be-04 §8.1）。这是 be-05 做「后置兜底」的前提 ——
   它拿 Milvus 命中的 `document_id` 回本表复核权限，id 对不上就复核不了。
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TABLE_KWARGS, Base


class KbChunk(Base):
    """一个切片。`chunk_index` 在同一文档内从 0 递增。"""

    __tablename__ = "kb_chunk"
    __table_args__ = (TABLE_KWARGS,)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: ⚠️ **不加外键** —— 删除文档时是「软删 `kb_document` + 物理删 `kb_chunk`」，
    #: 加 FK 只会让物理删除的顺序变得脆弱，而一致性本就由引擎显式保证。
    #: 索引是查询必需的：按 document_id 全量重建/删除/同步 ACL 都走它。
    document_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: 标题路径，如 `差旅制度 > 报销标准 > 住宿费`。由 chunking 维护的标题栈产出
    heading_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
