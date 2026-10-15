import { defineStore } from 'pinia'

import { governanceApi } from '@/api/governance'
import { DEFAULT_PAGE_SIZE, FAQ_MIN_FREQUENCY, GAP_MIN_FREQUENCY } from '@/constants/governance'
import type {
  CandidateQuery,
  FaqCandidateItem,
  FaqItem,
  FaqQuery,
  FaqReviewPayload,
  GapQuery,
  KnowledgeGapItem,
  MiningOverviewVo,
} from '@/types/governance'

/** 三个 Tab 的键。用于 `loading` 的分粒度与 `activeTab` */
export type TabKey = 'candidate' | 'faq' | 'gap'

/** 审核面板的编辑草稿。**与候选实体分离**，避免边打字边污染列表里的那一行 */
interface ReviewDraft {
  question: string
  answer: string
}

interface GovernanceState {
  overview: MiningOverviewVo | null
  candidates: FaqCandidateItem[]
  candidateTotal: number
  candidateQuery: CandidateQuery
  selectedCandidateId: string | null
  faqs: FaqItem[]
  faqTotal: number
  faqQuery: FaqQuery
  gaps: KnowledgeGapItem[]
  gapTotal: number
  gapQuery: GapQuery
  activeTab: TabKey
  reviewDraft: ReviewDraft
  /** 按 Tab 粒度，避免「切到 Tab2 时 Tab1 的表格也转圈」 */
  loading: Record<TabKey, boolean>
  overviewLoading: boolean
  submitting: boolean
  mining: boolean
}

function emptyDraft(): ReviewDraft {
  return { question: '', answer: '' }
}

/**
 * 治理中心的状态持有者（fe-06 §4.1）。
 *
 * 设计要点：
 * - 三个 Tab 是三个独立的 CRUD 面，各自持筛选/分页；**跨 Tab 唯一耦合**是「发布 FAQ 后
 *   统计块的命中率会变」，由页面层显式调 `fetchOverview()` 处理，**Tab 之间不互相通信**
 *   （CLAUDE.md 禁止 event bus）。
 * - 所有 action **不弹提示**，只抛错——提示文案属视图层，store 不该知道 Toast 长什么样。
 */
export const useGovernanceStore = defineStore('governance', {
  state: (): GovernanceState => ({
    overview: null,

    candidates: [],
    candidateTotal: 0,
    candidateQuery: {
      // 默认只看待审核——管理员进页面最想看到的是「有什么等我处理」
      status: 'pending',
      min_confidence: 0,
      page: 1,
      page_size: DEFAULT_PAGE_SIZE,
    },
    selectedCandidateId: null,

    faqs: [],
    faqTotal: 0,
    faqQuery: { keyword: '', page: 1, page_size: DEFAULT_PAGE_SIZE },

    gaps: [],
    gapTotal: 0,
    gapQuery: { min_frequency: GAP_MIN_FREQUENCY, page: 1, page_size: DEFAULT_PAGE_SIZE },

    activeTab: 'candidate',
    reviewDraft: emptyDraft(),

    loading: { candidate: false, faq: false, gap: false },
    overviewLoading: false,
    submitting: false,
    mining: false,
  }),

  getters: {
    /** 右侧详情面板的数据源。列表里找不到（如已被并发发布）时返回 null */
    selectedCandidate(state): FaqCandidateItem | null {
      return state.candidates.find((item) => item.id === state.selectedCandidateId) ?? null
    },

    /**
     * 草稿是否已被人工修改。决定「切换候选 / 离开页面」要不要提示——
     * 润色是人工劳动，丢了代价高（fe-06 §4.3①.7）。
     */
    isDirty(state): boolean {
      const candidate = state.candidates.find((item) => item.id === state.selectedCandidateId)
      if (!candidate) return false
      return (
        state.reviewDraft.question !== candidate.question ||
        state.reviewDraft.answer !== candidate.answer
      )
    },

    /**
     * Tab 徽标上的待办数。
     * 🟡 后端候选端点没有独立计数接口，只能退而求其次：当前筛选就是待审核时用 `total`，
     * 否则数当前页里的 pending 条数（跨页不准）。见 spec §9-5。
     */
    pendingCount(state): number {
      if (state.candidateQuery.status === 'pending') return state.candidateTotal
      return state.candidates.filter((item) => item.status === 'pending').length
    },

    faqCount(state): number {
      return state.faqTotal
    },

    /**
     * 缺口徽标数。
     * ⚠️ 是**缺口总数**而非「未处理数」：`GET /gaps` 没有 `status` 过滤参数（spec §9-6），
     * 拿不到「仅 open」的计数。
     */
    gapCount(state): number {
      return state.gapTotal
    },
  },

  actions: {
    setActiveTab(tab: TabKey): void {
      this.activeTab = tab
    },

    async fetchOverview(): Promise<void> {
      this.overviewLoading = true
      try {
        this.overview = await governanceApi.overview()
      } finally {
        this.overviewLoading = false
      }
    },

    async fetchCandidates(): Promise<void> {
      this.loading.candidate = true
      try {
        // `?.` 是**兜底**，不是「接口没实现」的遮挡：be-08 / be-09 已实现并回了
        // 分页信封 `{list,total,page,page_size}`。留 `?.` 是因为取不到信封时
        // 要在 `page.list` 上抛 TypeError（整页白屏），而空列表只是「没数据」。
        const page = await governanceApi.candidates(this.candidateQuery)
        this.candidates = page?.list ?? []
        this.candidateTotal = page?.total ?? 0

        // 选中项可能已不在当前结果里（翻了页 / 被并发处理掉）——不清的话右侧面板会
        // 展示一条「属于上一页」的幽灵数据
        if (!this.candidates.some((item) => item.id === this.selectedCandidateId)) {
          this.clearSelection()
        }
      } finally {
        this.loading.candidate = false
      }
    },

    async fetchFaqs(): Promise<void> {
      this.loading.faq = true
      try {
        const page = await governanceApi.faqs(this.faqQuery)
        this.faqs = page?.list ?? []
        this.faqTotal = page?.total ?? 0
      } finally {
        this.loading.faq = false
      }
    },

    async fetchGaps(): Promise<void> {
      this.loading.gap = true
      try {
        const page = await governanceApi.gaps(this.gapQuery)
        this.gaps = page?.list ?? []
        this.gapTotal = page?.total ?? 0
      } finally {
        this.loading.gap = false
      }
    },

    selectCandidate(id: string): void {
      this.selectedCandidateId = id
      this.resetDraft()
    },

    clearSelection(): void {
      this.selectedCandidateId = null
      this.resetDraft()
    },

    /** 把草稿重置为当前选中候选的原值。切候选必须调，否则会看到上一条的编辑内容 */
    resetDraft(): void {
      const candidate = this.candidates.find((item) => item.id === this.selectedCandidateId)
      this.reviewDraft = candidate
        ? { question: candidate.question, answer: candidate.answer }
        : emptyDraft()
    },

    /** 发布 / 驳回。`payload` 里是**人工修正后的当前值**，后端以请求体为准（fe-06 §3） */
    async reviewCandidate(id: string, payload: FaqReviewPayload): Promise<void> {
      this.submitting = true
      try {
        await governanceApi.reviewCandidate(id, payload)
        // 默认只看 pending：处理完就从列表摘掉，不做整页重查（少一次请求，也避免行跳动）
        this.candidates = this.candidates.filter((item) => item.id !== id)
        this.candidateTotal = Math.max(0, this.candidateTotal - 1)
        if (this.selectedCandidateId === id) this.clearSelection()
      } finally {
        this.submitting = false
      }
    },

    /**
     * 缓存开关。**成功后原位改**，失败由调用方把开关回弹——
     * 不做乐观切换（切了再回弹会让界面闪，且与后端状态短暂不一致，fe-06 §6）。
     */
    async toggleFaqCache(id: string, enabled: boolean): Promise<void> {
      const result = await governanceApi.toggleFaqCache(id, enabled)
      const target = this.faqs.find((item) => item.id === id)
      if (target) target.cache_enabled = result?.cache_enabled ?? enabled
    },

    /** 缺口转建。成功后把该行状态置为后端回的 status（应为 `tasked`） */
    async createGapTask(gapId: string, category?: string): Promise<void> {
      const result = await governanceApi.createGapTask(gapId, category)
      const target = this.gaps.find((item) => item.id === gapId)
      // 🟡 后端未回 status 时退化为 'tasked'：按 be-09 §4.4，转建成功必然落到 tasked
      if (target) target.status = result?.status ?? 'tasked'
    },

    /**
     * 手动触发聚类。返回后端结果（可能是 null —— 契约未定，spec §9-8），
     * 由页面层决定提示文案。
     */
    async triggerMine(): Promise<number | null> {
      this.mining = true
      try {
        const result = await governanceApi.mineFaq(FAQ_MIN_FREQUENCY)
        return result?.candidates_created ?? null
      } finally {
        this.mining = false
      }
    },
  },
})
