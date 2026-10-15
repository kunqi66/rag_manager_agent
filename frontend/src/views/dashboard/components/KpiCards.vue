<script setup lang="ts">
import { computed } from 'vue'
import { TrendingDown, TrendingUp } from 'lucide-vue-next'

import { KPI_CARDS, formatDelta } from '@/constants/dashboard'
import type { KpiCardDef } from '@/constants/dashboard'
import type { OverviewVo } from '@/types/stats'

/**
 * 5 张 KPI 卡。纯展示组件 —— 不碰 store、不发请求（fe-05 §7）。
 *
 * 🔴 **这里绝不能出现写死的数字**。原型画的是「填好状态」（`12,480` / `326` / `34%` …），
 * 那些全是演示数据。加载中 / 无数据 / 字段缺失一律显示 `—`：
 * 显示一个假的 `12,480` 比显示空更糟 —— 管理员会拿它做决策。
 */
const props = defineProps<{
  data: OverviewVo | null
  loading: boolean
}>()

interface KpiView {
  card: KpiCardDef
  value: string
  delta: number | null
  /** 环比方向是否为「好」——决定箭头颜色。延时下降是好事 */
  deltaIsGood: boolean
}

const cards = computed<KpiView[]>(() =>
  KPI_CARDS.map((card) => {
    const delta = deltaOf(card)
    return {
      card,
      value: displayValue(card),
      delta,
      deltaIsGood: delta === null ? false : isGoodDelta(card, delta),
    }
  }),
)

function displayValue(card: KpiCardDef): string {
  if (props.loading || !props.data) return '—'
  const raw = props.data[card.key]
  // avg_latency_ms 是可选的 🟡 字段（spec §9-9），缺失时显示 — 而不是 0：
  // 「量了是 0」和「没这个数」是两件事，混起来会让管理员以为响应快得离谱
  if (raw === undefined || Number.isNaN(raw)) return '—'
  return card.format(raw)
}

function deltaOf(card: KpiCardDef): number | null {
  const value = props.data?.deltas?.[card.key]
  return typeof value === 'number' ? value : null
}

/**
 * 「好 / 坏」的方向因指标而异：流量涨是好事，延时涨是坏事（原型 `-0.3s 环比` 用的是绿色）。
 * 方向由 `constants/dashboard.ts` 的 `goodWhenDown` 声明，不在这里 if 指标名。
 */
function isGoodDelta(card: KpiCardDef, delta: number): boolean {
  return card.goodWhenDown ? delta <= 0 : delta >= 0
}
</script>

<template>
  <div class="kpis">
    <article v-for="item in cards" :key="item.card.key" class="kpi">
      <div class="kpi__top">
        <span class="kpi__label">{{ item.card.label }}</span>
        <span
          class="kpi__icon"
          :style="{ background: `var(${item.card.softVar})`, color: `var(${item.card.colorVar})` }"
        >
          <component :is="item.card.icon" :size="13" />
        </span>
      </div>

      <p class="kpi__value">{{ item.value }}</p>

      <!-- 环比：字段缺失时整行不渲染（spec §9-10），绝不回填原型里的 +12.4% -->
      <p v-if="item.delta !== null" class="kpi__delta">
        <TrendingUp v-if="item.deltaIsGood" :size="12" class="kpi__delta-icon--good" />
        <TrendingDown v-else :size="12" class="kpi__delta-icon--bad" />
        <span>{{ formatDelta(item.delta) }}</span>
      </p>
    </article>
  </div>
</template>

<style scoped>
.kpis {
  display: flex;
  gap: 14px;
  width: 100%;
  min-width: 0;
}

.kpi {
  flex: 1 1 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 9px;
  padding: 13px 14px;
  background: var(--kb-bg-card);
  border: 1px solid var(--kb-border);
  border-radius: 9px;
}

.kpi__top {
  display: flex;
  align-items: center;
  gap: 8px;
}

.kpi__label {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 11.5px;
  color: var(--kb-text-secondary);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.kpi__icon {
  flex: none;
  width: 22px;
  height: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
}

.kpi__value {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
  line-height: 1.1;
  color: var(--kb-text-primary);
}

.kpi__delta {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 0;
  font-size: 11px;
  color: var(--kb-text-muted);
}

.kpi__delta-icon--good {
  color: var(--kb-success);
}

.kpi__delta-icon--bad {
  color: var(--kb-danger);
}
</style>
