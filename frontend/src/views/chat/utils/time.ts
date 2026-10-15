/**
 * 会话时间的相对化展示。
 *
 * 原型 `SessionItem/SessionTime` 展示的是「刚刚 / 昨天 16:20 / 3 天前 / 上周」，
 * 而后端 `qa_session.updated_at` 是 ISO 时间串——两种形态得由前端转换。
 * 口径是纯 UI 决策（fe-03 未规定），集中在页面私有工具里，避免散进组件。
 */

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n)
}

/**
 * 把 ISO 时间串转成原型风格的相对时间。
 * 解析失败时原样返回（宁可显示原始串，也不要显示 `Invalid Date`）。
 */
export function formatSessionTime(iso: string, now: Date = new Date()): string {
  const t = new Date(iso)
  if (Number.isNaN(t.getTime())) return iso

  const diffMs = now.getTime() - t.getTime()
  // 未来时间（时钟漂移）也按「刚刚」处理，不显示负数
  if (diffMs < 60_000) return '刚刚'

  const hm = `${pad2(t.getHours())}:${pad2(t.getMinutes())}`

  // 按「自然日」算差，而不是按 24 小时——昨天 23:00 在今天 01:00 应该是「昨天 23:00」
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const dayDiff = Math.floor((startOfToday - new Date(t.getFullYear(), t.getMonth(), t.getDate()).getTime()) / 86_400_000)

  if (dayDiff <= 0) return `今天 ${hm}`
  if (dayDiff === 1) return `昨天 ${hm}`
  if (dayDiff < 7) return `${dayDiff} 天前`
  if (dayDiff < 30) return `${Math.floor(dayDiff / 7)} 周前`
  return `${t.getFullYear()}-${pad2(t.getMonth() + 1)}-${pad2(t.getDate())}`
}

/** 毫秒 → 「1.2s」形式（原型 `AiMeta` 的「耗时 1.2s」） */
export function formatLatency(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(1)}s`
}
