"""SQLAlchemy ORM 模型。

所有模型必须在此处 import —— Alembic 的 `env.py` 只 import 本包，
靠这里的 import 把它们注册进 `Base.metadata`。**漏 import 一张表 = 迁移里凭空少一张表，
且不报错**（autogenerate 会以为那张表不需要动）。
"""

from backend.models.audit_log import SYSTEM_USER_ID, SysAuditLog
from backend.models.base import Base
from backend.models.faq import Faq, FaqCandidate
from backend.models.kb_chunk import KbChunk
from backend.models.kb_document import KbDocument, KbDocumentPermission
from backend.models.kb_import_task import KbImportTask
from backend.models.knowledge_gap import ACTIVE_STATUSES, KnowledgeGap
from backend.models.qa_log import QaLog, pack_embedding, unpack_embedding
from backend.models.qa_message import QaMessage
from backend.models.qa_session import QaSession
from backend.models.stats import StatsDaily, StatsHourly
from backend.models.sys_department import SysDepartment
from backend.models.sys_permission import SysPermission
from backend.models.sys_role import SysRole, SysRolePermission
from backend.models.sys_user import SysUser, SysUserRole

__all__ = [
    "ACTIVE_STATUSES",
    "SYSTEM_USER_ID",
    "Base",
    "Faq",
    "FaqCandidate",
    "KbChunk",
    "KbDocument",
    "KbDocumentPermission",
    "KbImportTask",
    "KnowledgeGap",
    "QaLog",
    "QaMessage",
    "QaSession",
    "StatsDaily",
    "StatsHourly",
    "SysAuditLog",
    "SysDepartment",
    "SysPermission",
    "SysRole",
    "SysRolePermission",
    "SysUser",
    "SysUserRole",
    "pack_embedding",
    "unpack_embedding",
]
