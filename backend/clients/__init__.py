"""第三方客户端封装（mineru / embedding / llm / milvus / redis）。

分层铁律：**禁止把第三方异常类型泄漏到上层** ——
每个客户端都要把库异常统一转成 `backend/core/exceptions.py` 里的自定义异常。
上层只认 `BizError` 家族，不认 `redis.exceptions.*` / `httpx.*` / `pymilvus.*`。
"""
