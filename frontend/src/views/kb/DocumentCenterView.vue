<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { FilePlus, Upload } from 'lucide-vue-next'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { ApiError } from '@/api/http'
import PageTopbar from '@/components/PageTopbar.vue'
import { useImportPolling } from '@/composables/useImportPolling'
import { OPEN_IMPORT_QUERY_KEY, OPEN_IMPORT_QUERY_VALUE } from '@/constants/governance'
import {
  EMPTY_DOCUMENT_QUERY,
  FOCUS_DOCUMENT_QUERY_ALIASES,
  FOCUS_DOCUMENT_QUERY_KEY,
  KB_PERMS,
  LIST_REFRESH_THROTTLE_MS,
  USER_SEARCH_DEBOUNCE_MS,
} from '@/constants/kb'
import { useKbDocumentStore } from '@/stores/kbDocument'

import DocumentEditDialog from './components/DocumentEditDialog.vue'
import DocumentTable from './components/DocumentTable.vue'
import FilterBar from './components/FilterBar.vue'
import ImportDrawer from './components/ImportDrawer.vue'
import PermissionDialog from './components/PermissionDialog.vue'

import type { UserBrief } from '@/types/org'
import type {
  DocumentQuery,
  DocumentUpdatePayload,
  KbDocumentVo,
  PermissionPayload,
} from '@/types/kb'

import 'element-plus/es/components/message/style/css'

/**
 * 知识维护与导入中心（fe-02）。
 *
 * 页面唯一职责是**编排**：把筛选、表格、抽屉、两个弹窗与轮询的生命周期接起来，
 * 具体渲染交给 5 个子组件，具体数据交给 store。本组件里不应出现任何布局细节之外的
 * 业务规则——否则「谁在什么时候发请求」会散成一片。
 *
 * 骨架严格按 `.app-page` / `.app-page__body`（全局类）+ `PageTopbar`：
 * 本页挂在 `AppLayout` 下，**不画侧栏**。
 */
const store = useKbDocumentStore()
const route = useRoute()
const router = useRouter()

// ---- 导入抽屉 ----

/** 抽屉显隐走 store：轮询的停止条件之一就是它变 false（fe-02 §4.3 ① 第 6 步） */
const drawerVisible = computed({
  get: () => store.drawerVisible,
  set: (value: boolean) => {
    store.drawerVisible = value
  },
})

const importMode = ref<'single' | 'batch'>('batch')
const uploading = ref(false)
/** 上传成功后自增，让抽屉清空待上传列表（不用乐观清空，见 ImportDrawer 注释） */
const importResetToken = ref(0)

// ---- 定位高亮 ----

/** 跨页跳转定位到的文档 id。空串 = 无定位。只负责「亮哪一行」，找不到时的提示在 focusDocument 里 */
const focusedDocumentId = ref('')

// ---- 权限弹窗 ----

const permVisible = ref(false)
const permDocument = ref<KbDocumentVo | null>(null)
const permInitial = ref<PermissionPayload | null>(null)
const permLoading = ref(false)
const permLoadError = ref(false)
const permSubmitting = ref(false)
const userOptions = ref<UserBrief[]>([])
/** 远程搜索的序号守卫：晚发的请求先回来会把新结果覆盖成旧结果 */
let userSearchSeq = 0
let userSearchTimer: ReturnType<typeof setTimeout> | null = null

// ---- 编辑弹窗 ----

const editVisible = ref(false)
const editDocument = ref<KbDocumentVo | null>(null)
const editSourceMarkdown = ref<string | null>(null)
const editLoading = ref(false)
const editLoadError = ref(false)
const editSubmitting = ref(false)

/**
 * 统一的错误提示。后端 `message` 是**纯文本**，一律插值渲染，
 * 绝不 v-html（避免后端消息被当富文本执行，spec §7 附加纪律）。
 */
function showError(error: unknown, fallback: string): void {
  const message = error instanceof ApiError ? error.message : fallback
  ElMessage.error(message || fallback)
}

// ---- 列表刷新节流 ----

let lastListRefreshAt = 0

/** 导入全部完成 / 删除后重拉台账。节流避免连续重试时打出一串并发请求 */
function refreshListThrottled(): void {
  const now = Date.now()
  if (now - lastListRefreshAt < LIST_REFRESH_THROTTLE_MS) return
  lastListRefreshAt = now
  void store.fetchList()
}

// ---- 轮询 ----

const polling = useImportPolling({
  // 全部任务终态：重拉台账（新导入的知识单元此刻才出现在列表里）
  onSettled: () => refreshListThrottled(),
  // 到达 15 分钟上限仍在跑：把「任务疑似卡死」这件事显式说出来。
  // 后端 Celery `include` 漏注册会让任务永远 pending，不提示的话用户只会看到一条永远不动的进度条。
  onTimeout: () =>
    ElMessage.warning('任务长时间未推进，请稍后重试或联系管理员（可能后端任务未正常执行）'),
  onError: (failures) => ElMessage.error(`进度更新连续失败 ${failures} 次，已暂停轮询`),
})

watch(
  () => store.drawerVisible,
  (visible) => {
    if (visible) {
      // 重新打开抽屉：若还有非终态任务就恢复轮询。
      // 不恢复的话，用户中途关掉抽屉再打开会看到一条**冻在关闭那一刻**的进度条，
      // 既不知道是否还在跑，也永远等不到它动。
      if (store.activeTasks.length > 0) polling.start()
      return
    }
    // 关闭 → 停轮询（fe-02 §4.3 ① 第 6 步），并清掉已终态任务避免下次看到旧进度
    polling.stop()
    store.clearSettledTasks()
  },
)

// 路由离开：组件若被 keep-alive 缓存，onScopeDispose 不会触发，必须单独兜住
onBeforeRouteLeave(() => {
  polling.stop()
})

onBeforeUnmount(() => {
  polling.stop()
  if (userSearchTimer !== null) clearTimeout(userSearchTimer)
})

onMounted(async () => {
  // 组织选项低频变更，进页面加载一次即可；失败不阻断台账浏览，故单独 catch
  void store.fetchOrgOptions().catch(() => {
    /* 权限弹窗打开时会再试一次（见 loadPermissionDetail） */
  })

  await store.fetchList()
  // 必须在列表到手**之后**再处理跳转定位：否则「目标其实就在当前页」也永远判定为 miss
  await consumeRouteQuery()
})

// 同路由内 query 变化不会重新挂载组件（如从看板再点一次榜单项）。
// 不监听的话这类跳转仍然静默失效。
watch(
  () => route.query,
  () => {
    void consumeRouteQuery()
  },
)

// ---- 跨页跳转参数的消费 ----

/**
 * 三处跨页跳转会带 query 进来，本页必须**全部认领**，否则就是静默失效
 * （跳过去了、什么都不发生、不报错——这是最难查的一类 bug）：
 *
 * | 发出方 | 参数 | 预期 |
 * |---|---|---|
 * | `views/kb/GovernanceView.vue`（fe-06 转建补料） | `openImport=1` | 自动打开导入抽屉 |
 * | `views/dashboard/DashboardView.vue`（fe-05 下钻） | `document_id=<id>` | 定位并高亮该文档 |
 * | `views/chat/ChatView.vue`（fe-03 引用跳转） | `highlight=<id>` | 同上（过渡别名） |
 *
 * 这些是**一次性动作指令**，不是页面状态：消费完立刻 `router.replace` 抹掉。
 * 留在 URL 上的后果是「从同一榜单再点一次同一条目」时 URL 没变 → 不触发跳转 →
 * 又变成「点了没反应」。
 */
async function consumeRouteQuery(): Promise<void> {
  const query = route.query
  let consumed = false

  if (readQueryValue(query[OPEN_IMPORT_QUERY_KEY]) === OPEN_IMPORT_QUERY_VALUE) {
    openImportDrawer('batch') // 转建补料通常是批量，单篇走页面内按钮
    consumed = true
  }

  const focusId = readFocusId(query)
  if (focusId) {
    consumed = true
    await focusDocument(focusId)
  }

  if (consumed) await router.replace({ query: {} })
}

/** query 值可能是 string / string[] / null（vue-router 三种都可能给），统一取第一个字符串 */
function readQueryValue(value: unknown): string {
  if (typeof value === 'string') return value
  if (Array.isArray(value) && typeof value[0] === 'string') return value[0]
  return ''
}

/** 主键优先 `document_id`，失败再退到别名 `highlight` */
function readFocusId(query: Record<string, unknown>): string {
  const primary = readQueryValue(query[FOCUS_DOCUMENT_QUERY_KEY])
  if (primary) return primary
  for (const alias of FOCUS_DOCUMENT_QUERY_ALIASES) {
    const value = readQueryValue(query[alias])
    if (value) return value
  }
  return ''
}

/**
 * 定位到某条文档：高亮其行并滚动入视口。
 *
 * 🔴 这里有个**只在集成时才暴露**的坑：`GET /api/import/documents` 是分页接口，
 * 且项目里**没有「按 id 取单篇」的接口**（就是 spec §9-2 那条未补的 #71），
 * 所以目标文档**很可能不在当前页**。绝不能「找不到就什么都不做」——
 * 那是跳转失效最典型的表现：用户点一下、页面闪一下、没有任何反馈，只会以为系统坏了。
 *
 * 策略（最多多打 **1** 次请求，不新增任何后端契约）：
 *   ① 当前页找到 → 高亮 + 滚动；
 *   ② 没找到且当前有筛选条件 → 清空筛选回第 1 页再找，并明确告知「已清空筛选」
 *      （跳转的语义是「我要看这条」，优先于用户上次留下的筛选条件）；
 *   ③ 还没找到 → **如实提示**，并靠「总数 > 本页条数」区分「可能在其他页」与「可能已没权限/已删」。
 */
async function focusDocument(documentId: string): Promise<void> {
  if (!findRow(documentId) && hasActiveFilter()) {
    store.setQuery({ ...EMPTY_DOCUMENT_QUERY })
    await store.fetchList()
    ElMessage.info('已清空筛选条件以定位该文档')
  }

  if (findRow(documentId)) {
    focusedDocumentId.value = documentId
    return
  }

  // 找不到也必须说话（这就是本次修复的核心目的）
  focusedDocumentId.value = ''
  if (store.total > store.list.length) {
    ElMessage.warning(`该文档不在第 1 页结果中（共 ${store.total} 条），请用标题搜索定位`)
  } else {
    ElMessage.warning('未找到该文档：可能已被删除，或你没有查看权限')
  }
}

function findRow(documentId: string): KbDocumentVo | undefined {
  return store.list.find((item) => item.id === documentId)
}

function hasActiveFilter(): boolean {
  const query = store.query
  return Boolean(query.keyword || query.category || query.status || query.permission_scope)
}

function clearFocus(): void {
  focusedDocumentId.value = ''
}

// ---- 筛选与分页 ----

function onQueryChange(patch: Partial<DocumentQuery>): void {
  store.setQuery(patch) // setQuery 内部已把 page 重置为 1
  clearFocus() // 换了筛选条件，之前的定位高亮已经没有意义
  void store.fetchList()
}

function onSearch(): void {
  clearFocus()
  void store.fetchList()
}

function onPageChange(page: number, pageSize: number): void {
  store.setPage(page, pageSize)
  clearFocus() // 翻页后高亮行已经不在屏上，留着会让下次定位的判断出错
  void store.fetchList()
}

// ---- 导入 ----

/**
 * 打开导入抽屉。
 * 函数名带 `Drawer` 后缀是为了与 query key `openImport` 区分开——
 * 两者同名时，`openImport(OPEN_IMPORT_QUERY_KEY)` 这种写法没人看得懂是谁调谁。
 */
function openImportDrawer(mode: 'single' | 'batch'): void {
  importMode.value = mode
  store.drawerVisible = true
}

async function onUpload(payload: { files: File[]; category: string }): Promise<void> {
  uploading.value = true
  try {
    const result =
      importMode.value === 'batch'
        ? await store.uploadBatch(payload.files, payload.category)
        : await store.uploadSingle(payload.files[0] as File, payload.category)

    importResetToken.value += 1 // 提交成功才清空待传列表
    ElMessage.success(`已提交 ${payload.files.length} 个文件，正在解析`)

    // 受理回执只有 id（单篇 `task_id` / 批量 `batch_id`），**没有任务对象** ——
    // store 已经拿它补拉过一次，所以这里按 id 判断「有没有东西可轮询」。
    if (result.task_id || result.batch_id) {
      polling.start()
    } else {
      // 真正拿不到 id 才是异常（后端形状变了 / 返回了 null）：
      // 宁可提示一句，也不要让用户对着空进度列表干等。
      ElMessage.warning('未获取到任务进度信息，请稍后在列表中查看结果')
    }
  } catch (error) {
    // 单次失败/超限不回滚已提交成功的文件，只提示（fe-02 §6）
    showError(error, '提交失败，请重试')
  } finally {
    uploading.value = false
  }
}

async function onRetry(taskId: string): Promise<void> {
  try {
    await store.retryTask(taskId)
    polling.start() // 重试后必须重新入轮询
  } catch (error) {
    showError(error, '重试失败')
  }
}

async function onRetryAll(): Promise<void> {
  const failed = Object.values(store.tasks).filter((task) => task.status === 'failed')
  if (failed.length === 0) return
  try {
    await Promise.all(failed.map((task) => store.retryTask(task.id)))
    polling.start()
  } catch (error) {
    showError(error, '批量重试失败')
  }
}

// ---- 编辑 ----

function onEdit(row: KbDocumentVo): void {
  editDocument.value = row
  editSourceMarkdown.value = null
  editLoadError.value = false
  editVisible.value = true
}

/**
 * 弹窗打开后拉详情取正文。
 * ✅ 2026-10-14：be-04 **已实现**（`import_router.py:134`），原注释「❌ 接口缺失」已作废。
 * 失败降级（只能改标题与分类）保留 —— 那是防「详情接口偶发失败」让整个编辑弹窗不可用。
 */
async function loadDocumentDetail(): Promise<void> {
  const row = editDocument.value
  if (!row) return
  editLoading.value = true
  editLoadError.value = false
  try {
    const detail = await store.loadDocument(row.id)
    editSourceMarkdown.value = detail?.source_markdown ?? null
    if (editSourceMarkdown.value === null) editLoadError.value = true
  } catch {
    // 这里刻意不弹 ElMessage：弹窗内已有 el-alert 说明原因，
    // 再弹一个全局提示只是噪音
    editLoadError.value = true
  } finally {
    editLoading.value = false
  }
}

async function onSubmitEdit(payload: DocumentUpdatePayload): Promise<void> {
  const row = editDocument.value
  if (!row) return
  editSubmitting.value = true
  try {
    await store.updateDocument(row.id, payload)
    editVisible.value = false
    // 提示语必须点明「可能滞后」，否则用户会怀疑保存没生效（fe-02 §4.3 ③ 第 5 步）
    ElMessage.success('已更新，切片与向量索引正在重建，期间检索结果可能滞后')
  } catch (error) {
    showError(error, '保存失败')
  } finally {
    editSubmitting.value = false
  }
}

// ---- 权限 ----

function onPermission(row: KbDocumentVo): void {
  permDocument.value = row
  permInitial.value = null
  permLoadError.value = false
  permVisible.value = true
}

/**
 * 权限回显。✅ 2026-10-14：be-04 **已实现**（`import_router.py:203`），
 * 原注释「❌ 未定义」已作废。
 *
 * 🔴 失败时**仍不禁用整个弹窗**，只禁用提交 —— 这条**与接口是否已实现无关**：
 * `PUT` 是全量替换语义，拿不到回显就保存会把原权限覆盖成全空，属数据破坏性缺陷。
 * 宁可让管理员这次改不了，也不能让他静默清空。
 */
async function loadPermissionDetail(): Promise<void> {
  const row = permDocument.value
  if (!row) return
  permLoading.value = true
  permLoadError.value = false
  try {
    permInitial.value = await store.loadPermissions(row.id)
  } catch {
    permInitial.value = null
    permLoadError.value = true
  } finally {
    permLoading.value = false
  }
  // 组织选项若进页面时加载失败，在这里补一次
  if (store.orgOptions.departments.length === 0 && store.orgOptions.roles.length === 0) {
    void store.fetchOrgOptions().catch(() => {
      /* 选项拉不到时权限弹窗仍可展示，只是选不了，不阻断 */
    })
  }
}

async function onSubmitPermission(payload: PermissionPayload): Promise<void> {
  const row = permDocument.value
  if (!row) return
  permSubmitting.value = true
  try {
    await store.updatePermissions(row.id, payload)
    permVisible.value = false
    ElMessage.success('权限已生效')
  } catch (error) {
    showError(error, '权限保存失败')
  } finally {
    permSubmitting.value = false
  }
}

function onSearchUsers(keyword: string): void {
  if (userSearchTimer !== null) clearTimeout(userSearchTimer)

  if (!keyword) {
    userOptions.value = []
    return
  }

  userSearchTimer = setTimeout(() => {
    userSearchTimer = null
    const seq = ++userSearchSeq
    void store
      .searchUsers(keyword)
      .then((users) => {
        if (seq !== userSearchSeq) return // 旧请求的迟到结果，丢弃
        userOptions.value = users
      })
      .catch(() => {
        if (seq === userSearchSeq) userOptions.value = []
      })
  }, USER_SEARCH_DEBOUNCE_MS)
}

// ---- 启停与删除 ----

async function onToggleStatus(row: KbDocumentVo): Promise<void> {
  const enable = row.status !== 'enabled'
  try {
    await store.toggleStatus(row.id, enable)
    ElMessage.success(enable ? '已启用，该文档恢复参与检索' : '已停用，该文档不再参与检索')
  } catch (error) {
    showError(error, enable ? '启用失败' : '停用失败')
  }
}

/**
 * 删除。**不做乐观更新**：删除涉及 Milvus 异步清理，接口成功 ≠ 索引已清干净，
 * 所以只在接口成功后移除行，再拉一次列表兜底（fe-02 §4.3 ④）。
 */
async function onDelete(row: KbDocumentVo): Promise<void> {
  try {
    await store.deleteDocument(row.id)
    ElMessage.success('已删除')
    void store.fetchList()
  } catch (error) {
    showError(error, '删除失败')
    void store.fetchList() // 404（已被他人删除）等场景以服务端为准
  }
}
</script>

<template>
  <div class="app-page">
    <PageTopbar title="知识维护与导入中心">
      <!-- ⚠️ v-permission 只做**显隐体验**，不是安全边界：后端对每个写接口独立校验功能权限
           （CLAUDE.md 红线 7）。手工构造无权限请求必须返回 403 —— 这是 A1 的验证点。 -->
      <el-button v-permission="KB_PERMS.upload" type="primary" @click="openImportDrawer('batch')">
        <Upload :size="15" />
        <span class="topbar-btn-text">批量导入</span>
      </el-button>

      <el-button v-permission="KB_PERMS.upload" @click="openImportDrawer('single')">
        <FilePlus :size="15" />
        <span class="topbar-btn-text">新建知识单元</span>
      </el-button>
    </PageTopbar>

    <div class="app-page__body">
      <FilterBar
        :model-value="store.query"
        :category-options="store.categoryOptions"
        :total="store.total"
        :total-chunks="store.totalChunks"
        @update:model-value="onQueryChange"
        @search="onSearch"
      />

      <DocumentTable
        :list="store.list"
        :loading="store.loading"
        :total="store.total"
        :page="store.page"
        :page-size="store.pageSize"
        :focus-id="focusedDocumentId"
        @edit="onEdit"
        @permission="onPermission"
        @toggle-status="onToggleStatus"
        @delete="onDelete"
        @page-change="onPageChange"
      />
    </div>

    <ImportDrawer
      v-model:visible="drawerVisible"
      :uploading="uploading"
      :tasks="store.sortedTasks"
      :has-failed-task="store.hasFailedTask"
      :mode="importMode"
      :reset-token="importResetToken"
      @upload="onUpload"
      @retry="onRetry"
      @retry-all="onRetryAll"
    />

    <PermissionDialog
      v-model:visible="permVisible"
      :document-title="permDocument?.title || permDocument?.file_name || ''"
      :departments="store.orgOptions.departments"
      :roles="store.orgOptions.roles"
      :user-options="userOptions"
      :initial="permInitial"
      :loading="permLoading"
      :load-error="permLoadError"
      :submitting="permSubmitting"
      @open="loadPermissionDetail"
      @search-users="onSearchUsers"
      @submit="onSubmitPermission"
    />

    <DocumentEditDialog
      v-model:visible="editVisible"
      :document="editDocument"
      :source-markdown="editSourceMarkdown"
      :loading="editLoading"
      :load-error="editLoadError"
      :submitting="editSubmitting"
      @open="loadDocumentDetail"
      @submit="onSubmitEdit"
    />
  </div>
</template>

<style scoped>
.topbar-btn-text {
  margin-left: 6px;
}

/* 原型 BtnUpload 把图标与文字硬编码为 #FFFFFF（与 --kb-bg-card 同值，但不是同一语义，
   故单列 --kb-text-on-brand）。Element Plus 的 primary 按钮文字色默认取
   --el-color-white，这里显式落到令牌上，避免「改一次主题发现按钮文字没跟着变」。 */
.app-page :deep(.el-button--primary) {
  color: var(--kb-text-on-brand);
}
</style>
