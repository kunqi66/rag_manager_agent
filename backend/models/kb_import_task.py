"""导入任务表（字典 §2.2 `kb_import_task`）。

前端靠 `GET /api/import/tasks/{task_id}` 轮询本表拿进度，因此**每次状态推进都要落库**，
不能只活在 worker 的内存里 —— 否则前端进度条会永远停在 0。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TABLE_KWARGS, Base


class KbImportTask(Base):
    """一次解析入库任务（单篇一个，批量导入则一批多个共享 `batch_id`）。"""

    __tablename__ = "kb_import_task"
    __table_args__ = (TABLE_KWARGS,)

    # 任务状态枚举（字典 §5.2）。与 MinerU 自己的任务态（pending/running/
    # converting/done/waiting-file）是**两套东西**，不要混用：
    # MinerU 那套只决定「要不要继续轮询」，本表这套是给前端看的业务进度。
    STATUS_PENDING = "pending"
    STATUS_PARSING = "parsing"
    STATUS_EMBEDDING = "embedding"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    #: 尚未结束的状态。正文编辑要挡在这些状态之后（字典 §9 #99）
    ACTIVE_STATUSES = (STATUS_PENDING, STATUS_PARSING, STATUS_EMBEDDING)

    #: 任务来源（字典 §2.2）
    TYPE_UPLOAD = "upload"
    TYPE_SUPPLEMENT = "supplement"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 批量导入批次号；单篇上传时为 None
    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: 本任务产出的知识单元。受理时与 `kb_document` **同事务**写入（字典 §9 #93）。
    #: 可空只为容纳 be-09「一键转建」的中间态：那时任务已建、文档还没上传（#64）。
    document_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: 本任务的来源知识缺口（be-09「一键转建」，字典 §9 #64 已定）。
    #: 🔴 **两个作用，缺一不可**：① 「这条缺口是哪条任务在补」可回查；
    #:    ② be-09 的**转建幂等键** —— 建任务与置 `tasked` 不在同一事务
    #:    （`create_supplement_task` 自己 commit，见其 docstring），置 `tasked`
    #:    失败后重试时靠它认出「任务已经建过了」，避免建出第二条。
    #: ⚠️ 刻意**不加外键**：项目一贯用应用层管一致性（同 `document_id`），
    #:    且缺口表与任务表的删除语义归不同模块 —— 加 FK 会把两边的删除顺序焊死。
    gap_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    type: Mapped[str] = mapped_column(String(16), nullable=False, default=TYPE_UPLOAD)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_PENDING, index=True
    )
    #: 0–100
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mineru_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_msg: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    uploaded_by: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("sys_user.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
