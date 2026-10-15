import { Database, Eye, Timer, Users, Zap } from 'lucide-vue-next'
import type { Component } from 'vue'

import type { KpiKey, RangePreset, TrendGranularity } from '@/types/stats'

/**
 * 运营看板（fe-05）的静态常量与格式化函数。
 *
 * 这里**只放「不随数据变化」的东西**：标签文案、图标、范围选项、阈值、格式化规则。
 * 所有**数字**（卡值、榜次、占比）一律来自接口 —— 本文件里出现任何业务数值都算 bug
 * （原型画的是「填好状态」，那些 `12,480` / `34%` / `218` 全是演示数据）。
 */

// ─────────────────────────────── 时间范围 / 粒度 ───────────────────────────────

/** 范围预设的天数。**只在这里改窗口长度**，别在组件里散算。 */
export const DASHBOARD_RANGE_DAYS: Record<RangePreset, number> = {
  '7d': 7,
  '30d': 30,
}

/** 顶栏下拉的选项。文案取自原型 `RangeSelect` 的「近 7 天」，30 天为对称补齐（见 spec §9-2）。 */
export const DASHBOARD_RANGE_OPTIONS: ReadonlyArray<{ value: RangePreset; label: string }> = [
  { value: '7d', label: '近 7 天' },
  { value: '30d', label: '近 30 天' },
]

export const DASHBOARD_DEFAULT_RANGE: RangePreset = '7d'

/**
 * 趋势粒度。原型里**没有粒度切换控件**（spec §9-15），故当前固定 `day`。
 * be-07 §3.1 约束：`hour` 只允许查近 7 天，超窗返回 1001 —— 将来加控件时必须联动禁用。
 */
export const DASHBOARD_DEFAULT_GRANULARITY: TrendGranularity = 'day'

/** 取某个预设对应的展示文案（趋势卡副标题用）。 */
export function rangeLabel(preset: RangePreset): string {
  return DASHBOARD_RANGE_OPTIONS.find((o) => o.value === preset)?.label ?? ''
}

/**
 * 自动轮询间隔（毫秒）。**0 = 关闭**。
 *
 * 预聚合每 10 分钟才跑一次（PRD §8.5 / be-07 §1.1），前端轮询比它快只会重复拿到同一份数据。
 * 所以默认关掉，改用页脚说明「数据非实时」（fe-05 §4.5）。
 * 要开就填 `600_000`——**不要填小于聚合周期的值**。
 */
export const DASHBOARD_AUTO_REFRESH_MS = 0

// ─────────────────────────────── 榜单 / 图表 ───────────────────────────────

/** 三张榜取多少行。原型各画了 6 行。be-07 的 `limit` 默认 10、上限 50。 */
export const TOP_LIST_LIMIT = 6

/** 前几名用品牌色徽标，其余用浅底徽标（原型 Rank1-3 是 `$brand-soft`，Rank4-6 是 `$border-soft`）。 */
export const RANK_HIGHLIGHT_COUNT = 3

/** 慢请求阈值（毫秒）。延时卡脚注「X% 的提问超过 2s」按它算。 */
export const LATENCY_SLOW_THRESHOLD_MS = 2000

export const DASHBOARD_EMPTY_TEXT = '暂无数据'

/** 页脚告知用户数据的更新节奏 —— 比让他反复点刷新好（fe-05 §4.5）。 */
export const DASHBOARD_FOOTER_NOTE = '数据每 10 分钟自动聚合一次，非实时。'

// ─────────────────────────────── KPI 卡定义 ───────────────────────────────

export interface KpiCardDef {
  /** 与 `OverviewVo` 的数值字段同名，取值直接 `data[key]` */
  key: KpiKey
  /** 卡片标题，逐字对齐原型 `KpiLabel` */
  label: string
  icon: Component
  /** 图标 / 数值的强调色令牌 */
  colorVar: string
  /** 图标盒底色令牌 */
  softVar: string
  /** 数值格式化。**只做格式化，不做换算**（fe-05 §8 承诺 2） */
  format: (value: number) => string
  /**
   * 「值越小越好」的指标（延时）。只影响环比箭头的**颜色语义**：
   * 延时 -0.3s 是好事，原型也用绿色（dev-frontend 提醒：环比好坏方向因指标而异）。
   */
  goodWhenDown?: boolean
}

export const KPI_CARDS: readonly KpiCardDef[] = [
  {
    key: 'pv',
    label: '访问次数 PV',
    icon: Eye,
    colorVar: '--kb-brand',
    softVar: '--kb-brand-soft',
    format: formatInteger,
  },
  {
    key: 'uv',
    label: '独立人数 UV',
    icon: Users,
    colorVar: '--kb-brand',
    softVar: '--kb-brand-soft',
    format: formatInteger,
  },
  {
    key: 'document_count',
    label: '知识单元数',
    icon: Database,
    colorVar: '--kb-success',
    softVar: '--kb-success-soft',
    format: formatInteger,
  },
  {
    key: 'avg_latency_ms',
    label: '平均响应时长',
    icon: Timer,
    colorVar: '--kb-warning',
    softVar: '--kb-warning-soft',
    format: formatSeconds,
    goodWhenDown: true,
  },
  {
    key: 'faq_hit_rate',
    label: 'FAQ 命中率',
    icon: Zap,
    colorVar: '--kb-success',
    softVar: '--kb-success-soft',
    format: formatPercent,
  },
]

// ─────────────────────────────── 格式化 ───────────────────────────────

/** 千分位整数，如 `12480` → `12,480`（原型就是千分位）。 */
export function formatInteger(value: number): string {
  return Math.round(value).toLocaleString('en-US')
}

/** 毫秒 → 秒，保留 1 位，如 `1400` → `1.4s`。 */
export function formatSeconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)}s`
}

/**
 * 比例（0–1）→ 百分比整数，如 `0.34` → `34%`。
 *
 * ⚠️ **量纲假设**：本页假定后端给 0–1 的小数（spec §9-6）。若后端给 0–100，
 * 这里会算出 `3400%` —— 那是口径不一致，改这里没用，要改后端或改这一行的假设。
 */
export function formatPercent(ratio: number): string {
  return `${Math.round(ratio * 100)}%`
}

/**
 * 环比文案。**单位未定**（spec §9-10）：后端给什么量纲就展示什么，只补正负号。
 * 字段缺失时整行不渲染，**不回填原型里的 `+12.4%`**。
 */
export function formatDelta(delta: number): string {
  const sign = delta > 0 ? '+' : ''
  return `${sign}${delta} 环比`
}

/**
 * 把历史延时桶的边界转成标签，如 `[0,200,500,1000,2000,5000]` →
 * `<0.2s` / `0.2-0.5s` / `0.5-1s` / `1-2s` / `>2s`。
 *
 * **标签由边界算出来，不是写死的字符串数组** —— 后端一改 `STATS_LATENCY_EDGES_MS`，
 * 前端跟着变，不会出现「桶边界变了但标签还是老的」这种对不上的情况。
 */
export function formatLatencyBucketLabel(edges: number[], index: number): string {
  const from = edges[index]
  const to = edges[index + 1]
  const fmt = (ms: number): string => {
    const seconds = ms / 1000
    return `${Number.isInteger(seconds) ? seconds : seconds.toFixed(1)}s`
  }
  if (from === undefined) return ''
  if (index === 0 && from === 0 && to !== undefined) return `<${fmt(to)}`
  if (to === undefined) return `>${fmt(from)}`
  return `${fmt(from)}-${fmt(to)}`
}

// ─────────────────────────────── 时间窗计算 ───────────────────────────────

/** 补零到 2 位。 */
function pad(value: number): string {
  return String(value).padStart(2, '0')
}

/**
 * 本地时区的 `YYYY-MM-DD HH:mm:ss`。
 *
 * 刻意**不用 `toISOString()`**：那是 UTC，会让「近 7 天」在 Asia/Shanghai 的日界上错一天。
 * ⚠️ 这个格式**未经后端确认**（spec §9-2），定稿后只改这一个函数。
 */
function toLocalDateTime(date: Date): string {
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}

/**
 * 由预设算出请求的 `start_time` / `end_time`。
 *
 * 这是**时间窗的算术**，不是对业务数据的聚合 —— 与 fe-05 §1.2「不做任何聚合计算」不冲突
 * （那条禁的是对返回数据做 `reduce` 求和）。
 */
export function resolveRange(
  preset: RangePreset,
  now: Date = new Date(),
): { start_time: string; end_time: string } {
  const days = DASHBOARD_RANGE_DAYS[preset]
  const start = new Date(now)
  // 「近 N 天」含今天，故往前推 N-1 天，并从当天 00:00:00 起算
  start.setDate(start.getDate() - (days - 1))
  start.setHours(0, 0, 0, 0)
  return { start_time: toLocalDateTime(start), end_time: toLocalDateTime(now) }
}
