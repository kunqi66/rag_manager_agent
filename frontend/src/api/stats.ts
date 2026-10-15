import { get } from './http'

import type {
  DashboardQuery,
  LatencyVo,
  OverviewVo,
  TopKnowledgeItem,
  TopQuestionItem,
  TrendQuery,
  TrendVo,
} from '@/types/stats'

/**
 * 运营看板接口封装。路径对齐 PRD §8.5。
 *
 * ✅ **2026-10-14：be-07 已实现**（`backend/api/stats_router.py` + `service/stats_service.py`），
 * 本文件是按**实际路由与 DTO** 回的核，不是设计稿推定了。
 * 5 个端点路径逐条对过 `stats_router.py`，参数名对过 `schemas/stats.py` 的
 * `DashboardQuery` / `TrendQuery`。
 *
 * ⚠️ 仍需真环境验证的：`verify_stats.py` 的 T2 一次都没跑过
 * （本机连不上云端 MySQL）。**参数名对不上时 FastAPI 会静默忽略**（不是 422），
 * 所以「时间控件看着能用、结果永远是默认窗口」这类问题只有 T2 能证明 —— 见 types/stats.ts。
 *
 * 注意：**不存在** `auditLogs` 方法 —— `GET /api/stats/audit-logs` 后端**已经实现了**
 * （`stats_router.py:36`，挂在同一个 router 上），但审计页的页面归属未定（spec §9-1），
 * 本页不消费它。归属定了再加，不要提前写。
 */
export const statsApi = {
  /** KPI 卡。无数据时后端返回零值而非 404（be-07 §3.1 约定）。 */
  overview: (params?: DashboardQuery) => get<OverviewVo>('/api/stats/dashboard/overview', params),

  /** 趋势序列。`granularity` 只有本端点收 —— 用 `TrendQuery`，别复用 `DashboardQuery`。 */
  trend: (params?: TrendQuery) => get<TrendVo>('/api/stats/dashboard/trend', params),

  /** 高频问题榜。返回的 `question` 是用户原始输入 —— 渲染必须走插值。 */
  topQuestions: (params?: DashboardQuery) =>
    get<TopQuestionItem[]>('/api/stats/dashboard/top-questions', params),

  /** 高频引用知识榜。返回文档**标题**，数据权限口径见 spec §9-17。 */
  topKnowledge: (params?: DashboardQuery) =>
    get<TopKnowledgeItem[]>('/api/stats/dashboard/top-knowledge', params),

  /** 延时直方图。响应形状按 be-07 §3.1 的 `{edges, counts}`（fe-05 §5 写法不同，见 spec §5.2）。 */
  latency: (params?: DashboardQuery) => get<LatencyVo>('/api/stats/dashboard/latency', params),
}
