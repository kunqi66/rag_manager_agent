"""创建知识缺口表 + 给导入任务补 gap_id

建 `knowledge_gap`（字段严格对齐 `核心数据字典.md` §2.4，be-09 设计、已冻结），
并给 `kb_import_task` 加一列 `gap_id`（字典 §9 #64 已定）。

手写而非 autogenerate，理由同 0001–0006：本机连的是**云端** rag_kb
（那台机器有勒索攻击的历史记录，只允许动 rag_kb 一个库），
不适合让它对一份不完全可控的库做全库 diff。

⚠️ 两处**刻意不加外键**，理由见各自的列注释。

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 表级字符集：字典 §1「全库 utf8mb4」
_CHARSET = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


def upgrade() -> None:
    op.create_table(
        "knowledge_gap",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("question", sa.Text(), nullable=False),
        # 768 × float32 **小端** = 恒 3072 字节（字典 §2.4.1）。
        # 🔴 与 `qa_log` / `faq` 是**同一个**二进制契约：字节序写反不会报错，
        #    只表现为「缺口永远聚不出簇 / 相似度是噪声」。读写口径只在
        #    `models/qa_log.pack_embedding` / `unpack_embedding` 一处。
        sa.Column("question_embedding", sa.LargeBinary(), nullable=True),
        # 聚合键的一半（另一半是上面的向量）。**没有 user_id** —— 缺口是部门粒度
        # 的运营数据，落提问人会让治理页变成「谁问了什么」的人员监控视图。
        sa.Column("department_id", sa.String(64), nullable=True),
        sa.Column("frequency", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_score", sa.Float(), nullable=True),
        sa.Column("suggested_category", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        # 🔴 **这一列同时是聚合水位线**（be-09 的幂等靠它，字典 §9 #65）：
        #    每轮只累加 `created_at > last_seen_at` 的提问。手工改它会让
        #    下一轮重复累加或漏计，**且不报错**（频次虚高的表现只是
        #    「运营觉得这问题好热门」）。
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        # 每轮聚合都 `WHERE status IN ('open','tasked')` 取活跃缺口，走这个索引。
        sa.Index("ix_knowledge_gap_status", "status"),
        # 双键匹配的部门侧。⚠️ 查询是「同部门的活跃缺口」，两列各自索引
        # 已经够用；**不建联合索引**是刻意的 —— 本表是运营级量级（几十~几千行），
        # 联合索引的收益抵不上它带来的写入开销与一处要同步维护的定义。
        sa.Index("ix_knowledge_gap_department_id", "department_id"),
        **_CHARSET,
    )

    # 给既有表补列。⚠️ 用 `op.add_column` 而不是重建表：
    #    `kb_import_task` 上可能已有线上数据（导入失败待重试的任务）。
    # 可空 + 索引：绝大多数历史行的 `gap_id` 是 NULL（它们不是缺口转建来的）。
    op.add_column(
        "kb_import_task",
        sa.Column("gap_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_kb_import_task_gap_id", "kb_import_task", ["gap_id"])


def downgrade() -> None:
    op.drop_index("ix_kb_import_task_gap_id", table_name="kb_import_task")
    op.drop_column("kb_import_task", "gap_id")
    op.drop_table("knowledge_gap")
