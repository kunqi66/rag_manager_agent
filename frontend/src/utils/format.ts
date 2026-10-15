/**
 * 知识库页面的展示格式化。只做「值 → 文案」的纯函数，不持有状态、不发请求。
 *
 * 抽出来的理由：同一个状态文案在表格、任务项、权限弹窗三处都要用，
 * 各写一份必然出现「一处写『启用』一处写『已启用』」。
 */

import type { ImportTaskStatus, KbDocumentStatus, PermissionScope } from '@/types/kb'

/** 空值统一占位符。后端多个字段可空，散写各种 `|| '-'` 会不统一 */
export const EMPTY_TEXT = '—'

/** 字节数 → 人类可读体积。用于任务项的「2.4 MB」 */
export function formatFileSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes) || bytes < 0) return EMPTY_TEXT
  if (bytes < 1024) return `${bytes} B`

  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  // 小于 10 时保留一位小数（2.4 MB），否则取整（128 MB）——与原型表格里数字的观感一致
  const text = value < 10 ? value.toFixed(1) : Math.round(value).toString()
  return `${text} ${units[unitIndex]}`
}

/**
 * ISO 时间 → `YYYY-MM-DD HH:mm`（原型表格的「2026-10-12 14:20」）。
 *
 * 用本地时区而非直接截字符串：后端若回 `...Z`，截取会显示 UTC 时间，
 * 与本机时钟差 8 小时，用户会以为数据没更新。
 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return EMPTY_TEXT
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso

  const pad = (n: number): string => n.toString().padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

/**
 * 知识单元状态 → 胶囊文案。
 * 原型里只画了「已启用」「已停用」；`draft` 按同一词法补「草稿」（spec §9-8）。
 */
export function documentStatusLabel(status: KbDocumentStatus): string {
  const labels: Record<KbDocumentStatus, string> = {
    draft: '草稿',
    enabled: '已启用',
    disabled: '已停用',
  }
  return labels[status] ?? status
}

/** 导入任务状态 → 文案（字典 §5.2） */
export function taskStatusLabel(status: ImportTaskStatus): string {
  const labels: Record<ImportTaskStatus, string> = {
    pending: '排队中',
    parsing: '解析中',
    embedding: '向量化中',
    success: '已完成',
    failed: '失败',
  }
  return labels[status] ?? status
}

/** 权限维度 → 筛选下拉的短标签。`summary` 缺失时的兜底文案（spec §9-7） */
export function permissionScopeLabel(scope: PermissionScope): string {
  const labels: Record<PermissionScope, string> = {
    global: '全局',
    department: '限定部门',
    role: '限定角色',
    user: '指定用户',
  }
  return labels[scope] ?? scope
}

/** 文件名扩展名（小写，不含点）。用于图标映射与展示 */
export function fileExtension(fileName: string): string {
  const index = fileName.lastIndexOf('.')
  return index > 0 ? fileName.slice(index + 1).toLowerCase() : ''
}
