/**
 * 知识库域的前端类型。与后端 `backend/schemas/` 一一对应（字典 §8）。
 *
 * 字段名**保持 snake_case**与后端一致，不做驼峰转换——转换层是 bug 温床，
 * 且与 axios 默认行为一致（fe-02 §5「命名约定」）。
 *
 * 枚举出处：字典 §5.1 生命周期 / §5.2 任务状态 / §5.3 entity_type。
 */

import type { PageResult } from '@/types/api'

/** 知识单元生命周期（字典 §5.1）。只有 `enabled` 参与检索。 */
export type KbDocumentStatus = 'draft' | 'enabled' | 'disabled'

/** 导入任务状态（字典 §5.2）。终态 = success | failed */
export type ImportTaskStatus = 'pending' | 'parsing' | 'embedding' | 'success' | 'failed'

/** 四维权限的授权维度（字典 §5.3） */
export type EntityType = 'global' | 'department' | 'role' | 'user'

/**
 * 「权限范围」筛选与展示用的维度别名。
 * 与 `EntityType` 同值，但语义不同：`EntityType` 是**写入**时的实体类型，
 * `PermissionScope` 是**读出**时「这条权限覆盖哪个维度」。
 * 分开命名是为了让 `PermissionTag` 的 props 不会误接一个写入用的枚举。
 */
export type PermissionScope = EntityType

/** 导入任务类型（字典 §2.2 `kb_import_task.type`） */
export type ImportTaskType = 'upload' | 'supplement'

/**
 * 知识单元台账行（字典 §2.2 `kb_document`）。
 *
 * 注意两处**不属于 `kb_document` 表**的字段（列表接口的聚合产出，见 spec §9-7）：
 * - `permission_scopes`：该文档已启用的授权维度；
 * - `permission_summary`：后端合成的权限标签文案（如「财务部 + 指定人」）。
 *
 * `source_markdown` **不在**本类型里——列表接口不返回长文本，
 * 正文只由 `getDocument`（详情接口，当前缺失）单独取，避免把 LONGTEXT 拖进列表。
 */
export interface KbDocumentVo {
  id: string
  title: string
  /** 原始文件名。库里可空（`varchar(255) NULL`）——故为 `| null` */
  file_name: string | null
  /** 扩展名：pdf / docx / md / txt …（不带点）。库里可空 */
  file_type: string | null
  category: string | null
  status: KbDocumentStatus
  chunk_count: number
  /** 上传人。**只读**——前端绝不回传该字段（红线 1，spec §7）。库里可空 */
  uploaded_by: string | null
  created_at: string
  updated_at: string
  /**
   * 已启用的授权维度集合。空数组 = 未配置任何权限（除知识管理员外无人可检索）。
   *
   * 🔴 **必给，不是可选**：`PermissionTag` 里 `props.scopes.includes(...)` 直接调它，
   * 拿到 `undefined` 会抛 `TypeError`，**整个台账表体渲染不出来**。
   * 后端 `DocumentListItem` 原先没有这个字段（放的是 `permission_summary` **对象**），
   * 2026-10-15 已补齐；`verify_import.py` 的 T1 对账用例守着它。
   */
  permission_scopes: PermissionScope[]
  /**
   * 后端合成的权限标签**文案**（如「限定部门 ×2 / 指定用户 ×3」）。
   * 一条权限都没配时为 `null`，此时前端按 `permission_scopes` 兜底（spec §9-7）。
   */
  permission_summary?: string | null
}

/**
 * 单条详情（`GET /api/import/documents/{id}`，**当前后端未定义**，字典 §9 #71）。
 *
 * 单独建一个类型而不是把 `source_markdown` 塞进 `KbDocumentVo`：
 * 列表接口**不返回**正文（LONGTEXT，不该拖进分页响应，见上面 `KbDocumentVo` 的说明），
 * 塞进同一个类型会让「列表里其实没有这个字段」变成编译期看不见的谎言——
 * 一旦有人写 `row.source_markdown` 就会在运行时拿到 `undefined` 而不是报错。
 * 两个接口 = 两个类型，用错的地方编译期就会红。
 */
export interface KbDocumentDetailVo extends KbDocumentVo {
  /** Markdown 正文。可能为 null（解析产物缺失） */
  source_markdown: string | null
}

/**
 * 单个导入任务。**与后端 `schemas/kb_import.py::ImportTaskDetail` 逐字段同构**
 * （字典 §2.2 `kb_import_task`）。
 *
 * 🔴 **2026-10-15 修正**：`batch_id` 与 `created_at` 原先是**声明的字段里后端不发的**——
 * `lifecycle._to_task_dto` 逐字段手抄 ORM 列时漏了这两列。`batch_id` 只是恒 `undefined`
 * （没人读它，无害），但 `created_at` 被 `store.sortedTasks` 直接 `.localeCompare()`，
 * 于是**抛异常 ⇒ 进度列表整块渲染不出来**。已把两列补进后端 DTO。
 *
 * ⚠️ **这个接口的字段不能靠「看着像」写** —— 它是手抄的，漏一个是必然不是意外。
 * `verify_import.py` 有一条静态用例把本接口的字段名与后端 DTO 逐一对账，改任一侧都会变红。
 */
export interface ImportTaskVo {
  id: string
  /** 单篇上传时为 `null`（后端 `str | None`）——**不要写成 `string`** */
  batch_id: string | null
  file_name: string | null
  /** 字节 */
  file_size: number
  type: ImportTaskType
  status: ImportTaskStatus
  /** 0–100 */
  progress: number
  total_chunks: number
  error_msg: string | null
  created_at: string
  finished_at: string | null
}

/**
 * 四维数据权限载荷。**与后端 `schemas/kb_import.py::PermissionPayload` 同名同构**
 * （字典 §6.3），提交语义是**全量覆盖**而非增量补丁（fe-02 §8）。
 */
export interface PermissionPayload {
  global_public: boolean
  departments: string[]
  roles: string[]
  users: string[]
}

/** 权限弹窗内部的表单态。与 `PermissionPayload` 同构，「指定用户」额外带展示名。 */
export interface PermissionForm {
  global_public: boolean
  departments: string[]
  roles: string[]
  users: string[]
}

/** 台账筛选条件（fe-02 §4.1）。`''` 表示「全部」，不参与下推。 */
export interface DocumentQuery {
  keyword: string
  category: string
  status: KbDocumentStatus | ''
  permission_scope: PermissionScope | 'none' | ''
}

/** 台账查询的 wire 参数（含分页） */
export interface DocumentListQuery extends DocumentQuery {
  page: number
  page_size: number
}

/**
 * 台账列表响应 = 标准分页 + 两个**可选**聚合（spec §9-14）。
 * 缺失时前端有降级路径（只看条数 / 分类退化为自由输入），不会因此报错。
 */
export interface DocumentListResult extends PageResult<KbDocumentVo> {
  /** 全量切片总数，供 FilterBar 的「共 N 个知识单元 · M 个切片」 */
  total_chunks?: number
  /** 分类去重聚合，供筛选栏分类下拉（fe-02 §10 #3 建议①） */
  categories?: string[]
}

/** 编辑知识单元（`PUT /api/import/documents/{id}`）。三个字段都是可选的局部更新。 */
export interface DocumentUpdatePayload {
  title?: string
  category?: string
  /** Markdown 正文。改动它会触发后端全量重建切片与向量 */
  source_markdown?: string
}

/**
 * 上传受理回执。**与后端 `UploadAccepted` 同构**（be-04 已实现，原 spec §9-17 已销）。
 *
 * 🔴 2026-10-14 更正：原先这里是 `{batch_id, tasks: ImportTaskVo[]}` ——
 * **后端从来不返回 `tasks`**，它回的是三个可空字符串的**扁平对象**：
 *
 * - **单篇**：`task_id` + `document_id` 有值，`batch_id` 为 `null`
 *   → 拿 `task_id` 去 `GET /api/import/tasks/{task_id}` 取那一条。
 * - **批量**：三者都有，但**每篇的 `task_id` / `document_id` 不在本响应里**
 *   （几十个文件逐个回传会让响应体膨胀且顺序无意义）
 *   → 拿 `batch_id` 去 `GET /api/import/tasks?batch_id=...` 拉全批。
 *
 * ⚠️ 按 `tasks` 解析的后果是**整条上传链路静默哑掉**：解析出空数组 ⇒
 * 进度轮询根本不启动 ⇒ 用户看到「未获取到任务进度信息」而其实上传已经受理了。
 */
export interface UploadResult {
  /** 单篇上传的任务 ID；批量时为后端给的占位值（不要用它去轮询单条） */
  task_id: string | null
  /** 单篇上传的文档 ID；批量时为 `null` */
  document_id: string | null
  /** 批量上传的批次 ID；单篇时为 `null`。用它拉全批进度 */
  batch_id: string | null
}
