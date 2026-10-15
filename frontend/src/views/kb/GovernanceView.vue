<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { RefreshCw } from 'lucide-vue-next'
import { computed, onMounted } from 'vue'
import { onBeforeRouteLeave, useRouter } from 'vue-router'

import PageTopbar from '@/components/PageTopbar.vue'
import { ApiError } from '@/api/http'
import {
  DIRTY_CONFIRM_TEXT,
  DIRTY_CONFIRM_TITLE,
  ERROR_FALLBACK,
  ERROR_NO_PERMISSION,
  ERROR_STALE_RECORD,
  GAP_TASK_DONE_CANCEL,
  GAP_TASK_DONE_CONFIRM,
  GAP_TASK_DONE_TITLE,
  GAP_TASK_PROMPT_PLACEHOLDER,
  GAP_TASK_PROMPT_TITLE,
  GAP_TASK_SUCCESS_TOAST,
  MINE_BUTTON_TOOLTIP,
  MINE_DONE_TEXT,
  MINE_EMPTY_TEXT,
  MINE_RUNNING_TEXT,
  MINE_TIMEOUT_TEXT,
  OPEN_IMPORT_QUERY_KEY,
  OPEN_IMPORT_QUERY_VALUE,
  PERM,
  PUBLISH_CONFIRM_TEXT,
  PUBLISH_CONFIRM_TITLE,
  PUBLISH_SUCCESS_TOAST,
  REJECT_SUCCESS_TOAST,
  mineDoneText,
} from '@/constants/governance'
import { useGovernanceStore } from '@/stores/governance'
import type { TabKey } from '@/stores/governance'
import type { CandidateStatus, KnowledgeGapItem } from '@/types/governance'

import FaqCandidateTab from './governance/FaqCandidateTab.vue'
import GapListTab from './governance/GapListTab.vue'
import PublishedFaqTab from './governance/PublishedFaqTab.vue'
import StatsSummary from './governance/StatsSummary.vue'
import type { ReviewDraft, ReviewSubmit } from './governance/types'

/**
 * fe-06 知识沉淀与运营管理（治理中心）。路由 `/kb/governance`，原型 `VOwfE`。
 *
 * 本文件是**唯一编排点**：三个 Tab 组件都只 emit，请求与提示在这里落地。
 * 这样做的代价是这个文件偏长，收益是「一条链路的全部副作用」在一个地方看得全
 * ——治理中心最容易出错的不是样式，而是「发布之后要连带刷新什么」。
 *
 * ✅ 2026-10-14：后端 be-08 / be-09 **已实现**，8 个端点全部落地
 * （原注释说「尚未实现、全是 `data: null` 的壳子」已作废）。
 *
 * ⚠️ 但**本页仍未在真环境跑过**：本机连不上云端 MySQL ⇒
 * `verify_gap.py` / `verify_faq.py` 的 T2 一次都没执行。
 * 首次联调要看的：非管理员访问候选列表 / 缺口清单应 **403**
 * （清单含全公司的提问原文，而侧栏不按权限过滤菜单）。
 */
const store = useGovernanceStore()
const router = useRouter()

/** Tab 标题与徽标数。徽标是「待办量」，为 0 时不显示（免得挂个 0 像出错） */
const tabs = computed<{ key: TabKey; label: string; badge: number }[]>(() => [
  { key: 'candidate', label: 'FAQ 候选审核', badge: store.pendingCount },
  { key: 'faq', label: '已发布 FAQ', badge: store.faqCount },
  { key: 'gap', label: '知识缺口清单', badge: store.gapCount },
])

onMounted(() => {
  // 两件事互不依赖，并行发起；任一失败不影响另一个（fe-06 §4.3①.1）
  void store.fetchOverview().catch(() => undefined)
  void store.fetchCandidates().catch((error: unknown) => reportError(error, ERROR_FALLBACK))
})

// ---------------------------------------------------------------------------
// 错误归一
// ---------------------------------------------------------------------------

/**
 * 把后端错误码翻译成人话（fe-06 §6）。
 * 🔒 一律用 `ElMessage` 的**纯文本**渲染：后端 message 是不可信输入，
 * 绝不为它开 `dangerouslyUseHTMLString`。
 */
function reportError(error: unknown, fallback: string): void {
  if (!(error instanceof ApiError)) {
    ElMessage.error(fallback)
    return
  }
  if (error.code === 2002) {
    ElMessage.error(ERROR_NO_PERMISSION)
    return
  }
  // 404/409 都意味着「这条数据不在你看到的状态里了」——并发审核是真实场景
  if (error.code === 3001 || error.code === 3002) {
    ElMessage.error(ERROR_STALE_RECORD)
    void refreshActiveTab()
    return
  }
  ElMessage.error(error.message || fallback)
}

/**
 * 重新拉取当前 Tab。
 * ⚠️ 失败时**只报错、不再回调 `reportError`**——`reportError` 在 3001/3002 时也会调本函数，
 * 两边互相调用就成了死循环（列表本身返回 3001 的概率极低，但不值得赌）。
 */
async function refreshActiveTab(): Promise<void> {
  try {
    await loadTab(store.activeTab)
  } catch {
    ElMessage.error(ERROR_FALLBACK)
  }
}

async function loadTab(tab: TabKey): Promise<void> {
  if (tab === 'candidate') {
    await store.fetchCandidates()
    return
  }
  if (tab === 'faq') {
    await store.fetchFaqs()
    return
  }
  await store.fetchGaps()
}

// ---------------------------------------------------------------------------
// Tab 切换与刷新
// ---------------------------------------------------------------------------

function onTabClick(key: TabKey): void {
  if (key === store.activeTab) return
  store.setActiveTab(key)
  void refreshActiveTab()
}

// ---------------------------------------------------------------------------
// Tab1：候选审核
// ---------------------------------------------------------------------------

/** 切换候选前先拦草稿丢失（fe-06 §4.3①.7）：润色是人工劳动，丢了代价高 */
async function confirmDiscardDraft(): Promise<boolean> {
  if (!store.isDirty) return true
  try {
    await ElMessageBox.confirm(DIRTY_CONFIRM_TEXT, DIRTY_CONFIRM_TITLE, {
      type: 'warning',
      confirmButtonText: '放弃修改',
      cancelButtonText: '继续编辑',
    })
    return true
  } catch {
    return false
  }
}

async function onSelectCandidate(id: string): Promise<void> {
  if (id === store.selectedCandidateId) return
  if (!(await confirmDiscardDraft())) return
  store.selectCandidate(id)
}

async function onClosePanel(): Promise<void> {
  if (!(await confirmDiscardDraft())) return
  store.clearSelection()
}

function onUpdateDraft(draft: ReviewDraft): void {
  store.reviewDraft = draft
}

/**
 * 发布 / 驳回。**注意 `question`/`answer` 发的是编辑框里的当前值**，
 * 即使用户没改也原样提交——后端以请求体为准，不做「空值就回退原推荐答案」的猜测（fe-06 §3）。
 */
async function onReview(payload: ReviewSubmit): Promise<void> {
  if (payload.action === 'publish') {
    try {
      await ElMessageBox.confirm(PUBLISH_CONFIRM_TEXT, PUBLISH_CONFIRM_TITLE, {
        type: 'warning',
        confirmButtonText: '确认发布',
        cancelButtonText: '再想想',
      })
    } catch {
      return
    }
  }

  try {
    await store.reviewCandidate(payload.id, {
      action: payload.action,
      question: payload.question,
      answer: payload.answer,
    })
  } catch (error) {
    reportError(error, payload.action === 'publish' ? '发布失败' : '操作失败')
    return
  }

  if (payload.action === 'publish') {
    // 文案是 A7 的可演示点，逐字来自 constants，不要改写
    ElMessage.success(PUBLISH_SUCCESS_TOAST)
    // 命中率与覆盖率会变——跨 Tab 唯一的耦合点，必须显式刷新
    void store.fetchOverview().catch(() => undefined)
  } else {
    ElMessage.success(REJECT_SUCCESS_TOAST)
  }
}

function onCandidateSearch(query: { status: CandidateStatus; min_confidence: number }): void {
  store.candidateQuery.status = query.status
  store.candidateQuery.min_confidence = query.min_confidence
  store.candidateQuery.page = 1 // 换了筛选就该回第一页，否则会停在一个不存在的页码上
  void refreshActiveTab()
}

function onCandidatePageChange(page: number): void {
  store.candidateQuery.page = page
  void refreshActiveTab()
}

// ---------------------------------------------------------------------------
// Tab2：已发布 FAQ
// ---------------------------------------------------------------------------

function onFaqSearch(keyword: string): void {
  store.faqQuery.keyword = keyword
  store.faqQuery.page = 1
  void refreshActiveTab()
}

function onFaqPageChange(page: number): void {
  store.faqQuery.page = page
  void refreshActiveTab()
}

/** 缓存开关：**不做乐观切换**，失败时开关自然回弹（fe-06 §6） */
async function onToggleCache(id: string, enabled: boolean): Promise<void> {
  try {
    await store.toggleFaqCache(id, enabled)
    ElMessage.success(enabled ? '已恢复该 FAQ 的缓存' : '已停用该 FAQ 的缓存')
  } catch (error) {
    reportError(error, '缓存开关操作失败')
  }
}

// ---------------------------------------------------------------------------
// Tab3：知识缺口
// ---------------------------------------------------------------------------

function onGapSearch(minFrequency: number): void {
  store.gapQuery.min_frequency = minFrequency
  store.gapQuery.page = 1
  void refreshActiveTab()
}

function onGapPageChange(page: number): void {
  store.gapQuery.page = page
  void refreshActiveTab()
}

/**
 * 一键转建。两段式：先确认分类（可改），成功后再问「要不要现在去上传补料文档」。
 *
 * ⚠️ 措辞必须是「创建补充任务」而非「关闭缺口」——转建不等于补料完成，
 * 缺口要变 `closed` 得靠补料后**再次提问命中**（fe-06 §4.3③.4，A8 的闭环判定点）。
 */
async function onCreateGapTask(item: KnowledgeGapItem): Promise<void> {
  let category = item.suggested_category ?? ''
  try {
    const result = await ElMessageBox.prompt(
      '转建只会创建「知识补充任务」。缺口要变为「已关闭」，需补料文档导入后再次提问命中。',
      GAP_TASK_PROMPT_TITLE,
      {
        confirmButtonText: '创建任务',
        cancelButtonText: '取消',
        inputValue: category,
        inputPlaceholder: GAP_TASK_PROMPT_PLACEHOLDER,
      },
    )
    category = result.value ?? ''
  } catch {
    return
  }

  try {
    await store.createGapTask(item.id, category)
  } catch (error) {
    reportError(error, '创建补充任务失败')
    return
  }

  // 用确认框承载「去上传补料文档」按钮，而不是往 ElMessage 里塞 HTML
  // （那条路要开 dangerouslyUseHTMLString，等于自己打开 XSS 的门）
  try {
    await ElMessageBox.confirm(GAP_TASK_SUCCESS_TOAST, GAP_TASK_DONE_TITLE, {
      type: 'success',
      confirmButtonText: GAP_TASK_DONE_CONFIRM,
      cancelButtonText: GAP_TASK_DONE_CANCEL,
    })
  } catch {
    return // 选了「稍后再说」
  }

  // 只跳转、**不改 fe-02 的任何文件**：fe-02 需自行支持这个 query 打开导入抽屉（spec §9-21）
  const importQuery: Record<string, string> = {
    [OPEN_IMPORT_QUERY_KEY]: OPEN_IMPORT_QUERY_VALUE,
  }
  await router.push({ name: 'kb-documents', query: importQuery })
}

// ---------------------------------------------------------------------------
// 手动触发聚类
// ---------------------------------------------------------------------------

async function onMine(): Promise<void> {
  try {
    const created = await store.triggerMine()
    await store.fetchCandidates()
    if (created === null) ElMessage.success(MINE_DONE_TEXT)
    else if (created === 0) ElMessage.info(MINE_EMPTY_TEXT)
    else ElMessage.success(mineDoneText(created))
  } catch (error) {
    // ⚠️ 这个接口是同步慢操作，而 `http.ts` 全局 15s 超时——超时几乎必然发生，
    // 且会被误判成「后端挂了」。定向提示见 spec §9-1。
    if (error instanceof ApiError && error.code === 5000 && error.message.includes('超时')) {
      ElMessage.warning(MINE_TIMEOUT_TEXT)
      return
    }
    reportError(error, '触发聚类失败')
  }
}

// ---------------------------------------------------------------------------
// 离开保护
// ---------------------------------------------------------------------------

onBeforeRouteLeave(async () => {
  if (!store.isDirty) return true
  try {
    await ElMessageBox.confirm(DIRTY_CONFIRM_TEXT, DIRTY_CONFIRM_TITLE, {
      type: 'warning',
      confirmButtonText: '放弃修改',
      cancelButtonText: '继续编辑',
    })
    return true
  } catch {
    return false
  }
})
</script>

<template>
  <div class="app-page">
    <PageTopbar title="知识沉淀与运营管理">
      <!-- ⚠️ v-permission 只控制显隐；后端 POST /api/mining/faq/mine 必须独立校验
           faq:candidate:mine（红线 7）。注释写在 el-tooltip 外层：el-tooltip 只接受单个
           子节点，注释留在插槽里会变成多余 vnode -->
      <el-tooltip :content="MINE_BUTTON_TOOLTIP" placement="bottom" :show-after="150">
        <el-button
          v-permission="PERM.CANDIDATE_MINE"
          class="mine-btn"
          type="primary"
          :loading="store.mining"
          @click="onMine"
        >
          <RefreshCw v-if="!store.mining" :size="14" />
          <span class="mine-btn__text">
            {{ store.mining ? MINE_RUNNING_TEXT : '手动触发聚类' }}
          </span>
        </el-button>
      </el-tooltip>
    </PageTopbar>

    <div class="app-page__body">
      <StatsSummary :data="store.overview" :loading="store.overviewLoading" />

      <!-- 自定义 Tab 条而不是 el-tabs：原型 (UmwuI) 要求统计块夹在 Tab 条与内容之间，
           且统计块被三个 Tab 共用——el-tabs 的插槽位置给不了这个结构（spec §9-17） -->
      <nav class="tabbar">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          class="tabbar__item"
          :class="{ 'tabbar__item--active': store.activeTab === tab.key }"
          type="button"
          @click="onTabClick(tab.key)"
        >
          <span class="tabbar__label">{{ tab.label }}</span>
          <span v-if="tab.badge > 0" class="tabbar__badge">{{ tab.badge }}</span>
        </button>
      </nav>

      <FaqCandidateTab
        v-if="store.activeTab === 'candidate'"
        :items="store.candidates"
        :total="store.candidateTotal"
        :loading="store.loading.candidate"
        :query="store.candidateQuery"
        :selected-id="store.selectedCandidateId"
        :candidate="store.selectedCandidate"
        :draft="store.reviewDraft"
        :submitting="store.submitting"
        @select="onSelectCandidate"
        @search="onCandidateSearch"
        @page-change="onCandidatePageChange"
        @review="onReview"
        @update:draft="onUpdateDraft"
        @close="onClosePanel"
      />

      <PublishedFaqTab
        v-else-if="store.activeTab === 'faq'"
        :items="store.faqs"
        :total="store.faqTotal"
        :loading="store.loading.faq"
        :query="store.faqQuery"
        @search="onFaqSearch"
        @page-change="onFaqPageChange"
        @toggle-cache="onToggleCache"
      />

      <GapListTab
        v-else
        :items="store.gaps"
        :total="store.gapTotal"
        :loading="store.loading.gap"
        :query="store.gapQuery"
        @search="onGapSearch"
        @page-change="onGapPageChange"
        @create-task="onCreateGapTask"
      />
    </div>
  </div>
</template>

<style scoped>
/* 原型 UmwuI：底边 1px、与内容 gap 16（由 .app-page__body 的 gap 提供） */
.tabbar {
  flex: none;
  display: flex;
  align-items: center;
  gap: 22px;
  border-bottom: 1px solid var(--kb-border);
}

.tabbar__item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 2px 10px;
  border: 0;
  background: transparent;
  font-family: inherit;
  font-size: 13.5px;
  font-weight: 500;
  color: var(--kb-text-secondary);
  cursor: pointer;
}

.tabbar__item:hover {
  color: var(--kb-text-primary);
}

.tabbar__item--active {
  font-weight: 600;
  color: var(--kb-brand);
}

/* 下划用伪元素而不是 border-bottom：border 会把 Tab 条本身撑高 2px */
.tabbar__item--active::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: -1px;
  height: 2px;
  border-radius: 1px;
  background: var(--kb-brand);
}

.tabbar__badge {
  min-width: 18px;
  padding: 1px 5px;
  border-radius: 9px;
  background: var(--kb-brand-soft);
  color: var(--kb-brand);
  font-size: 11px;
  font-weight: 600;
  text-align: center;
}

.mine-btn {
  height: 34px;
}

.mine-btn__text {
  margin-left: 6px;
}
</style>
