"""部门表（字典 §2.1 `sys_department`）。

`path` 是**物化路径**（形如 `/dept_01/dept_02/dept_04/`），
用来一次 SQL 取出「自身 + 全部祖先」得到 `effective_depts` —— PRD §6.1.2 明确不做递归。
因此 `path` 必须建索引。
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TABLE_KWARGS, Base


class SysDepartment(Base):
    """组织部门。权限继承的载体（D10 向下继承）。"""

    __tablename__ = "sys_department"
    __table_args__ = (
        Index("ix_sys_department_path", "path"),
        TABLE_KWARGS,
    )

    # 字典 §1「主键一律字符串」；本项目的测试造数用可读编号（dept_01…）
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 根节点为 NULL
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 枚举 🟡 待定（字典 §2.1）；暂用 active/disabled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
