import { defineStore } from 'pinia'

import { importApi } from '@/api/import'
import { DEFAULT_PAGE_SIZE, EMPTY_DOCUMENT_QUERY } from '@/constants/kb'

import type { DepartmentNode, RoleBrief, UserBrief } from '@/types/org'
import type {
  DocumentQuery,
  DocumentUpdatePayload,
  ImportTaskStatus,
  ImportTaskVo,
  KbDocumentDetailVo,
  KbDocumentVo,
  PermissionPayload,
  UploadResult,
} from '@/types/kb'

/** 任务终态。轮询的停止条件只看这两个（字典 §5.2） */
const TERMINAL_STATUSES: readonly ImportTaskStatus[] = ['success', 'failed']

/**
 * 把「可能是裸数组、也可能是分页对象」的响应归一成数组。
 *
 * 这是**跨域共用**的兜底：本页同时消费导入域（`/api/import/*`）、组织域
 * （`/api/org/*`：部门树 / 角色 / 用户）与权限域。
 * ✅ 2026-10-14：这几域的响应形状**都已冻结**（be-02 / be-04 均已实现并回核）——
 * 保留归一化是因为「列表端点回裸数组、用户选择器回分页对象」这个差异**本来就是真的**
 * （`GET /api/org/roles` 回数组，`GET /api/org/users` 回 `{list,total,...}`），
 * 而这一个函数同时服务两者。不是「形状没定」的权宜之计。
 */
function asArray<T>(value: unknown): T[] {
  if (Array.isArray(value)) return value as T[]
  if (value && typeof value === 'object') {
    const list = (value as { list?: unknown }).list
    if (Array.isArray(list)) return list as T[]
  }
  return []
}

interface KbDocumentState {
  list: KbDocumentVo[]
  total: number
  /** 全量切片数汇总。列表接口未回该聚合时为 null，合计文案只显示条数（spec §9-14） */
  totalChunks: number | null
  page: number
  pageSize: number
  query: DocumentQuery
  loading: boolean
  /**
   * 导入任务字典，按 `task_id` 索引。
   * 用字典而非数组：进度是 2s 一次的高频**局部**更新，数组每次都要整表 diff 才能定位到某行。
   */
  tasks: Record<string, ImportTaskVo>
  /** 最近一次上传批次，轮询时带上可只拉本批任务 */
  currentBatchId: string
  drawerVisible: boolean
  orgOptions: {
    departments: DepartmentNode[]
    roles: RoleBrief[]
  }
  /** 分类下拉的可选值：优先用后端聚合，缺省时靠本页累积（spec §9-3） */
  categoryOptions: string[]
}

export const useKbDocumentStore = defineStore('kbDocument', {
  state: (): KbDocumentState => ({
    list: [],
    total: 0,
    totalChunks: null,
    page: 1,
    pageSize: DEFAULT_PAGE_SIZE,
    query: { ...EMPTY_DOCUMENT_QUERY },
    loading: false,
    tasks: {},
    currentBatchId: '',
    drawerVisible: false,
    orgOptions: { departments: [], roles: [] },
    categoryOptions: [],
  }),

  getters: {
    /** 非终态任务，供轮询判断是否还有必要继续 */
    activeTasks: (state): ImportTaskVo[] =>
      Object.values(state.tasks).filter((task) => !TERMINAL_STATUSES.includes(task.status)),

    /** 是否存在失败任务，决定抽屉是否展示「全部重试」 */
    hasFailedTask: (state): boolean =>
      Object.values(state.tasks).some((task) => task.status === 'failed'),

    /** 全部任务均终态（空集视作已终态）。轮询的停止条件 */
    allTasksSettled: (state): boolean =>
      Object.values(state.tasks).every((task) => TERMINAL_STATUSES.includes(task.status)),

    /**
     * 抽屉里按创建时间倒序展示（最新的在最上面）。
     *
     * 🔴 **比较函数必须容忍 `created_at` 缺失**，这不是防御性洁癖：
     * 这个 getter 原先写的是 `b.created_at.localeCompare(...)`，而
     * `created_at` 一度**不在后端 DTO 里**（`lifecycle._to_task_dto` 漏抄了这一列），
     * 于是 `undefined.localeCompare` 抛 `TypeError` ——
     * getter 一抛，模板里 `v-if="tasks.length === 0"` 就先炸，
     * **整块进度区渲染不出来**，用户看到的是「上传完什么都没出现」。
     * 排序错一点只是观感问题；**渲染全炸则是「上传成功被显示成失败」**，代价不对称。
     *
     * ⚠️ 这是**兜底不是契约**：`created_at` / `batch_id` 是后端必发字段
     * （已补进 `ImportTaskDetail`），契约由 `verify_import.py` 的
     * 「前端类型 ↔ 后端 DTO 逐字段对账」静态用例守着。
     * 若哪天那条用例变红，**去补后端 DTO，不要靠这里继续兜** ——
     * 那样只会让契约漂移重新变得无法察觉。
     */
    sortedTasks: (state): ImportTaskVo[] =>
      Object.values(state.tasks).sort((a, b) => {
        const left = a.created_at ?? ''
        const right = b.created_at ?? ''
        if (left === right) return a.id.localeCompare(b.id) // 同秒时给一个稳定序
        return right.localeCompare(left)
      }),
  },

  actions: {
    // ---- 台账 ----

    async fetchList(): Promise<void> {
      this.loading = true
      try {
        const result = await importApi.listDocuments({
          ...this.query,
          page: this.page,
          page_size: this.pageSize,
        })
        this.list = result.list ?? []
        this.total = result.total ?? 0
        this.totalChunks = typeof result.total_chunks === 'number' ? result.total_chunks : null
        this.rememberCategories(result.categories, this.list)
      } finally {
        // 放 finally：请求失败也必须摘掉 loading，否则表格永远转圈、用户以为页面卡死
        this.loading = false
      }
    },

    /**
     * 合并筛选条件并**重置回第一页**。
     * 不重置会出现「筛完只剩 3 条却停在第 5 页」的空表，用户以为数据没了。
     */
    setQuery(patch: Partial<DocumentQuery>): void {
      this.query = { ...this.query, ...patch }
      this.page = 1
    },

    setPage(page: number, pageSize?: number): void {
      this.page = page
      if (pageSize !== undefined && pageSize !== this.pageSize) {
        this.pageSize = pageSize
        this.page = 1 // 页长变了，原页码可能越界
      }
    },

    /**
     * 累积分类选项。优先采信后端聚合（`categories`），
     * 否则从当前页已见分类里补——这是 spec §9-3 的降级路径：
     * 分类下拉退化为「已见值 + 自由输入」，不引入第二处本地存储。
     */
    rememberCategories(aggregated: string[] | undefined, rows: KbDocumentVo[]): void {
      const next = new Set(this.categoryOptions)
      if (aggregated?.length) {
        for (const item of aggregated) if (item) next.add(item)
      } else {
        for (const row of rows) if (row.category) next.add(row.category)
      }
      this.categoryOptions = [...next].sort((a, b) => a.localeCompare(b))
    },

    // ---- 导入 ----

    /** 把后端返回的任务并进字典。以上传返回值写入，避免等下一次轮询才有行可看 */
    mergeTasks(tasks: ImportTaskVo[]): void {
      for (const task of tasks) this.tasks[task.id] = task
    },

    async uploadSingle(file: File, category: string): Promise<UploadResult> {
      const result = await importApi.uploadSingle(file, category)
      await this.acceptUpload(result)
      return result
    },

    async uploadBatch(files: File[], category: string): Promise<UploadResult> {
      const result = await importApi.uploadBatch(files, category)
      await this.acceptUpload(result)
      return result
    },

    /**
     * 受理回执 → store 状态。
     *
     * 🔴 **回执里没有任务对象**（`UploadAccepted` 只有三个 id），所以「有行可看」
     * 必须**再发一次 GET** —— 单篇按 `task_id` 取那一条，批量按 `batch_id` 拉全批。
     * 原实现直接读 `result.tasks`（后端不返回该字段）⇒ 永远是空的 ⇒
     * 进度抽屉一片空白而且轮询不启动。
     *
     * 单篇**不**走 `fetchTasks()`（那会带上 `currentBatchId`，而单篇它是空串
     * ⇒ 退化成「拉全库最近任务」），所以用 `getTask` 精确取一条。
     */
    async acceptUpload(result: UploadResult): Promise<void> {
      this.currentBatchId = result.batch_id ?? ''

      if (result.batch_id) {
        await this.fetchTasks()
      } else if (result.task_id) {
        this.mergeTasks([await importApi.getTask(result.task_id)])
      }
    },

    /** 拉任务列表并**以服务端为准**覆盖本地项（本地只是乐观占位） */
    async fetchTasks(): Promise<void> {
      const tasks = asArray<ImportTaskVo>(await importApi.listTasks(this.currentBatchId || undefined))
      this.mergeTasks(tasks)
    },

    /** 重试：把该任务状态乐观重置为 pending 并清掉失败原因，让用户立刻看到「动起来了」 */
    async retryTask(taskId: string): Promise<void> {
      await importApi.retryTask(taskId)
      const task = this.tasks[taskId]
      if (task) {
        this.tasks[taskId] = { ...task, status: 'pending', progress: 0, error_msg: null }
      }
    },

    /** 清空任务集合（抽屉关闭并全部终态后调用，避免下次打开看到上次的旧任务） */
    clearSettledTasks(): void {
      if (!this.allTasksSettled) return
      this.tasks = {}
      this.currentBatchId = ''
    },

    // ---- 台账写操作（均为「接口成功 → 局部更新」，不整页重拉以避免闪烁） ----

    async updateDocument(id: string, payload: DocumentUpdatePayload): Promise<void> {
      const updated = await importApi.updateDocument(id, payload)
      this.patchRow(id, updated)
    },

    async toggleStatus(id: string, enabled: boolean): Promise<void> {
      const updated = await importApi.updateDocumentStatus(id, enabled)
      // 后端若换了返回形状（不回实体），退化为只改本地状态，避免整页重拉
      if (updated) this.patchRow(id, updated)
      else {
        const row = this.list.find((item) => item.id === id)
        if (row) row.status = enabled ? 'enabled' : 'disabled'
      }
    },

    /**
     * 删除。**不做乐观更新**：删除涉及 Milvus 异步清理，接口成功 ≠ 索引已清干净，
     * 所以以接口响应为准移除行，再由调用方拉一次列表兜底（fe-02 §4.3 ④）。
     */
    async deleteDocument(id: string): Promise<void> {
      await importApi.deleteDocument(id)
      this.list = this.list.filter((item) => item.id !== id)
      if (this.total > 0) this.total -= 1
    },

    /** 局部替换某行。找不到就忽略（可能已被他人删除，或不在当前页） */
    patchRow(id: string, updated: KbDocumentVo | null | undefined): void {
      if (!updated?.id) return
      const index = this.list.findIndex((item) => item.id === id)
      if (index >= 0) this.list[index] = updated
    },

    // ---- 权限 ----

    /**
     * 读取当前权限。✅ 2026-10-14：be-04 **已实现**（`import_router.py:203`）。
     * 仍保留抛错即降级（弹窗顶部告警 + 提交置灰）—— 那是防「无回显时盲写全量覆盖」，
     * 与接口是否已实现无关。
     */
    loadPermissions(id: string): Promise<PermissionPayload> {
      return importApi.getPermissions(id)
    },

    /**
     * 读取单条详情。✅ 2026-10-14：be-04 **已实现**（`import_router.py:134`）。
     * 比列表多一个 `source_markdown`（编辑正文的唯一来源）。
     */
    loadDocument(id: string): Promise<KbDocumentDetailVo> {
      return importApi.getDocument(id)
    },

    async updatePermissions(id: string, payload: PermissionPayload): Promise<void> {
      await importApi.updatePermissions(id, payload)
      // 权限标签变了，但后端不回新标签 → 用本地 payload 反推 scopes 先更新观感
      this.patchPermissionScopes(id, payload)
    },

    patchPermissionScopes(id: string, payload: PermissionPayload): void {
      const row = this.list.find((item) => item.id === id)
      if (!row) return
      const scopes: KbDocumentVo['permission_scopes'] = []
      if (payload.global_public) scopes.push('global')
      if (payload.departments.length) scopes.push('department')
      if (payload.roles.length) scopes.push('role')
      if (payload.users.length) scopes.push('user')
      row.permission_scopes = scopes
      // summary 无法本地合成（后端才拿得到实体名），置空让组件走兜底文案
      row.permission_summary = null
    },

    // ---- 组织选项 ----

    /** 低频变更，进页面加载一次即可，不随筛选刷新 */
    async fetchOrgOptions(): Promise<void> {
      const [departments, roles] = await Promise.all([
        importApi.listDepartments(),
        importApi.listRoles(),
      ])
      this.orgOptions.departments = asArray<DepartmentNode>(departments)
      this.orgOptions.roles = asArray<RoleBrief>(roles)
    },

    /**
     * 权限弹窗「指定用户」的远程搜索。
     * 放 store 而不是让页面直接调 api：调用链单向（组件 → store → api），
     * 也让并发序号守卫这类状态有个统一的落点。
     */
    async searchUsers(keyword: string, page = 1, pageSize = 20): Promise<UserBrief[]> {
      const result = await importApi.searchUsers(keyword, page, pageSize)
      return asArray<UserBrief>(result)
    },
  },
})
