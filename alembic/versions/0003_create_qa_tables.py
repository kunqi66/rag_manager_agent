"""创建问答三张表

建 qa_session / qa_message / qa_log，字段严格对齐 `核心数据字典.md` §2.3。

手写而非 autogenerate，理由同 0001/0002：本机连的是**云端** rag_kb
（那台机器有勒索攻击的历史痕迹，只允许动 rag_kb 一个库），
不适合让它对一份不完全可控的库做全库 diff。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 表级字符集：字典 §1「全库 utf8mb4」
_CHARSET = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


def upgrade() -> None:
    # ---------- 1. 会话 ----------
    op.create_table(
        "qa_session",
        sa.Column("id", sa.String(64), primary_key=True),
        # ⚠️ **不加外键到 sys_user**：与 `kb_document.uploaded_by` 的处理一致 ——
        #    用户被删时要保留其历史问答（be-06 审计语义也依赖它）。
        sa.Column("user_id", sa.String(64), nullable=False),
        # 可空：首问落库前还没有标题；取首问前 qa_session_title_chars 字
        sa.Column("title", sa.String(64), nullable=True),
        # 与 qa_message 的行数**同事务**维护，因此两者永远一致（be-05 §10-106）
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
        **_CHARSET,
    )
    # 会话列表恒是「按 user_id 过滤 + 按 updated_at 倒序」
    op.create_index("ix_qa_session_user_id", "qa_session", ["user_id"])

    # ---------- 2. 消息 ----------
    op.create_table(
        "qa_message",
        sa.Column("id", sa.String(64), primary_key=True),
        # 不加外键：会话删除时消息是**显式物理删**（引擎里逐条 delete），
        # 一致性由引擎保证，加 FK 只会让删除顺序变脆弱
        sa.Column("session_id", sa.String(64), nullable=False),
        # user / assistant（字典 §5.4）
        sa.Column("role", sa.String(16), nullable=False),
        # 🔴 **绝不存 HTML** —— 前端渲染前过 DOMPurify（红线 2）
        sa.Column("content", sa.Text(), nullable=False),
        # 引用溯源（Citation 的 dict 形态），只有 assistant 那条有值
        sa.Column("citations", sa.JSON(), nullable=True),
        # 本轮被权限挡掉的文档数。**只存数量不存标题**（红线 4）
        sa.Column("blocked_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        **_CHARSET,
    )
    # 🔴 **必须是复合索引**，不能只索引 session_id：
    #    同一事务里 user 与 assistant 两条的 created_at 完全相同
    #    （同一个 server_default=now()），排序要靠 id 兜底；
    #    只索引 session_id 时 MySQL 还要对这批做 filesort。
    op.create_index(
        "ix_qa_message_session_created", "qa_message", ["session_id", "created_at"]
    )

    # ---------- 3. 问答日志（**不随会话删除**） ----------
    op.create_table(
        "qa_log",
        sa.Column("id", sa.String(64), primary_key=True),
        # 可空 —— 与 be-09 转建的会话解耦；且会话被删后本列会成为悬空引用。
        # 这是**可接受**的，正是「读本表不得 JOIN qa_session」这条约定的由来。
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        # 提问时用户的**自身**部门，不是含祖先的 depts。
        # 这是**归属口径**（谁问的），不是权限口径 ——
        # 混用会让 be-07 按部门统计时把祖先部门也算一遍，重复计数且不报错。
        sa.Column("department_id", sa.String(64), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        # 🔴 **必须写**（768 × float32 ≈ 3 KB）。少了它这一条永远无法参与
        #    be-08 的 P3 聚类，而 P1 期间不会有任何提示 ——
        #    等 P3 才发现「历史数据全是废的」。
        #    用 BLOB 而非 MEDIUMBLOB：3 KB 距 BLOB 的 64 KB 上限很远。
        sa.Column("question_embedding", sa.LargeBinary(), nullable=True),
        # 🔴 **三列 id 口径已冻结**（be-05 §10-19 / #113）：
        #    recalled_ids / allowed_ids 存 **chunk id**，blocked_ids 存 **document id**。
        #    be-07 算文档热度时必须 JOIN kb_chunk 展开，不能直接当 document id 用。
        sa.Column("recalled_ids", sa.JSON(), nullable=True),
        sa.Column("allowed_ids", sa.JSON(), nullable=True),
        sa.Column("blocked_ids", sa.JSON(), nullable=True),
        # 最高**余弦**相似度（与 kb_min_score 同量纲）。**不是 RRF 分数** ——
        # 存 RRF 分的话 kb_min_score 那套阈值就完全失效，且不报错。
        sa.Column("top_score", sa.Float(), nullable=True),
        sa.Column("token_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("token_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("faq_hit", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        **_CHARSET,
    )
    # 字典 §2.3 标注的两个索引：
    # ① 「某用户近 N 天的提问」—— 智能联想与个人统计走它
    op.create_index("ix_qa_log_user_created", "qa_log", ["user_id", "created_at"])
    # ② 「全平台近 N 天」—— be-07 的看板时间窗聚合走它
    op.create_index("ix_qa_log_created_at", "qa_log", ["created_at"])


def downgrade() -> None:
    # 逆序删除（本库无 FK 依赖，保持习惯以免将来加了 FK 后忘改）
    op.drop_table("qa_log")
    op.drop_table("qa_message")
    op.drop_table("qa_session")
