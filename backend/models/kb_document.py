"""知识单元表与四维权限表（字典 §2.2 `kb_document` / `kb_document_permission`）。

`kb_document` 是**权威台账**：Milvus 里的 `kb_chunk` 只是它的检索副本。
两边以 `document_id` 关联，所有一致性判断以本表为准（be-04 §8.1）。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TABLE_KWARGS, Base


class KbDocument(Base):
    """知识单元（一份被导入的文档）。

    生命周期见 PRD §6.5 / 字典 §5.1：`draft → enabled ⇄ disabled`，删除是**软删除**。
    """

    __tablename__ = "kb_document"
    __table_args__ = (TABLE_KWARGS,)

    # 状态枚举的唯一出处。散在引擎里写字面量 "enabled" 拼错一次，
    # 症状是「文档导入成功但永远检索不到」，且不报任何错。
    STATUS_DRAFT = "draft"
    STATUS_ENABLED = "enabled"
    STATUS_DISABLED = "disabled"
    #: 只有这个状态参与检索（be-05 侧落实，本模块只维护状态）
    SEARCHABLE_STATUS = STATUS_ENABLED

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: 扩展名（小写、不含点）：pdf / docx / md / txt …
    file_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_DRAFT, index=True
    )
    #: MinerU 解析后的 Markdown 原文。
    #: ⚠️ 它同时是**重试的可重放输入**（字典 §9 #94）—— 解析一成功就必须回写，
    #:    否则切片/向量化阶段失败时，重试拿不到任何输入（原始文件没有落盘）。
    #: 🔴 **LONGTEXT，不是 TEXT**：MySQL 的 TEXT 上限 64 KB，一份百页 PDF 的
    #:    markdown 轻松超过它 → 超长正文会被静默截断，表现为「导入成功但正文残缺」。
    #:    与 `alembic/versions/0002` 必须保持一致，否则将来 autogenerate 会报假差异。
    source_markdown: Mapped[str | None] = mapped_column(
        Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=True
    )
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    uploaded_by: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("sys_user.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
    #: 软删除标记（D16）。**非空即视为已删除**，所有查询都要带上它 ——
    #: 漏一处就会让「已删除的文档」重新出现在台账或检索结果里。
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class KbDocumentPermission(Base):
    """四维数据权限（一行一个授权实体）。

    判定规则（字典 §6.1.2）：四维 **OR** 充分条件；**没有任何权限行 = 除知识管理员外不可检索**。
    后者是刻意的：新导入的文档默认谁都不能查，必须显式授权。

    ⚠️ 写入语义是**全量替换**（先删后插，be-04 §5.6）。因此前端的权限弹窗
    必须能回显 —— 否则用户打开是空的、一保存就把原权限覆盖成全空。
    """

    __tablename__ = "kb_document_permission"
    __table_args__ = (
        # 「查某文档的全部授权行」是最高频的读（检索过滤 + 权限回显 + ACL 同步）
        Index("ix_kb_doc_perm_document_id", "document_id"),
        # 「某实体被授权了哪些文档」的反向查询（删除部门/角色前的引用校验走这条）
        Index("ix_kb_doc_perm_entity", "entity_type", "entity_id"),
        TABLE_KWARGS,
    )

    #: 授权维度枚举（字典 §5.3）
    ENTITY_GLOBAL = "global"
    ENTITY_DEPARTMENT = "department"
    ENTITY_ROLE = "role"
    ENTITY_USER = "user"
    #: `global` 维度的 entity_id 固定为 `*`（字典 §5.3），不是空串也不是任意值
    GLOBAL_ENTITY_ID = "*"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
