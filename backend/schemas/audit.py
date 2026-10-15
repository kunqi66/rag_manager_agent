"""审计域 DTO（be-06）。

分两组，**不要混用**：

- **入参 DTO**（`AuditLogQuery`）—— service 与路由之间的查询条件。
  用模型而不是 7 个位置参数：审计列表的筛选维度和分页参数会一起长，
  长参数表在调用点全靠顺序对齐，加一个参数就可能把 `page` 和 `page_size` 调反，
  而这个错误**不会报错**，只会让某页悄悄多返回几行。
- **出参 DTO**（`AuditLogItem`）—— 契约层模型，路由直接返回。

🔴 全部 ID 字段为 `str`（字典 §1：主键一律字符串，库里是 VARCHAR）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from backend.core.settings import settings

# ═══════════════════════════════════════════════════════════════
# 查询条件
# ═══════════════════════════════════════════════════════════════


class AuditLogQuery(BaseModel):
    """审计列表的筛选与分页条件。

    全部筛选维度**互相独立、可任意组合**，`None` = 不按该维度过滤。
    刻意不提供「全字段模糊搜索」：审计查询的价值在于「精确复现某次操作」，
    模糊匹配出来的结果集无法作为证据。
    """

    user_id: str | None = Field(None, description="按操作人过滤")
    action: str | None = Field(None, description="按动作过滤（字典 §5.5 全集）")
    target_type: str | None = Field(None, description="按目标类型过滤")
    target_id: str | None = Field(None, description="按目标 ID 过滤")
    start_time: datetime | None = Field(None, description="起始时间（含）")
    end_time: datetime | None = Field(None, description="结束时间（含）")
    page: int = Field(1, ge=1, description="页码，从 1 起")
    # 上限走配置（`AUDIT_PAGE_SIZE_MAX`）而不是写字面量 100：
    # 审计列表是**无过滤即全表**的端点，上限就是它的成本阀；
    # 运维要在不改代码的前提下调它（CLAUDE.md：阈值一律进配置）。
    page_size: int = Field(
        20, ge=1, le=settings.audit_page_size_max, description="每页条数"
    )


# ═══════════════════════════════════════════════════════════════
# 出参
# ═══════════════════════════════════════════════════════════════


class AuditLogItem(BaseModel):
    """一条审计记录（列表项）。

    ⚠️ **不含 `password_hash` 之类的任何凭据字段** —— 不只是因为不该有，
    而是 `detail` 本身已在**写入时**过白名单脱敏（安全红线 6）。
    读取侧再脱敏是错的：真正的防线必须在入口，那里才拦得住数据。
    """

    id: str = Field(..., description="审计 ID")
    user_id: str = Field(..., description="操作人；系统自动动作为 'system'")
    # ⚠️ **左连接**取来，可为 `None`：用户被删后审计行必须仍在（这正是审计的意义），
    # 所以这里**不是**内连接 —— 内连接会静默丢掉「离职员工做过什么」这类
    # 最需要追溯的记录，而且丢得毫无痕迹（列表只是少了几行）。
    username: str | None = Field(None, description="操作人登录名；用户已删或系统动作为 None")
    action: str = Field(..., description="动作类型")
    target_type: str | None = Field(None, description="目标类型")
    target_id: str | None = Field(None, description="目标 ID")
    detail: dict[str, Any] | None = Field(None, description="变更摘要（已脱敏）")
    ip: str | None = Field(None, description="来源 IP")
    created_at: datetime = Field(..., description="发生时间")
