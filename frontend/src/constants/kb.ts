/**
 * 知识维护与导入中心的常量与阈值。
 *
 * CLAUDE.md：「常量、阈值全部进配置，禁止硬编码魔数」。放这里而不是散在组件里，
 * 是因为这些值同时被 store / composable / 组件读，散开就会有人只改一处。
 */

import type { DocumentQuery, PermissionScope } from '@/types/kb'

/**
 * 空筛选条件。
 * 必须与 store 的初始 `query` 是**同一份**——否则「清空筛选」会漏清一个维度，
 * 表现为「清了筛选还是搜不到」。
 */
export const EMPTY_DOCUMENT_QUERY: DocumentQuery = {
  keyword: '',
  category: '',
  status: '',
  permission_scope: '',
}

/**
 * 「定位到某条文档」的 query 参数名（跨页跳转用）。
 *
 * fe-03（问答引用跳转）与 fe-05（看板知识点下钻）**各自独立命名了同一件事**：
 * 统一口径用 `document_id`，`highlight` 作为**过渡别名**一并接受。
 * 多收一个别名的成本是一行数组；少收一个的代价是「跳过去什么都没发生」——
 * 后者是静默失效，没人会发现。
 */
export const FOCUS_DOCUMENT_QUERY_KEY = 'document_id'
export const FOCUS_DOCUMENT_QUERY_ALIASES = ['highlight'] as const

/**
 * 功能权限码。✅ **已冻结（2026-10-14，字典 §5.6.1 / §9 #11）**，与
 * `backend/scripts/seed_org.py` 的 `PERMISSIONS`（perm_14…perm_19）逐字一致，已实测比对通过。
 *
 * ⚠️ 权限码是前端常量与后端种子之间的**隐式契约**：两边各写各的码名，
 * 后果是按钮对**所有人（含 `sys_admin`）静默不可见**，且前后端都不报错。
 * 改这里务必同改 §5.6 与种子脚本并重灌 —— 这就是把这些串集中定义的原因。
 *
 * 注意 `permission` 的域是 `kb` 而非 `kb:document`（PRD §3.2 逐字如此），别「顺手改齐」。
 * 另有 `kb:document:list` 不在本对象里 —— 它是**跳转闸门**，定义在
 * `constants/chat.ts` 的 `CITE_JUMP_PERMISSION`。
 */
export const KB_PERMS = {
  upload: 'kb:document:upload',
  edit: 'kb:document:update',
  status: 'kb:document:status',
  remove: 'kb:document:delete',
  permission: 'kb:permission:update',
} as const

/** 导入任务轮询间隔（fe-02 §4.3 ① 第 4 步：2s） */
export const IMPORT_POLL_INTERVAL_MS = 2000

/**
 * 轮询上限（fe-02 §7 末尾「已知坑」）：对齐 MinerU 总超时 15 min。
 *
 * 没有上限就是无限轮询——后端 `celery_app.include` 漏注册任务会让状态**永远 pending**，
 * 而静默失败在前端表现为「进度条永远转圈」，无人察觉。到点必须停并提示。
 */
export const IMPORT_POLL_MAX_DURATION_MS = 15 * 60 * 1000

/** 连续轮询失败多少次才停止（fe-02 §6 末行：单次失败静默重试，连续 3 次停） */
export const IMPORT_POLL_MAX_FAILURES = 3

/** 关键字输入的防抖时长（fe-02 §4.2：避免每个字符打一次接口） */
export const FILTER_DEBOUNCE_MS = 300

/**
 * 导入任务全部完成后重拉台账的最小间隔（fe-02 §4.3 ① 第 8 步：节流刷新）。
 * 没有它，用户连续「全部重试」时会出现一串并发的列表请求。
 */
export const LIST_REFRESH_THROTTLE_MS = 1500

/** 权限弹窗「指定用户」远程搜索的防抖时长 */
export const USER_SEARCH_DEBOUNCE_MS = 300

/** 单文件体积上限（PRD §6.4.1 MinerU 限制）。用于前置校验与错误文案 */
export const MAX_UPLOAD_FILE_MB = 200
export const MAX_UPLOAD_FILE_BYTES = MAX_UPLOAD_FILE_MB * 1024 * 1024

/** 支持的扩展名（PRD §6.4.1 的解析链路分支）。用于 el-upload 的 accept 与前置校验 */
export const UPLOAD_ACCEPT_EXTENSIONS = [
  'pdf',
  'png',
  'jpg',
  'jpeg',
  'docx',
  'pptx',
  'xlsx',
  'md',
  'txt',
] as const

/** 状态筛选的下拉选项。`''` = 全部（不下推该参数） */
export const STATUS_FILTER_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'draft', label: '草稿' },
  { value: 'enabled', label: '已启用' },
  { value: 'disabled', label: '已停用' },
] as const

/** 权限范围筛选的下拉选项。`''` = 全部，`none` = 未配置任何权限 */
export const PERMISSION_FILTER_OPTIONS: ReadonlyArray<{
  value: PermissionScope | 'none' | ''
  label: string
}> = [
  { value: '', label: '全部权限' },
  { value: 'global', label: '全局' },
  { value: 'department', label: '限定部门' },
  { value: 'role', label: '限定角色' },
  { value: 'user', label: '指定用户' },
  { value: 'none', label: '未配置' },
]

/** 分页可选页长 */
export const PAGE_SIZE_OPTIONS = [10, 20, 50] as const

export const DEFAULT_PAGE_SIZE = 20
