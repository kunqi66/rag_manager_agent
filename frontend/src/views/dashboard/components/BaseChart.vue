<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import type { EChartsOption } from 'echarts'

import { DASHBOARD_EMPTY_TEXT } from '@/constants/dashboard'
import { echarts, type ChartInstance } from '../echarts'

/**
 * ECharts 实例的**唯一**持有者。所有图表都渲染在它里面，别再各自 `echarts.init`。
 *
 * 本组件存在的唯一理由：**把实例生命周期收在一处**。ECharts 实例持有 canvas、事件监听与
 * 内部定时器，少一次 `dispose()` 就会在路由切换后常驻 —— 表现是「看着能用、反复进出越用越卡」，
 * 直到标签页崩溃。4 个图表组件各写一份 `onMounted/onUnmounted/resize`，必然有 1-2 份漏掉。
 *
 * 逐条落实 fe-05 §4.2：
 * 1. `onMounted` + `await nextTick()` 再 init（尺寸为 0 时 init 会得到 0×0 画布，resize 也难救回）；
 * 2. `ResizeObserver` 而非 `window.onresize`（侧栏折叠改容器宽度但**窗口尺寸不变**）；
 * 3. resize 用 `requestAnimationFrame` 合帧（拖窗口时 observer 高频回调）；
 * 4. `setOption(o, { notMerge: true })`（防切换范围后旧系列残留成鬼影）；
 * 5. 卸载时**先 disconnect 再 dispose**。
 */
const props = withDefaults(
  defineProps<{
    /** ECharts 配置。为 `null` 或 `empty` 为真时只清空画布，不画空坐标轴 */
    option: EChartsOption | null
    /** 无数据：覆盖一层「暂无数据」。空坐标轴看起来像故障，不如明说没有数据 */
    empty?: boolean
    emptyText?: string
    /** 无障碍：读屏用户至少要能知道这里是什么图 */
    ariaLabel?: string
  }>(),
  { empty: false, emptyText: DASHBOARD_EMPTY_TEXT, ariaLabel: '数据图表' },
)

const el = ref<HTMLDivElement | null>(null)
/** shallowRef：ECharts 实例内部结构巨大，深响应式会拖垮渲染，而我们只需要「有没有实例」 */
const chart = shallowRef<ChartInstance | null>(null)

let observer: ResizeObserver | null = null
let rafId = 0

function render(): void {
  if (!el.value) return
  // 空态：清空画布，由模板里的占位层顶上
  if (props.empty || !props.option) {
    chart.value?.clear()
    return
  }
  if (!chart.value) chart.value = echarts.init(el.value)
  // notMerge 必须是 true：切换时间范围后桶数会变（某些桶为空），
  // 默认的合并模式会把上一次的系列留着，出现「图例里有、图上没有」的鬼影
  chart.value.setOption(props.option, { notMerge: true })
}

function scheduleResize(): void {
  if (rafId !== 0) return
  rafId = requestAnimationFrame(() => {
    rafId = 0
    chart.value?.resize()
  })
}

onMounted(async () => {
  await nextTick()
  if (!el.value) return
  chart.value = echarts.init(el.value)
  render()
  observer = new ResizeObserver(scheduleResize)
  observer.observe(el.value)
})

watch(() => props.option, render, { deep: true })
watch(() => props.empty, render)

onBeforeUnmount(() => {
  // 顺序不能反：先断开 observer，否则实例销毁后它还会继续回调。
  // dispose() 负责释放 canvas 与内部监听 —— 没有它，反复进出本页内存线性增长。
  observer?.disconnect()
  observer = null
  if (rafId !== 0) {
    cancelAnimationFrame(rafId)
    rafId = 0
  }
  chart.value?.dispose()
  chart.value = null
})
</script>

<template>
  <div class="chart" role="img" :aria-label="ariaLabel">
    <div ref="el" class="chart__canvas" />
    <div v-if="empty" class="chart__empty">{{ emptyText }}</div>
  </div>
</template>

<style scoped>
.chart {
  position: relative;
  flex: 1 1 auto;
  width: 100%;
  height: 100%;
  min-width: 0;
  min-height: 0;
}

.chart__canvas {
  width: 100%;
  height: 100%;
}

.chart__empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12.5px;
  color: var(--kb-text-muted);
  background: var(--kb-bg-card);
}
</style>
