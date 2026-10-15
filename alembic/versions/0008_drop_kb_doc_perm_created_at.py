"""删除 kb_document_permission.created_at（修历史漂移）

`kb_document_permission` 在 `0002` 里多建了一列 `created_at`（NOT NULL +
`server_default CURRENT_TIMESTAMP`），而 `核心数据字典.md` §2.2 与
`backend/models/kb_document.py` 的 `KbDocumentPermission` **都没有这一列**。

于是历史迁移与 ORM 长期不一致：`alembic check` / autogenerate 会一直想删它，
而运行期从不读它 —— 一致漂移着、谁也不报错。

🔴 **为什么新开一条迁移，而不是直接改 `0002`**：`0002` 已经应用到云端
   `rag_kb`（实测 `alembic current` 曾为 `0002`，且该列确实存在于库中）。
   改写已发布的迁移是**最危险的一类改动** —— 对已经跑过它的库完全无效
   （alembic 只记版本号、不会重放），对新库又产生不同结果，
   两边从此分叉且没有任何提示。历史迁移一律只读。

🔴 **必须写成「存在才删」**（MySQL 8 没有 `DROP COLUMN IF EXISTS`，
   那是 MariaDB 的扩展；照抄 MariaDB 语法会直接语法错误）。
   两个场景都要能跑通：
     ① 库是 `0002` 建的老库 —— 有这列，删掉；
     ② 库是后来用「改过的 0002」新建的 —— 没这列，本迁移是 no-op。
   写成无条件 `drop_column` 的话，场景 ② 会抛 "Can't DROP 'created_at'"，
   而那条报错发生在 `upgrade head` 的中途，会把整条启动链卡死。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "kb_document_permission"
_COLUMN = "created_at"


def _has_column() -> bool:
    """当前库里 `kb_document_permission.created_at` 是否存在。

    用 inspector 而不是 `information_schema` 裸查：inspector 走 SQLAlchemy 的
    方言适配，换库（本地 sqlite 做试验）时不用改这里。
    """
    bind = op.get_bind()
    columns = sa.inspect(bind).get_columns(_TABLE)
    return any(col["name"] == _COLUMN for col in columns)


def upgrade() -> None:
    if not _has_column():
        # 老库才有这一列。走到这里说明库是「新 0002」建的，本迁移无事可做。
        return
    op.drop_column(_TABLE, _COLUMN)


def downgrade() -> None:
    """把列加回去。

    ⚠️ **数据是回不来的**：列里原有时间戳在 upgrade 时已经丢失，这里只会补上
       一排「回滚那一刻的时间」。本次之所以可接受，是因为该列从未被任何代码读过、
       也没有索引依赖它 —— 那是**这一列的特例**，不能推广成「删列随时可回滚」。
    """
    if _has_column():
        return
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
