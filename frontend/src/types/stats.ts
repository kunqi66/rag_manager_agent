/**
 * 运营看板（fe-05）的类型定义。**与后端 `backend/schemas/stats.py` 一一对应**。
 *
 * ✅ **2026-10-14 已回核**：be-07 已实现（`api/stats_router.py` + `service/stats_service.py` +
 * `schemas/stats.py`），本文件按**实际响应 DTO** 重写，不再按设计稿推定。
 * 原 `P2-05-fe-dashboard` §9 挂的 6 条缺口，逐条销账：
 *
 * | 原缺口 | 回核结果 |
 * |---|---|
 * | §9-21 `document_count` / `today_question_count` 名字对不上 | ✅ 后端就是这两个名字，**一致** |
 * | §9-3 环形图分段无来源 | ✅ `OverviewDTO` 给了 `faq_hit_count` / `blocked_count` / `no_hit_count`，可画真三段 |
 * | §9-9 KPI 平均响应时长无字段 | ✅ `avg_latency_ms`（**必填**，后端恒返回） |
 * | §9-10 KPI 环比 `deltas` 无字段 | 🔴 **仍未提供** —— 后端 `OverviewDTO` 里确实没有，本文件保留 `deltas?` 可选，UI 不渲染 |
 * | §9-11 趋势多指标 vs 单 `metric` | ✅ 后端**没有 `metric` 参数**，`points` 一次给全指标 ⇒ 多指标模型胜出 |
 * | §9-13 `latency` 无 P95 | ✅ `LatencyHistogramDTO` 有 `p95_ms`（另加 `avg_ms`） |
 *
 * 缺字段时的**统一处置**：对应 UI **不渲染或显示 `—`**，绝不回填原型里的演示数字。
 * 现在只剩 `deltas` 一条走这条规则。
 */

/** 看板分区键。loading / error 按这个粒度隔离，一个端点挂了不影响其它分区（fe-05 §4.1）。 */
export type PanelKey = 'overview' | 'trend' | 'topQuestions' | 'topKnowledge' | 'latency'

/** 趋势粒度。`hour` 只允许查近 7 天（be-07 §3.1），超窗后端返回 1001。 */
export type TrendGranularity = 'day' | 'hour'

/** 顶栏时间范围预设。目前只做「近 7 天 / 近 30 天」两档（原型只画了「近 7 天」）。 */
export type RangePreset = '7d' | '30d'

/**
 * 请求参数（后端 `DashboardQuery`）。
 *
 * ⚠️ **必须是 `type` 而不是 `interface`**：`api/http.ts` 的 `get<T>(url, params?: Record<string, unknown>)`
 * 收的是带索引签名的类型，而 **interface 没有隐式索引签名、不能赋给它**（type 别名可以）。
 * 改成 interface 会在调用处报「缺少索引签名」。
 *
 * ✅ 参数名与格式已冻结（全局 #77）：`start_time` / `end_time` 为
 * `YYYY-MM-DD HH:mm:ss` 或 `YYYY-MM-DD`，**naive、Asia/Shanghai**。
 *
 * 🔴 **名字错了不会报 422 —— 会被静默忽略**（见 `schemas/stats.py` 的类 docstring）：
 * 多余的 query 参数根本不在后端签名里，进不了校验。症状是「时间控件看着能用，
 * 结果永远是默认窗口」。守门用例：`verify_stats.py` T2-11。
 *
 * ⚠️ `granularity` **不在本类型里** —— 只有 `trend` 收它，见 `TrendQuery`。
 */
export type DashboardQuery = {
  start_time?: string
  end_time?: string
  limit?: number
}

/** `trend` 的查询条件 = 公共时间窗 + 粒度（后端 `TrendQuery`）。 */
export type TrendQuery = DashboardQuery & {
  granularity?: TrendGranularity
}

/**
 * KPI 卡数据（PRD §8.5 `overview`，后端 `OverviewDTO`）。
 *
 * 🔴 **各字段时间口径不同，这是刻意的**：`document_count` 是**全量**、
 * `today_question_count` 是**当日**，其余随所选范围变。所以 `start_time`/`end_time`
 * 是**必填**：不回带窗口时，用户切范围会看到「有的卡变了、有的没变」，以为页面坏了。
 *
 * `faq_hit_rate` / `coverage_rate` 都是 **0–1 的小数**（后端 `ge=0, le=1` 强约束）：
 * 展示时 `×100` 取整，**不做换算**。
 */
export interface OverviewVo {
  /** 生效窗口起点（后端补过默认值的结果），ISO 字符串 */
  start_time: string
  /** 生效窗口终点 */
  end_time: string
  /** 所查桶中最近一次聚合时间；无数据时 `null` → 顶部显示「数据截至 —」 */
  updated_at: string | null
  /** 实际查的是小时表还是日表 */
  granularity: TrendGranularity

  /** 提问量（人次） */
  pv: number
  /**
   * 去重人数。
   * ⚠️ 跨桶不可加：`stats_hourly.uv` 是**桶内**去重用户数，`SUM(uv)` 会把同一用户数多次。
   */
  uv: number
  /**
   * 🔴 `uv` 能不能当「人数」读。`true` ⟺ 窗口恰好落在一个桶内。
   * **不许省略**：只回一个数字，前端无从判断能不能加「人」字 ——
   * 而把「人次」当「人数」展示是一个**必然发生**的误读。
   */
  uv_is_exact: boolean

  /** 知识单元总数（全量，不随范围变） */
  document_count: number
  /** 今日提问量（当日，不随范围变） */
  today_question_count: number

  /** FAQ 命中条数（所选范围） */
  faq_hit_count: number
  /** FAQ 命中率，**0–1 的小数** */
  faq_hit_rate: number
  /** 权限受限拦截条数（安全红线 4 的侧信道触发次数） */
  blocked_count: number
  /** 未命中提问数 */
  no_hit_count: number
  /** 知识覆盖率 = `1 - no_hit_count / pv`，**0–1 的小数** */
  coverage_rate: number

  /** 平均响应耗时（ms）。🔴 按 `pv` **加权**的整体均值，不是各桶 `avg` 的算术平均 */
  avg_latency_ms: number
  /** P95 响应耗时（ms）。⚠️ 由延时直方图**插值估算**，桶边界越密越准 */
  latency_p95_ms: number

  // ↓↓↓ 🟡 唯一仍缺的字段：后端 `OverviewDTO` 里确实没有环比（原 spec §9-10）。
  //     保持可选，UI 不渲染 —— 绝不回填原型演示值。

  /**
   * 环比增量。键与 KPI 卡一一对应。
   * 量纲由后端定义（百分比 / 百分点 / 绝对值），本页**原样加正负号展示**；
   * 「好/坏」的方向由 `constants/dashboard.ts` 的 `goodWhenDown` 决定（延时下降是好事）。
   */
  deltas?: Partial<Record<KpiKey, number>>
}

/** KPI 卡的键。与 `OverviewVo` 的数值字段同名，便于 `data[key]` 直取。 */
export type KpiKey = 'pv' | 'uv' | 'document_count' | 'avg_latency_ms' | 'faq_hit_rate'

/** 趋势图的一个时间桶。 */
export interface TrendBucket {
  /** 桶标签，由后端给（如 `2026-10-08` 或 `2026-10-08 15:00`），前端不自己造 */
  bucket: string
  token_in: number
  token_out: number
  question_count: number
  avg_latency_ms: number
}

/**
 * 趋势序列（后端 `TrendDTO`）。
 *
 * ✅ **多指标模型胜出**（原 spec §9-11）：后端**没有 `metric` 参数**，一次返回
 * `token_in` / `token_out` / `question_count` / `avg_latency_ms` 四列
 * ⇒ 原型「两条系列共用一根柱」照画，**不需要**并发发两次再在前端拼桶。
 * （拼桶是聚合，违反 fe-05 §1.2「不做任何聚合计算」。）
 */
export interface TrendVo {
  granularity: TrendGranularity
  start_time: string
  end_time: string
  /**
   * ⚠️ **只含有数据的桶，后端不补空洞**。两个范围不同的响应点数因此不同
   * ⇒ 前端**不能按数组下标对齐**曲线，必须按 `bucket` 字符串对齐。
   */
  points: TrendBucket[]
}

/** 常见问题 TOP 榜的一行。`question` 是**用户原始输入**，渲染必须走插值，禁止 v-html。 */
export interface TopQuestionItem {
  question: string
  count: number
  /** 🟡 可选。原型没画占比条，本页不渲染。 */
  ratio?: number
}

/** 知识热度榜的一行。`title` 是文档标题，渲染走插值（安全红线 4 / spec §7）。 */
export interface TopKnowledgeItem {
  document_id: string
  title: string
  cite_count: number
}

/**
 * 响应延时直方图（后端 `LatencyHistogramDTO`）。
 *
 * ✅ 形状是 `{edges, counts, avg_ms, p95_ms}`（原 spec §5.2 / §9-13 已销）——
 * 均值与 P95 **都在这个端点里**，不必另发 `overview`。
 * 原型表头那句「P95 2.1s」现在有来源了。
 */
export interface LatencyVo {
  /** 桶边界（毫秒），如 `[0, 200, 500, 1000, 2000, 5000]`；**最后一个为开区间** */
  edges: number[]
  /**
   * 各桶计数，长度 = `edges.length - 1`。
   * 🔴 少了最后那个开区间，「超过 5 秒」的请求会**全部落空**，
   * 直方图总数 ≠ `pv` 而**没人会发现**（图看起来只是矮了一点）。
   */
  counts: number[]
  /** 按 `pv` 加权的平均耗时（ms） */
  avg_ms: number
  /** P95 耗时；⚠️ 直方图插值估算，非精确分位数 */
  p95_ms: number
}

/**
 * 环形图的一段（**视图模型**，不来自接口）。
 * 由 `DashboardView` 从 `OverviewVo` 派生。
 *
 * ✅ 原 spec §9-3 已销：后端给了 `faq_hit_count` / `blocked_count` / `no_hit_count`，
 * 所以**能画出真三段**（命中 / 被拦 / 未命中），不必再退回两段。
 */
export interface DonutSegment {
  name: string
  value: number
  /** CSS 变量名（如 `--kb-brand`），由图表组件经 `token()` 换成真实色值 */
  colorVar: string
}
