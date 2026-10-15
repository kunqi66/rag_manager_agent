"""功能权限表（字典 §2.1 `sys_permission`）。

⚠️ `code` 的取值**只准**来自字典 §5.6 —— 该节是权限码的唯一权威来源，
且目前只冻结了 `org:*` 一族。`kb:*` / `faq:*` / `gap:*` / `stats:*` 仍是
字典 §9 的 #11/#46/#49/#61 待定项，**种子里一个都不能出现**。

为什么这件事要紧：权限码是后端 `sys_permission` 种子与前端 `v-permission`
常量之间的**隐式契约**。两边各写各的码名，后果是「按钮永远不显示 / 接口永远 403，
而且不报任何错」—— 前后端各自自洽、互不知情。
"""

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TABLE_KWARGS, Base


class SysPermission(Base):
    """功能权限（菜单 / 按钮两级）。"""

    __tablename__ = "sys_permission"
    __table_args__ = (TABLE_KWARGS,)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 树形结构的父节点，顶层为 NULL
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # menu / button（字典 §2.1）
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    # 前端路由，仅 menu 用
    path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
