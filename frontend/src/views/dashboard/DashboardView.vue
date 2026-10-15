<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { Calendar, RotateCw, TriangleAlert } from 'lucide-vue-next'
import { useRouter } from 'vue-router'

import PageTopbar from '@/components/PageTopbar.vue'
import {
  DASHBOARD_AUTO_REFRESH_MS,
  DASHBOARD_FOOTER_NOTE,
  DASHBOARD_RANGE_OPTIONS,
  formatPercent,
} from '@/constants/dashboard'
import { useStatsStore } from '@/stores/stats'
import type { DonutSegment, RangePreset, TopKnowledgeItem, TopQuestionItem } from '@/types/stats'

import KpiCards from './components/KpiCards.vue'
import LatencyHistogram from './components/LatencyHistogram.vue'
import PanelError from './components/PanelError.vue'
import SourceDonutChart from './components/SourceDonutChart.vue'
import TopKnowledgeTable from './components/TopKnowledgeTable.vue'
import TopQuestionsTable from './components/TopQuestionsTable.vue'
import TrendChart from './components/TrendChart.vue'

/**
 * 运营看板与数据大盘（fe-05 / P2）—— 页面壳：顶栏 + 三层栅格 + 加载编排。
 *
 * ✅ 2026-10-14：be-07 **已实现**，5 个端点均可用（原注释说「必然全部报错」已作废）。
 * 但本机连不上云端 MySQL ⇒ `verify_stats.py` 的 T2 从未跑过，**首次联调要先跑它**。
 *
 * 本页是**纯消费端**：不做任何聚合计算，只把后端预聚合好的数字原样展示（fe-05 §1.2）。
 */
const store = useStatsStore()
const router = useRouter()

/** 顶栏范围下拉。用可写 computed 包一层，`el-select` 的 v-model 才能驱动 store。 */
const rangePreset = computed<RangePreset>({
  get: () => store.rangePreset,
  set: (value) => {
    void store.setRangePreset(value)
  },
})

const todayQuestionCount = computed(() => store.overview?.today_question_count ?? null)

/**
 * P95 耗时。
 *
 * ✅ 2026-10-14：be-07 的 `LatencyHistogramDTO` **现在带 `p95_ms`**（原 spec §9-13 已销），
 * 所以这里从 `latency` 面板真取，不再是写死的 `null`。
 * 面板未加载 / 报错时仍为 `null` → 表头 hint 不渲染（**不是**显示原型里的 `P95 2.1s`）。
 */
const p95Ms = computed<number | null>(() => store.latency?.p95_ms ?? null)

/**
 * 环形图分段。
 *
 * ✅ 2026-10-14：be-07 的 `OverviewDTO` **现在带 `faq_hit_count` / `blocked_count`**
 * （原 spec §9-3 已销），所以能画原型那三段：检索生成 / FAQ 命中 / **权限拦截**。
 *
 * ⚠️ 用**条数**算而不是用 `faq_hit_rate`：比率只有一个维度，拆不出「拦截」这一档。
 * 口径：`pv` 是总提问量，命中与拦截各占一档，**剩下的**才算走了完整检索生成。
 */
const donutSegments = computed<DonutSegment[]>(() => {
  const o = store.overview
  if (!o) return []

  const pv = o.pv ?? 0
  const hit = o.faq_hit_count ?? 0
  const blocked = o.blocked_count ?? 0
  // 夹到 ≥ 0：`blocked` 是 ACL 侧信道计数，理论上可能与命中重叠，
  // 不夹的话「检索生成」会画成负数弧（ECharts 不报错，只是图形反了）
  const generated = Math.max(pv - hit - blocked, 0)

  if (pv <= 0 && hit <= 0 && blocked <= 0) return []
  return [
    { name: '检索生成', value: generated, colorVar: '--kb-brand' },
    { name: 'FAQ 命中', value: hit, colorVar: '--kb-success' },
    { name: '权限拦截', value: blocked, colorVar: '--kb-warning' },
  ]
})

/** 圆心显示**品牌色那一段（检索生成）的占比**，与原型「58% 检索生成占比」的口径一致。 */
const donutCenterValue = computed(() => {
  const first = donutSegments.value[0]
  return first ? formatPercent(first.value) : '—'
})

const DONUT_CENTER_LABEL = '检索生成占比'

let autoRefreshTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  void store.fetchAll()

  // 默认关闭（`DASHBOARD_AUTO_REFRESH_MS = 0`）：预聚合每 10 分钟才更新一次，
  // 轮询比它更快只是重复拿同一份数据（fe-05 §4.5）。真要开，见常量注释。
  if (DASHBOARD_AUTO_REFRESH_MS > 0) {
    autoRefreshTimer = setInterval(() => {
      void store.fetchAll()
    }, DASHBOARD_AUTO_REFRESH_MS)
  }
})

onBeforeUnmount(() => {
  // 不清定时器的话，离开本页后它还会继续发请求，且持有本组件的闭包无法回收
  if (autoRefreshTimer !== null) {
    clearInterval(autoRefreshTimer)
    autoRefreshTimer = null
  }
})

function onRefresh(): void {
  void store.fetchAll()
}

function retryOverview(): void {
  void store.refreshPanel('overview')
}

function retryTrend(): void {
  void store.refreshPanel('trend')
}

function retryTopQuestions(): void {
  void store.refreshPanel('topQuestions')
}

function retryTopKnowledge(): void {
  void store.refreshPanel('topKnowledge')
}

function retryLatency(): void {
  void store.refreshPanel('latency')
}

/**
 * 榜单下钻。
 *
 * 只负责**带 query 跳转**，接收端（fe-03 问答页 / fe-02 台账）各自实现填充。
 * ⚠️ 参数名需与那两个页面约定一致，否则跳过去内容不会填充，而且**不报任何错**（spec §9-20）。
 */
function onQuestionClick(item: TopQuestionItem): void {
  void router.push({ name: 'chat', query: { q: item.question } })
}

function onKnowledgeClick(item: TopKnowledgeItem): void {
  void router.push({ name: 'kb-documents', query: { document_id: item.document_id } })
}
</script>

<template>
  <div class="app-page">
    <!-- 标题逐字对齐原型 `m5RsN/Topbar/PageTitle` -->
    <PageTopbar title="运营看板与数据大盘">
      <el-select v-model="rangePreset" class="range-select">
        <template #prefix>
          <Calendar :size="14" class="range-select__icon" />
        </template>
        <el-option
          v-for="option in DASHBOARD_RANGE_OPTIONS"
          :key="option.value"
          :label="option.label"
          :value="option.value"
        />
      </el-select>

      <!-- 原型这里画的是「导出报表」，但 fe-05 §1.2 明确不做数据导出（spec §9-8），
           改用手动刷新（fe-05 §4.5 要求）。本页**零写操作**。 -->
      <el-button type="primary" class="btn-refresh" :loading="store.anyLoading" @click="onRefresh">
        <RotateCw :size="15" />
        <span>刷新</span>
      </el-button>
    </PageTopbar>

    <div class="app-page__body">
      <!-- 全部端点都失败 → 页面级提示（fe-05 §6）。
           注意：这只在**五个都挂**时出现。单个端点挂掉只影响它那一张卡，
           不至于把整页判定为故障 —— 能看的图照常看。 -->
      <div v-if="store.allFailed" class="dash__banner" role="alert">
        <TriangleAlert :size="15" class="dash__banner-icon" />
        <span>看板数据加载失败，请稍后重试。若持续失败，请联系管理员检查统计服务。</span>
        <button type="button" class="dash__banner-btn" @click="onRefresh">全部重试</button>
      </div>

      <!-- KPI：5 张卡同源于 overview 端点，所以整行一起进错误态，而不是 5 张卡各显示一个 — -->
      <div class="dash__row dash__row--kpi">
        <KpiCards
          v-if="!store.errorMap.overview"
          :data="store.overview"
          :loading="store.loadingMap.overview"
        />
        <PanelError
          v-else
          :message="store.errorMap.overview ?? ''"
          @retry="retryOverview"
        />
      </div>

      <div class="dash__row dash__row--main">
        <TrendChart
          class="dash__grow"
          :data="store.trend"
          :loading="store.loadingMap.trend"
          :error="store.errorMap.trend"
          :range-text="store.rangeText"
          :today-question-count="todayQuestionCount"
          :granularity="store.granularity"
          @retry="retryTrend"
        />
        <SourceDonutChart
          class="dash__donut"
          :segments="donutSegments"
          :center-value="donutCenterValue"
          :center-label="DONUT_CENTER_LABEL"
          :loading="store.loadingMap.overview"
          :error="store.errorMap.overview"
          @retry="retryOverview"
        />
      </div>

      <div class="dash__row dash__row--bottom">
        <TopQuestionsTable
          class="dash__grow"
          :items="store.topQuestions"
          :loading="store.loadingMap.topQuestions"
          :error="store.errorMap.topQuestions"
          @retry="retryTopQuestions"
          @row-click="onQuestionClick"
        />
        <TopKnowledgeTable
          class="dash__grow"
          :items="store.topKnowledge"
          :loading="store.loadingMap.topKnowledge"
          :error="store.errorMap.topKnowledge"
          @retry="retryTopKnowledge"
          @row-click="onKnowledgeClick"
        />
        <LatencyHistogram
          class="dash__latency"
          :data="store.latency"
          :p95-ms="p95Ms"
          :loading="store.loadingMap.latency"
          :error="store.errorMap.latency"
          @retry="retryLatency"
        />
      </div>

      <p class="dash__footer">{{ DASHBOARD_FOOTER_NOTE }}</p>
    </div>
  </div>
</template>

<style scoped>
/* 原型 Body 是 padding 16 20 / gap 14，与全局骨架（18 20 / 16）略有出入。
   本页是密集看板，按原型收紧一档。 */
.app-page__body {
  padding: 16px 20px;
  gap: 14px;
}

.dash__row {
  display: flex;
  gap: 14px;
  width: 100%;
  min-width: 0;
}

.dash__row--kpi {
  flex: none;
}

/* 原型 Row2 固定 300 高 */
.dash__row--main {
  flex: none;
  height: 300px;
}

.dash__row--bottom {
  flex: 1 1 auto;
  min-height: 280px;
}

/* 子组件的根元素会带上本组件的 scope 属性，所以这两个选择器能直接命中 */
.dash__grow {
  flex: 1 1 0;
  min-width: 0;
}

.dash__donut {
  flex: 0 0 340px;
  width: 340px;
}

.dash__latency {
  flex: 0 0 300px;
  width: 300px;
}

.dash__banner {
  flex: none;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  font-size: 12.5px;
  color: var(--kb-warning-text);
  background: var(--kb-warning-soft);
  border: 1px solid var(--kb-warning-border);
  border-radius: 9px;
}

.dash__banner-icon {
  flex: none;
  color: var(--kb-warning);
}

.dash__banner span {
  flex: 1 1 auto;
  min-width: 0;
}

.dash__banner-btn {
  flex: none;
  padding: 3px 10px;
  font-family: inherit;
  font-size: 12px;
  color: var(--kb-warning-text);
  background: transparent;
  border: 1px solid var(--kb-warning-border);
  border-radius: 5px;
  cursor: pointer;
}

.dash__footer {
  flex: none;
  margin: 0;
  padding-bottom: 4px;
  font-size: 11px;
  text-align: center;
  color: var(--kb-text-muted);
}

/* ---- 顶栏控件 ---- */

.range-select {
  width: 122px;
}

.range-select__icon {
  color: var(--kb-text-muted);
}

.range-select :deep(.el-select__wrapper) {
  height: 34px;
  min-height: 34px;
  padding: 0 12px;
  border-radius: 8px;
}

.range-select :deep(.el-select__selected-item) {
  font-size: 12.5px;
  color: var(--kb-text-secondary);
}

.btn-refresh {
  height: 34px;
  padding: 0 14px;
  gap: 7px;
  font-size: 13px;
  border-radius: 8px;
}
</style>
