"""统计与审计路由（be-06 / be-07）。

**文件归属**：`stats_router.py` 这个文件归 be-07（运营看板），本模块（be-06 审计）
只在其中**注册一个** `GET /api/stats/audit-logs`，实现体转调 `service/audit_service.py`。
be-07 落地时在同一文件追加其余 5 个看板端点，不另起 router。

**分层**：本文件只做「校验 + 注入 + 转发」——
不出现 SQL，不出现业务判断。所有查询构造在 `audit_service.list_audit_logs`。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.db import get_db
from backend.core.deps import require_permission
from backend.core.response import ok, page_result
from backend.schemas.audit import AuditLogQuery
from backend.schemas.auth import AuthContext
from backend.schemas.stats import DashboardQuery, TrendQuery
from backend.service import audit_service, stats_service

router = APIRouter(prefix="/api/stats", tags=["统计与审计"])

# 🔴 每个端点**独立**挂权限（安全红线 7），不依赖前端隐藏菜单。
#    `stats:audit:list` 只给 `sys_admin`（字典 §5.6.2）—— 审计流水含全组织的
#    操作轨迹，`kb_admin` 看自己域内的就够，给他全量等于让他看到组织侧的增删改。
_AUDIT_LIST = require_permission("stats:audit:list")

# ⚠️ **看板给 `kb_admin`，审计不给** —— 这个不对称是刻意的（be-07 §10）。
#    看板看的是自己的知识库被用得怎么样（运营职责，`kb_admin` 本就要管）；
#    审计看的是全组织的增删改（含组织侧），那是 `sys_admin` 的职责范围。
#    把两者共用一个码是最省事的写法，代价是 `kb_admin` 顺带拿到全组织操作轨迹。
_DASHBOARD_VIEW = require_permission("stats:dashboard:view")


@router.get("/audit-logs", summary="审计流水（分页）")
async def list_audit_logs(
    q: AuditLogQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _ctx: AuthContext = Depends(_AUDIT_LIST),
) -> dict:
    items, total = await audit_service.list_audit_logs(db, q)
    return ok(
        page_result(
            # `mode="json"` 是必须的：`created_at` 是 datetime，直接丢进 JSONResponse
            # 会被 FastAPI 再序列化一次，但这里我们把它塞进的是**普通 dict**
            # （page_result 的返回值），那条路径不走 FastAPI 的 encoder。
            [i.model_dump(mode="json") for i in items],
            total,
            q.page,
            q.page_size,
        )
    )


# ═══════════════════════════════════════════════════════════════
# 运营看板（be-07）—— 5 个端点，同文件同 router
# ═══════════════════════════════════════════════════════════════
#
# 🔴 **路径必须带 `dashboard` 段**（`/api/stats/dashboard/overview` 而非 `/api/stats/overview`）。
#    三份独立来源一致要求它：PRD §8.5、字典 §5.6、以及**真正消费它的前端**
#    `frontend/src/api/stats.ts`（fe-05 已实现完毕）。
#
#    ⚠️ 本文件首版**就是漏了这一段**，而它差点没被发现：路径错了不会报任何错，
#    只是前端 5 个图**全部 404**。当时没拦住它的是两条同时失效的防线 ——
#    ① 验收脚本的「路由已注册」断言是**照实现抄的期望值**（自证式断言，永远绿）；
#    ② T2 接口层因 MySQL 不可达一直 SKIP，而从没真跑过。
#    → 修法不只是改路径：`verify_stats.py` 的 T1-67 现在**从前端源码里读 URL**，
#    把「实现 ↔ 消费方」变成一条会变红的断言（见该用例注释）。
#
# ⚠️ **出参一律回 `模型实例`，不要 `model_dump()`**。
#    `ok()` 的返回值会被 FastAPI 用它的 encoder 序列化，`datetime` 会正确地
#    变成 ISO 字符串。手动 `model_dump()` 得到的是**普通 dict**，
#    里面的 `datetime` 会走 `json.dumps` 的默认分支 —— 那是 `TypeError`。
#    （这与上面 `list_audit_logs` 相反：那里塞进的是 `page_result` 的裸 dict，
#     **已经离开了 FastAPI 的 encoder 路径**，所以必须自己 dump。
#     两条路径的差别就是「返回到手的对象是不是 pydantic 模型」。）


@router.get("/dashboard/overview", summary="看板 KPI 总览")
async def stats_overview(
    q: DashboardQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(_DASHBOARD_VIEW),
) -> dict:
    """KPI 卡数据。响应**回带生效窗口**，前端据此显示「数据截至 …」。"""
    return ok(await stats_service.get_overview(db, ctx, q))


@router.get("/dashboard/trend", summary="提问量与 Token 趋势")
async def stats_trend(
    q: TrendQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _ctx: AuthContext = Depends(_DASHBOARD_VIEW),
) -> dict:
    """双系列趋势。**没有 `metric` 参数** —— 一次返回全部指标。"""
    return ok(await stats_service.get_trend(db, q))


@router.get("/dashboard/top-questions", summary="高频问题榜")
async def stats_top_questions(
    q: DashboardQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _ctx: AuthContext = Depends(_DASHBOARD_VIEW),
) -> dict:
    """⚠️ `question` 是**用户原始输入**，前端必须走插值渲染（安全红线 2）。"""
    return ok(await stats_service.get_top_questions(db, q))


@router.get("/dashboard/top-knowledge", summary="知识热度榜")
async def stats_top_knowledge(
    q: DashboardQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    ctx: AuthContext = Depends(_DASHBOARD_VIEW),
) -> dict:
    """🔴 返回**文档标题** ⇒ 除权限码外**再做一次角色硬闸**（红线 4）。

    看门的两层：① `stats:dashboard:view`（本文件）；② `sys_admin` / `kb_admin`
    角色（`stats_service.assert_admin_scope`）。第二层防的是「哪天把这个码授给
    `staff`」——那时第一层会放行，而本端点就成了静默的标题泄漏点。
    """
    return ok(await stats_service.get_top_knowledge(db, ctx, q))


@router.get("/dashboard/latency", summary="响应延时分布")
async def stats_latency(
    q: DashboardQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _ctx: AuthContext = Depends(_DASHBOARD_VIEW),
) -> dict:
    """直方图 + 均价 + P95（`counts.length == edges.length - 1`）。"""
    return ok(await stats_service.get_latency_histogram(db, q))
