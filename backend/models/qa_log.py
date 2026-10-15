"""问答日志表（字典 §2.3 `qa_log`）。

**这是 be-05 唯一「为下游而写」的表**：
- be-08（FAQ 挖掘，P3）聚类读 `question_embedding` + `question`；
- be-07（看板，P2）算提问量、命中率、缺口读 `allowed_ids` / `top_score` / `faq_hit`；
- be-09（缺口闭环，P3）判「未命中」读 `allowed_ids` 是否为空。

🔴 三条硬约束，漏掉任何一条都不会报错：

1. **`question_embedding` 必须写**。少了它，这一条永远无法参与 P3 聚类 ——
   而 P1 期间不会有任何提示，等到 P3 才发现「历史数据全是废的」。
2. **不随会话删除**。`DELETE /sessions/{id}` 只删 `qa_session` / `qa_message`，
   本表必须留着，否则用户删自己的会话会让看板指标凭空下跌。
   ⇒ 因此**读本表时不得 JOIN `qa_session`**。
3. **三列 id 口径已冻结**（be-05 §10-19 / #113）：
   `recalled_ids` / `allowed_ids` 存 **chunk id**，`blocked_ids` 存 **document id**。
   ⚠️ 字典 §2.5 的 `top_documents` 写「展开 `allowed_ids` 关联 `kb_chunk.document_id`」——
   这里的「关联」是 **JOIN**，不是直接当 document id 用。be-07 按错的读法写，
   文档热度会算错且**不报错**。
"""

import struct
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TABLE_KWARGS, Base


class QaLog(Base):
    """一次提问的现场快照。**每问一行**，流式与非流式都落。"""

    __tablename__ = "qa_log"
    __table_args__ = (
        # 字典 §2.3 标注的两个索引
        Index("ix_qa_log_user_created", "user_id", "created_at"),
        Index("ix_qa_log_created_at", "created_at"),
        TABLE_KWARGS,
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 可空 —— 与 be-09 转建的会话解耦；且会话被删后本列会成为悬空引用（这是可接受的，
    #: 正是「不 JOIN qa_session」这条约定的由来）。
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 提问时用户的**自身**部门（`AuthContext.department_id`），不是含祖先的 `depts`。
    #: 这是**归属口径**（谁问的），不是权限口径 —— 混用会让看板按部门统计时重复计数。
    department_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    #: 768 × float32 ≈ 3 KB。**必须写**（见模块头部第 1 条）。
    question_embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    #: 融合后放行候选的 **chunk** id
    recalled_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    #: 后置兜底复核通过的 **chunk** id
    allowed_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    #: 受限探测差集 ∪ 后置剔除集的 **document** id（去重）
    blocked_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    #: 最高余弦相似度（与 `kb_min_score` 同量纲）。**不是 RRF 分数** —— 见 be-05 §10-21。
    top_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    token_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    faq_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


def pack_embedding(vector: list[float] | None) -> bytes | None:
    """`list[float]` → BLOB（`<f4` 小端 float32，768×4 = 3072 字节）。

    🔴 字节序与 dtype 必须与 be-08 的读取端**完全一致**。
       用 `struct.pack` 而不是 `array.tobytes()`：后者的 dtype 依赖平台，
       在大端机上会得到相反的字节序 —— 症状是聚类结果全是噪声，不报错。
    """
    if not vector:
        return None
    return struct.pack(f"<{len(vector)}f", *vector)


def unpack_embedding(blob: bytes | None) -> list[float]:
    """BLOB → `list[float]`。be-08 读取用；本模块只写不读，但读写口径放一起避免分叉。"""
    if not blob:
        return []
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))
