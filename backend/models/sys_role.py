"""角色表与角色-权限关系（字典 §2.1 `sys_role` / `sys_role_permission`）。"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TABLE_KWARGS, Base


class SysRole(Base):
    """角色。权限通过 `sys_role_permission` 授予。"""

    __tablename__ = "sys_role"
    __table_args__ = (TABLE_KWARGS,)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 内置角色不可删（字典 §2.1）
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class SysRolePermission(Base):
    """角色-权限关系（复合主键）。

    不设 ondelete 级联 —— 字典 §9 待确认 36 定了「删除角色/权限前先检查引用，
    有悬挂引用则拒绝删除」，所以这里要的是默认的 RESTRICT 语义，
    由服务层显式判断并报 3002，而不是让数据库静默级联删掉授权行。
    """

    __tablename__ = "sys_role_permission"
    __table_args__ = (TABLE_KWARGS,)

    role_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sys_role.id"), primary_key=True
    )
    permission_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sys_permission.id"), primary_key=True
    )
