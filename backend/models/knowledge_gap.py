"""知识缺口表（字典 §2.4，be-09）。

这张表存的是「**没人答得上来的提问**」的聚合台账，供运营据此补料。
它是全项目里**唯一**一张由离线任务持续累加（而不是一次性写）的业务表，
所以两条约束只在这里成立：

1. 🔴 **`last_seen_at` 同时是业务字段和聚合水位线**。be-09 的幂等靠它：
   每轮聚合只累加 `created_at > last_seen_at` 的提问。**若有人把它当成
   「最后出现时间」去手工 UPDATE 成别的值，下一轮聚合会重复累加或漏计，
   而且不报错** —— 频次虚高一个数量级的表现只是「运营觉得这个问题好热门」。
   见 be-09 模块设计 §4.2 / 字典 §9 #65。
2. 🔴 **`department_id` 是聚合键的一半**（另一半是 `question_embedding`）。
   同一问题被不同部门问 ⇒ **各成一条缺口**（字典 §9 #66 已定）。
   把它当成「提问人所在部门」的展示字段而随手填 NULL 或填错，
   会让跨部门缺口塌成一条，且**塌掉之后无法从数据里还原**。

⚠️ **没有 `user_id`**（刻意）——缺口是部门粒度的运营数据，
   落提问人会让治理页变成「谁问了什么」的人员监控视图。
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, LargeBinary, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TABLE_KWARGS, Base

#: 缺口状态（字典 §5.4 ✅）。三个值同 be-09 模块设计 §4.4 的状态机。
STATUS_OPEN = "open"
STATUS_TASKED = "tasked"
STATUS_CLOSED = "closed"

#: 「活跃」状态 = 还能被聚合命中的状态。**自动关闭与匹配都只认这两个** ——
#: 若把 `closed` 也算进匹配池，已关闭的缺口会被新提问重新激活，
#: 台账就会在 open/closed 之间反复横跳（be-09 spec §10-145 决定不回退）。
ACTIVE_STATUSES: tuple[str, ...] = (STATUS_OPEN, STATUS_TASKED)


class KnowledgeGap(Base):
    """一条聚合后的知识缺口。"""

    __tablename__ = "knowledge_gap"
    __table_args__ = (TABLE_KWARGS,)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 代表性问题 = **首个**触发这条缺口的提问原文（不做 LLM 改写：
    #: 缺口是给人看的线索，改写过的问句会让运营对不上原始诉求）。
    question: Mapped[str] = mapped_column(Text, nullable=False)
    #: 768×`<f4` 小端，恒 3072 字节（字典 §2.4.1）。转换只走 `similarity.from_blob`。
    question_embedding: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    #: 聚合键的另一半，见模块头部第 2 条。
    department_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: 累加频次。⚠️ 与 `qa_log` 行数**不是**一对一 —— 只看水位线之后的提问。
    frequency: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 历次触发里**最高**的相似度（是 max 不是 last，也不是平均）。
    max_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: DeepSeek 推荐的分类，失败留空（be-09 §4.5）。转建时人工可覆盖。
    suggested_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: `open` / `tasked` / `closed`，**建索引**：每轮聚合都要 `WHERE status IN (...)` 取活跃缺口。
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_OPEN, index=True
    )
    #: 最近一次触发时间。🔴 **同时是聚合水位线**，见模块头部第 1 条。
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
