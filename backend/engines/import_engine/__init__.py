"""知识导入引擎（be-04）。

**这是本模块对外的唯一门面** —— 其它模块（be-05 检索、be-09 缺口转建）
与路由层都从这里 import，不要直接 import `lifecycle` / `pipeline` 里的函数。
这样将来拆分「查」与「写」、或者把 pipeline 挪进独立的 worker 包时，
调用方一行都不用改。

内部结构：

| 文件 | 跑在哪 | 职责 |
|---|---|---|
| `lifecycle.py` | API 进程 | 12 个端点背后的业务：读写 MySQL 权威状态 + 派发任务 |
| `pipeline.py` | Celery worker | 解析 → 切片 → 向量化 → 双写 |
| `chunking.py` | 两边都用 | 结构感知切片（纯函数，无 IO） |
| `tokenizer.py` | 两边都用 | BGE 口径的 token 计数（纯函数，懒加载） |
"""

from backend.engines.import_engine.chunking import split_markdown
from backend.engines.import_engine.lifecycle import (
    create_supplement_task,
    delete_document,
    derive_acl,
    get_document,
    get_permissions,
    get_task,
    list_documents,
    list_tasks,
    list_visible_document_ids,
    retry_import_task,
    set_document_status,
    update_document,
    update_permissions,
    upload_document,
    upload_documents,
)
from backend.engines.import_engine.pipeline import (
    run_import_pipeline,
    run_rebuild_pipeline,
)
from backend.engines.import_engine.tokenizer import count_tokens

__all__ = [
    # 台账
    "list_documents",
    "get_document",
    "upload_document",
    "upload_documents",
    "update_document",
    "set_document_status",
    "delete_document",
    # 权限
    "get_permissions",
    "update_permissions",
    "derive_acl",
    # 任务
    "list_tasks",
    "get_task",
    "retry_import_task",
    # 跨模块
    "create_supplement_task",
    "list_visible_document_ids",
    # worker 侧
    "run_import_pipeline",
    "run_rebuild_pipeline",
    # 纯函数
    "split_markdown",
    "count_tokens",
]
