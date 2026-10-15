/**
 * 知识导入 / 台账 / 权限的全部请求封装（fe-02 §2 分层落点）。
 *
 * 路径以 PRD §8.2 与本页模块设计 §3 为准。组件与 store **不得**绕过本文件直接 axios。
 *
 * 组织类三个方法（`listDepartments` / `listRoles` / `searchUsers`）暂落在此处，
 * 因为本页的归属文件清单不含 `src/api/org.ts`，而 fe-04 同时在写系统管理页——
 * 两边各建一份 `org.ts` 会分叉。若 fe-04 建了 `api/org.ts`，把这三个方法迁过去即可（spec §9-15）。
 */

import { get, post, put, del } from './http'

import type { PageResult } from '@/types/api'
import type { DepartmentNode, RoleBrief, UserBrief } from '@/types/org'
import type {
  DocumentListQuery,
  DocumentListResult,
  DocumentUpdatePayload,
  ImportTaskVo,
  KbDocumentDetailVo,
  KbDocumentVo,
  PermissionPayload,
  UploadResult,
} from '@/types/kb'

/**
 * 解析上传受理回执（后端 `UploadAccepted`）。
 *
 * ✅ 2026-10-14：形状已冻结，`{task_id, document_id, batch_id}` 三个**可空字符串**。
 *
 * 🔴 这里原先是个「兼容多种可能形状」的猜测式解析，猜的形状（`tasks` 数组、
 *    `{id, file_name}` 单任务对象）**后端一个都不沾** ⇒ 恒返回全空
 *    ⇒ `onUpload` 判定「没拿到任务」⇒ **进度轮询从不启动**。
 *    上传本身是成功的（后端已受理、Celery 在解析），但用户看到的是
 *    「未获取到任务进度信息」+ 空列表 —— 看起来像上传失败。
 *
 * 仍逐字段做类型检查而不是直接断言：后端某天把可空字段改成必填、
 * 或返回 `null` 而非对象时，要退化成一个「拿不到 id」的结果（前端会提示），
 * 而不是抛异常让整条上传链路崩掉 —— 上传是 A2 的主路径。
 */
function normalizeUploadResult(data: unknown): UploadResult {
  const empty: UploadResult = { task_id: null, document_id: null, batch_id: null }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return empty

  const record = data as Record<string, unknown>
  const str = (value: unknown): string | null =>
    typeof value === 'string' && value.length > 0 ? value : null

  return {
    task_id: str(record.task_id),
    document_id: str(record.document_id),
    batch_id: str(record.batch_id),
  }
}

/** 拼上传用的 FormData。`category` 为空时**不传该键**，避免后端把空串当成一个分类 */
function buildUploadForm(files: File[], category: string): FormData {
  const form = new FormData()
  for (const file of files) form.append('files', file)
  if (category) form.append('category', category)
  return form
}

/**
 * 丢掉取值为空串的查询参数。
 *
 * 🔴 **空串 ≠ 未传**，这条踩过一次且症状极具误导性：
 *    `EMPTY_DOCUMENT_QUERY` 用 `''` 表示「不筛选」，但后端 `DocumentListQuery.status`
 *    是强类型枚举（`DocumentStatus | None`，字典 §9 #15 定的三态）。axios 会把
 *    `status: ''` 序列化成 `?status=`，FastAPI 解析枚举失败 → **整个接口 400**，
 *    于是台账列表**整页空白**。看起来像「前端没渲染」，实际是请求根本没成功。
 *
 *    修法对齐同文件 `buildUploadForm`：**没值就不带这个键**。
 *    只丢空串，不动 `0` / `false`（它们可能是合法取值）。
 */
function dropEmptyParams(params: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(params).filter(([, value]) => value !== ''))
}

export const importApi = {
  /** 台账分页列表（含权限标签） */
  listDocuments: (params: DocumentListQuery) =>
    get<DocumentListResult>('/api/import/documents', dropEmptyParams({ ...params })),

  /**
   * 单篇上传。
   *
   * 🔒 **不传 `uploaded_by`**：该字段是**壳子里的客户端传参**，be-04 落地时**已删除**
   * （红线 1 / PRD §11.3 R3：身份只能从 JWT 解析）。前端一个身份字段都不构造。
   */
  uploadSingle: async (file: File, category: string): Promise<UploadResult> =>
    normalizeUploadResult(await post<unknown>('/api/import/documents', buildUploadForm([file], category))),

  /** 批量上传（多文件 / 文件夹）。`files` 逐项 append，与后端的 `list[UploadFile]` 对齐 */
  uploadBatch: async (files: File[], category: string): Promise<UploadResult> =>
    normalizeUploadResult(
      await post<unknown>('/api/import/documents/batch', buildUploadForm(files, category)),
    ),

  /** 任务列表（批量进度总览，抽屉轮询体默认走这个而非逐个刷新） */
  listTasks: (batchId?: string) =>
    get<ImportTaskVo[]>('/api/import/tasks', batchId ? { batch_id: batchId } : undefined),

  /** 单个任务进度（仅在需要精准刷新个别任务时用） */
  getTask: (taskId: string) => get<ImportTaskVo>(`/api/import/tasks/${taskId}`),

  /** 失败任务重试 */
  retryTask: (taskId: string) => post<null>(`/api/import/tasks/${taskId}/retry`),

  /** 编辑知识单元（标题 / 分类 / 正文）。改正文会触发后端全量重建 */
  updateDocument: (documentId: string, payload: DocumentUpdatePayload) =>
    put<KbDocumentVo>(`/api/import/documents/${documentId}`, payload),

  /**
   * 启用 / 停用。
   * 传 `{ status }` 而非 `{ enabled }`：与端点名 `/status`、三态生命周期一致（spec §9-13）。
   */
  updateDocumentStatus: (documentId: string, enabled: boolean) =>
    put<KbDocumentVo>(`/api/import/documents/${documentId}/status`, {
      status: enabled ? 'enabled' : 'disabled',
    }),

  /** 软删除（后端异步清 Milvus，接口成功 ≠ 索引已清干净） */
  deleteDocument: (documentId: string) => del<null>(`/api/import/documents/${documentId}`),

  /** 配置四维数据权限。**全量覆盖**语义，不是增量补丁（fe-02 §8） */
  updatePermissions: (documentId: string, payload: PermissionPayload) =>
    put<null>(`/api/import/documents/${documentId}/permissions`, payload),

  /**
   * 权限回显。✅ 2026-10-14：be-04 **已实现**（`import_router.py:203`），
   * 原注释「❌ 后端未定义」已作废。后端回 `PermissionView`，
   * 与 `PermissionPayload` **同构** ⇒ 可直接回填表单。
   *
   * 🔴 这个接口**不是可选项**：`PUT` 是全量替换语义，不回显的话弹窗每次都是空的，
   * 管理员一保存就把原权限**覆盖成全空**（验收 A3 原文要求「权限弹窗回显正确」）。
   */
  getPermissions: (documentId: string) =>
    get<PermissionPayload>(`/api/import/documents/${documentId}/permissions`),

  /**
   * 单条详情（取 `source_markdown`）。✅ 2026-10-14：be-04 **已实现**
   * （`import_router.py:134`），原注释「❌ 后端未定义」已作废。
   *
   * 返回 `KbDocumentDetailVo` 而非 `KbDocumentVo`：详情比列表多一个 `source_markdown`
   * （后端 `DocumentDetail(DocumentListItem)` 只加这一个字段）。
   */
  getDocument: (documentId: string) =>
    get<KbDocumentDetailVo>(`/api/import/documents/${documentId}`),

  // ---- 组织选项（权限弹窗用；落点见本文件头注释） ----

  listDepartments: () => get<DepartmentNode[]>('/api/org/departments/tree'),

  listRoles: () => get<RoleBrief[]>('/api/org/roles'),

  searchUsers: (keyword: string, page = 1, pageSize = 20) =>
    get<PageResult<UserBrief>>('/api/org/users', { keyword, page, page_size: pageSize }),
}
