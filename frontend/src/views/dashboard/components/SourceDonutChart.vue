<script setup lang="ts">
import { computed } from 'vue'
import type { EChartsOption } from 'echarts'

import { DASHBOARD_EMPTY_TEXT } from '@/constants/dashboard'
import type { DonutSegment } from '@/types/stats'
import { token, tooltipDefaults } from '../echarts'

import BaseChart from './BaseChart.vue'
import ChartSkeleton from './ChartSkeleton.vue'
import PanelCard from './PanelCard.vue'
import PanelError from './PanelError.vue'

/**
 * 答案来源构成（环形图）。
 *
 * ✅ 2026-10-14：`overview` **已回 `faq_hit_count` / `blocked_count` / `no_hit_count`**
 * （原 spec §9-3 已销），原型那三段（检索生成 / FAQ 命中 / **权限拦截**）现在画得出来。
 *
 * 组件本身**不关心几段** —— 它只接 `segments` 数组。所以这次补第三段
 * 只改了 `DashboardView` 的派生逻辑，**这个文件一行都没动**（原注释预言对了）。
 */
const props = defineProps<{
  segments: DonutSegment[]
  /** 圆心的主值（如 `66%`）。文案由调用方给，组件不自己算 */
  centerValue: string
  centerLabel: string
  loading: boolean
  error: string | null
}>()

const emit = defineEmits<{ retry: [] }>()

const hasData = computed(() => props.segments.some((segment) => segment.value > 0))

const option = computed<EChartsOption | null>(() => {
  if (!hasData.value) return null
  return {
    tooltip: { ...tooltipDefaults(), trigger: 'item', formatter: '{b}：{c}（{d}%）' },
    series: [
      {
        type: 'pie',
        // innerRadius 0.68 取自原型 `DonutStack`，外圈顶满容器
        radius: ['68%', '100%'],
        center: ['50%', '50%'],
        label: { show: false },
        labelLine: { show: false },
        // 段间留一圈卡片底色，否则相邻两段（品牌蓝与成功绿）边界糊在一起
        itemStyle: { borderColor: token('--kb-bg-card'), borderWidth: 2 },
        data: props.segments.map((segment) => ({
          name: segment.name,
          value: segment.value,
          itemStyle: { color: token(segment.colorVar) },
        })),
      },
    ],
  }
})
</script>

<template>
  <PanelCard title="答案来源构成" padded>
    <div class="donut__slot">
      <ChartSkeleton v-if="loading" />
      <PanelError v-else-if="error" :message="error" @retry="emit('retry')" />
      <div v-else class="donut">
        <div class="donut__chart">
          <BaseChart
            :option="option"
            :empty="!hasData"
            :empty-text="DASHBOARD_EMPTY_TEXT"
            aria-label="答案来源构成环形图"
          />
          <!-- 圆心文案用 HTML 叠层而不是 ECharts 的 graphic：省一个按需引入的组件，
               也免去 graphic 里再取一次坐标与字号 -->
          <div v-if="hasData" class="donut__center">
            <strong class="donut__value">{{ centerValue }}</strong>
            <span class="donut__label">{{ centerLabel }}</span>
          </div>
        </div>
      </div>
    </div>
  </PanelCard>
</template>

<style scoped>
.donut__slot {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
}

.donut {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}

.donut__chart {
  position: relative;
  width: 132px;
  height: 132px;
}

.donut__center {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  /* 让点击穿透到 canvas，环形图的扇区 tooltip 才不会被圆心挡掉 */
  pointer-events: none;
}

.donut__value {
  font-size: 20px;
  font-weight: 600;
  line-height: 1;
  color: var(--kb-text-primary);
}

.donut__label {
  font-size: 10px;
  color: var(--kb-text-muted);
}
</style>
