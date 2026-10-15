"""会话与问答日志的落库 / 读取（be-05 §5.2-⑧）。

三张表的分工与**删除语义**（be-05 §10-105 已拍板）：

| 表 | 写入时机 | 删除会话时 |
|---|---|---|
| `qa_session` | 首问时建；每轮更新 `message_count` / `updated_at` | **删** |
| `qa_message` | 每轮 user + assistant 各一条 | **删** |
| `qa_log` | 每轮一条（全列） | **保留** |

🔴 **`qa_log` 不跟着删**：它是 be-07 看板与 be-08/be-09 挖掘的数据源。
   跟着删的后果是「用户删掉自己的会话，全平台的提问量指标凭空下跌」。
   ⇒ 因此**读 `qa_log` 的一方不得 JOIN `qa_session`**（会话可能已被删）。

🔴 **归属校验**：`get_owned_session` 对「不存在」与「不属于你」返回**同一个** 3001。
   用 403 区分两者，等于告诉攻击者「这个 session_id 是存在的，只是不属于你」——
   一个可枚举的横向越权探针。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import TextClause

from backend.core.exceptions import NotFoundError
from backend.core.ids import new_id
from backend.core.settings import settings
from backend.models.qa_log import QaLog, pack_embedding
from backend.models.qa_message import QaMessage
from backend.models.qa_session import QaSession
from backend.schemas.auth import AuthContext
from backend.schemas.query import ChatMessageVo, CitationItem, SessionBrief, SessionDetail

logger = logging.getLogger(__name__)

__all__ = [
    "TurnLogData",
    "create_session",
    "delete_session",
    "get_owned_session",
    "load_history",
    "persist_turn",
    "session_detail",
    "list_sessions",
    "suggestions",
]

_SESSION_PREFIX = "sess"
_MESSAGE_PREFIX = "msg"
_LOG_PREFIX = "qlog"


@dataclass(slots=True)
class TurnLogData:
    """`qa_log` 的全列（除自动字段外）。

    ⚠️ 做成 dataclass 而不是一长串关键字参数：`qa_log` 有 15 个业务列，
    位置参数写起来必然出错，而**错位不会报错** —— 类型全是 int/str，
    只会安静地写进去，直到看板数字对不上才发现。
    """

    question: str
    #: 768 维问题向量。**必须给**（be-08 聚类的输入），见 `qa_log.py` 头部第 1 条。
    question_embedding: list[float] | None = None
    recalled_ids: list[str] = field(default_factory=list)
    allowed_ids: list[str] = field(default_factory=list)
    blocked_ids: list[str] = field(default_factory=list)
    top_score: float | None = None
    token_in: int = 0
    token_out: int = 0
    latency_ms: int = 0
    faq_hit: bool = False


async def create_session(
    db: AsyncSession, ctx: AuthContext, first_question: str
) -> QaSession:
    """新建会话并提交。标题取首问前 `qa_session_title_chars` 字。

    ⚠️ 用 `" ".join(x.split())` 而不是 `strip().replace("\\n", " ")`：
       后者只处理换行，制表符/连续空格/全角空格会原样留在标题里，
       侧栏渲染出「怎样报销住宿费　　第二行」这种带一串空格的条目。
       `str.split()` 无参时按 **Unicode 空白**切分（含 U+3000 全角空格），
       一次搞定压平与去首尾。
    """
    title = " ".join(first_question.split())[: settings.qa_session_title_chars]
    session = QaSession(
        id=new_id(_SESSION_PREFIX),
        user_id=ctx.user_id,
        # 首问全空白时兜一个占位，避免前端侧栏出现一条没有文字的条目
        title=title or "新会话",
        message_count=0,
    )
    db.add(session)
    await db.commit()
    return session


async def get_owned_session(
    db: AsyncSession, session_id: str, ctx: AuthContext
) -> QaSession:
    """取会话并校验归属。

    Raises:
        NotFoundError: 会话不存在**或**不属于当前用户。两者**刻意不区分**
            （见模块头部）。
    """
    stmt = select(QaSession).where(QaSession.id == session_id)
    session = (await db.scalars(stmt)).one_or_none()
    if session is None or session.user_id != ctx.user_id:
        # ⚠️ 日志里记 user_id 与被请求的 id，但**不区分**是哪种情况 ——
        #    这个区别只应该出现在日志里，不应该出现在响应里。
        logger.warning(
            "会话访问被拒：user_id=%s session_id=%s exists=%s",
            ctx.user_id,
            session_id,
            session is not None,
        )
        raise NotFoundError("会话不存在")
    return session


async def load_history(
    db: AsyncSession, session_id: str, rounds: int | None = None
) -> list[QaMessage]:
    """取会话最近的若干**轮**历史，返回**时间正序**（最早的在前）。

    `rounds` 默认 `settings.qa_history_rounds`。1 轮 = user + assistant 两条，
    所以实际取 `rounds * 2` 条。

    为什么要「取最近的、再翻正序」：直接正序取前 N 条会拿到**最早的** N 条，
    多轮对话里等于给模型看一段远古历史而看不到刚说的话 —— 而这是**静默**的，
    表现只是「模型怎么老答偏」。
    """
    window = (rounds if rounds is not None else settings.qa_history_rounds) * 2
    if window <= 0:
        return []
    # 倒序取最近 window 条，再在 Python 里翻正序（比在 SQL 里套一层子查询直观）
    stmt = (
        select(QaMessage)
        .where(QaMessage.session_id == session_id)
        .order_by(QaMessage.created_at.desc(), QaMessage.id.desc())
        .limit(window)
    )
    recent = list((await db.scalars(stmt)).all())
    recent.reverse()
    return recent


async def persist_turn(
    db: AsyncSession,
    *,
    session: QaSession,
    ctx: AuthContext,
    question: str,
    message_id: str,
    answer: str,
    citations: list[CitationItem],
    blocked_count: int,
    log: TurnLogData,
    commit: bool = True,
) -> None:
    """一轮问答的三表落库（be-05 §10-106：**同步、一次事务**）。

    Args:
        message_id: assistant 消息的 id。**由调用方在 `start` 事件之前生成**
            （前端 `SseStart.message_id` 要用它），这里只负责落库 ——
            因此本函数不返回 id。
        commit: 传 `False` 时只 `flush` 不 `commit`，留给调用方控制事务边界。
            `ask()` 折叠路径用得上。

    落库顺序上，user 消息的 `created_at` 与 assistant 的相同（同一事务、
    同一 `server_default=now()`），排序靠 `id` 兜底 —— 因此 `load_history`
    的 `order_by` 带了 `id` 作为次序键。
    """
    db.add(
        QaMessage(
            id=new_id(_MESSAGE_PREFIX),
            session_id=session.id,
            role=QaMessage.ROLE_USER,
            content=question,
            citations=None,
            blocked_count=0,
        )
    )
    db.add(
        QaMessage(
            id=message_id,
            session_id=session.id,
            role=QaMessage.ROLE_ASSISTANT,
            content=answer,
            # `model_dump(mode="json")` 让 datetime 等类型可 JSON 序列化；
            # 这里只有 str/float，但保持与 SSE 一致的写法，避免将来加字段时踩坑。
            citations=[c.model_dump(mode="json") for c in citations] or None,
            blocked_count=blocked_count,
        )
    )
    db.add(
        QaLog(
            id=new_id(_LOG_PREFIX),
            session_id=session.id,
            user_id=ctx.user_id,
            # **自身**部门，不是含祖先的 `depts` —— 归属口径与权限口径不能混
            department_id=ctx.department_id,
            question=log.question,
            question_embedding=pack_embedding(log.question_embedding),
            recalled_ids=log.recalled_ids,
            allowed_ids=log.allowed_ids,
            blocked_ids=log.blocked_ids,
            top_score=log.top_score,
            token_in=log.token_in,
            token_out=log.token_out,
            latency_ms=log.latency_ms,
            faq_hit=log.faq_hit,
        )
    )

    session.message_count += 2
    session.updated_at = func.now()

    if commit:
        await db.commit()
    else:
        await db.flush()


async def list_sessions(
    db: AsyncSession, ctx: AuthContext, page: int, page_size: int
) -> tuple[list[QaSession], int]:
    """分页取当前用户的会话，按 `updated_at` 倒序（最近问过的在前）。"""
    base = select(QaSession).where(QaSession.user_id == ctx.user_id)
    total = (
        await db.scalar(
            select(func.count()).select_from(base.subquery())
        )
    ) or 0
    stmt = (
        base.order_by(QaSession.updated_at.desc(), QaSession.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list((await db.scalars(stmt)).all()), int(total)


async def session_detail(
    db: AsyncSession, session_id: str, ctx: AuthContext, limit: int
) -> SessionDetail:
    """取单个会话的最近 `limit` 条消息（**正序**返回）。

    ⚠️ `limit` 限制的是「最近多少条」而不是「最早多少条」，理由同 `load_history`。
    """
    session = await get_owned_session(db, session_id, ctx)
    stmt = (
        select(QaMessage)
        .where(QaMessage.session_id == session_id)
        .order_by(QaMessage.created_at.desc(), QaMessage.id.desc())
        .limit(limit)
    )
    recent = list((await db.scalars(stmt)).all())
    recent.reverse()

    messages: list[ChatMessageVo] = []
    for item in recent:
        citations = None
        if item.citations:
            # 历史里的 citations 是当初落库的 dict；这里重新过一遍 DTO，
            # 让「库里存了脏数据」在**读取时**就暴露，而不是原样吐给前端。
            citations = [
                CitationItem.model_validate(c)
                for c in item.citations
                if isinstance(c, dict)
            ]
        messages.append(
            ChatMessageVo(
                id=item.id,
                session_id=item.session_id,
                role=item.role,  # type: ignore[arg-type]
                content=item.content,
                citations=citations or None,
                blocked_count=item.blocked_count,
                created_at=item.created_at,
            )
        )

    return SessionDetail(
        id=session.id,
        title=session.title or "",
        message_count=session.message_count,
        messages=messages,
    )


async def delete_session(db: AsyncSession, session_id: str, ctx: AuthContext) -> None:
    """删除会话及其消息。**`qa_log` 不动**（见模块头部）。

    🔴 必须是**真删除**（be-05 §10-105）。`query_router.py` 原先的 docstring
       写的是「清空指定会话的上下文记忆」，那会让侧边栏删不掉条目 ——
       而前端的确认框文案是「删除后不可恢复」。
    """
    session = await get_owned_session(db, session_id, ctx)

    # 先 `.all()` 物化再遍历：边遍历结果集边发 DELETE，会让「正在读的行」
    # 与「正在删的行」重叠，在流式游标的实现下可能漏删中间几条 ——
    # 而漏删是静默的（会话没了、消息还在，孤儿行只在下次统计时才暴露）。
    messages = list(
        (await db.scalars(select(QaMessage).where(QaMessage.session_id == session_id))).all()
    )
    for message in messages:
        await db.delete(message)
    await db.delete(session)
    await db.commit()
    logger.info("已删除会话 %s（qa_log 保留）", session_id)


async def suggestions(
    db: AsyncSession, ctx: AuthContext, limit: int
) -> list[str]:
    """当前用户的**高频问题**（be-05 §10-23）。

    🔴 **只取自己的**。做全局热词的后果是把他人的提问文本展示给所有人 ——
       提问里可能含敏感信息（「XX 项目的报价是多少」），而这是一条 PRD
       从未评估过的泄漏面。全局源要等 be-07 看板侧有了明确的脱敏口径再说。

    统计口径：按问题原文分组取频次，取近 `mining_lookback_days` 天。
    """
    stmt = (
        select(QaLog.question, func.count().label("freq"))
        .where(
            QaLog.user_id == ctx.user_id,
            QaLog.question.is_not(None),
            QaLog.created_at >= func.date_sub(func.now(), _interval_days(settings.mining_lookback_days)),
        )
        .group_by(QaLog.question)
        .order_by(func.count().desc(), QaLog.question.asc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [row[0] for row in rows if isinstance(row[0], str) and row[0].strip()]


def _interval_days(days: int) -> TextClause:
    """`INTERVAL n DAY` 字面量。

    ⚠️ 必须用 `text()` 而不是传裸字符串：裸字符串会被 SQLAlchemy 渲染成
       **绑定参数**（`date_sub(now(), %s)`），而 MySQL 的 INTERVAL 语法位
       不接受占位符 —— 结果是运行期语法错误。

    ⚠️ 这也是全模块**唯一**一处把数字拼进 SQL 的地方，故在此 `int()` 强转，
       保证它不可能带上任何用户输入（`days` 的来源是 settings，不是请求）。
       这不违反「禁 f-string 拼 SQL」：那条针对的是**用户数据**，
       而这里既无用户数据、又无参数化写法可用。
    """
    return text(f"INTERVAL {int(days)} DAY")
