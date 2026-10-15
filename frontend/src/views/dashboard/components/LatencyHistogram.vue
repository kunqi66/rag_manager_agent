<script setup lang="ts">
import { computed } from 'vue'
import { TriangleAlert } from 'lucide-vue-next'
import type { EChartsOption } from 'echarts'

import {
  DASHBOARD_EMPTY_TEXT,
  LATENCY_SLOW_THRESHOLD_MS,
  formatLatencyBucketLabel,
  formatSeconds,
} from '@/constants/dashboard'
import type { LatencyVo } from '@/types/stats'
import { axisLabelColor, splitLineStyle, token, tooltipDefaults } from '../echarts'

import BaseChart from './BaseChart.vue'
import ChartSkeleton from './ChartSkeleton.vue'
import PanelCard from './PanelCard.vue'
import PanelError from './PanelError.vue'

/**
 * 响应延时分布直方图。
 *
 * 桶标签**由后端给的 `edges` 算出来**，不是写死的字符串数组 —— 后端一改
 * `STATS_LATENCY_EDGES_MS`（be-07 §5.4），标签跟着变，不会出现「桶边界变了但标签还是老的」。
 */
const props = defineProps<{
  data: LatencyVo | null
  /**
   * P95 耗时。
   * ✅ 2026-10-14：`latency` 端点**现在回 `p95_ms`**（原 spec §9-13 已销），
   * 由 `DashboardView` 从 `store.latency` 取。面板未加载 / 报错时为 `null`
   * → 表头 hint 不渲染，**而不是显示原型里的 `P95 2.1s`**。
   */
  p95Ms: number | null
  loading: boolean
  error: string | null
}>()

const emit = defineEmits<{ retry: [] }>()

/** 桶标签与计数。两者长度按 `edges` 对齐，防止后端给的数组长度不匹配时越界。 */
const buckets = computed(() => {
  const data = props.data
  // `Array.isArray` 而不是直接取 `.length`：形状对不上时要退化成空态，而不是抛异常白屏。
  // （be-07 已实现、字段名已回核，但后端少给一个桶数组仍会让整页白屏 —— 兜底留着。）
  if (!data || !Array.isArray(data.edges) || !Array.isArray(data.counts)) return []
  if (data.edges.length < 2 || data.counts.length === 0) return []
  const size = Math.min(data.counts.length, data.edges.length - 1)
  return Array.from({ length: size }, (_, index) => ({
    label: formatLatencyBucketLabel(data.edges, index),
    count: data.counts[index] ?? 0,
  }))
})

const hasData = computed(() => buckets.value.length > 0)

const hint = computed(() => (props.p95Ms === null ? undefined : `P95 ${formatSeconds(props.p95Ms)}`))

const option = computed<EChartsOption | null>(() => {
  const items = buckets.value
  if (items.length === 0) return null

  const counts = items.map((item) => item.count)
  const max = Math.max(...counts)

  return {
    grid: { top: 22, right: 4, bottom: 2, left: 4, containLabel: true },
    tooltip: { ...tooltipDefaults(), trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: {
      type: 'category',
      data: items.map((item) => item.label),
      axisTick: { show: false },
      axisLine: { lineStyle: { color: token('--kb-border') } },
      axisLabel: { color: axisLabelColor(), fontSize: 9.5, hideOverlap: true },
    },
    yAxis: {
      type: 'value',
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { show: false },
      splitLine: splitLineStyle(),
    },
    series: [
      {
        name: '提问数',
        type: 'bar',
        data: counts.map((count) => ({
          value: count,
          // 峰值柱用品牌色挑出来，其余用浅色 —— 一眼看出延时集中在哪一档
          itemStyle: {
            color: token(count === max ? '--kb-brand' : '--kb-brand-soft'),
            borderRadius: [3, 3, 0, 0],
          },
        })),
        barMaxWidth: 26,
        label: {
          show: true,
          position: 'top',
          fontSize: 10.5,
          color: token('--kb-text-secondary'),
        },
      },
    ],
  }
})

/**
 * 「X% 的提问超过 2s」。
 *
 * 这里的求和是对**一组完整的预聚合桶**相加，不是对原始明细行做 `reduce` ——
 * 桶由后端算好、无分页无抽样，所以不存在 fe-05 §1.2 担心的「与看板数字不一致」。
 * 阈值取自常量，且**后端改了桶边界这里仍然正确**（按下界与阈值比，不写死桶序号）。
 */
const slowRatio = computed<number | null>(() => {
  const data = props.data
  if (!data || buckets.value.length === 0) return null
  const total = data.counts.reduce((sum, count) => sum + count, 0)
  if (total <= 0) return null
  const slow = data.counts.reduce(
    (sum, count, index) => ((data.edges[index] ?? 0) >= LATENCY_SLOW_THRESHOLD_MS ? sum + count : sum),
    0,
  )
  return slow / total
})

const footText = computed(() => {
  const ratio = slowRatio.value
  if (ratio === null) return ''
  const percent = Math.round(ratio * 1000) / 10
  return `${percent}% 的提问超过 ${LATENCY_SLOW_THRESHOLD_MS / 1000}s。`
})
</script>

<template>
  <PanelCard title="响应延时分布" :hint="hint" bordered>
    <div class="lat">
      <div class="lat__chart">
        <ChartSkeleton v-if="loading" />
        <PanelError v-else-if="error" :message="error" @retry="emit('retry')" />
        <BaseChart
          v-else
          :option="option"
          :empty="!hasData"
          :empty-text="DASHBOARD_EMPTY_TEXT"
          aria-label="响应延时分布直方图"
        />
      </div>

      <!-- 脚注只保留**能由数据算出来**的那半句。
           原型还有「主要来自首次全库检索」——那是个根因结论，接口里没有任何字段能支撑它，
           前端编不出来也不该编（spec §9-13）。 -->
      <div v-if="footText" class="lat__foot">
        <TriangleAlert :size="13" class="lat__foot-icon" />
        <span class="lat__foot-text">{{ footText }}</span>
      </div>
    </div>
  </PanelCard>
</template>

<style scoped>
.lat {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 16px 14px;
}

.lat__chart {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
}

.lat__foot {
  flex: none;
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 11px;
  background: var(--kb-warning-soft);
  border-radius: 8px;
}

.lat__foot-icon {
  flex: none;
  margin-top: 1px;
  color: var(--kb-warning);
}

.lat__foot-text {
  font-size: 11px;
  line-height: 1.5;
  color: var(--kb-warning-text);
}
</style>
