"""用户表与用户-角色关系（字典 §2.1 `sys_user` / `sys_user_role`）。"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TABLE_KWARGS, Base


class SysUser(Base):
    """用户。

    ⚠️ `password_hash` 是 Argon2 哈希（红线 3），**任何路径都不得回传**——
    对外 DTO（如 `/api/auth/me` 的 `UserDetail`）里没有这个字段。
    """

    __tablename__ = "sys_user"
    __table_args__ = (TABLE_KWARGS,)

    # 状态枚举的唯一出处。枚举本身仍待定（字典 §9 待确认 5），
    # 但取值必须只有一处 —— 散在 service 里的字面量 "active" 拼错一次就是
    # 「所有账号突然登录不了」且不报错。
    # 注意「锁定」**不在此枚举**：那是登录防爆破状态，放 Redis（login_lock_minutes）。
    STATUS_ACTIVE = "active"
    STATUS_DISABLED = "disabled"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    real_name: Mapped[str] = mapped_column(String(64), nullable=False)
    department_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("sys_department.id"), nullable=True
    )
    email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 枚举 🟡 待定（字典 §2.1 / §9 待确认 5）；暂用 active/disabled。
    # 「锁定」不进枚举 —— 那个状态放 Redis（login_lock_minutes）。
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )


class SysUserRole(Base):
    """用户-角色关系（复合主键）。理由同 `SysRolePermission`：不级联。"""

    __tablename__ = "sys_user_role"
    __table_args__ = (TABLE_KWARGS,)

    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sys_user.id"), primary_key=True
    )
    role_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sys_role.id"), primary_key=True
    )
