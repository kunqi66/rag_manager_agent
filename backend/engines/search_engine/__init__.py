"""检索问答引擎（be-05）。

**这是本模块对外的唯一门面** —— 路由层与其它模块从这里 import，
不要直接 import `retrieval` / `acl` / `session` / `qa` 里的函数。
将来把 FAQ 接进来、或把检索拆成独立服务时，调用方一行都不用改。

内部结构：

| 文件 | 职责 |
|---|---|
| `acl.py` | 前置过滤表达式 + **后置兜底复核**（安全核心，纯逻辑 + 一次 SQL） |
| `retrieval.py` | 向量化 → 混合检索 → 取余弦分 → 受限探测 → 复核，产出可入 Prompt 的切片 |
| `qa.py` | Prompt 编排、DeepSeek 流式、产出 `SseEvent`；`ask` 由 `ask_stream` 折叠而来 |
| `session.py` | `qa_session` / `qa_message` / `qa_log` 的落库与读取（含归属校验） |

⚠️ 本包**不 import FastAPI** —— SSE 的线格式拼装在 `api/query_router.py`。
"""

from backend.engines.search_engine.acl import build_acl_expr, post_verify
from backend.engines.search_engine.qa import SYSTEM_PROMPT, ask, ask_stream, search
from backend.engines.search_engine.retrieval import RetrievalResult, embed_query, retrieve
from backend.engines.search_engine.session import (
    TurnLogData,
    delete_session,
    list_sessions,
    session_detail,
    suggestions,
)

__all__ = [
    # 检索问答
    "ask",
    "ask_stream",
    "search",
    "SYSTEM_PROMPT",
    # 会话
    "list_sessions",
    "session_detail",
    "delete_session",
    "suggestions",
    # 内部件（其它模块若确需复用，从这里 import 而不是深入子模块）
    "retrieve",
    "embed_query",
    "RetrievalResult",
    "build_acl_expr",
    "post_verify",
    "TurnLogData",
]
