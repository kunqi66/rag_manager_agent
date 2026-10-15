"""创建审计日志表

建 `sys_audit_log`，字段与索引严格对齐 `核心数据字典.md` §2.1（8 列 + 3 索引）。

手写而非 autogenerate，理由同 0001/0002/0003：本机连的是**云端** rag_kb
（那台机器有勒索攻击的历史痕迹，只允许动 rag_kb 一个库），
不适合让它对一份不完全可控的库做全库 diff。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 表级字符集：字典 §1「全库 utf8mb4」
_CHARSET = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


def upgrade() -> None:
    op.create_table(
        "sys_audit_log",
        # 字典 §2.1：**取 VARCHAR**（原文写「VARCHAR/BIGINT」二选一，§9 #33 定为
        # 与全项目 ID 策略统一）。用 BIGINT 的话这里就是一个孤立的自增列，
        # 与 `new_id("aud")` 的生成口径对不上。
        sa.Column("id", sa.String(64), primary_key=True),
        # ⚠️ **不加外键到 sys_user**：与 `kb_document.uploaded_by` 一致 ——
        #    用户被删后审计行必须还在，否则「谁删的」在删人之后永久失去答案。
        #    系统自动动作写固定串 'system'。
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        # JSON 而非 TEXT：筛选要按语义读键（后续若加 JSON 索引也才有得加）
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        **(_CHARSET),
    )

    # 🔴 三个索引 = 三种查询形态，缺哪个哪种查询就全表扫（字典 §9 #42）：
    #   (user_id, created_at)                  → 「某人某段时间做了什么」
    #   (created_at)                           → 列表页主路径（默认近 7 天翻页）
    #   (target_type, target_id, created_at)   → 「这份文档被谁改过」
    # ⚠️ 顺序不能调换：B+ 树索引只在**最左前缀**上有效，
    #    写成 (created_at, user_id) 就服务不了「不带时间按人查」。
    op.create_index(
        "ix_sys_audit_log_user_created",
        "sys_audit_log",
        ["user_id", "created_at"],
    )
    op.create_index("ix_sys_audit_log_created_at", "sys_audit_log", ["created_at"])
    op.create_index(
        "ix_sys_audit_log_target",
        "sys_audit_log",
        ["target_type", "target_id", "created_at"],
    )


def downgrade() -> None:
    # 先删索引再删表：虽然 MySQL 删表会连带删索引，但显式写出来，
    # 让 downgrade 在「表已存在、索引被单独删过」的中间态下也能跑通。
    op.drop_index("ix_sys_audit_log_target", table_name="sys_audit_log")
    op.drop_index("ix_sys_audit_log_created_at", table_name="sys_audit_log")
    op.drop_index("ix_sys_audit_log_user_created", table_name="sys_audit_log")
    op.drop_table("sys_audit_log")
