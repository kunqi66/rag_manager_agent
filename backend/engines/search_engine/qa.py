"""问答编排：Prompt → 生成 → 事件流 / 折叠成响应（be-05 §5.2）。

🔴 **`ask` 与 `ask_stream` 必须同源**：非流式是把流式的事件**折叠**出来的，
   不是另写一条路径（spec §4 关键契约点 4）。两条路径各写一份实现迟早漂移，
   而症状是「流式答对了、非流式少一条引用」—— 只在降级场景才暴露。

🔴 **本模块不 import FastAPI**。它只产出 `SseEvent`（普通 Pydantic DTO）
   的异步生成器，线格式的拼装留在 `api/query_router.py`。

🔴 **未授权内容不进 Prompt、不进响应**：进这里的内容已经过
   `acl.post_verify` 复核（`retrieval.retrieve` 的产物）。
   本模块**不允许**自己去查 Milvus 或 MySQL 取候选 —— 那会绕过复核。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from backend.clients import llm_client
from backend.core.exceptions import BizError, ErrorCode
from backend.core.ids import new_id
from backend.engines.search_engine import retrieval, session as session_engine
from backend.schemas.auth import AuthContext
from backend.schemas.query import (
    AskResponse,
    CitationItem,
    SearchResponse,
    SseBlocked,
    SseCitations,
    SseDelta,
    SseDone,
    SseError,
    SseEvent,
    SseStart,
)

logger = logging.getLogger(__name__)

__all__ = ["ask", "ask_stream", "search", "SYSTEM_PROMPT"]

_MESSAGE_PREFIX = "msg"

SYSTEM_PROMPT = (
    "你是企业内部知识助手。请严格依据【参考资料】回答用户问题，遵守以下规则：\n"
    "1. 只依据参考资料作答，不要使用资料之外的知识，也不要推测。\n"
    "2. 资料不足以回答时，直接说明「根据现有资料无法回答」，不要编造。\n"
    "3. 回答末尾**不要**罗列参考资料编号或来源 —— 系统会单独下发引用卡片。\n"
    "4. 绝不透露任何未出现在参考资料中的内容，也不要提及资料之外的文档名。\n"
    "5. 用简洁的中文回答，可以使用 Markdown，但不要输出 HTML 标签。"
)


def _try_faq_match(normalized_query_vector: list[float]):  # type: ignore[no-untyped-def]
    """FAQ 进程内索引查询。命中返回 `FaqHit`，未命中返回 `None`。

    ✅ **P3 / be-08 已落地**（原为 P1 的降级桩，见 `P1-11` §10 #143）。
       be-08 交付了 `backend/faq/faq_cache.py`，索引由
       `service/faq_service.startup()` 在 `lifespan` 里装载。

    ⚠️ 传进来的向量需**已按 be-08 的约定做 L2 归一化**（be-05 §5.2-①），
       与 Milvus 用的原始向量是两个口径。
       ⚠️ 但 `FaqCache.match` 自己也会再归一化一次 —— 这是刻意的冗余：
       「归一化」若取决于调用方的记性，漏了会让相似度变成未归一化的点积
       （数值偏大 ⇒ 大量误命中，用户看到「问什么都答同一条 FAQ」）。
       多算一次 O(768) 换来这条路径不可能出错。

    ⚠️ **保留 try/except**：索引未装载（启动时 MySQL 抖动）、
       或 `faq_cache` 内部出任何意外时，正确行为是**当作没有缓存**走正常检索，
       而不是让整个问答 500。「FAQ 不命中」从来不是错误。
    """
    try:
        from backend.faq.faq_cache import faq_cache
    except ImportError:
        return None
    try:
        return faq_cache.match(normalized_query_vector)
    except Exception:  # noqa: BLE001 —— 见 docstring：缓存故障必须降级
        logger.exception("FAQ 索引匹配失败，本轮按未命中处理")
        return None


def _build_messages(
    query: str,
    chunks: list[dict],
    history: list,
) -> list[dict[str, str]]:
    """组装 OpenAI 格式的 messages。

    上下文条数 = `retrieval_top_k`（`retrieval` 已截断），**不另设 token 预算** ——
    每条 chunk 已被 be-04 的 `chunk_max_tokens` 限过，上界是两者的乘积，
    再加一个旋钮只会多一处可调错的地方（be-05 §10-22）。

    历史用**直接拼接**而非问题改写：改写要多一次 LLM 调用，P1 不值得。
    """
    parts: list[str] = []
    if chunks:
        parts.append("【参考资料】")
        for index, chunk in enumerate(chunks, start=1):
            heading = chunk.get("heading_path") or ""
            title = chunk.get("title") or ""
            # 路径与标题都给：路径帮模型定位层级，标题帮它判断资料是否对题
            label = " > ".join(p for p in (title, heading) if p)
            parts.append(f"[{index}] {label}\n{chunk.get('content') or ''}")
    else:
        # 显式说明「没有资料」。不给这一段的话，空上下文会被模型当成
        # 「用户让我自由发挥」，于是开始凭空作答 —— 这正是知识库最不能容忍的行为。
        parts.append("【参考资料】\n（本轮没有检索到任何相关资料）")

    if history:
        parts.append("【历史对话】")
        for message in history:
            speaker = "用户" if message.role == "user" else "助手"
            parts.append(f"{speaker}：{message.content}")

    parts.append(f"【当前问题】{query}")

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


async def _prepare_session(
    db: AsyncSession,
    ctx: AuthContext,
    session_id: str | None,
    query: str,
):  # type: ignore[no-untyped-def]
    """拿到（或新建）会话。会话存在时**必须**校验归属。"""
    if session_id:
        return await session_engine.get_owned_session(db, session_id, ctx)
    return await session_engine.create_session(db, ctx, query)


async def ask_stream(
    db: AsyncSession,
    *,
    query: str,
    session_id: str | None,
    top_k: int,
    ctx: AuthContext,
) -> AsyncIterator[SseEvent]:
    """流式问答。产出 `start → delta* → citations → blocked? → done`。

    异常不会向上抛：**流已经开始后改不了 HTTP 状态码**，所有失败都以
    `error` 事件收尾（be-05 §6）。唯一会抛的是「还没产出任何事件就失败」——
    那种情况交给 API 层的全局处理器，能给出正常的 HTTP 错误码。
    """
    started = time.perf_counter()

    # ⚠️ 会话解析放在第一个 yield **之前**：此时还没发帧，失败可以正常抛 HTTP 错误
    #    （例如「拿着别人的 session_id 来问」→ 3001）。
    session = await _prepare_session(db, ctx, session_id, query)
    message_id = new_id(_MESSAGE_PREFIX)

    yield SseEvent.make("start", SseStart(session_id=session.id, message_id=message_id))

    answer_parts: list[str] = []
    citations: list[CitationItem] = []
    blocked_count = 0
    token_in = 0
    token_out = 0
    log = session_engine.TurnLogData(question=query)
    verified_done = False

    try:
        # ---- ① 向量化（复用给检索与 qa_log）----
        query_dense = await retrieval.embed_query(query)
        log.question_embedding = query_dense

        # ---- ② FAQ 短路：命中则**直接回答，不检索、不调 LLM**（be-08 §5.5）----
        hit = _try_faq_match(_l2_normalize(query_dense))
        if hit is not None:
            # 这一段是整个 P3 的价值所在（PRD §9.2 场景二的验收口径）：
            # `token_in = token_out = 0` —— 缓存命中不产生任何生成开销。
            answer_parts.append(hit.answer)
            yield SseEvent.make("delta", SseDelta(content=hit.answer))

            # 🔴 `citations` **必须发空数组**，不是「不发这个事件」。
            #    前端的 SSE 消费端按事件名组装状态，缺了 `citations` 事件
            #    引用区会保持上一次的状态（切换会话后还能看到上一轮的引用卡片），
            #    看起来像「引用了不相关的东西」。
            yield SseEvent.make("citations", SseCitations(items=[]))
            # 🔴 **不发 `blocked`**：FAQ 的回答不来自任何文档的检索结果，
            #    没有「受限文档被挡住」这回事。凭空发一个 count=0 的 blocked，
            #    前端会渲染出「有 0 篇资料因权限不可见」的提示条。

            log.faq_hit = True
            log.token_in = 0
            log.token_out = 0
            # 检索相关的列留空（`recalled_ids` / `allowed_ids` / `top_score`）——
            # 这是**如实**记录「本轮没检索」，而不是填 0 或空数组冒充检索过。
            # be-09 的缺口判定读 `allowed_ids`，填假值会污染缺口聚合。
            log.latency_ms = int((time.perf_counter() - started) * 1000)
            # 🔴 **命中也要落 `qa_log`**（be-05 §10-24 / be-08 §5.5-3）：
            #    ① 看板的 FAQ 命中率要它；② **本模块自己的挖掘窗口**
            #    `WHERE faq_hit = false` 也读它 —— 不落库的话，
            #    已发布 FAQ 的问法会**永远**留在挖掘窗口里，
            #    每轮聚类都把它当成新候选重新提一遍。
            await session_engine.persist_turn(
                db,
                session=session,
                ctx=ctx,
                question=query,
                message_id=message_id,
                answer=hit.answer,
                citations=[],
                blocked_count=0,
                log=log,
            )
            verified_done = True

            yield SseEvent.make(
                "done",
                SseDone(
                    latency_ms=log.latency_ms,
                    token_in=0,
                    token_out=0,
                    faq_hit=True,
                    # 🔴 `llm_called=False` 是**如实**的：控制流根本没走到生成。
                    #    写死 `True` 会让前端一直显示「AI 生成中」的动效，
                    #    也让「缓存省了多少次调用」这个数永远等于提问数。
                    llm_called=False,
                ),
            )
            return  # 🔴 跳过检索与 LLM —— 这才是「加速」的全部意义

        # ---- ③④⑤⑥ 检索 + 权威复核 ----
        result = await retrieval.retrieve(db, query, ctx, top_k)
        citations = result.citations
        blocked_count = result.blocked_count
        log.recalled_ids = result.recalled_ids
        log.allowed_ids = result.allowed_ids
        log.blocked_ids = result.blocked_ids
        log.top_score = result.top_score

        history = await session_engine.load_history(db, session.id)

        # ---- ⑦ 生成 ----
        # 走到这里说明 FAQ 未命中（命中已在 ② 返回），所以 `faq_hit` 恒为 False。
        # 用一个具名变量而不是到处写 `False`，是为了让「这一轮到底命中没有」
        # 在落库与 `done` 两处只有一个来源。
        faq_hit = False
        messages = _build_messages(query, result.chunks, history)
        llm_called = True
        async for chunk in llm_client.stream_chat(messages):
            if chunk.token_in is not None:
                token_in = chunk.token_in
            if chunk.token_out is not None:
                token_out = chunk.token_out
            if chunk.text:
                answer_parts.append(chunk.text)
                yield SseEvent.make("delta", SseDelta(content=chunk.text))

        answer = "".join(answer_parts)

        # ---- 引用与受限提示：**顺序不可换**（PRD §6.2.5）----
        yield SseEvent.make("citations", SseCitations(items=citations))
        if blocked_count > 0:
            yield SseEvent.make(
                "blocked",
                SseBlocked(count=blocked_count, titles=result.blocked_titles),
            )

        # ---- ⑧ 落库：**在 done 之前**（be-05 §10-106）----
        log.token_in = token_in
        log.token_out = token_out
        log.faq_hit = faq_hit
        log.latency_ms = int((time.perf_counter() - started) * 1000)
        await session_engine.persist_turn(
            db,
            session=session,
            ctx=ctx,
            question=query,
            message_id=message_id,
            answer=answer,
            citations=citations,
            blocked_count=blocked_count,
            log=log,
        )
        verified_done = True

        yield SseEvent.make(
            "done",
            SseDone(
                latency_ms=log.latency_ms,
                token_in=token_in,
                token_out=token_out,
                faq_hit=faq_hit,
                llm_called=llm_called,
            ),
        )

    except (GeneratorExit, asyncio.CancelledError):
        # ---- 客户端中途断开（be-05 §10-25）----
        # 已生成的部分照落库：半截回答对用户仍是「问过了」，
        # 不落库会让 be-09 的缺口判定丢掉现场。
        #
        # 🔴 用 `shield` 包住落库：一旦任务被取消，后续 await 会**立即**再抛
        #    CancelledError，普通写法根本执行不到 commit —— 表现为
        #    「断开的那一轮永远不在库里」，而且不报错。
        if not verified_done and answer_parts:
            log.latency_ms = int((time.perf_counter() - started) * 1000)
            log.token_in = token_in
            log.token_out = token_out
            with anyio.CancelScope(shield=True):
                try:
                    await session_engine.persist_turn(
                        db,
                        session=session,
                        ctx=ctx,
                        question=query,
                        message_id=message_id,
                        answer="".join(answer_parts),
                        citations=citations,
                        blocked_count=blocked_count,
                        log=log,
                    )
                    logger.info("客户端断开，已落库半截回答：session=%s", session.id)
                except Exception:  # noqa: BLE001 —— 落库失败不能盖掉取消
                    logger.exception("客户端断开后落库失败：session=%s", session.id)
        raise

    except BizError as exc:
        # 业务异常：已发过帧，只能用 error 事件收尾
        logger.warning("问答失败（业务异常）：%s", exc.message)
        yield SseEvent.make("error", SseError(code=exc.code, message=exc.message))
    except Exception as exc:  # noqa: BLE001 —— 流已开始，不能让它冒泡成 500 页
        logger.exception("问答失败（未预期异常）")
        yield SseEvent.make(
            "error",
            SseError(code=ErrorCode.INTERNAL_ERROR, message=f"生成失败：{type(exc).__name__}"),
        )


def _l2_normalize(vector: list[float]) -> list[float]:
    """L2 归一化。给 FAQ 匹配用（be-08 约定），**Milvus 侧不需要**。"""
    norm = sum(v * v for v in vector) ** 0.5
    if norm <= 0:
        return vector
    return [v / norm for v in vector]


async def ask(
    db: AsyncSession,
    *,
    query: str,
    session_id: str | None,
    top_k: int,
    ctx: AuthContext,
) -> AskResponse:
    """非流式问答 —— **把 `ask_stream` 的事件折叠起来**（同源，见模块头部）。

    ⚠️ 出现的 `error` 事件在这里转成对应的 `BizError` 抛出：
       调用方（路由层）需要一个正常的 HTTP 状态码，而不是一个 200 + 空回答。
    """
    session_id_out = ""
    message_id_out = ""
    parts: list[str] = []
    citations: list[CitationItem] = []
    blocked_count = 0
    blocked_titles: list[str] | None = None
    latency_ms = 0
    token_in = 0
    token_out = 0
    faq_hit = False
    llm_called = False

    async for event in ask_stream(
        db, query=query, session_id=session_id, top_k=top_k, ctx=ctx
    ):
        if event.event == "start":
            session_id_out = event.data["session_id"]
            message_id_out = event.data["message_id"]
        elif event.event == "delta":
            parts.append(event.data["content"])
        elif event.event == "citations":
            citations = [CitationItem.model_validate(c) for c in event.data["items"]]
        elif event.event == "blocked":
            blocked_count = event.data["count"]
            blocked_titles = event.data.get("titles")
        elif event.event == "done":
            latency_ms = event.data["latency_ms"]
            token_in = event.data["token_in"]
            token_out = event.data["token_out"]
            faq_hit = event.data["faq_hit"]
            # 老前端不发这个字段时按「调用过」推断 —— 与旧行为一致，不会突变
            llm_called = event.data.get("llm_called", True)
        elif event.event == "error":
            code = event.data["code"]
            message = event.data["message"]
            raise BizError(message, code, _http_status_for(code))
        else:  # pragma: no cover —— 事件名是 Literal，穷尽了
            logger.warning("未知 SSE 事件：%s", event.event)

    return AskResponse(
        session_id=session_id_out,
        message_id=message_id_out,
        content="".join(parts),
        citations=citations or None,
        blocked_count=blocked_count,
        blocked_titles=blocked_titles,
        latency_ms=latency_ms,
        token_in=token_in,
        token_out=token_out,
        faq_hit=faq_hit,
        llm_called=llm_called,
    )


def _http_status_for(code: int) -> int:
    """业务码 → HTTP 状态码。与 `core/exceptions.py` 的映射保持一致。

    ⚠️ 这里**写死数字**而不是 `from fastapi import status` ——
       `engines/` 铁律禁止 import FastAPI。这两个模块的映射关系不常变，
       而违反分层带来的耦合是长期的。
    """
    return {
        ErrorCode.PARAM_ERROR: 400,
        ErrorCode.UNAUTHORIZED: 401,
        ErrorCode.FORBIDDEN: 403,
        ErrorCode.NOT_FOUND: 404,
        ErrorCode.CONFLICT: 409,
        ErrorCode.DEPENDENCY_ERROR: 502,
    }.get(code, 500)


async def search(
    db: AsyncSession,
    *,
    query: str,
    top_k: int,
    ctx: AuthContext,
) -> SearchResponse:
    """只检索不生成（`POST /api/query/search`）。

    ⚠️ **不落 `qa_log`** —— 这个端点不是「提问」，是「看看有什么」。
       落日志会污染 be-08 的聚类输入（同一问题被检索一次、提问一次 = 双计）。
    ⚠️ 同样**不落 `qa_session`**（没有会话概念）。
    """
    result = await retrieval.retrieve(db, query, ctx, top_k)
    return SearchResponse(
        items=result.citations,
        blocked_count=result.blocked_count,
        blocked_titles=result.blocked_titles,
    )
