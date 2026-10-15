<script setup lang="ts">
import { Database, Gauge, MessageSquare, Users, Zap } from 'lucide-vue-next'
import type { Component } from 'vue'

import { formatPercent } from './format'

import type { MiningOverviewVo } from '@/types/governance'

/**
 * 顶部统计块。
 *
 * ⚠️ **与原型 `VOwfE/UMG8S` 内容不同，这是有意为之**（spec §9-2）：原型画的是
 * 「待审核候选 / 已发布 FAQ / 开放知识缺口」三个**队列计数**，与 Tab 徽标数字重复；
 * fe-06 §4.4 要求这里消费 `GET /api/mining/stats/overview` 的**运营效能指标**。
 * 本组件按设计实现，队列计数保留在 Tab 徽标上。卡片图元（32 图标盒 / 20-12 字号）
 * 仍逐项沿用原型。
 *
 * 只用 `/api/mining/stats/overview`——**不要**误用 fe-05 的
 * `/api/stats/dashboard/overview`，两者聚合口径与归属模块都不同（fe-06 §4.4）。
 */
defineProps<{
  data: MiningOverviewVo | null
  loading: boolean
}>()

type MetricTone = 'brand' | 'success' | 'warning'

interface Metric {
  key: keyof MiningOverviewVo
  label: string
  icon: Component
  tone: MetricTone
  /** 数值展示形态：计数原样带千分位，比率转百分比，延时带单位 */
  format: (value: number) => string
}

const asCount = (value: number): string => value.toLocaleString('zh-CN')
const asLatency = (value: number): string => `${Math.round(value)} ms`

const METRICS: readonly Metric[] = [
  { key: 'question_count', label: '提问量', icon: MessageSquare, tone: 'brand', format: asCount },
  { key: 'unique_user_count', label: '独立提问人数', icon: Users, tone: 'brand', format: asCount },
  { key: 'faq_hit_rate', label: 'FAQ 缓存命中率', icon: Zap, tone: 'success', format: (v) => formatPercent(v) },
  { key: 'kb_coverage', label: '知识库覆盖率', icon: Database, tone: 'warning', format: (v) => formatPercent(v) },
  { key: 'avg_latency_ms', label: '平均问答延时', icon: Gauge, tone: 'brand', format: asLatency },
]
</script>

<template>
  <section class="stats">
    <article v-for="metric in METRICS" :key="metric.key" class="stat">
      <div class="stat__icon" :class="`stat__icon--${metric.tone}`">
        <component :is="metric.icon" :size="16" />
      </div>
      <div class="stat__col">
        <span class="stat__label">{{ metric.label }}</span>
        <!-- 无数据/加载中都显示占位符，**不显示 0**——0 是一个会骗人的确定值 -->
        <span class="stat__value">
          {{ data ? metric.format(data[metric.key]) : loading ? '…' : '—' }}
        </span>
      </div>
    </article>
  </section>
</template>

<style scoped>
/* 原型 UMG8S：横向排布、gap 14 */
.stats {
  flex: none;
  display: flex;
  gap: 14px;
}

/* 原型 Stat/X9044：flex 1 / 圆角 9 / 1px 描边 / padding [12,14] / gap 11 */
.stat {
  flex: 1 1 0;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 12px 14px;
  border: 1px solid var(--kb-border);
  border-radius: 9px;
  background: var(--kb-bg-card);
}

.stat__icon {
  flex: none;
  width: 32px;
  height: 32px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.stat__icon--brand {
  background: var(--kb-brand-soft);
  color: var(--kb-brand);
}

.stat__icon--success {
  background: var(--kb-success-soft);
  color: var(--kb-success);
}

.stat__icon--warning {
  background: var(--kb-warning-soft);
  color: var(--kb-warning);
}

.stat__col {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.stat__label {
  font-size: 12px;
  color: var(--kb-text-secondary);
}

.stat__value {
  font-size: 20px;
  font-weight: 600;
  color: var(--kb-text-primary);
  line-height: 1.2;
}
</style>
