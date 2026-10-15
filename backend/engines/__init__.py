"""业务引擎层。

**本层禁止 import FastAPI 的任何东西**（CLAUDE.md 分层铁律）：
引擎要能被 Celery worker、脚本、单测直接调用，不能依赖 HTTP 上下文。
需要 `UploadFile` 之类的 HTTP 类型时，在 `api/` 层读成 `bytes` 再传进来。

| 引擎 | 模块 | 职责 |
|---|---|---|
| `import_engine` | be-04 | 解析 → 切片 → 向量化 → 双写；知识单元生命周期与四维权限 |
| `search_engine` | be-05 | 权限感知混合检索、SSE 问答（未开工） |
| `data_mining_engine` | be-08 | FAQ 聚类与知识缺口挖掘（未开工） |
"""
