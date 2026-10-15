"""BGE 向量化封装（PRD D17：公司 GPU 服务器只跑 BGE）。

走的是 **OpenAI 兼容的 `/v1/embeddings`**（`settings.embedding_base_url`），
所以换任何兼容实现都不用改这里。

🔴 **本模块只处理「文档片段」，不加 BGE 的查询指令前缀。**
   `BAAI/bge-*` 系列在**检索**时要求给 query 加
   `为这个句子生成表示以用于检索相关文章：` 前缀，而 **passage 不加**。
   两边都加或都不加都会让相似度整体偏移 —— 表现为「检索结果全都差不多」，
   不报错、不崩溃。be-05（检索）是加前缀的那一侧，**不要在这里「顺手统一」**。
"""

from __future__ import annotations

import logging

import httpx

from backend.core.exceptions import DependencyError
from backend.core.settings import settings

logger = logging.getLogger(__name__)

__all__ = ["embed_texts", "embed_batch"]

#: 单次请求的超时。BGE 在 GPU 上是毫秒级，给 120 s 是留给「冷启动 + 大 batch」
_TIMEOUT = httpx.Timeout(connect=10.0, read=settings.embedding_timeout_seconds,
                         write=30.0, pool=10.0)


def _wrap(exc: Exception, op: str) -> DependencyError:
    """httpx 异常 → `DependencyError`。第三方异常类型绝不外泄。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return DependencyError(f"向量服务{op}失败：HTTP {exc.response.status_code}")
    if isinstance(exc, httpx.TimeoutException):
        return DependencyError(f"向量服务{op}超时")
    return DependencyError(f"向量服务{op}失败：网络异常")


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """一批文本 → 一批向量，**顺序与入参一一对应**。

    顺序保证来自两处：请求体里 `input` 保持原序，响应里按 `index` 回填。
    只靠「响应数组的顺序」是不够的 —— OpenAI 兼容实现**允许**乱序返回，
    一旦乱序就会把 A 的向量挂到 B 的正文上，且完全静默
    （检索时表现为「问 A 答 B」，但相似度看着很正常）。
    """
    if not texts:
        return []

    url = f"{settings.embedding_base_url.rstrip('/')}/embeddings"
    payload = {"model": settings.embedding_model, "input": texts}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            body = resp.json()
    except Exception as exc:  # noqa: BLE001 —— 统一收敛
        raise _wrap(exc, "调用") from exc

    data = body.get("data")
    if not isinstance(data, list) or len(data) != len(texts):
        raise DependencyError(
            f"向量服务返回条数不符：请求 {len(texts)} 条，返回 "
            f"{len(data) if isinstance(data, list) else '非法'} 条"
        )

    ordered: list[list[float] | None] = [None] * len(texts)
    for position, item in enumerate(data):
        index = item.get("index", position)
        if not isinstance(index, int) or not 0 <= index < len(texts):
            raise DependencyError("向量服务返回的 index 越界")
        vector = item.get("embedding")
        if not isinstance(vector, list):
            raise DependencyError("向量服务返回的 embedding 字段非法")
        ordered[index] = vector

    if any(v is None for v in ordered):
        raise DependencyError("向量服务返回的 index 有缺失或重复")

    for vector in ordered:
        if len(vector) != settings.embedding_dim:  # type: ignore[arg-type]
            raise DependencyError(
                f"向量维度不符：期望 {settings.embedding_dim}，"
                f"实际 {len(vector)}"  # type: ignore[arg-type]
            )

    return ordered  # type: ignore[return-value]


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """按 `embedding_batch_size` 分批向量化，**输出与输入严格同序**。

    为什么要分批：一次几百条文本会把远端 BGE 的显存打满（返回 500 或直接超时），
    而对端超时属可重试错误 —— 于是任务会以同样的批次大小重试三次，次次失败。
    分批是**正确性**问题，不只是性能优化。
    """
    if not texts:
        return []

    batch_size = max(1, settings.embedding_batch_size)
    out: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        window = texts[start : start + batch_size]
        out.extend(await embed_batch(window))
        logger.debug("已向量化 %d/%d", min(start + batch_size, len(texts)), len(texts))
    return out
