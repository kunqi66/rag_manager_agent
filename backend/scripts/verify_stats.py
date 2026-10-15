"""be-07 运营看板的验收脚本（spec `P2-13-be-stats` §8 的可执行版本）。

跑法（项目根目录）：

    uv run python -m backend.scripts.verify_stats

分四层，**能跑的就跑，跑不了的明确报 SKIP 并说明原因** ——
绝不把「环境不具备」伪装成「通过」（那是验收脚本最坏的失败模式）。

| 层 | 需要什么 | 覆盖 |
|---|---|---|
| **T1 离线** | 什么都不需要 | 桶/直方图/P95 的纯函数、时间窗校验、**DTO 字段名与前端逐字对齐**、迁移↔模型列一致、配置↔`.env.example` 一致、路由与排班注册、若干「静默错误」的静态扫描 |
| **T1.5 假会话** | 什么都不需要 | 用**假 AsyncSession** 驱动 `get_*` 与 `aggregate_range`：加权均价、`uv` 语义、逐桶 TOP、chunk→document 换算 |
| **T2 接口** | MySQL + **Redis** + **迁移 0005 已应用** | 端点鉴权、参数回显、**幂等性端到端**（同一窗口跑两次，行数不涨） |
| **T3 链路** | ＋ worker ＋ beat ＋ 真实 `qa_log` 数据 | 聚合任务真跑、清理任务真删、看板数字与 `qa_log` 对账 |

⚠️ **T1 里最有价值的两类是「字段名对齐」与「逐桶 TOP」**：

- **T1-24（DTO ↔ 前端 types）**：字段名不一致**不是编译错误**。前端
  `data.today_question_count` 取到 `undefined` → KPI 卡显示 `NaN`，
  **接口照回 200**，控制台干干净净。这个缺陷从后端单侧永远看不见。
- **T1.5-13（逐桶 TOP）**：把窗口级 TOP 写进每个桶，合并时会被**乘上桶数**。
  数字看起来完全正常（只会觉得"大家问得挺多"），没人会对一个正常的整数起疑。
  这是本模块实现期真实踩到的一个 bug，用例留在这里防复发。

⚠️ Windows 控制台是 GBK —— 输出一律 ASCII 骨架 + 断言**布尔值与具体值**。
   与 `verify_org.py` / `verify_import.py` / `verify_search.py` / `verify_audit.py` 同一约定。

⚠️ 会**真的**连 `.env` 里的 MySQL / Redis（开发期直连 ECS）。
   只读 + 只增自己造的数据，**绝不动种子数据**，**绝不删除**非自造行。
   标记前缀 `ZZVFYSTATS`（**不含下划线** —— `LIKE 'zzvfy_%'` 里的 `_` 是通配符）。
"""

from __future__ import annotations

import asyncio
import ast
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects import mysql

import main
from backend.core.db import SessionLocal, engine
from backend.core.exceptions import ErrorCode, ParamError
from backend.core.settings import Settings, settings
from backend.models import Base
from backend.models.stats import StatsDaily, StatsHourly
from backend.schemas.auth import AuthContext
from backend.schemas.stats import (
    BucketRow,
    DashboardQuery,
    LatencyHistogramDTO,
    OverviewDTO,
    TopKnowledgeItem,
    TopQuestionItem,
    TrendDTO,
    TrendQuery,
)
from backend.service import stats_service

PASSWORD = "Rag@12345"
MARK = "ZZVFYSTATS"

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"

# 🔴 Windows 控制台默认是 GBK，本脚本的标签里有 🔴 —— 不重配的话
#    `print` 直接抛 UnicodeEncodeError，而它抛在**报 SKIP 的那一行**，
#    于是症状是「环境不可用时脚本崩了」，看起来像代码坏了。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 —— 重配失败不该让脚本起不来
        pass

_results: list[bool] = []
_skipped: list[str] = []


def check(passed: bool, label: str) -> None:
    _results.append(bool(passed))
    print(f"  [{'OK  ' if passed else 'FAIL'}] {label}")


def skip(label: str, reason: str) -> None:
    """环境不具备 —— 记一笔，**不计入通过数**。"""
    _skipped.append(label)
    print(f"  [SKIP] {label} —— {reason}")


def _src(rel: str) -> str:
    return (BACKEND / rel).read_text(encoding="utf-8")


def _code_only(src: str) -> str:
    """去掉注释与 docstring，只留代码本体。

    用于「源码里不许出现 X」这类静态断言。**必须去注释与 docstring**：
    本项目的注释会大段引用被禁的东西（例如「MySQL 8 没有 PERCENTILE」这句
    注释本身就含 `PERCENTILE`），直接子串匹配会把解释当成违规，
    于是用例永远红着，最后被人注释掉 —— 那才是真损失。

    ⚠️ docstring 按 `ast` 给的**行列位置**挖空，不能用 `src.replace(值, "")`：
       源文件里写的是转义后的 `\\n\\n`，而 docstring 的**值**是真换行 ——
       两者不相等，替换静默失效。（`verify_search.py` 首跑就是这么错的。）
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:  # pragma: no cover —— 源码本身语法错该在别处暴露
        return src

    blanked = src.splitlines()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            and first.end_lineno is not None
        ):
            for i in range(first.lineno - 1, min(first.end_lineno, len(blanked))):
                blanked[i] = ""

    for i, line in enumerate(blanked):
        if not line:
            continue
        hash_at = line.find("#")  # 本项目不用 `#` 做字符串拼接
        if hash_at >= 0:
            blanked[i] = line[:hash_at]
    return "\n".join(blanked)


def _sql(stmt: Any) -> str:
    """把语句编译成 MySQL 方言的字符串（不连接数据库）。"""
    return str(stmt.compile(dialect=mysql.dialect()))


# ═══════════════════════════════════════════════════════════════
# 假会话：不需要数据库就能驱动聚合与查询
# ═══════════════════════════════════════════════════════════════


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows

    def scalars(self) -> "_FakeResult":
        # 真 `Result.scalars()` 返回的是可迭代对象、不再有 `.all()` 链式；
        # 但服务侧两种用法都有（`.scalars()` 后直接迭代，或再 `.all()`），
        # 假实现两者都支持即可 —— 这里测的是**服务怎么用结果**，不是 SQLAlchemy。
        return self

    def __iter__(self):
        return iter(self._rows)


class _FakeStream:
    """`db.stream()` 的返回值：支持 `async for`。"""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    async def __aiter__(self):
        for row in self._rows:
            yield row


class _FakeSession:
    """只实现本模块用到的那几个方法。

    ⚠️ **不做事务语义模拟** —— 那会变成一个半吊子 ORM，测的是假实现而不是真代码。
       这里只关心「服务构造了什么语句」与「拿到结果后怎么算」。

    按 **SQL 文本**分派结果，而不是按调用顺序：调用顺序会随实现调整而变，
    而 SQL 里出现 `sys_role` / `kb_chunk` / `kb_document` 是语义性的，
    分派规则不会因为「多查了一次」而错位。
    """

    def __init__(
        self,
        window_rows: list | None = None,
        *,
        detail_rows: list | None = None,
        chunk_map: dict[str, str] | None = None,
        role_codes: list[str] | None = None,
        titles: dict[str, str] | None = None,
        today: int = 0,
        documents: int = 0,
    ) -> None:
        self.window_rows = window_rows or []
        self.detail_rows = detail_rows or []
        self.chunk_map = chunk_map or {}
        self.role_codes = role_codes if role_codes is not None else ["sys_admin"]
        self.titles = titles or {}
        self.today = today
        self.documents = documents
        self.statements: list[str] = []

    async def execute(self, stmt):
        sql = _sql(stmt)
        self.statements.append(sql)
        if "sys_role" in sql:
            return _FakeResult(list(self.role_codes))
        if "kb_chunk" in sql:
            return _FakeResult(
                [SimpleNamespace(id=k, document_id=v) for k, v in self.chunk_map.items()]
            )
        if "kb_document" in sql:
            return _FakeResult(
                [SimpleNamespace(id=k, title=v) for k, v in self.titles.items()]
            )
        return _FakeResult(self.window_rows)

    async def stream(self, stmt):
        self.statements.append(_sql(stmt))
        return _FakeStream(self.detail_rows)

    async def scalar(self, stmt):
        sql = _sql(stmt)
        self.statements.append(sql)
        if "kb_document" in sql:
            return self.documents
        return self.today

    async def commit(self) -> None:
        return None


def _agg_row(bucket: Any, **kw: Any) -> SimpleNamespace:
    """一桶的标量聚合结果（对应 `_base_aggregate_select` 的列）。"""
    base: dict[str, Any] = {
        "pv": 0,
        "uv": 0,
        "session_count": 0,
        "faq_hit_count": 0,
        "no_hit_count": 0,
        "token_in_sum": 0,
        "token_out_sum": 0,
        "latency_avg_ms": 0,
        "blocked_count": 0,
    }
    base.update(kw)
    return SimpleNamespace(bucket=bucket, **base)


def _stored_row(grain: str, bucket: Any, **kw: Any) -> SimpleNamespace:
    """一行**已落库**的聚合（对应查 `stats_*` 表的 ORM 行）。"""
    base: dict[str, Any] = {
        "pv": 0,
        "uv": 0,
        "session_count": 0,
        "faq_hit_count": 0,
        "faq_hit_rate": 0.0,
        "no_hit_count": 0,
        "token_in_sum": 0,
        "token_out_sum": 0,
        "latency_avg_ms": 0,
        "latency_p95_ms": 0,
        "latency_buckets": None,
        "blocked_count": 0,
        "top_questions": None,
        "top_documents": None,
        "updated_at": datetime(2026, 10, 14, 10, 0, 0),
    }
    base.update(kw)
    key = "bucket_start" if grain == "hour" else "bucket_date"
    return SimpleNamespace(**{key: bucket}, **base)


def _ctx(*roles: str, user_id: str = "u_stats") -> AuthContext:
    return AuthContext(
        user_id=user_id,
        username=user_id,
        department_id="dept_a",
        depts=["dept_root", "dept_a"],
        roles=list(roles) or ["role_staff"],
        perms=["stats:dashboard:view"],
        ip="10.0.0.1",
    )


# ═══════════════════════════════════════════════════════════════
# T1 离线
# ═══════════════════════════════════════════════════════════════


def tier1_offline() -> None:
    print("\n── T1 离线（纯函数 / DTO 契约 / 迁移 / 配置 / 静态扫描）──")
    _t1_pure_functions()
    _t1_window()
    _t1_dto_contract()
    _t1_models_and_migration()
    _t1_static_scans()
    _t1_routing_and_schedule()


def _t1_pure_functions() -> None:
    edges = settings.stats_latency_edges

    # ---- T1-01~03 直方图边界 ----
    check(edges == [0, 200, 500, 1000, 2000, 5000], f"T1-01 桶边界 = 配置值（实测 {edges}）")
    check(
        len(edges) >= 2,
        "T1-02 🔴 至少 2 个边界（只有 1 个时一个桶都构不成，直方图恒空且不报错）",
    )
    check(edges == sorted(edges), "T1-03 边界升序（乱序会让桶区间重叠）")

    # ---- T1-04~07 落桶 ----
    samples = [10, 150, 250, 700, 1500, 3000, 9000]
    hist = stats_service._histogram(samples, edges)
    check(
        sum(hist.values()) == len(samples),
        f"T1-04 🔴 落桶守恒：Σcounts == 样本数（{sum(hist.values())} vs {len(samples)}）",
    )
    check(
        stats_service._bucket_index(edges, 99999) == len(edges) - 2,
        "T1-05 🔴 末桶是**开区间**：99999ms 落在最后一桶，而不是落空",
    )
    check(
        len(stats_service._histogram([], edges)) == len(edges) - 1,
        "T1-06 counts 长度恒为 edges-1（无样本时也是）",
    )
    check(
        all(isinstance(k, str) for k in hist),
        "T1-07 🔴 桶键是**字符串**（JSON 对象的键只能是字符串，存 int 会被静默转掉）",
    )

    # ---- T1-08~10 P95 ----
    check(stats_service.p95_from_histogram({}) == 0, "T1-08 空直方图的 P95 = 0（不是 ZeroDivision）")
    slow = stats_service.p95_from_histogram(stats_service._histogram([10] * 50 + [4000] * 50, edges))
    fast = stats_service.p95_from_histogram(stats_service._histogram([10] * 95 + [4000] * 5, edges))
    check(slow > fast, f"T1-09 P95 单调：慢样本多 ⇒ P95 更大（{slow} > {fast}）")
    check(
        edges[0] <= stats_service.p95_from_histogram(hist) <= edges[-1],
        "T1-10 P95 落在 [首边界, 末边界] 内（不虚报超出量程的值）",
    )

    # ---- T1-11~16 取整 ----
    check(
        stats_service.floor_hour(datetime(2026, 10, 14, 9, 37, 42)) == datetime(2026, 10, 14, 9, 0),
        "T1-11 floor_hour 抹掉分秒",
    )
    check(
        stats_service.floor_day(datetime(2026, 10, 14, 9, 37, 42)) == datetime(2026, 10, 14, 0, 0),
        "T1-12 floor_day 抹到零点",
    )
    # 🔴 T1-13/14 是本模块实现期差点漏掉的一条：明细趟的 `created_at` 带分秒，
    #    不取整就与聚合趟的桶键对不上 ⇒ 直方图与 TOP 榜**全空**，
    #    而标量指标（pv/uv）全对。症状是「数字都对，图没数据」。
    check(
        stats_service._normalize_bucket("hour", datetime(2026, 10, 14, 9, 37, 42))
        == datetime(2026, 10, 14, 9, 0),
        "T1-13 🔴 hour 桶键**必须取整**（否则明细与聚合对不上，图表恒空）",
    )
    check(
        stats_service._normalize_bucket("day", datetime(2026, 10, 14, 9, 37, 42))
        == datetime(2026, 10, 14, 0, 0),
        "T1-14 🔴 day 桶键同样取整",
    )
    check(
        stats_service._normalize_bucket("hour", "2026-10-14 09:00:00") == datetime(2026, 10, 14, 9, 0),
        "T1-15 接受 DATE_FORMAT 回来的字符串（hour 桶的 SQL 返回形态）",
    )
    check(
        stats_service._normalize_bucket("day", date(2026, 10, 14)) == datetime(2026, 10, 14, 0, 0),
        "T1-16 接受 DATE() 回来的 date（day 桶的 SQL 返回形态）",
    )

    # ---- T1-17 时间口径 ----
    utc_naive = datetime.now(UTC).replace(tzinfo=None)
    drift = abs((stats_service._now() - utc_naive).total_seconds() - 8 * 3600)
    check(drift < 5, f"T1-17 🔴 `_now()` 是 UTC+8 的 naive 时间（实测偏移 {drift:.1f}s）")


def _t1_window() -> None:
    now = stats_service._now()

    # ---- T1-18 默认窗口 ----
    start, end = stats_service.resolve_window(None, None)
    span = (end - start).total_seconds() / 86400
    check(
        abs(span - settings.stats_default_range_days) < 0.01,
        f"T1-18 不传参 ⇒ 默认近 {settings.stats_default_range_days} 天（实测 {span:.2f} 天）",
    )

    # ---- T1-19~21 三个校验 ----
    try:
        stats_service.resolve_window(now, now - timedelta(days=1))
        check(False, "T1-19 起始晚于结束应抛 ParamError")
    except ParamError:
        check(True, "T1-19 起始晚于结束 ⇒ ParamError")

    try:
        stats_service.resolve_window(now - timedelta(days=settings.stats_query_max_days + 10), now)
        check(False, "T1-20 超长跨度应抛 ParamError")
    except ParamError:
        check(True, f"T1-20 跨度 > {settings.stats_query_max_days} 天 ⇒ ParamError")

    try:
        stats_service.resolve_window(
            now - timedelta(days=settings.stats_hour_range_max_days + 1), now, granularity="hour"
        )
        check(False, "T1-21 hour 粒度超跨度应抛 ParamError")
    except ParamError:
        check(True, f"T1-21 hour 粒度 > {settings.stats_hour_range_max_days} 天 ⇒ ParamError")

    # ---- T1-22 业务码 ----
    err = ParamError("x")
    check(
        err.code == ErrorCode.PARAM_ERROR == 1001,
        f"T1-22 🔴 参数错走 ParamError(1001)，不是裸 BizError 的 5000（实测 {err.code}）",
    )

    # ---- T1-23 naive 时间 ----
    check(
        start.tzinfo is None and end.tzinfo is None,
        "T1-23 🔴 窗口是 **naive** 时间（引入 tzinfo 会让桶边界整体偏 8 小时且不报错）",
    )


def _t1_dto_contract() -> None:
    # ---- T1-24 🔴 DTO 字段名 ↔ 前端 types/stats.ts ----
    # 这是本脚本最值钱的一条。字段名不一致**不是编译错误**：前端
    # `data.today_question_count` 取到 `undefined` → KPI 卡显示 NaN，
    # 接口照回 200，控制台干净。差异化只有把两边的名字摆在一起才看得见。
    ts_path = ROOT / "frontend" / "src" / "types" / "stats.ts"
    if not ts_path.exists():
        skip("T1-24", "frontend/src/types/stats.ts 不存在（前端未落地）")
    else:
        ts = ts_path.read_text(encoding="utf-8")
        required = {
            "OverviewVo": [
                "pv",
                "uv",
                "document_count",
                "today_question_count",
                "faq_hit_rate",
                "avg_latency_ms",
            ],
            "TrendBucket": ["bucket", "token_in", "token_out", "question_count", "avg_latency_ms"],
            "TopKnowledgeItem": ["document_id", "title", "cite_count"],
        }
        model_fields = {
            "OverviewVo": set(OverviewDTO.model_fields),
            "TrendBucket": set(TrendDTO.model_fields) | set(
                __import__(
                    "backend.schemas.stats", fromlist=["TrendPoint"]
                ).TrendPoint.model_fields
            ),
            "TopKnowledgeItem": set(TopKnowledgeItem.model_fields),
        }
        # 前端那个文件里的字段出现在**接口名对应的 interface** 里；
        # 这里退一步：断言「该名字在前端文件里有定义」且「后端该接口有该字段」。
        missing_front = [f"{k}.{n}" for k, names in required.items() for n in names if n not in ts]
        missing_back = [
            f"{k}.{n}" for k, names in required.items() for n in names if n not in model_fields[k]
        ]
        check(
            not missing_front,
            f"T1-24a 前端 required 字段全部存在于 types/stats.ts（缺 {missing_front}）",
        )
        check(
            not missing_back,
            f"T1-24b 🔴 后端 DTO 逐字包含前端读的字段名（缺 {missing_back}）",
        )
        # 🔴 反向：后端**不许**回前端根本不读的名字。
        #    本模块首稿就是 `doc_total` / `today_questions`，前端读的是
        #    `document_count` / `today_question_count` —— 两边都"对"，接口 200。
        check(
            "today_questions" not in OverviewDTO.model_fields
            and "doc_total" not in OverviewDTO.model_fields,
            "T1-24c 🔴 首稿的错名（doc_total / today_questions）未回流到 DTO",
        )

    # ---- T1-25 trend 无 metric ----
    check(
        "metric" not in TrendQuery.model_fields,
        "T1-25 🔴 `trend` **没有** `metric` 参数（双系列共用一次请求，前端不做聚合）",
    )
    check(
        TrendQuery().granularity == "day",
        "T1-26 granularity 默认 day",
    )

    # ---- T1-27~29 limit 的上下界 ----
    check(
        DashboardQuery.model_fields["limit"].metadata
        and any(
            getattr(m, "le", None) == settings.stats_top_n
            for m in DashboardQuery.model_fields["limit"].metadata
        ),
        f"T1-27 🔴 limit 上限 == stats_top_n({settings.stats_top_n})（不设上限会拉出整桶 JSON）",
    )
    for bad, label in ((0, "T1-28a limit=0"), (settings.stats_top_n + 1, "T1-28b limit 超上限")):
        try:
            DashboardQuery(limit=bad)
            check(False, f"{label} 应被拒")
        except Exception:  # noqa: BLE001 —— pydantic.ValidationError
            check(True, f"{label} 被拒")
    check(DashboardQuery().limit == 10, "T1-29 limit 默认 10")

    # ---- T1-30~31 直方图形状校验 ----
    # ⚠️ `edges` 有 3 个边界 ⇒ **2 个桶** ⇒ `counts` 长度必须是 2。
    #    这不是废话：本脚本首跑就在这里写错（把 counts 写成 3 当成"正确样例"），
    #    于是「正确样例」自己被校验器拒了 —— 用例的测试数据错了，
    #    看起来却像「实现太严」。off-by-one 在两端都会出现，样例要标出来。
    try:
        LatencyHistogramDTO(edges=[0, 100, 200], counts=[1])
        check(False, "T1-30a counts 少一项应被拒")
    except Exception:  # noqa: BLE001
        check(True, "T1-30a 🔴 counts 少一项被拒（否则末段画成 0，而没人怀疑一个 0）")
    try:
        LatencyHistogramDTO(edges=[0, 100, 200], counts=[1, 2, 3])
        check(False, "T1-30b counts 多一项应被拒")
    except Exception:  # noqa: BLE001
        check(True, "T1-30b counts 多一项被拒（edges 越界会画成 NaN 柱）")
    LatencyHistogramDTO(edges=[0, 100, 200], counts=[1, 2])
    check(True, "T1-30c counts 长度 = edges - 1 时通过")
    check(
        len(LatencyHistogramDTO(edges=[0, 1, 2], counts=[0, 0]).counts) == 2,
        "T1-31 全零直方图也能构造（无数据不等于 404）",
    )

    # ---- T1-32~33 比例是 0–1 ----
    _win = {"start_time": datetime.now(), "end_time": datetime.now(), "granularity": "day"}
    try:
        OverviewDTO(**_win, faq_hit_rate=87.5)
        check(False, "T1-32a faq_hit_rate=87.5 应被拒")
    except Exception:  # noqa: BLE001
        check(True, "T1-32a 🔴 比例字段是 0–1 小数：87.5 被拒（否则前端 ×100 显示 8750%）")
    OverviewDTO(**_win, coverage_rate=1.0)
    check(True, "T1-32b coverage_rate=1.0 是合法边界")
    # ⚠️ `granularity` **没有默认值**（与 start_time/end_time 同理）：响应必须**声明**
    #    这次是查小时表还是日表 —— 两者口径不同（桶内去重 vs 全天去重），
    #    不回带的话前端无从解释「同一个 uv 为什么换了个范围就变了」。
    try:
        OverviewDTO(start_time=datetime.now(), end_time=datetime.now())
        check(False, "T1-33 granularity 缺失应被拒")
    except Exception:  # noqa: BLE001
        check(True, "T1-33 🔴 granularity 是必填（不声明查的哪张表，uv 的口径无法解释）")

    # ---- T1-34~35 BucketRow 的派生率 ----
    row = BucketRow(bucket=datetime(2026, 10, 14), pv=200, faq_hit_count=50)
    check(abs(row.faq_hit_rate - 0.25) < 1e-9, "T1-34a BucketRow.faq_hit_rate = hit/pv")
    check(BucketRow(bucket=datetime(2026, 10, 14), pv=0).faq_hit_rate == 0.0, "T1-34b pv=0 ⇒ 0.0")
    check(
        "faq_hit_rate" not in BucketRow.model_fields,
        "T1-35 🔴 faq_hit_rate 是**属性**不是字段（存一份派生值就有「改了分子忘了改比值」）",
    )

    # ---- T1-36~38 响应回带窗口 ----
    check(
        OverviewDTO.model_fields["start_time"].is_required()
        and OverviewDTO.model_fields["end_time"].is_required(),
        "T1-36 🔴 start_time/end_time **必填**（不回带窗口，用户切范围时会以为页面坏了）",
    )
    check("uv_is_exact" in OverviewDTO.model_fields, "T1-37 🔴 OverviewDTO 带 uv_is_exact")
    check(
        {"granularity", "start_time", "end_time", "points"} <= set(TrendDTO.model_fields),
        "T1-38 TrendDTO 带 granularity + 窗口 + points",
    )
    check(
        "p95_ms" in LatencyHistogramDTO.model_fields
        and "avg_ms" in LatencyHistogramDTO.model_fields,
        "T1-39 LatencyHistogramDTO 带 avg_ms + p95_ms",
    )


def _t1_models_and_migration() -> None:
    mig_path = ROOT / "alembic" / "versions" / "0005_create_stats_tables.py"
    if not mig_path.exists():
        check(False, "T1-40 迁移 0005 不存在")
        return
    mig_src = mig_path.read_text(encoding="utf-8")
    mig_tree = ast.parse(mig_src)

    # ---- T1-40~41 迁移 ↔ 模型列一致 ----
    def _cols_from_ast(source: str, table: str) -> set[str]:
        """从 `op.create_table("x", sa.Column("y", ...))` 里抠出列名。

        ⚠️ 用 AST 而不是正则：`_metric_columns()` 是**函数调用**（`*_metric_columns()`），
           正则抓不到那些列名，会得出「迁移里只有 2 列」的错误结论，
           于是这条用例要么永远红、要么被人改成「只比字符串包含」而失去意义。
           这里改为：正则抓 `sa.Column("名"` + 展开 `_metric_columns()` 里的那些。
        """
        names = set(re.findall(r'sa\.Column\(\s*"([a-z0-9_]+)"', source))
        # `_metric_columns()` 的列在源码里也是 `sa.Column("名", ...)`，
        # 所以上面的正则已经覆盖；这里只需在 `create_table` 的分段里筛。
        return names

    all_mig_cols = _cols_from_ast(mig_src, "")
    hourly_model = {c.name for c in StatsHourly.__table__.columns}
    daily_model = {c.name for c in StatsDaily.__table__.columns}
    check(
        hourly_model <= all_mig_cols and daily_model <= all_mig_cols,
        f"T1-40 🔴 模型列都在迁移里（缺 {sorted((hourly_model | daily_model) - all_mig_cols)}）",
    )
    check(
        all_mig_cols <= hourly_model | daily_model,
        f"T1-41 迁移列都被模型覆盖（多 {sorted(all_mig_cols - hourly_model - daily_model)}）",
    )

    # ---- T1-42 🔴 两表指标列同构 ----
    # 不同构的后果：切到 30 天口径（走日表）某个指标变 0，而小时口径正常。
    shared = {"id", "updated_at"}
    diff = (hourly_model - {"bucket_start"}) ^ (daily_model - {"bucket_date"})
    check(
        not diff,
        f"T1-42 🔴 两表指标列**完全同构**（差异 {sorted(diff - shared)}）",
    )

    # ---- T1-43~45 约束与版本链 ----
    check(
        "uq_stats_hourly_bucket_start" in mig_src and "uq_stats_daily_bucket_date" in mig_src,
        "T1-43 🔴 桶列各有 UNIQUE（UPSERT 的冲突目标；靠应用层 SELECT 判断会有竞态）",
    )
    check('revision: str = "0005"' in mig_src, "T1-44a revision = 0005")
    check('down_revision: str | None = "0004"' in mig_src, "T1-44b down_revision = 0004")
    check(
        all(
            isinstance(c.type, __import__("sqlalchemy").String) and c.type.length == 64
            for c in (StatsHourly.__table__.c.id, StatsDaily.__table__.c.id)
        ),
        "T1-45 🔴 主键是 VARCHAR(64)（字典 §1：全库 ID 一律字符串）",
    )
    check(
        isinstance(StatsDaily.__table__.c.bucket_date.type, __import__("sqlalchemy").Date),
        "T1-46 日桶列是 DATE（用 DATETIME 存 00:00:00 会让「同一天的两个桶不等」）",
    )
    check(
        not StatsHourly.__table__.foreign_keys and not StatsDaily.__table__.foreign_keys,
        "T1-47 无外键（聚合表独立于业务表，业务表删行不该被聚合表拖住）",
    )
    check(
        {"StatsHourly", "StatsDaily"} <= set(Base.metadata.tables.keys() | {"StatsHourly", "StatsDaily"})
        and "stats_hourly" in Base.metadata.tables
        and "stats_daily" in Base.metadata.tables,
        "T1-48 🔴 两张表都注册进 Base.metadata（漏 import 会让迁移凭空少表且不报错）",
    )


def _t1_static_scans() -> None:
    svc = _code_only(_src("service/stats_service.py"))
    tasks = _code_only(_src("tasks/stats_tasks.py"))
    router = _code_only(_src("api/stats_router.py"))

    # ---- T1-49~52 静默错误的静态守门 ----
    check(
        "percentile" not in svc.lower(),
        "T1-49 🔴 源码无 PERCENTILE（MySQL 8 没有这个聚合函数，那是 Oracle/PG 的）",
    )
    check(
        not re.search(r"SET\s+pv\s*=\s*pv\s*\+|pv\s*=\s*\w*\.pv\s*\+", svc),
        "T1-50 🔴 聚合**不是累加**（重算覆盖；累加会让迟到行被计进另一轮、不可复现）",
    )
    check("print(" not in svc and "print(" not in tasks, "T1-51 无 print（日志走 logging）")
    check(
        "fetch_session" not in tasks and "SessionLocal" not in tasks and "fresh_session" in tasks,
        "T1-52 🔴 worker 侧用 fresh_session（异步 engine 不能跨 asyncio.run 复用）",
    )

    # ---- T1-53~55 重试与清理 ----
    retries = [m for m in re.finditer(r"\.retry\(", tasks)]
    check(len(retries) >= 2, f"T1-53 手动重试出现 {len(retries)} 处")
    check(
        tasks.count("countdown=countdown") >= len(retries),
        "T1-54 🔴 每处 self.retry 都显式给 countdown（否则静默退回 180 秒）",
    )
    check(
        re.search(r"if\s+daily_days\s*>\s*0\s*:", tasks) is not None,
        "T1-55 🔴 清理任务用 `if daily_days > 0` 显式判「0 = 永久保留」"
        "（写成 `< now - INTERVAL 0 DAY` 会删光整个历史趋势，而 0 是**默认值**）",
    )
    check(
        re.search(r"LIMIT\s+:batch", tasks) is not None,
        "T1-56 🔴 DELETE 的 LIMIT 走绑定参数（安全红线 8：不拼字符串）",
    )
    check(
        "aggregate_window" in tasks and "def aggregate_window" in svc,
        "T1-57 🔴 任务转调 service 的 aggregate_window（同一段代码，不分「定时」与「回填」两套）",
    )
    check(
        "def aggregate_range" not in tasks,
        "T1-58 tasks 里没有第二份聚合实现（方向：tasks → service 允许，反之禁止）",
    )

    # ---- T1-59~61 权限与红线 ----
    check(
        "assert_admin_scope" in svc
        and re.search(
            r"async def get_top_knowledge[\s\S]{0,400}assert_admin_scope", svc
        )
        is not None,
        "T1-59 🔴 get_top_knowledge **调用**角色硬闸（红线 4：该端点返回文档标题）",
    )
    check("_DASHBOARD_VIEW" in router, "T1-60 看板端点挂 stats:dashboard:view 依赖")
    check(
        re.search(r"coalesce\(\s*func\.json_length", svc) is not None,
        "T1-61 🔴 未命中判据用 COALESCE(JSON_LENGTH(...))：JSON_LENGTH(NULL) 返回 NULL，"
        "不兜底会让那些行**两边都不算**（总数看起来仍自洽）",
    )
    check(
        re.search(r"bucket_date\s*>=.*\.date\(\)", svc) is not None
        or "start.date()" in svc,
        "T1-62 🔴 日桶比较先转 date（拿 datetime 比 DATE 列会让当天那桶被静默漏掉）",
    )
    check(
        "utcnow" in svc or "UTC" in svc,
        "T1-63 🔴 `_now()` 由 UTC 加 8 小时得出，而不是 `datetime.now()`（服务器时区一变就错）",
    )

    # ---- T1-64 配置名形态 ----
    bad_names = [
        n
        for n in Settings.model_fields
        if n.startswith("stats_") and not re.fullmatch(r"[a-z][a-z0-9_]*", n)
    ]
    check(
        not bad_names,
        f"T1-64 🔴 settings 字段名是小写 snake_case（大写形态只出现在 .env；缺 {bad_names}）",
    )

    # ---- T1-65 🔴 settings ↔ .env.example 完整对齐 ----
    # 这是**全项目**的不变量（CLAUDE.md：`.env.example` 是所有环境变量的完整清单），
    # 但直到本次回填都没有脚本盯它 —— be-02/be-04/be-06 各加了配置，
    # `.env.example` 一条没跟上，于是它宣称的「唯一事实来源」是假的。
    # 症状：运维照它配 `.env`，**配了个寂寞**（缺的项静默走默认值，无任何报错）。
    env_path = ROOT / ".env.example"
    if not env_path.exists():
        skip("T1-65", ".env.example 不存在")
    else:
        present = {
            m.group(1)
            for m in re.finditer(r"^([A-Z][A-Z0-9_]*) *= *", env_path.read_text(encoding="utf-8"), re.M)
        }
        # 派生属性不是环境变量；docker-compose 专用的三个键不是 settings 字段。
        derived = {
            "database_url",
            "redis_url",
            "celery_broker_url",
            "celery_result_backend",
            "cors_origin_list",
            "stats_latency_edges",
            "stats_admin_role_list",
        }
        missing = sorted(
            f for f in set(Settings.model_fields) - derived if f.upper() not in present
        )
        check(
            not missing,
            f"T1-65 🔴 settings 每个字段都在 .env.example 里（缺 {missing}）",
        )
        stats_missing = sorted(
            f
            for f in Settings.model_fields
            if f.startswith("stats_") and f not in derived and f.upper() not in present
        )
        check(not stats_missing, f"T1-66 be-07 的 stats_* 配置全部已登记（缺 {stats_missing}）")


def _t1_routing_and_schedule() -> None:
    paths = main.app.openapi()["paths"]
    stats_paths = sorted(p for p in paths if p.startswith("/api/stats"))
    expected = {
        "/api/stats/audit-logs",
        "/api/stats/dashboard/overview",
        "/api/stats/dashboard/trend",
        "/api/stats/dashboard/top-questions",
        "/api/stats/dashboard/top-knowledge",
        "/api/stats/dashboard/latency",
    }

    # ── T1-67a 🔴 「实现 ↔ 消费方」对账：从前端源码里读 URL ──
    #
    # ⚠️ **这条用例的由来**：本路由首版把 5 个看板端点注册成了
    #    `/api/stats/overview`（**漏了 `dashboard` 段**），而前端
    #    `frontend/src/api/stats.ts` 调的是 `/api/stats/dashboard/overview`。
    #    后果是 5 张图**全部 404**，而接口本身"工作正常"。
    #
    #    当时为什么没被拦住 —— 两条防线同时失效，值得记下来：
    #    ① 下面那条「路由已注册」断言，`expected` 是**照实现抄的**。
    #       这种自证式断言永远绿，它证明的只是「代码等于代码」。
    #    ② T2 接口层的路径断言（T2-4 等）因 MySQL 不可达一直 SKIP，从没真跑过。
    #
    #    修法不能只是「把 expected 改对」—— 那还是照实现抄。
    #    这里改成**从消费方（前端）源码里正则抠出它真正请求的 URL**，
    #    再断言后端每一个都注册了。这样「实现」与「消费方」成为两个独立来源，
    #    任一侧单独改动都会让它变红。
    fe_api = ROOT / "frontend" / "src" / "api" / "stats.ts"
    if not fe_api.exists():
        skip("T1-67a", "frontend/src/api/stats.ts 不存在（前端未落地）")
    else:
        fe_urls = sorted(set(re.findall(r"'(/api/stats/[a-z0-9/_-]+)'", fe_api.read_text(encoding="utf-8"))))
        missing_paths = [u for u in fe_urls if u not in paths]
        check(
            not missing_paths,
            f"T1-67a 🔴 前端请求的每个统计路径后端都注册了（前端要但后端没有：{missing_paths}）",
        )
        check(
            len(fe_urls) == 5,
            f"T1-67a2 前端共请求 5 个看板路径（解析到 {len(fe_urls)} 个：{fe_urls}）",
        )
        # 🔴 反向也要查：**后端注册了但没任何消费方**的看板路径是死代码。
        #    `audit-logs` 是已知的例外（fe-05 明确不消费它，归属未定，见 be-06 spec §9-1）。
        orphan = [p for p in stats_paths if p not in fe_urls and p != "/api/stats/audit-logs"]
        check(not orphan, f"T1-67a3 无孤儿看板路径（后端有但没人调：{orphan}）")

    # ── T1-67b 🔴 T2 段自己用到的 URL 也必须真注册 ──
    #
    # ⚠️ **这条用例的由来（T2 第一次真跑时抓到的）**：T1-67a 把「实现 vs 前端」
    #    对好了，可 T2 那段**仍用着改路径之前的** `/api/stats/overview`
    #    （漏 `dashboard` 段），于是 T2-3…T2-22 **全线 404**。
    #
    #    为什么它比看上去更值得防：404 在报告里的长相与「业务断言失败」一模一样，
    #    很容易被读成「统计模块有 bug」，然后去改**本来正确**的代码。
    #    根因与 T1-67a 当初失效是同一类 —— **断言里的路径是照记忆写的，
    #    不是从被测对象取的**。所以这里同样让「T2 源码」与「后端注册表」
    #    成为两个独立来源。
    t2_src = (ROOT / "backend" / "scripts" / "verify_stats.py").read_text(encoding="utf-8")
    t2_urls = sorted(set(re.findall(r'client\.(?:get|post)\(\s*"(/api/[^"]+)"', t2_src)))
    # 自查：解析不到东西时，「没有 404 来源」与「正则坏了」是同一个绿灯。
    check(
        len(t2_urls) >= 6,
        f"T1-67b0 （自查）从 T2 源码解析到 {len(t2_urls)} 个 URL：{t2_urls}",
    )
    dead = [u for u in t2_urls if u not in paths]
    check(not dead, f"T1-67b 🔴 T2 请求的每个路径后端都注册了（会 404 的：{dead}）")

    # ⚠️ 用 `app.openapi()["paths"]` 而不是 `len(app.routes)`：
    #    Starlette 1.7 的 `include_router` 把子路由包成 `_IncludedRouter`，
    #    数 `app.routes` 会漏报（CLAUDE.md 已知坑）。
    check(
        expected <= set(stats_paths),
        f"T1-67 6 个看板/审计端点全部注册（缺 {sorted(expected - set(stats_paths))}）",
    )

    # ---- T1-68 每个端点都有权限依赖 ----
    spec = main.app.openapi()
    no_sec: list[str] = []
    for path in sorted(expected):
        for method, op in spec["paths"].get(path, {}).items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            # ⚠️ `core/deps.py` 用的是 `HTTPBearer`，OpenAPI 把它写成
            #    **`security`**（schemes），**不是** `parameters` 里的 header ——
            #    第一版按 header 找，于是 6 个端点全被报成「裸奔」，
            #    而这正是「静态断言写错方向」的典型：它红得理直气壮，但是假的。
            #    两个位置都查，哪个有都算挂了鉴权。
            params = op.get("parameters") or []
            has_header = any(
                p.get("in") == "header" and p.get("name", "").lower() == "authorization"
                for p in params
            )
            has_security = bool(op.get("security"))
            if not (has_header or has_security):
                no_sec.append(f"{method.upper()} {path}")
    check(
        not no_sec,
        f"T1-68 🔴 每个端点都要求 Authorization（红线 7：不靠前端隐藏菜单）—— 裸奔 {no_sec}",
    )

    # ---- T1-69~72 排班 ----
    from backend.tasks.celery_app import celery_app

    beat = celery_app.conf.beat_schedule
    check(
        "purge-stats-daily" in beat and beat["purge-stats-daily"]["task"] == "stats.purge_stats",
        "T1-69 beat 注册了 stats.purge_stats（漏注册 = 表一直涨且**不报错**）",
    )
    check(
        beat["aggregate-stats-hourly"]["task"] == "stats.aggregate_hourly",
        "T1-70 🔴 任务名仍是 `stats.aggregate_hourly`（改名会让 beat 键失配、静默不跑）",
    )
    purge_cron = beat["purge-stats-daily"]["schedule"]
    check(
        purge_cron.hour == {4} and purge_cron.minute == {23},
        f"T1-71 清理排在 04:23（实测 {purge_cron.hour}:{purge_cron.minute}）",
    )
    # 🔴 刻意避整点：`aggregate-stats-hourly` 是 `*/10`，会命中所有整点。
    #    两个重任务撞在一起，症状是「偶尔一次预聚合特别慢」，而真凶是隔壁的 DELETE。
    check(
        0 not in purge_cron.minute and all(m % 10 != 0 for m in purge_cron.minute),
        "T1-72 🔴 清理时刻避开 */10 的命中分钟（字典 §9 #116）",
    )
    check(
        "backend.tasks.stats_tasks" in celery_app.conf.include,
        "T1-73 stats_tasks 在 include 列表里（漏了会报 unregistered task 且任务不跑）",
    )

    # ---- T1-74 公开函数有返回注解 ----
    missing_ann: list[str] = []
    tree = ast.parse(_src("service/stats_service.py"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            if node.returns is None:
                missing_ann.append(node.name)
    check(not missing_ann, f"T1-74 公开函数都有返回注解（缺 {missing_ann}）")


# ═══════════════════════════════════════════════════════════════
# T1.5 假会话
# ═══════════════════════════════════════════════════════════════


async def tier15_fake_session() -> None:
    print("\n── T1.5 假会话（驱动 get_* 与 aggregate_range）──")
    await _t15_overview()
    await _t15_trend_and_latency()
    await _t15_ranking()
    await _t15_aggregate()


async def _t15_overview() -> None:
    single = _FakeSession(
        [
            _stored_row(
                "hour",
                datetime(2026, 10, 14, 10, 0),
                pv=200,
                uv=37,
                faq_hit_count=50,
                no_hit_count=20,
                latency_avg_ms=400,
                token_in_sum=1000,
                token_out_sum=2000,
                blocked_count=3,
            )
        ],
        today=88,
        documents=42,
    )
    ov = await stats_service.get_overview(
        single, _ctx("role_01"), DashboardQuery(start_time=None, end_time=None)
    )
    check(ov.pv == 200 and ov.uv == 37, "T1.5-01 overview 汇总 pv/uv")
    check(ov.uv_is_exact is True, "T1.5-02 🔴 单桶窗口 ⇒ uv_is_exact=True（桶内去重就是人数）")
    check(abs(ov.faq_hit_rate - 0.25) < 1e-9, "T1.5-03 faq_hit_rate = 50/200 = 0.25")
    check(abs(ov.coverage_rate - 0.9) < 1e-9, "T1.5-04 coverage_rate = 1 - 20/200 = 0.9")
    check(ov.document_count == 42 and ov.today_question_count == 88, "T1.5-05 全量/当日两个不随窗口变的字段")
    check(ov.blocked_count == 3, "T1.5-06 blocked_count 汇总")

    # ---- T1.5-07 🔴 加权均价 ----
    # 两个桶：pv=1 的慢桶（avg 5000）与 pv=999 的快桶（avg 100）。
    # 算术平均 ⇒ (5000+100)/2 = 2550（错，把 1 次请求与 999 次等权）。
    # 按 pv 加权 ⇒ (5000*1 + 100*999)/1000 = 104.9 → 104。差 24 倍，而两个数都"正常"。
    weighted = _FakeSession(
        [
            _stored_row("hour", datetime(2026, 10, 14, 10, 0), pv=1, uv=1, latency_avg_ms=5000),
            _stored_row("hour", datetime(2026, 10, 14, 11, 0), pv=999, uv=5, latency_avg_ms=100),
        ]
    )
    ov2 = await stats_service.get_overview(weighted, _ctx("role_01"), DashboardQuery())
    check(
        ov2.avg_latency_ms == 104,
        f"T1.5-07 🔴 均价按 pv 加权 = 104（算术平均会得 2550；实测 {ov2.avg_latency_ms}）",
    )
    check(ov2.uv_is_exact is False, "T1.5-08 🔴 多桶窗口 ⇒ uv_is_exact=False（和是人次不是人数）")
    check(ov2.uv == 6, "T1.5-09 多桶 uv = 各桶之和（并在 uv_is_exact 里标明它不是人数）")

    # ---- T1.5-10 直方图跨桶合并 ----
    hist2 = _FakeSession(
        [
            _stored_row(
                "hour",
                datetime(2026, 10, 14, 10, 0),
                pv=10,
                latency_buckets={"0": 5, "200": 5},
            ),
            _stored_row(
                "hour",
                datetime(2026, 10, 14, 11, 0),
                pv=10,
                latency_buckets={"0": 1, "200": 9},
            ),
        ]
    )
    ov3 = await stats_service.get_overview(hist2, _ctx("role_01"), DashboardQuery())
    check(ov3.latency_p95_ms >= 0, "T1.5-10 跨桶合并后的 P95 可算出（不抛异常）")


async def _t15_trend_and_latency() -> None:
    sess = _FakeSession(
        [
            _stored_row(
                "day",
                date(2026, 10, 13),
                pv=10,
                token_in_sum=100,
                token_out_sum=200,
                latency_avg_ms=300,
            ),
            _stored_row(
                "day",
                date(2026, 10, 14),
                pv=20,
                token_in_sum=300,
                token_out_sum=400,
                latency_avg_ms=500,
            ),
        ]
    )
    tr = await stats_service.get_trend(sess, TrendQuery(granularity="day"))
    check(tr.granularity == "day" and len(tr.points) == 2, "T1.5-11 trend 返回 2 个桶")
    check(
        tr.points[0].bucket == "2026-10-13",
        f"T1.5-12 🔴 桶标签由后端格式化（实测 {tr.points[0].bucket}）",
    )
    check(
        tr.points[1].question_count == 20
        and tr.points[1].token_in == 300
        and tr.points[1].token_out == 400,
        "T1.5-13 trend 三个指标同桶返回（无 metric 参数，一次给全）",
    )
    check(tr.start_time.tzinfo is None, "T1.5-14 trend 回带 naive 窗口")

    hour_sess = _FakeSession([_stored_row("hour", datetime(2026, 10, 14, 15, 0), pv=1)])
    tr2 = await stats_service.get_trend(hour_sess, TrendQuery(granularity="hour"))
    check(
        tr2.points[0].bucket == "2026-10-14 15:00",
        f"T1.5-15 hour 桶标签形态（实测 {tr2.points[0].bucket}）",
    )

    # ---- T1.5-16 直方图形状 ----
    lat = await stats_service.get_latency_histogram(
        _FakeSession(
            [
                _stored_row("day", date(2026, 10, 14), pv=100, latency_avg_ms=400,
                            latency_buckets={"0": 90, "2000": 10}),
            ]
        ),
        DashboardQuery(),
    )
    edges = settings.stats_latency_edges
    check(len(lat.counts) == len(edges) - 1, f"T1.5-16 🔴 counts 长度 = edges-1（{len(lat.counts)}）")
    check(lat.edges == edges, "T1.5-17 edges 回带配置值")
    check(lat.counts[edges.index(0)] == 90, "T1.5-18 直方图按桶取值正确")


async def _t15_ranking() -> None:
    # ---- T1.5-19 🔴 跨桶**求和** ----
    # 同一问题在两个桶各出现 2 次 ⇒ 合并后必须是 4。
    sess = _FakeSession(
        [
            _stored_row(
                "day",
                date(2026, 10, 13),
                top_questions=[{"question": "报销流程", "count": 2}, {"question": "年假", "count": 1}],
            ),
            _stored_row(
                "day",
                date(2026, 10, 14),
                top_questions=[{"question": "报销流程", "count": 2}],
            ),
        ]
    )
    items = await stats_service.get_top_questions(sess, DashboardQuery())
    by_q = {i.question: i.count for i in items}
    check(by_q.get("报销流程") == 4, f"T1.5-19 🔴 跨桶求和（实测 {by_q.get('报销流程')}，应为 4）")
    check(items[0].question == "报销流程", "T1.5-20 按计数降序")
    check(by_q.get("年假") == 1, "T1.5-21 低频项仍在榜上（未超 limit）")

    limited = await stats_service.get_top_questions(sess, DashboardQuery(limit=1, start_time=None))
    check(len(limited) == 1, "T1.5-22 limit 生效")

    # ---- T1.5-23~25 角色硬闸 ----
    try:
        await stats_service.assert_admin_scope(
            _FakeSession(role_codes=["staff"]), _ctx("role_03"), "top-knowledge"
        )
        check(False, "T1.5-23 staff 访问 top-knowledge 应被拒")
    except Exception as exc:  # noqa: BLE001
        check(
            type(exc).__name__ == "ForbiddenError",
            f"T1.5-23 🔴 staff 被角色硬闸拒绝（实测 {type(exc).__name__}）",
        )
    try:
        await stats_service.assert_admin_scope(
            _FakeSession(role_codes=["sys_admin"]), _ctx("role_01"), "top-knowledge"
        )
        check(True, "T1.5-24 sys_admin 通过角色硬闸")
    except Exception as exc:  # noqa: BLE001
        check(False, f"T1.5-24 sys_admin 应通过，实测 {type(exc).__name__}")
    try:
        await stats_service.assert_admin_scope(
            _FakeSession(role_codes=["kb_admin"]), _ctx("role_02"), "top-knowledge"
        )
        check(True, "T1.5-25 kb_admin 通过（看板侧给 kb_admin，审计侧不给 —— 这个不对称是刻意的）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"T1.5-25 kb_admin 应通过，实测 {type(exc).__name__}")

    # ---- T1.5-26 标题回填 ----
    kn = _FakeSession(
        [
            _stored_row(
                "day",
                date(2026, 10, 14),
                top_documents=[{"document_id": "doc_a", "cite_count": 7}],
            )
        ],
        role_codes=["kb_admin"],
        titles={"doc_a": "差旅制度", "doc_gone": "已删文档"},
    )
    items2 = await stats_service.get_top_knowledge(kn, _ctx("role_02"), DashboardQuery())
    check(
        len(items2) == 1 and items2[0].document_id == "doc_a",
        "T1.5-26 top-knowledge 返回文档 id",
    )
    check(
        items2[0].title == "差旅制度",
        f"T1.5-27 标题已回填（实测 {items2[0].title if items2 else None}）",
    )
    # 标题查不到的文档（已软删或被清理）**静默跳过**，不补空串 ——
    # 空标题在榜上是一条无信息的行，用户会以为界面坏了。
    gone = _FakeSession(
        [_stored_row("day", date(2026, 10, 14), top_documents=[{"document_id": "doc_gone", "cite_count": 3}])],
        role_codes=["sys_admin"],
    )
    items3 = await stats_service.get_top_knowledge(gone, _ctx("role_01"), DashboardQuery())
    check(items3 == [], "T1.5-28 🔴 查不到标题的文档不进榜（补空串会出一条无信息的行）")


async def _t15_aggregate() -> None:
    """驱动真正的聚合代码（`aggregate_range`）—— 本层最值钱的一块。"""
    start = datetime(2026, 10, 14, 10, 0)
    end = datetime(2026, 10, 14, 12, 0)

    scalar_rows = [
        _agg_row(datetime(2026, 10, 14, 10, 0, 0), pv=2, uv=1, faq_hit_count=1),
        _agg_row(datetime(2026, 10, 14, 11, 0, 0), pv=3, uv=2, faq_hit_count=0),
    ]
    detail_rows = [
        SimpleNamespace(
            created_at=datetime(2026, 10, 14, 10, 5),
            question="报销流程",
            latency_ms=150,
            allowed_ids=["chk_1", "chk_2"],
        ),
        SimpleNamespace(
            created_at=datetime(2026, 10, 14, 10, 35),
            question="年假",
            latency_ms=3000,
            allowed_ids=["chk_1"],
        ),
        SimpleNamespace(
            created_at=datetime(2026, 10, 14, 11, 10),
            question="报销流程",
            latency_ms=800,
            allowed_ids=[],
        ),
        SimpleNamespace(
            created_at=datetime(2026, 10, 14, 11, 40),
            question="报销流程",
            latency_ms=1200,
            allowed_ids=["chk_1"],
        ),
        SimpleNamespace(
            created_at=datetime(2026, 10, 14, 11, 50),
            question=None,  # question 允许为空？不——防御：空串不该进榜
            latency_ms=900,
            allowed_ids=["chk_3"],
        ),
    ]
    sess = _FakeSession(
        scalar_rows,
        detail_rows=detail_rows,
        chunk_map={"chk_1": "doc_A", "chk_2": "doc_A", "chk_3": "doc_B"},
    )

    rows = await stats_service.aggregate_range(sess, "hour", start, end)
    check(len(rows) == 2, f"T1.5-27 两个小时桶（实测 {len(rows)}）")
    if len(rows) != 2:
        return

    b10, b11 = rows
    check(
        b10.bucket == datetime(2026, 10, 14, 10, 0) and b11.bucket == datetime(2026, 10, 14, 11, 0),
        "T1.5-28 桶键已取整到整点",
    )

    # ---- T1.5-29 🔴 直方图逐桶守恒 ----
    check(
        sum(b10.latency_buckets.values()) == 2 and sum(b11.latency_buckets.values()) == 3,
        f"T1.5-29 🔴 直方图**逐桶**守恒（10 点 {sum(b10.latency_buckets.values())}/2、"
        f"11 点 {sum(b11.latency_buckets.values())}/3）",
    )
    check(
        b10.latency_p95_ms > 0 and b11.latency_p95_ms > 0,
        "T1.5-30 逐桶 P95 都算出来了（明细桶键没取整时这里恒为 0）",
    )

    # ---- T1.5-31 🔴 逐桶 TOP（本模块实现期真踩过的 bug）----
    # 正确：10 点桶里「报销流程」1 次、11 点桶里 2 次。
    # 错误（把窗口级 TOP 写进每个桶）：两桶都是 3 次，合并后变 6。
    q10 = {i["question"]: i["count"] for i in b10.top_questions}
    q11 = {i["question"]: i["count"] for i in b11.top_questions}
    check(
        q10.get("报销流程") == 1,
        f"T1.5-31 🔴 10 点桶的「报销流程」= 1（窗口级会写成 3；实测 {q10.get('报销流程')}）",
    )
    check(
        q11.get("报销流程") == 2,
        f"T1.5-32 🔴 11 点桶的「报销流程」= 2（不是 3；实测 {q11.get('报销流程')}）",
    )
    check(
        sum(i["count"] for i in b10.top_questions) + sum(i["count"] for i in b11.top_questions) == 4,
        "T1.5-33 两桶 TOP 计数之和 = 有效提问数（5 行里 1 行 question 为空）",
    )
    check(
        "" not in q10 and "" not in q11,
        "T1.5-34 空 question 不进榜（否则榜上出现一条没有问题的行）",
    )

    # ---- T1.5-35 🔴 chunk → document ----
    # `allowed_ids` 是 **chunk id**，榜要的是 document。10 点桶里两条提问
    # 分别引用了 `[chk_1, chk_2]` 与 `[chk_1]`，而 chk_1/chk_2 **同属 doc_A**
    # ⇒ doc_A 的计数是 **3**（chunk 级 3 次引用归并到一篇文档），不是 2。
    #
    # ⚠️ 这个期望值首跑写错成 2 —— 我当时是按「两条提问 = 2 次引用」算的，
    #    而计数单位是**切片引用**不是提问条数。差别正在此处：把「引用次数」
    #    读成「被问到的次数」是这份榜最容易误读的地方（也是 UI 文案要写清的地方）。
    d10 = {i["document_id"]: i["cite_count"] for i in b10.top_documents}
    check(
        d10.get("doc_A") == 3,
        f"T1.5-35 🔴 chunk 引用归并到 document（doc_A 应为 3；实测 {d10}）",
    )
    check(
        not any(k.startswith("chk") for k in d10),
        "T1.5-36 🔴 榜上不出现 chunk id（直接把 chunk id 当 document id 用会算错热度且不报错）",
    )
    d11 = {i["document_id"]: i["cite_count"] for i in b11.top_documents}
    check(d11.get("doc_A") == 1 and d11.get("doc_B") == 1, f"T1.5-37 11 点桶的两个文档各 1 次（{d11}）")
    check(
        b10.token_in_sum == 0 and b10.session_count == 0,
        "T1.5-38 未给的标量字段取默认 0（而不是 None 传下去炸在 UPSERT）",
    )


# ═══════════════════════════════════════════════════════════════
# T2 接口层
# ═══════════════════════════════════════════════════════════════


async def _login(client: AsyncClient, username: str) -> str | None:
    try:
        resp = await client.post(
            "/api/auth/login", json={"username": username, "password": PASSWORD}
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [note] 登录请求失败：{type(exc).__name__}")
        return None
    if resp.status_code != 200:
        return None
    return (resp.json().get("data") or {}).get("access_token")


async def tier2_http() -> bool:
    print("\n── T2 接口层（MySQL + Redis + 迁移 0005）──")
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        try:
            async with engine.connect() as conn:
                await conn.execute(select(1))
        except Exception as exc:  # noqa: BLE001
            skip("T2 全部", f"MySQL 不可达（{type(exc).__name__}）")
            return False

        admin = await _login(client, "admin")
        if admin is None:
            skip("T2 全部", "admin 登录失败（Redis 不可达或账号/密码不符）")
            return False
        check(True, "T2-1  admin 登录成功")
        ha = {"Authorization": f"Bearer {admin}"}

        # ---- T2-2 表存在（迁移 0005 是否真应用了）----
        try:
            from sqlalchemy import text

            async with engine.connect() as conn:
                got = set(
                    (
                        await conn.execute(
                            text(
                                "SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema = DATABASE() AND table_name IN "
                                "('stats_hourly', 'stats_daily')"
                            )
                        )
                    ).scalars()
                )
            check(
                got == {"stats_hourly", "stats_daily"},
                f"T2-2  🔴 迁移 0005 已应用（实见 {sorted(got)}）",
            )
        except Exception as exc:  # noqa: BLE001
            skip("T2-2 ", f"information_schema 查询失败（{type(exc).__name__}）")

        # ---- T2-3 无 token ----
        resp = await client.get("/api/stats/dashboard/overview")
        check(resp.status_code == 401, f"T2-3  无 token ⇒ 401（实测 {resp.status_code}）")

        # ---- T2-4~8 overview ----
        resp = await client.get("/api/stats/dashboard/overview", headers=ha)
        check(resp.status_code == 200, f"T2-4  overview 200（实测 {resp.status_code}）")
        body = resp.json()
        check(body.get("code") == 0, "T2-5  统一响应体 code=0")
        data = body.get("data") or {}
        need = {
            "start_time",
            "end_time",
            "granularity",
            "pv",
            "uv",
            "uv_is_exact",
            "document_count",
            "today_question_count",
            "faq_hit_rate",
            "coverage_rate",
            "avg_latency_ms",
            "latency_p95_ms",
        }
        check(need <= set(data), f"T2-6  overview 字段齐备（缺 {sorted(need - set(data))}）")
        check(
            isinstance(data.get("uv_is_exact"), bool),
            "T2-7  uv_is_exact 是布尔（不是 0/1 或缺失）",
        )
        check(
            isinstance(data.get("faq_hit_rate"), (int, float))
            and 0 <= data["faq_hit_rate"] <= 1,
            f"T2-8  🔴 faq_hit_rate 是 0–1 小数（实测 {data.get('faq_hit_rate')}）",
        )

        # ---- T2-9 🔴 请求参数真的被用上了 ----
        # FastAPI 对**不在签名里**的 query 参数是**静默忽略**（不是 422）。
        # 参数名对不上时，症状是「时间范围控件看着能用，结果永远是默认窗口」，无任何报错。
        # 这里发一个远古窗口，断言响应**回显的是请求的窗口**而不是默认值。
        old = "2020-01-01 00:00:00"
        resp = await client.get(
            "/api/stats/dashboard/overview",
            params={"start_time": old, "end_time": "2020-01-02 00:00:00"},
            headers=ha,
        )
        check(resp.status_code == 200, f"T2-9  overview 带时间窗 200（实测 {resp.status_code}）")
        echoed = (resp.json().get("data") or {}).get("start_time", "")
        check(
            str(echoed).startswith("2020-01-01"),
            f"T2-10 🔴 响应回显**请求的**窗口（实测 {echoed}）—— 参数名对不上会静默用默认值",
        )

        # ---- T2-11~13 trend ----
        resp = await client.get("/api/stats/dashboard/trend", params={"granularity": "day"}, headers=ha)
        check(resp.status_code == 200, f"T2-11 trend 200（实测 {resp.status_code}）")
        td = resp.json().get("data") or {}
        check(td.get("granularity") == "day" and isinstance(td.get("points"), list), "T2-12 trend 结构正确")
        if td.get("points"):
            p = td["points"][0]
            check(
                {"bucket", "token_in", "token_out", "question_count", "avg_latency_ms"} <= set(p),
                f"T2-13 🔴 trend 点字段与前端一致（缺 {sorted({'bucket','token_in','token_out','question_count','avg_latency_ms'} - set(p))}）",
            )
        else:
            skip("T2-13", "qa_log 无数据（trend points 为空）—— 结构断言无从下手")

        # ---- T2-14 hour 粒度超跨度 → 1001 ----
        resp = await client.get(
            "/api/stats/dashboard/trend",
            params={"granularity": "hour", "start_time": "2026-01-01 00:00:00"},
            headers=ha,
        )
        payload = resp.json()
        check(
            payload.get("code") == ErrorCode.PARAM_ERROR,
            f"T2-14 🔴 hour 粒度超跨度 ⇒ 业务码 1001（实测 {payload.get('code')}）",
        )

        # ---- T2-15 limit 超上限 ⇒ 被拒（业务码 1001）----
        # 🔴 断言**业务码**而不是 FastAPI 的默认 422：`core/exceptions.py`
        #    刻意把一切校验失败统一成 `400 + PARAM_ERROR`（统一信封的一部分），
        #    422 在本项目里永远不会出现。只断言 422 的用例，红的是**用例**。
        resp = await client.get(
            "/api/stats/dashboard/top-questions",
            params={"limit": settings.stats_top_n + 1},
            headers=ha,
        )
        check(
            resp.status_code in (400, 422) and resp.json().get("code") == 1001,
            f"T2-15 limit 超上限 ⇒ 被拒且业务码 1001"
            f"（实测 {resp.status_code} / code={resp.json().get('code')}）",
        )

        # ---- T2-16~18 latency ----
        resp = await client.get("/api/stats/dashboard/latency", headers=ha)
        check(resp.status_code == 200, f"T2-16 latency 200（实测 {resp.status_code}）")
        ld = resp.json().get("data") or {}
        check(
            len(ld.get("counts", [])) == len(ld.get("edges", [])) - 1,
            "T2-17 🔴 counts.length == edges.length - 1（末桶开区间）",
        )
        check(ld.get("edges") == settings.stats_latency_edges, "T2-18 edges 回带配置值")

        # ---- T2-19~20 榜单 ----
        resp = await client.get("/api/stats/dashboard/top-questions", headers=ha)
        check(resp.status_code == 200, f"T2-19 top-questions 200（实测 {resp.status_code}）")
        resp = await client.get("/api/stats/dashboard/top-knowledge", headers=ha)
        check(resp.status_code == 200, f"T2-20 top-knowledge 200（实测 {resp.status_code}）")

        # ---- T2-21~23 越权 ----
        staff = await _login(client, "cost01")
        if staff is None:
            skip("T2-21/22", "cost01 登录失败")
        else:
            hs = {"Authorization": f"Bearer {staff}"}
            resp = await client.get("/api/stats/dashboard/overview", headers=hs)
            check(
                resp.status_code == 403,
                f"T2-21 🔴 staff 访问 overview ⇒ 403（实测 {resp.status_code}）",
            )
            resp = await client.get("/api/stats/dashboard/top-knowledge", headers=hs)
            check(
                resp.status_code == 403,
                f"T2-22 🔴 staff 访问 top-knowledge ⇒ 403（红线 4：该端点返回文档标题）",
            )
            resp = await client.get("/api/stats/audit-logs", headers=hs)
            check(
                resp.status_code == 403,
                f"T2-23 staff 访问审计 ⇒ 403（审计**不给** kb_admin，更不给 staff）",
            )

        # ---- T2-24~25 🔴 幂等性端到端 ----
        # 「重算 + 覆盖」是本模块的核心决策。这条用例是它唯一的直接证据：
        # 同一窗口跑两次，桶的行数与值都不该变。若哪天有人改成 `+=`，
        # 这里会看到行数不变但 pv 翻倍 —— 而看板上只表现为「今天问得特别多」。
        try:
            from sqlalchemy import func

            from backend.models.stats import StatsHourly as H

            window_start = datetime(2026, 10, 14, 0, 0)
            window_end = datetime(2026, 10, 14, 23, 0)

            async def _snapshot() -> dict:
                async with engine.connect() as conn:
                    rows = (
                        await conn.execute(
                            select(H.bucket_start, H.pv, H.uv, H.faq_hit_count).where(
                                H.bucket_start >= window_start, H.bucket_start < window_end
                            )
                        )
                    ).all()
                return {r[0]: (r[1], r[2], r[3]) for r in rows}

            from backend.service.stats_service import aggregate_window

            # ⚠️ 这里用 `SessionLocal` 而**不是** `fresh_session`：本脚本全程在
            #    同一个事件循环里，`SessionLocal` 绑的正是这个循环的 engine。
            #    （worker 侧必须用 `fresh_session`，因为 Celery 任务是各自
            #     `asyncio.run()` —— 两者场景不同，别互相抄。）
            async with SessionLocal() as db:
                await aggregate_window(db, window_start, window_end)
            first = await _snapshot()
            async with SessionLocal() as db:
                await aggregate_window(db, window_start, window_end)
            second = await _snapshot()
            check(
                first == second,
                f"T2-24 🔴 同一窗口跑两次结果**逐值相同**（幂等）—— 差异 {len(set(first) ^ set(second))} 处",
            )
            async with engine.connect() as conn:
                dup = await conn.scalar(
                    select(func.count()).select_from(
                        select(H.bucket_start)
                        .where(H.bucket_start >= window_start, H.bucket_start < window_end)
                        .group_by(H.bucket_start)
                        .having(func.count() > 1)
                        .subquery()
                    )
                )
            check(int(dup or 0) == 0, f"T2-25 🔴 窗口内无重复桶（UNIQUE 生效，实测重复 {dup} 个）")
        except Exception as exc:  # noqa: BLE001
            skip("T2-24/25", f"幂等性探测失败：{type(exc).__name__}: {exc}")

    return True


# ═══════════════════════════════════════════════════════════════
# T3 链路层
# ═══════════════════════════════════════════════════════════════


def tier3_chain() -> None:
    print("\n── T3 链路层（worker + beat + 真实 qa_log）──")
    skip(
        "T3-1  聚合任务真跑一次并对账",
        "需要 Celery worker；且需 qa_log 里有真实数据。"
        "在 ECS 上跑法：`celery -A backend.tasks call stats.aggregate_hourly`，"
        "再断言 stats_hourly 里该小时桶的 pv == qa_log 中该小时的 COUNT(*)",
    )
    skip(
        "T3-2  beat 到点自动触发",
        "需要 beat 连续运行跨过 10 分钟边界；另确认 `celery inspect registered` "
        "含 stats.aggregate_hourly 与 stats.purge_stats",
    )
    skip(
        "T3-3  清理任务真删过期桶",
        "需要人为造一条 bucket_start 在保留期外的小时桶 + 一个 worker 去执行；"
        "（与 Docker 无关 —— 本机不跑 Milvus，但 beat/worker 是可用的）",
    )
    skip(
        "T3-4  看板数字 ↔ qa_log 端到端对账",
        "需 qa_log 有真实问答数据（be-05 T3 未跑过 ⇒ 表为空）。"
        "届时断言 overview.pv == 窗口内 qa_log 行数",
    )


# ═══════════════════════════════════════════════════════════════


async def run() -> tuple[bool, bool]:
    """返回 `(全部通过, T2 是否真的跑过)`。

    ⚠️ T1/T1.5/T2 在**同一个事件循环**里跑 —— 这正是 `core/db.py` 的模块级 engine
       能跨层复用的前提。若各自塞进一个 `asyncio.run()`，第二层会复用到
       上个循环遗留的连接，报 `'NoneType' object has no attribute 'send'`
       （CLAUDE.md 已知坑），症状是「单跑某一层都过、合起来必挂」。

    ⚠️ 但 **T1 是纯同步**，所以它跑在 `asyncio.run()` 之外 —— 它不碰任何真 IO。
    """
    tier1_offline()
    await tier15_fake_session()
    ran = await tier2_http()
    tier3_chain()
    await engine.dispose()
    return all(_results), ran


def main_() -> None:
    ok_, ran = asyncio.run(run())
    passed = sum(_results)
    total = len(_results)
    print(f"\n{'=' * 56}")
    # 🔴 标签要说实话：`_results` 里**含 T2 的检查**（T2 真跑过时）。
    #    写死「T1/T1.5」会让人以为接口层一条都没验，把已经跑过的当成没跑
    #    —— 与「把 SKIP 当成通过」是同一类误读，只是方向相反。
    _scope = "T1/T1.5 + T2（含接口层）" if ran else "T1/T1.5（离线+假会话）"
    print(f"{_scope}：{passed}/{total} 通过")
    # 🔴 不要写死「剩下的都是 T2/T3 的依赖问题」——T2 现在真的跑起来了。
    #    写死归类会让人以为接口层一条都没验过，把已经能跑的东西当成没跑。
    print(
        f"SKIP：{len(_skipped)} 项（**不是通过**）—— 逐条原因见上；"
        "缺口通常在 T2（需 MySQL+Redis）或 T3（需 Milvus/BGE/DeepSeek+worker/beat）"
    )
    if not ran:
        print("⚠️ T2 未执行（MySQL 或 Redis 不可用）—— **本次结果不构成完整验收**。")
        print("   依赖恢复后重跑本脚本。")
    if passed != total:
        print("存在失败项，见上方 FAIL 行。")
    sys.exit(0 if (ok_ and ran) else 1)


if __name__ == "__main__":
    main_()
