"""FAQ 沉淀两张表（字典 §2.4）：`faq_candidate`（候选）与 `faq`（已发布）。

两者是**生命周期上的前后两态**，不是主从：候选发布后写出一行 `faq`，
但候选行**留着**（`status=published`）—— 运营要看「这条是谁审的、什么时候审的」，
删掉候选行这些信息就没了。

🔴 三条漏了就静默出错的约束：

1. **`faq.question_embedding` 必须写**。它是进程内索引的**唯一**数据来源，
   少了它这条 FAQ 发布了也永远命中不了 —— 而接口会正常回 200、
   `cache_enabled` 也是 `true`，表现像「阈值没调好」。
2. **`faq_candidate.related_doc_ids` 存的是 `document_id` 不是 `chunk_id`**。
   `qa_log.allowed_ids` 里存的是 chunk id（be-05 §10-19 冻结），
   直接搬进来会让「关联文档」永远查不到标题，而列表**有值**、看起来正常。
3. **`cluster_key` 不承担跨天去重**。它是 `md5(排序后的成员 id)`，
   同一个问题今天聚出簇 A、明天簇 B，键天然不同。跨天去重靠**向量比对**
   （`similarity.nearest_existing`）。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TABLE_KWARGS, Base


class FaqCandidate(Base):
    """一条待审核（或已审核）的候选 FAQ。"""

    __tablename__ = "faq_candidate"
    __table_args__ = (TABLE_KWARGS,)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: `md5("|".join(sorted(成员 qa_log.id)))[:16]`。**仅供排障定位**，见模块头部第 3 条。
    cluster_key: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 代表性问题 = 簇内距质心最近那条的原文（`similarity.representative_index`）
    question: Mapped[str] = mapped_column(Text, nullable=False)
    #: 推荐答案。优先 LLM 合并去重，**失败降级**为簇内最高相似度那条原文。
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    #: 簇内成员数。⚠️ 是「提问次数」，与 be-07 `top_documents` 的「切片引用次数」不是一个单位。
    frequency: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: `avg_sim × min(1, frequency / FAQ_CONFIDENCE_FREQ_REF)`，恒在 [0,1]
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: **document_id** 数组（不是 chunk id），见模块头部第 2 条
    related_doc_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    #: `pending` / `published` / `rejected`（字典 §5.4）
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    reviewer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class Faq(Base):
    """一条已发布 FAQ。**只能由候选发布产生**，没有手工新建路径。"""

    __tablename__ = "faq"
    __table_args__ = (TABLE_KWARGS,)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    #: 768×`<f4` 小端，恒 3072 字节（字典 §2.4.1）。见模块头部第 1 条。
    question_embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    #: **建索引**（字典 §2.4 明写）：进程启动要 `WHERE cache_enabled = true` 全量加载。
    cache_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )
    source_candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_by: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    #: 命中计数。⚠️ 每命中都写库是**已知的写放大**（字典 §9 #59），本期接受。
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
