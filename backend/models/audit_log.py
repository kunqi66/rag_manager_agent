"""审计日志表（字典 §2.1 `sys_audit_log`）。

**只增不改不删**（除保留期清理）：审计行的价值全在「事后能还原当时发生了什么」，
任何 UPDATE 都会让这个保证失效。因此本模型**没有** `updated_at`，
业务侧也**不得**提供编辑入口。

🔴 `detail` **严禁落密码 / token / 权限快照**。写入前一律过
`service/audit_service.py` 的白名单脱敏（安全红线 3 与 6）。
本模型的 `to_dict()` 是唯一对外出口，不要在别处手拼字段字典 ——
手拼时漏掉一个字段不报错，但会让审计页少一列且无人发现。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TABLE_KWARGS, Base

#: 系统自动动作（定时任务等）的 `user_id` 固定值。字典 §2.1 定的字面量，
#: **不要**用 `None` —— 可空列会让「按操作人筛选」出现一个查不到的隐形分组。
SYSTEM_USER_ID = "system"

#: **登录失败**时若账号根本不存在，`user_id` 落这个值。
#: ⚠️ 不用 `username` 顶替 —— `user_id` 列的含义是「sys_user.id」，
#: 混进登录名会让「按操作人聚合」出现一批永远关联不到用户的幽灵行。
#: 登录名本身进 `detail.username`（该动作的白名单里有这个键）。
ANONYMOUS_USER_ID = "anonymous"


class SysAuditLog(Base):
    """一条审计记录。"""

    __tablename__ = "sys_audit_log"

    # 🔴 三个索引的列顺序即「查询形态」，不能随意调换：
    #   - (user_id, created_at)      —— 按操作人 + 时间（前缀 user_id 可单独用）
    #   - (created_at)               —— 列表页主路径，**不带 user_id** 的按时间翻页
    #   - (target_type, target_id, created_at) —— 「这份文档被谁改过」
    # 只有第一个时，看板审计页的通用流水会全表扫（字典 §9 #42 踩过）。
    __table_args__ = (
        Index("ix_sys_audit_log_user_created", "user_id", "created_at"),
        Index("ix_sys_audit_log_created_at", "created_at"),
        Index("ix_sys_audit_log_target", "target_type", "target_id", "created_at"),
        TABLE_KWARGS,
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # ⚠️ **不加外键到 sys_user**：与 `kb_document.uploaded_by` 一致 ——
    #    用户被删后审计行必须还在，否则「谁删的」这个问题在删人之后就永久失去答案。
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 动作类型，全集见字典 §5.5（28 个）。**双写点**：这里是约束，不是枚举列，
    #: 有意不建 MySQL ENUM —— 加动作时改 ENUM 要 DDL，而动作集会随模块增加。
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 变更摘要（**已脱敏**）。用 JSON 而非 TEXT：筛选要按语义读键，不是字符串匹配。
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    #: 来源 IP，只信 nginx 覆盖写入的 `X-Real-IP`（见 P2-12 §5.1.1）。
    #: 取不到时留空 —— **不要**回填 "unknown"，那会与真实 IP 混在同一个筛选值里。
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    def to_dict(self) -> dict[str, Any]:
        """审计列表项的唯一构造出口。

        `detail` 已在写入时脱敏，这里**不再二次处理** —— 出口再脱敏会产生
        「看起来脱敏了」的假象：真正的防线必须在入口，因为入口才是能拦下数据的地方。
        """
        return {
            "id": self.id,
            "user_id": self.user_id,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "detail": self.detail,
            "ip": self.ip,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
