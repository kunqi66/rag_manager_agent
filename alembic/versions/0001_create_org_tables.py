"""创建组织与权限六张表

建 sys_department / sys_permission / sys_role / sys_user /
sys_user_role / sys_role_permission，字段严格对齐 `核心数据字典.md` §2.1。

手写而非 autogenerate —— 当前没有任何可连的 MySQL 实例，
autogenerate 需要连库做 diff，跑不了。

Revision ID: 0001
Revises:
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 表级字符集：字典 §1「全库 utf8mb4」。
# 不显式设的话中文会乱码，且只在真实数据进来后才暴露。
_CHARSET = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


def upgrade() -> None:
    # ---------- 1. 部门 ----------
    op.create_table(
        "sys_department",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        # 根节点为 NULL
        sa.Column("parent_id", sa.String(64), nullable=True),
        # 物化路径，用于一次 SQL 取「自身 + 全部祖先」
        sa.Column("path", sa.String(255), nullable=False),
        sa.Column("sort", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        **_CHARSET,
    )
    op.create_index("ix_sys_department_path", "sys_department", ["path"])

    # ---------- 2. 功能权限 ----------
    op.create_table(
        "sys_permission",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("parent_id", sa.String(64), nullable=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("path", sa.String(255), nullable=True),
        sa.Column("sort", sa.Integer(), nullable=False),
        sa.UniqueConstraint("code", name="uq_sys_permission_code"),
        **_CHARSET,
    )

    # ---------- 3. 角色 ----------
    op.create_table(
        "sys_role",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("is_builtin", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("code", name="uq_sys_role_code"),
        **_CHARSET,
    )

    # ---------- 4. 用户 ----------
    op.create_table(
        "sys_user",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        # Argon2 哈希（红线 3）。任何路径都不得回传。
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("real_name", sa.String(64), nullable=False),
        sa.Column("department_id", sa.String(64), nullable=True),
        sa.Column("email", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["sys_department.id"],
            name="fk_sys_user_department_id_sys_department",
        ),
        sa.UniqueConstraint("username", name="uq_sys_user_username"),
        **_CHARSET,
    )

    # ---------- 5. 用户-角色 ----------
    # 不设 ondelete 级联：字典 §9 待确认 36 定了「有引用则拒绝删除」，
    # 要的是默认 RESTRICT，由服务层显式报 3002，而不是数据库静默级联。
    op.create_table(
        "sys_user_role",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("role_id", sa.String(64), primary_key=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["sys_user.id"], name="fk_sys_user_role_user_id_sys_user"
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["sys_role.id"], name="fk_sys_user_role_role_id_sys_role"
        ),
        **_CHARSET,
    )

    # ---------- 6. 角色-权限 ----------
    op.create_table(
        "sys_role_permission",
        sa.Column("role_id", sa.String(64), primary_key=True),
        sa.Column("permission_id", sa.String(64), primary_key=True),
        sa.ForeignKeyConstraint(
            ["role_id"], ["sys_role.id"], name="fk_sys_role_permission_role_id_sys_role"
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["sys_permission.id"],
            name="fk_sys_role_permission_permission_id_sys_permission",
        ),
        **_CHARSET,
    )


def downgrade() -> None:
    # 反序删除，避免外键挡住
    op.drop_table("sys_role_permission")
    op.drop_table("sys_user_role")
    op.drop_table("sys_user")
    op.drop_table("sys_role")
    op.drop_table("sys_permission")
    op.drop_index("ix_sys_department_path", table_name="sys_department")
    op.drop_table("sys_department")
