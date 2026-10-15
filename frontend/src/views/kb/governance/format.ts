/**
 * 治理中心的展示格式化。
 *
 * 只做纯函数，不碰 DOM、不碰状态——这些格式在 4 个 Tab 组件里都要用，
 * 复制到各处必然出现「一个 Tab 显示 10-08、另一个显示 2026/10/08」这种不一致。
 *
 * ⚠️ 后端时间字段的序列化格式**尚未定**（be-08/be-09 未实现）。这里用 `Date` 解析，
 * 解析不出来就**原样回显**——宁可显示后端给的原始串，也不要显示「Invalid Date」。
 * 后端定稿后回来核对（spec §9）。
 */

const PAD = (n: number): string => String(n).padStart(2, '0')

function toDate(value: string | null | undefined): Date | null {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

/** `2026-10-14 09:30` */
export function formatDateTime(value: string | null | undefined): string {
  const date = toDate(value)
  if (!date) return value ?? '—'
  return `${date.getFullYear()}-${PAD(date.getMonth() + 1)}-${PAD(date.getDate())} ${PAD(date.getHours())}:${PAD(date.getMinutes())}`
}

/** `10-08`（详情面板的「首次」胶囊用，原型是这种短格式） */
export function formatShortDate(value: string | null | undefined): string {
  const date = toDate(value)
  if (!date) return value ?? '—'
  return `${PAD(date.getMonth() + 1)}-${PAD(date.getDate())}`
}

/** 0.94 → `0.94`；null/undefined → `—`（相似度、置信度这类原始分不做百分号换算） */
export function formatScore(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return '—'
  return value.toFixed(digits)
}

/** 0.4 → `40.0%` */
export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

/** 长文本截断。列表里的答案摘要用，避免一行被撑成两行破坏 50 高的行距 */
export function truncate(text: string | null | undefined, max = 48): string {
  if (!text) return '—'
  return text.length > max ? `${text.slice(0, max)}…` : text
}

/** 计数带千分位（命中次数这类会到四位数）。null/undefined → `0` 而不是 `—`：计数为 0 是真实值 */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return '0'
  return value.toLocaleString('zh-CN')
}
