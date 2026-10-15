"""创建运营看板预聚合表

建 `stats_hourly` / `stats_daily`，字段严格对齐 `核心数据字典.md` §2.5（be-07 §5.1 设计、已冻结）。

手写而非 autogenerate，理由同 0001–0004：本机连的是**云端** rag_kb
（那台机器有勒索攻击的历史痕迹，只允许动 rag_kb 一个库），
不适合让它对一份不完全可控的库做全库 diff。

两表**指标列完全同构**，只有时间桶列与颗粒度不同。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 表级字符集：字典 §1「全库 utf8mb4」
_CHARSET = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


def _metric_columns() -> list[sa.Column]:
    """两表共用的指标列。

    🔴 **抽成一个函数而不是复制两遍**：两表的指标列必须**完全同构**（聚合代码按
       `grain` 复用同一段），但它们是**两张独立的表**，复制两遍后某天给小时表加一列
       而忘了日表，症状是「切到 30 天口径某个指标变 0」—— 而且不报错。
       共用一份定义让这种漂移在结构上不可能发生。
    """
    return [
        # 提问量（`qa_log` 行数，人次）
        sa.Column("pv", sa.Integer(), nullable=False, server_default="0"),
        # 🔴 **桶内**去重用户数。跨桶相加是**人次**不是**人数** ——
        #    `SUM(uv)` 得到的数看起来完全正常，没人会起疑（字典 §9 / be-07 §4.4）。
        #    看板侧用 `uv_is_exact` 告诉前端这个数能不能当「人数」读。
        sa.Column("uv", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("session_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("faq_hit_count", sa.Integer(), nullable=False, server_default="0"),
        # 0–1 的小数，**不是百分比**。前端会 ×100，给百分比会显示成 3400%（字典 §9 #120）。
        sa.Column("faq_hit_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("no_hit_count", sa.Integer(), nullable=False, server_default="0"),
        # BIGINT：token 是累计量，`qa_log.token_in` 单行就可能上万，
        # 一个高流量日的日桶用 INT(21 亿) 够用但没必要冒险。
        sa.Column("token_in_sum", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("token_out_sum", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("latency_avg_ms", sa.Integer(), nullable=False, server_default="0"),
        # P95 是**直方图插值估算**（MySQL 8 没有 PERCENTILE，字典 §9 #117）。
        sa.Column("latency_p95_ms", sa.Integer(), nullable=False, server_default="0"),
        # 键是桶下界**字符串**（JSON 对象的键只能是字符串），故可空而非默认 '{}'：
        # 未算过的桶（历史数据回填前）与「算了但没有样本」是两件事。
        sa.Column("latency_buckets", sa.JSON(), nullable=True),
        sa.Column("blocked_count", sa.Integer(), nullable=False, server_default="0"),
        # TOP 榜 JSON 快照（`P2-13-be-stats` §5.2：不发明第三张表）。
        sa.Column("top_questions", sa.JSON(), nullable=True),
        sa.Column("top_documents", sa.JSON(), nullable=True),
        # 本桶最近一次重算时间。**静默失败的唯一可观测手段** ——
        # beat 停一晚，这一列就停在昨晚（字典 §9 #123）。
        # 不用 server_default=now() 之外的花样：`onupdate` 由 ORM 侧负责，
        # 而聚合走的是显式 UPSERT，`updated_at` 在 UPSERT 的 SET 里显式给值。
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "stats_hourly",
        # 字典 §1 + §9 #33：主键一律 VARCHAR 字符串，与 `new_id("sth")` 的生成口径一致。
        sa.Column("id", sa.String(64), primary_key=True),
        # 整点时间桶起点。**naive**，与 `qa_log.created_at` 同一口径（Asia/Shanghai）。
        sa.Column("bucket_start", sa.DateTime(), nullable=False),
        *_metric_columns(),
        # 唯一约束而不是主键：主键是 `id`。「一桶一行」用 UNIQUE 表达更直白，
        # 同时给 `INSERT ... ON DUPLICATE KEY UPDATE` 一个明确的冲突目标 ——
        # 幂等性的保证必须在数据库里，不在应用代码的 SELECT-then-分支里
        # （那有两次往返和一个竞态窗口：手动回填撞上 beat 会 IntegrityError）。
        sa.UniqueConstraint("bucket_start", name="uq_stats_hourly_bucket_start"),
        **_CHARSET,
    )

    op.create_table(
        "stats_daily",
        sa.Column("id", sa.String(64), primary_key=True),
        # DATE 而不是 DATETIME：日桶的语义就是「哪一天」，
        # 用 DATETIME 存 00:00:00 会让「两个日桶同一天却不等」成为可能。
        sa.Column("bucket_date", sa.Date(), nullable=False),
        *_metric_columns(),
        sa.UniqueConstraint("bucket_date", name="uq_stats_daily_bucket_date"),
        **_CHARSET,
    )


def downgrade() -> None:
    # 先删日表再删小时表（无外键，顺序只是对称性）。
    op.drop_table("stats_daily")
    op.drop_table("stats_hourly")
