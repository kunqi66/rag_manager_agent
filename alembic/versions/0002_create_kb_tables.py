"""创建知识库四张表

建 kb_document / kb_document_permission / kb_chunk / kb_import_task，
字段严格对齐 `核心数据字典.md` §2.2。

手写而非 autogenerate，与 0001 同理 —— 但这次理由不同：
autogenerate 需要对着真实库做 diff，而本机连的是**云端** rag_kb
（那台机器有勒索攻击的历史痕迹，只允许动 rag_kb 一个库）。
让它对着一份不完全可控的库结构做全库 diff，风险与收益不成比例。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 表级字符集：字典 §1「全库 utf8mb4」
_CHARSET = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


def upgrade() -> None:
    # ---------- 1. 知识单元 ----------
    op.create_table(
        "kb_document",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=True),
        # 扩展名（小写、不含点）
        sa.Column("file_type", sa.String(16), nullable=True),
        sa.Column("category", sa.String(64), nullable=True),
        # 生命周期：draft / enabled / disabled（字典 §5.1）
        sa.Column("status", sa.String(16), nullable=False),
        # 🔴 **必须是 LONGTEXT，不能是 sa.Text。**
        #    MySQL 的 TEXT 上限是 64 KB，而一份百页 PDF 的 markdown 轻松超过它 ——
        #    用 TEXT 的话，超长正文会在插入时被**静默截断**（非严格模式）
        #    或直接报错（严格模式，MySQL 8 默认）。前者更可怕：
        #    文档"导入成功"，但检索到的正文是残缺的。
        #    它同时是失败重试的可重放输入（字典 §9 #94），截断等于丢掉重试能力。
        sa.Column(
            "source_markdown",
            sa.Text().with_variant(mysql.LONGTEXT(), "mysql"),
            nullable=True,
        ),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploaded_by", sa.String(64), nullable=True),
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
        # 软删除（D16）。**非空即已删除**，所有查询都要带它 ——
        # 漏一处就会让「已删除的文档」重新出现在台账或检索结果里。
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["uploaded_by"], ["sys_user.id"], name="fk_kb_document_uploaded_by"
        ),
        **_CHARSET,
    )
    op.create_index("ix_kb_document_status", "kb_document", ["status"])
    op.create_index("ix_kb_document_updated_at", "kb_document", ["updated_at"])

    # ---------- 2. 四维数据权限 ----------
    op.create_table(
        "kb_document_permission",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("document_id", sa.String(64), nullable=False),
        # global / department / role / user（字典 §5.3）
        sa.Column("entity_type", sa.String(16), nullable=False),
        # global 维度固定为 "*"
        sa.Column("entity_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # ⚠️ **一处外键都不建**，两个不同的理由：
        #    ① 不到 kb_document：写权限是「先删后插」的全量替换，
        #       加 FK 只会让替换操作在边界情况下随机失败；
        #    ② 不到 sys_department / sys_role / sys_user：`entity_id` 是**多态列**
        #       （四维共用一个），FK 语法上就无法表达这种多态约束。
        #    悬空引用由 be-04 的写入时校验挡住（字典 §9 #98）。
        **_CHARSET,
    )
    # 「查某文档的全部授权行」是最高频的读（检索过滤 + 权限回显 + ACL 同步）
    op.create_index(
        "ix_kb_doc_perm_document_id", "kb_document_permission", ["document_id"]
    )
    # 「某实体被授权了哪些文档」的反向查询（删除部门/角色前的引用校验走这条）
    op.create_index(
        "ix_kb_doc_perm_entity", "kb_document_permission", ["entity_type", "entity_id"]
    )

    # ---------- 3. 切片（MySQL 权威侧） ----------
    op.create_table(
        "kb_chunk",
        sa.Column("id", sa.String(64), primary_key=True),
        # ⚠️ **不加外键**：删除文档时是「软删 kb_document + 物理删 kb_chunk」，
        #    加 FK 只会让物理删除的顺序变得脆弱，而一致性本就由引擎显式保证。
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        # 这里的 TEXT（64 KB）够用：单片上限约 1000 token ≈ 4 KB，
        # 与 kb_document.source_markdown 的情况不同，别照抄那个 LONGTEXT。
        sa.Column("content", sa.Text(), nullable=False),
        # 标题路径，如 `差旅制度 > 报销标准 > 住宿费`
        sa.Column("heading_path", sa.String(512), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        **_CHARSET,
    )
    # 按 document_id 全量重建 / 删除 / 同步 ACL 都走它
    op.create_index("ix_kb_chunk_document_id", "kb_chunk", ["document_id"])

    # ---------- 4. 导入任务 ----------
    op.create_table(
        "kb_import_task",
        sa.Column("id", sa.String(64), primary_key=True),
        # 批量导入批次号；单篇上传时为 NULL
        sa.Column("batch_id", sa.String(64), nullable=True),
        # 可空：be-09「一键转建」的任务先于文档存在（字典 §9 #64）
        sa.Column("document_id", sa.String(64), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=True),
        # BigInteger：单个文件上限 200 MB，但 INT 装得下 —— 用 BIGINT
        # 是为了将来放宽上限时不必再动一次表
        sa.Column("file_size", sa.BigInteger(), nullable=False, server_default="0"),
        # upload / supplement（字典 §2.2）
        sa.Column("type", sa.String(16), nullable=False),
        # pending / parsing / embedding / success / failed（字典 §5.2）
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_chunks", sa.Integer(), nullable=False, server_default="0"),
        # MinerU 侧的 job id（形如 `batch_id|uploaded_name`）。
        # 它是重试的**第二条可重放路径**：解析已提交但没轮询完时凭它取回结果，
        # 无需重新上传。有了它，字典 §9 #96 的残差只剩「提交阶段失败」——
        # 而提交发生在同步受理阶段，那种情况压根不会留下失败任务。
        sa.Column("mineru_task_id", sa.String(128), nullable=True),
        sa.Column("error_msg", sa.String(1024), nullable=True),
        sa.Column("uploaded_by", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["uploaded_by"], ["sys_user.id"], name="fk_kb_import_task_uploaded_by"
        ),
        **_CHARSET,
    )
    op.create_index("ix_kb_import_task_batch_id", "kb_import_task", ["batch_id"])
    op.create_index("ix_kb_import_task_document_id", "kb_import_task", ["document_id"])
    op.create_index("ix_kb_import_task_status", "kb_import_task", ["status"])


def downgrade() -> None:
    # 逆序删除（本库无 FK 依赖，保持习惯以免将来加了 FK 后忘改）
    op.drop_table("kb_import_task")
    op.drop_table("kb_chunk")
    op.drop_table("kb_document_permission")
    op.drop_table("kb_document")
