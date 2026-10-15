"""DeepSeek 生成模型封装（`deepseek-chat`，PRD 技术栈已锁定）。

走 OpenAI 兼容的 `/chat/completions`，所以换任何兼容实现都不用改这里。

🔴 **本模块只做「发请求 + 解析 SSE 线协议」**，不碰 Prompt 编排、不碰检索、
   不碰落库。Prompt 在 `engines/search_engine/qa.py`，那边才知道知识正文长什么样。

🔴 **第三方异常一律转 `DependencyError`**（分层铁律）：`httpx` 的类型绝不外泄。

⚠️ 与 `embedding_client` 的一处关键差异：那边是**一问一答**，
   这边是**流式**。流式的失败可能发生在「已经吐了一半」之后 ——
   此时 HTTP 状态码早已发出去了，改不了，只能靠上层把已生成的部分落库
   并在 SSE 里补一个 `error` 帧（be-05 §10-25）。
   所以本模块**不吞掉中途的异常**，让它带着「已经产出了多少」抛给上层。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from backend.core.exceptions import DependencyError
from backend.core.settings import settings

logger = logging.getLogger(__name__)

__all__ = ["LlmChunk", "LlmReply", "chat", "stream_chat"]

#: 上游 SSE 的终止哨兵（OpenAI 兼容约定）
_DONE_SENTINEL = "[DONE]"


@dataclass(slots=True)
class LlmChunk:
    """流式生成的一段。

    两种 chunk：
    - **正文片**：`text` 非空、`token_*` 为 `None`；
    - **用量片**：`text` 为空、`token_*` 有值 —— 上游在最后单独发一帧。

    用同一个类型承载（而不是「yield str，再把 usage 塞进共享变量」）是为了
    让消费方在 `async for` 里就能拿到用量，不必再维护一个跨越生成器边界的
    可变对象 —— 那种写法在调用方提前 `break` 时状态是不确定的。
    """

    text: str = ""
    token_in: int | None = None
    token_out: int | None = None


@dataclass(slots=True)
class LlmReply:
    """非流式生成的完整回复（be-08 的候选答案合并用它）。"""

    text: str = ""
    token_in: int = 0
    token_out: int = 0


def _wrap(exc: Exception, op: str) -> DependencyError:
    """httpx 异常 → `DependencyError`。第三方异常类型绝不外泄。"""
    if isinstance(exc, httpx.HTTPStatusError):
        # 上游把错误细节放在 body 里（如「余额不足」），带上更利于排查；
        # 但只截前 200 字符，避免把整页 HTML 塞进日志与响应。
        detail = ""
        try:
            detail = exc.response.text[:200]
        except Exception:  # noqa: BLE001 —— 读 body 失败不该掩盖原始错误
            detail = ""
        return DependencyError(
            f"生成模型{op}失败：HTTP {exc.response.status_code}"
            + (f" {detail}" if detail else "")
        )
    if isinstance(exc, httpx.TimeoutException):
        return DependencyError(f"生成模型{op}超时")
    return DependencyError(f"生成模型{op}失败：网络异常")


def _parse_line(line: str) -> LlmChunk | None:
    """解析上游的一行 SSE。

    返回 `None` 表示这一行不需要产出任何东西（空行 / 注释 / 非 data 行）。
    """
    if not line or not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload or payload == _DONE_SENTINEL:
        return None

    try:
        body = json.loads(payload)
    except json.JSONDecodeError:
        # 🔴 **不抛异常**：上游偶尔会发心跳/注释行，为一行噪声中断整轮生成本末倒置。
        #    但也**不能静默到看不见** —— 真的协议变更会表现为「回答是空的」，
        #    所以留一条 warning。
        logger.warning("生成模型返回了无法解析的 SSE 行（已跳过）：%s", payload[:120])
        return None

    # 用量帧：choices 通常为空数组
    usage = body.get("usage")
    if isinstance(usage, dict):
        return LlmChunk(
            token_in=int(usage.get("prompt_tokens") or 0),
            token_out=int(usage.get("completion_tokens") or 0),
        )

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    delta = choices[0].get("delta")
    if not isinstance(delta, dict):
        return None
    text = delta.get("content")
    if not isinstance(text, str) or not text:
        # 首个 chunk 常带 `role` 而没有 content —— 正常，跳过
        return None
    return LlmChunk(text=text)


async def stream_chat(
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> AsyncIterator[LlmChunk]:
    """流式生成。`messages` 是 OpenAI 格式的 `[{role, content}, ...]`。

    ⚠️ **不做重试**。流式重试意味着「把已经吐给用户的内容再吐一遍」，
       用户会看到回答变成两遍。瞬时故障由用户自己重问一次更诚实。
       （这与 be-04 的 MinerU 任务重试是两回事：那边没有实时消费者。）
    """
    if not settings.deepseek_api_key:
        raise DependencyError("未配置 DEEPSEEK_API_KEY，无法调用生成模型")

    url = f"{settings.deepseek_api_base.rstrip('/')}/chat/completions"
    payload: dict[str, object] = {
        "model": settings.deepseek_model,
        "messages": messages,
        "stream": True,
        # 🔴 不加这个开关时上游**不会**在流末尾发 usage 帧，
        #    `token_in` / `token_out` 就永远是 0 —— 看板的 token 统计会全为零，
        #    而且没有任何报错。
        "stream_options": {"include_usage": True},
        "temperature": (
            settings.deepseek_temperature if temperature is None else temperature
        ),
        "max_tokens": settings.deepseek_max_tokens if max_tokens is None else max_tokens,
    }
    timeout = httpx.Timeout(
        connect=10.0, read=settings.deepseek_timeout_seconds, write=30.0, pool=10.0
    )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    chunk = _parse_line(line)
                    if chunk is not None:
                        yield chunk
    except DependencyError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 统一收敛为 DependencyError
        raise _wrap(exc, "调用") from exc


async def chat(
    messages: list[dict[str, str]],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout_seconds: float | None = None,
) -> LlmReply:
    """非流式生成。`messages` 同 `stream_chat`。

    **为什么要有这个函数**：be-08 的候选答案合并是**离线批处理**
    （worker 里跑，没有实时消费者），用 `stream_chat` 得把片段拼起来 ——
    那等于在调用方重写一遍「拼接到 `[DONE]`、顺便收 usage」的逻辑；
    而上游 `stream=False` 时这些都由服务端一次给全。

    ⚠️ **与 `stream_chat` 不同，这里可以做重试**：没有「已经吐给用户一半」
       的问题。但本期**刻意不做** —— 重试要配退避、要判哪些状态码可重试，
       而调用方（`faq_mining`）本来就有一层「LLM 失败就降级用原文」的兜底，
       那才是这个场景正确的失败姿态：不要让一个非关键步骤把整轮挖掘拖住。
       上层的降级见 `engines/data_mining_engine/faq_mining.mine_faq_clusters`。

    ## `timeout_seconds`：为什么这个函数需要「按调用方调超时」

    默认走 `DEEPSEEK_TIMEOUT_SECONDS`（给「这一步的结果就是这一步的产出」的调用方）。
    但 be-09 的缺口分类推荐是个**可选装饰**：它失败完全不影响缺口入池。
    让它共用全局超时，意味着「一轮聚合的预算被一个装饰性步骤吃掉」——
    而缺口的产出会因此整批延后一轮（10 分钟）。

    所以超时按调用方给：这里只提供口子，**具体值由调用方自己的配置定义**
    （be-09 = `GAP_LLM_SUGGEST_TIMEOUT_SECONDS`）——
    不在这里写「哪个调用方该用几秒」，那会把业务参数塞进客户端。
    """
    #: `None` ⇒ 用全局值；给了就用给的。**不写成 `timeout_seconds or default`** ——
    #: 那样传 `0` 会被当成「没传」，而这个函数的读超时是 `httpx.Timeout(read=...)`，
    #: 0 是个合法的「立即超时」（压测/演练用）。
    read_timeout = (
        settings.deepseek_timeout_seconds if timeout_seconds is None else timeout_seconds
    )
    if not settings.deepseek_api_key:
        raise DependencyError("未配置 DEEPSEEK_API_KEY，无法调用生成模型")

    url = f"{settings.deepseek_api_base.rstrip('/')}/chat/completions"
    payload: dict[str, object] = {
        "model": settings.deepseek_model,
        "messages": messages,
        # 非流式：一次拿全文 + usage。**不加 `stream_options`** ——
        # 那个开关只在流式下有含义，跟着非流式请求发过去属于无意义参数。
        "stream": False,
        "temperature": (
            settings.deepseek_temperature if temperature is None else temperature
        ),
        "max_tokens": settings.deepseek_max_tokens if max_tokens is None else max_tokens,
    }
    timeout = httpx.Timeout(connect=10.0, read=read_timeout, write=30.0, pool=10.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            body = resp.json()
    except DependencyError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 统一收敛为 DependencyError
        raise _wrap(exc, "调用") from exc

    usage = body.get("usage") or {}
    choices = body.get("choices") or []
    text = ""
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            text = content
    return LlmReply(
        text=text.strip(),
        token_in=int(usage.get("prompt_tokens") or 0),
        token_out=int(usage.get("completion_tokens") or 0),
    )
