<script setup lang="ts">
import { computed } from 'vue'
import type { EChartsOption } from 'echarts'

import { DASHBOARD_EMPTY_TEXT, formatInteger } from '@/constants/dashboard'
import type { TrendGranularity, TrendVo } from '@/types/stats'
import { axisLabelColor, splitLineStyle, token, tooltipDefaults } from '../echarts'

import BaseChart from './BaseChart.vue'
import ChartSkeleton from './ChartSkeleton.vue'
import PanelCard from './PanelCard.vue'
import PanelError from './PanelError.vue'

/**
 * Token 消耗与提问量趋势。双 Y 轴柱状图。
 *
 * **为什么用双 Y 轴**：Token 消耗是万级、提问量是百级，共用一个轴的话提问量的柱子会被压成一条线
 * （fe-05 §4.3 明确要求）。但双轴有个通病——两根柱的高矮**不能横向比较**，
 * 所以图例里写清两个系列名，不靠相对高度引导解读。
 */
const props = defineProps<{
  data: TrendVo | null
  loading: boolean
  error: string | null
  /** 当前时间范围的文案（如「近 7 天」）。**不是**硬编码文案 */
  rangeText: string
  /** 今日提问量。缺省时副标题里不出现这一段，而不是显示 `0 次` */
  todayQuestionCount: number | null
  granularity: TrendGranularity
}>()

const emit = defineEmits<{ retry: [] }>()

// `Array.isArray` 而不是 `props.data.points.length`：be-07 已实现、字段名已回核
// （原 spec §9-1 已销），留着是防「后端不回数组了」时把空态变成白屏。
// ⚠️ `points` **只含有数据的桶**（后端不补空洞）⇒ 两条范围不同的曲线点数不同，
// 所以下面按 `bucket` 对齐，**不能按下标**。
const hasData = computed(() => Array.isArray(props.data?.points) && props.data.points.length > 0)

/** 副标题「近 7 天 · 今日提问 218 次」—— `218` 必须来自接口，不是原型里的演示值。 */
const subtitle = computed(() => {
  const parts: string[] = [props.rangeText]
  if (typeof props.todayQuestionCount === 'number') {
    parts.push(`今日提问 ${formatInteger(props.todayQuestionCount)} 次`)
  }
  return parts.filter(Boolean).join(' · ')
})

const option = computed<EChartsOption | null>(() => {
  const points = props.data?.points ?? []
  if (points.length === 0) return null

  const categories = points.map((point) => point.bucket)
  const tokenSeries = points.map((point) => point.token_in + point.token_out)
  const questionSeries = points.map((point) => point.question_count)

  return {
    grid: { top: 12, right: 4, bottom: 2, left: 4, containLabel: true },
    tooltip: { ...tooltipDefaults(), trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: {
      type: 'category',
      data: categories,
      axisTick: { show: false },
      axisLine: { lineStyle: { color: token('--kb-border') } },
      axisLabel: {
        color: axisLabelColor(),
        fontSize: 10.5,
        // 小时粒度下桶标签长得多（`2026-10-14 15:00`），不转就会互相重叠
        rotate: props.granularity === 'hour' ? 45 : 0,
        hideOverlap: true,
      },
    },
    yAxis: [
      {
        type: 'value',
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: axisLabelColor(), fontSize: 10.5, formatter: formatInteger },
        splitLine: splitLineStyle(),
      },
      {
        type: 'value',
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: axisLabelColor(), fontSize: 10.5, formatter: formatInteger },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: 'Token 消耗',
        type: 'bar',
        yAxisIndex: 0,
        data: tokenSeries,
        barMaxWidth: 16,
        itemStyle: { color: token('--kb-brand'), borderRadius: [3, 3, 0, 0] },
      },
      {
        name: '提问量',
        type: 'bar',
        yAxisIndex: 1,
        data: questionSeries,
        barMaxWidth: 16,
        itemStyle: { color: token('--kb-brand-soft'), borderRadius: [3, 3, 0, 0] },
      },
    ],
  }
})
</script>

<template>
  <PanelCard title="Token 消耗与提问量趋势" :sub="subtitle" padded>
    <!-- 自绘图例：原型的图例是 8×8 圆点，ECharts 自带图例的方块与间距对不上 -->
    <template #extra>
      <div class="legend">
        <span class="legend__item">
          <i class="legend__dot legend__dot--token" />Token 消耗
        </span>
        <span class="legend__item">
          <i class="legend__dot legend__dot--ask" />提问量
        </span>
      </div>
    </template>

    <div class="trend__slot">
      <ChartSkeleton v-if="loading" />
      <PanelError v-else-if="error" :message="error" @retry="emit('retry')" />
      <BaseChart
        v-else
        :option="option"
        :empty="!hasData"
        :empty-text="DASHBOARD_EMPTY_TEXT"
        aria-label="Token 消耗与提问量趋势图"
      />
    </div>
  </PanelCard>
</template>

<style scoped>
.trend__slot {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
}

.legend {
  display: flex;
  align-items: center;
  gap: 12px;
}

.legend__item {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--kb-text-secondary);
}

.legend__dot {
  width: 8px;
  height: 8px;
  border-radius: 2px;
}

.legend__dot--token {
  background: var(--kb-brand);
}

.legend__dot--ask {
  background: var(--kb-brand-soft);
}
</style>
