import { defineStore } from 'pinia'

import { ApiError } from '@/api/http'
import { statsApi } from '@/api/stats'
import {
  DASHBOARD_DEFAULT_GRANULARITY,
  DASHBOARD_DEFAULT_RANGE,
  TOP_LIST_LIMIT,
  rangeLabel,
  resolveRange,
} from '@/constants/dashboard'
import type {
  LatencyVo,
  OverviewVo,
  PanelKey,
  RangePreset,
  TopKnowledgeItem,
  TopQuestionItem,
  TrendGranularity,
  TrendVo,
} from '@/types/stats'

/**
 * 运营看板的状态持有者（P2-05-fe-dashboard §2.1）。
 *
 * 图表组件是**纯展示组件**：只收 props、只 emit，不碰本 store（fe-05 §7）。
 * 这样每个图表都能单独用假数据渲染测试，不必起 Pinia。
 *
 * ✅ 2026-10-14：本 store 依赖的 5 个端点**后端已全部实现**（be-07），
 * 可以真调用。但**本机连不上云端 MySQL**，所以 `verify_stats.py` 的 T2 从未跑过 ——
 * 首次联调时先跑它，重点看 T2-24（幂等）与 T2-10（响应回显请求的窗口）。
 */

/** 所有分区键。顺序即 `fetchAll()` 的发起顺序，无业务含义。 */
const PANEL_KEYS = ['overview', 'trend', 'topQuestions', 'topKnowledge', 'latency'] as const

/**
 * 请求序号守卫（**模块级、非响应式** —— 它不该进 DevTools 的 state）。
 *
 * 解决的是「快速连点时间范围」时的**乱序覆盖**：A（近 7 天）先发、B（近 30 天）后发，
 * 若 A 后到，旧数据会盖住新数据，界面停在一个与下拉框不符的范围上，且**不报任何错**。
 * 只接受每个分区最后一次发出的请求的结果。
 *
 * 为什么不用 `AbortController`：`api/http.ts` 的 `get()` 不透传 `AbortSignal`，
 * 而它是本页不可修改的跨页基础设施。且 abort 只能省一次往返，
 * **防不了「响应已回来、只是顺序不对」**（spec §4.3 / §9-18）。
 */
const requestSeq: Record<PanelKey, number> = {
  overview: 0,
  trend: 0,
  topQuestions: 0,
  topKnowledge: 0,
  latency: 0,
}

function nextSeq(key: PanelKey): number {
  requestSeq[key] += 1
  return requestSeq[key]
}

function isLatest(key: PanelKey, token: number): boolean {
  return token === requestSeq[key]
}

/** loading / error 的初值。手写而不是循环构造，省得 `Record<PanelKey, _>` 推导出宽类型。 */
function emptyLoadingMap(): Record<PanelKey, boolean> {
  return { overview: false, trend: false, topQuestions: false, topKnowledge: false, latency: false }
}

function emptyErrorMap(): Record<PanelKey, string | null> {
  return { overview: null, trend: null, topQuestions: null, topKnowledge: null, latency: null }
}

/**
 * 单个分区的通用加载流程：序号守卫 + loading + 错误归档。
 *
 * 抽出来是因为 5 个端点的流程完全一致；复制 5 份，迟早有一份漏掉守卫
 * 或漏掉 `finally` —— 那正是「切了范围但数据没变」这类偶发 bug 的来源。
 *
 * **三处回写（data / error / loading）都要过守卫**：旧请求的 `finally` 若照样关 loading，
 * 会把新请求的 loading 提前熄掉，用户看到的是「转了一下就停但数据还是旧的」。
 */
async function loadPanel<T>(
  key: PanelKey,
  hooks: { setLoading: (value: boolean) => void; setError: (message: string | null) => void },
  request: () => Promise<T>,
  apply: (data: T) => void,
): Promise<void> {
  const token = nextSeq(key)
  hooks.setLoading(true)
  hooks.setError(null)
  try {
    const data = await request()
    if (!isLatest(key, token)) return
    apply(data)
  } catch (error) {
    if (!isLatest(key, token)) return
    hooks.setError(error instanceof ApiError ? error.message : '加载失败，请稍后重试')
  } finally {
    if (isLatest(key, token)) hooks.setLoading(false)
  }
}

interface StatsState {
  rangePreset: RangePreset
  granularity: TrendGranularity
  overview: OverviewVo | null
  trend: TrendVo | null
  topQuestions: TopQuestionItem[]
  topKnowledge: TopKnowledgeItem[]
  latency: LatencyVo | null
  /** 按分区的加载态。不用全局布尔：一个端点挂了不该让另外四张图也转圈（fe-05 §4.1） */
  loadingMap: Record<PanelKey, boolean>
  /** 按分区的错误文案。非空即该卡显示错误态 + 重试 */
  errorMap: Record<PanelKey, string | null>
}

export const useStatsStore = defineStore('stats', {
  state: (): StatsState => ({
    rangePreset: DASHBOARD_DEFAULT_RANGE,
    granularity: DASHBOARD_DEFAULT_GRANULARITY,
    overview: null,
    trend: null,
    topQuestions: [],
    topKnowledge: [],
    latency: null,
    loadingMap: emptyLoadingMap(),
    errorMap: emptyErrorMap(),
  }),

  getters: {
    /** 基础时间窗（不含粒度与条数，那两个只有部分端点用得上）。 */
    rangeQuery(state): { start_time: string; end_time: string } {
      return resolveRange(state.rangePreset)
    },

    /** 当前范围的展示文案，如「近 7 天」。 */
    rangeText(state): string {
      return rangeLabel(state.rangePreset)
    },

    anyLoading(state): boolean {
      return PANEL_KEYS.some((key) => state.loadingMap[key])
    },

    /** 全部分区都失败 → 页面级错误态（fe-05 §6）。单个失败只影响那一张卡。 */
    allFailed(state): boolean {
      return PANEL_KEYS.every((key) => state.errorMap[key] !== null)
    },
  },

  actions: {
    /**
     * 首屏并发拉 5 个端点。
     *
     * 用 `allSettled` 而不是 `all`：`all` 的语义是「一个 reject 就整体 reject」，
     * 与「局部失败可单独重试」直接矛盾。各 action 自己 catch 自己的错误并按分区归档，
     * 这里只需要「等它们都结束」。
     */
    async fetchAll(): Promise<void> {
      await Promise.allSettled([
        this.fetchOverview(),
        this.fetchTrend(),
        this.fetchTopQuestions(),
        this.fetchTopKnowledge(),
        this.fetchLatency(),
      ])
    },

    async fetchOverview(): Promise<void> {
      await loadPanel<OverviewVo>(
        'overview',
        {
          setLoading: (value) => {
            this.loadingMap.overview = value
          },
          setError: (message) => {
            this.errorMap.overview = message
          },
        },
        () => statsApi.overview(this.rangeQuery),
        (data) => {
          this.overview = data
        },
      )
    },

    async fetchTrend(): Promise<void> {
      await loadPanel<TrendVo>(
        'trend',
        {
          setLoading: (value) => {
            this.loadingMap.trend = value
          },
          setError: (message) => {
            this.errorMap.trend = message
          },
        },
        // 粒度只对趋势有意义，所以只有这里带 granularity
        () => statsApi.trend({ ...this.rangeQuery, granularity: this.granularity }),
        (data) => {
          this.trend = data
        },
      )
    },

    async fetchTopQuestions(): Promise<void> {
      await loadPanel<TopQuestionItem[]>(
        'topQuestions',
        {
          setLoading: (value) => {
            this.loadingMap.topQuestions = value
          },
          setError: (message) => {
            this.errorMap.topQuestions = message
          },
        },
        () => statsApi.topQuestions({ ...this.rangeQuery, limit: TOP_LIST_LIMIT }),
        (data) => {
          // 后端若返回 null（无数据），归一成空数组，免得下游到处判空
          this.topQuestions = data ?? []
        },
      )
    },

    async fetchTopKnowledge(): Promise<void> {
      await loadPanel<TopKnowledgeItem[]>(
        'topKnowledge',
        {
          setLoading: (value) => {
            this.loadingMap.topKnowledge = value
          },
          setError: (message) => {
            this.errorMap.topKnowledge = message
          },
        },
        () => statsApi.topKnowledge({ ...this.rangeQuery, limit: TOP_LIST_LIMIT }),
        (data) => {
          this.topKnowledge = data ?? []
        },
      )
    },

    async fetchLatency(): Promise<void> {
      await loadPanel<LatencyVo>(
        'latency',
        {
          setLoading: (value) => {
            this.loadingMap.latency = value
          },
          setError: (message) => {
            this.errorMap.latency = message
          },
        },
        () => statsApi.latency(this.rangeQuery),
        (data) => {
          this.latency = data
        },
      )
    },

    /** 单分区重试：卡片上的「重试」按钮走这里，只重载它自己。 */
    async refreshPanel(key: PanelKey): Promise<void> {
      switch (key) {
        case 'overview':
          await this.fetchOverview()
          break
        case 'trend':
          await this.fetchTrend()
          break
        case 'topQuestions':
          await this.fetchTopQuestions()
          break
        case 'topKnowledge':
          await this.fetchTopKnowledge()
          break
        case 'latency':
          await this.fetchLatency()
          break
      }
    },

    /** 切换时间范围。**5 个端点都受影响**，所以整体重载。 */
    async setRangePreset(preset: RangePreset): Promise<void> {
      if (preset === this.rangePreset) return
      this.rangePreset = preset
      await this.fetchAll()
    },

    /** 切换粒度。**只有趋势受影响** —— 重载全部是白搭的 5 倍请求量（fe-05 §4.1）。 */
    async setGranularity(granularity: TrendGranularity): Promise<void> {
      if (granularity === this.granularity) return
      this.granularity = granularity
      await this.fetchTrend()
    },
  },
})
