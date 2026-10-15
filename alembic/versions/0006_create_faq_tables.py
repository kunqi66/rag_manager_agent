"""创建 FAQ 沉淀表

建 `faq_candidate` / `faq`，字段严格对齐 `核心数据字典.md` §2.4（be-08 设计、已冻结）。

手写而非 autogenerate，理由同 0001–0005：本机连的是**云端** rag_kb
（那台机器有勒索攻击的历史记录，只允许动 rag_kb 一个库），
不适合让它对一份不完全可控的库做全库 diff。

⚠️ 两处**刻意不加外键**，理由见各自的列注释。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 表级字符集：字典 §1「全库 utf8mb4」
_CHARSET = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


def upgrade() -> None:
    op.create_table(
        "faq_candidate",
        sa.Column("id", sa.String(64), primary_key=True),
        # 簇标识 = md5(排序后的成员 qa_log.id)[:16]。**只用于排障定位**，
        # 不承担跨天去重（同一问题今天聚出簇 A、明天簇 B，键天然不同）。
        sa.Column("cluster_key", sa.String(64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("frequency", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        # ⚠️ 存的是 **document_id** 数组，不是 chunk id —— `qa_log.allowed_ids`
        #    里是 chunk id，必须反查 `kb_chunk` 后再存。存错会让「关联文档」
        #    永远查不到标题，而列表**有值**、看起来完全正常。
        sa.Column("related_doc_ids", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        #: 审核人。**只由 JWT 写入**（红线 1），请求体里不存在这个字段。
        sa.Column("reviewer_id", sa.String(64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        # 列表页固定按 `status` 过滤 + 按 `confidence` 排序，索引给前者。
        sa.Index("ix_faq_candidate_status", "status"),
        **_CHARSET,
    )

    op.create_table(
        "faq",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        # 768 × float32 **小端** = 恒 3072 字节（字典 §2.4.1）。
        # 🔴 这是跨模块二进制契约：字节序写反不会报错，只表现为
        #    「FAQ 发布了但从来命中不了」。读写口径只在
        #    `models/qa_log.pack_embedding` / `unpack_embedding` 一处。
        sa.Column("question_embedding", sa.LargeBinary(), nullable=True),
        sa.Column("cache_enabled", sa.Boolean(), nullable=False, server_default="1"),
        # ⚠️ **不加外键**：候选行被清理时不该把已发布的 FAQ 一起拖住 ——
        #    发布出去的 FAQ 是线上回答的依据，它的生命周期独立于候选台账。
        sa.Column("source_candidate_id", sa.String(64), nullable=True),
        sa.Column("published_by", sa.String(64), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default="0"),
        # 字典 §2.4 明写 `cache_enabled` 要**索引**：进程启动时的
        # `WHERE cache_enabled = true` 全量加载走它。
        sa.Index("ix_faq_cache_enabled", "cache_enabled"),
        **_CHARSET,
    )


def downgrade() -> None:
    # 先删 faq 再删候选（无外键，顺序只是与 upgrade 对称）。
    op.drop_table("faq")
    op.drop_table("faq_candidate")
