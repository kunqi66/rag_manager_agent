import { get, post, put } from './http'

import type { PageResult } from '@/types/api'
import type {
  CandidateQuery,
  CandidateReviewResult,
  FaqCacheResult,
  FaqCandidateItem,
  FaqItem,
  FaqQuery,
  FaqReviewPayload,
  GapQuery,
  GapTaskResult,
  KnowledgeGapItem,
  MineResult,
  MiningOverviewVo,
} from '@/types/governance'

/**
 * `/api/mining/*` 请求封装（治理中心 fe-06）。
 *
 * ✅ **2026-10-14：后端 be-08 / be-09 已实现**，`backend/api/data_mining_router.py`
 * **8 个端点全部落地**（be-08 六个 + be-09 两个），已无 `TODO` 壳子。
 * 路径与参数形态按**实际路由**核对过。
 *
 * ⚠️ **仍未验证**：本机连不上云端 MySQL ⇒ `verify_gap.py` / `verify_faq.py` 的
 * T2（接口层）一次都没跑过。别把「代码在」当成「接口通」。
 *
 * 为什么有三个函数手工拼 query：
 * `http.ts` 的 `post` / `put` 只接受 body、不接受 `params`，而 `mine` / `task` / `cache`
 * 三个端点的参数是 **Query**（壳子定义）。**不得**为图省事绕过 `http.ts` 另起 axios
 * （那会重复实现 token 注入与 401 单飞刷新），故在本层用 URLSearchParams 拼串。
 * 若 `http.ts` 后续给 `post`/`put` 补 `params` 形参，这三处可改回工具签名（spec §9-15）。
 */
function query(params: Record<string, string | number | boolean>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const governanceApi = {
  /**
   * 顶部统计块。✅ 后端有 `MiningOverviewDTO`，5 个字段已逐条核对（原 spec §9-4 已销）。
   * ⚠️ 与 fe-05 的 `/api/stats/dashboard/overview` 是**两个端点**，不要复用那个 ——
   * 后端本端点是对 `stats_service.get_overview()` 的**投影**，数值必须与看板同源。
   * ⚠️ 本端点**不收时间窗**（传 `start_time`/`end_time` 会报 1001），所以本函数也不传参数。
   */
  overview: () => get<MiningOverviewVo>('/api/mining/stats/overview'),

  /**
   * 候选 FAQ 列表。✅ 后端 `CandidateQuery` 有 `status` / `min_confidence` / `page` / `page_size`
   * —— 四个参数全都收（原 spec §9-5 说「没有分页」已作废）。
   */
  candidates: (q: CandidateQuery) =>
    get<PageResult<FaqCandidateItem>>('/api/mining/faq/candidates', {
      status: q.status,
      min_confidence: q.min_confidence,
      page: q.page,
      page_size: q.page_size,
    }),

  /**
   * 审核候选（发布 / 驳回）。
   * ✅ 请求体与后端 `FaqReviewPayload` 同名同构；🔒 不含任何身份字段（红线 1 ——
   * 审核人由后端从 JWT 取，前端传了也会被忽略）。
   */
  reviewCandidate: (candidateId: string, payload: FaqReviewPayload) =>
    post<CandidateReviewResult>(
      `/api/mining/faq/candidates/${encodeURIComponent(candidateId)}/review`,
      payload,
    ),

  /** 已发布 FAQ 分页列表。✅ 后端 `FaqQuery` 有 keyword / page / page_size */
  faqs: (q: FaqQuery) =>
    get<PageResult<FaqItem>>('/api/mining/faq', {
      keyword: q.keyword,
      page: q.page,
      page_size: q.page_size,
    }),

  /**
   * 启用 / 停用单条 FAQ 缓存。
   * ✅ `enabled` 是 **Query 参数**（后端 `Query(...)` **必填**），不是请求体——别「顺手」改成 body。
   * 🔴 后端**刻意不给默认值**：漏传会 422 而不是「静默打开缓存」。
   * 这个开关是运营「这条答得不对，先别用」的唯一手段，静默打开等于关掉它。
   */
  toggleFaqCache: (faqId: string, enabled: boolean) =>
    put<FaqCacheResult>(
      `/api/mining/faq/${encodeURIComponent(faqId)}/cache${query({ enabled })}`,
    ),

  /**
   * 知识缺口清单。✅ 后端 `GapQuery` 有 `min_frequency` / `page` / `page_size`。
   * ❌ **仍无 `status` 过滤**（spec §9-6 未销）：后端就是这个签名，
   * 状态筛选只能在已取回的那一页上做，**且分页总数是过滤前的** ——
   * 别在本地过滤后又拿 `total` 当「筛完的总数」显示。
   */
  gaps: (q: GapQuery) =>
    get<PageResult<KnowledgeGapItem>>('/api/mining/gaps', {
      min_frequency: q.min_frequency,
      page: q.page,
      page_size: q.page_size,
    }),

  /**
   * 缺口一键转建知识补充任务。
   * ✅ 后端回 `GapTaskResult{gap_id, status, task_id}`（原 spec §9-7 已销），
   * `task_id` 用于跳转与回查。
   *
   * 🔴 **可重复点**：`create_supplement_task` 与「缺口转 `tasked`」**不是同一事务**，
   * 所以存在「任务建好了、缺口还是 `open`」的中间态。后端用 `_find_open_task`
   * 做了补偿 —— 再点一次会**复用**那条任务，不会建出第二条。
   * 前端因此**不必**为了防重复而禁用按钮（那反而会让中间态卡死）。
   *
   * ⚠️ `category` 传 `""` 表示「没传」，**不是**「把建议分类清空」：
   * 后端 `Query("")` + `category or None`，清空一个已有建议不会有任何报错。
   */
  createGapTask: (gapId: string, category?: string) =>
    post<GapTaskResult>(
      `/api/mining/gaps/${encodeURIComponent(gapId)}/task${query({ category: category ?? '' })}`,
    ),

  /**
   * 手动触发聚类挖掘（供演示与手动补跑；常态是 beat 每日）。
   * ✅ 后端回 `MineResultDTO{candidates_created, clusters_scanned, window_*, partial}`
   * （原 spec §9-8 已销）。
   *
   * 🔴 **同步慢接口**：后端上限走 `FAQ_MINE_SYNC_MAX_SECONDS`（默认 10s，
   * **刻意小于 `http.ts` 的全局 15s**）。超限时它**不中断**，而是截短窗口并回
   * `partial: true` —— 收到 `partial` 要把文案退化成「已挖掘近 N 天」，
   * **不要**当成失败重试（候选已经写进库了，重试只会得到 0 条新增）。
   *
   * ⚠️ `start_time` / `end_time` 后端**不支持**（传了报 1001），所以本函数不发这两个参数。
   */
  mineFaq: (minFrequency: number) =>
    post<MineResult | null>(`/api/mining/faq/mine${query({ min_frequency: minFrequency })}`),
}
