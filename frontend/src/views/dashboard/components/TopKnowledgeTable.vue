<script setup lang="ts">
import { computed } from 'vue'

import { DASHBOARD_EMPTY_TEXT, RANK_HIGHLIGHT_COUNT, formatInteger } from '@/constants/dashboard'
import type { TopKnowledgeItem } from '@/types/stats'

import ChartSkeleton from './ChartSkeleton.vue'
import PanelCard from './PanelCard.vue'
import PanelError from './PanelError.vue'

/**
 * 知识热度榜 TOP。
 *
 * ⚠️ 本榜**展示文档标题**（安全红线 4 的敏感点）：看板当前限系统管理员，
 * 管理员的台账访问不受数据权限约束（PRD §3.3），故合规。
 * **但将来若把看板放宽给非管理员，`top-knowledge` 必须按调用者的数据权限过滤**，
 * 否则可以靠热度榜推断出自己无权查看的文档存在 —— 一个静默的越权泄漏点（spec §9-17）。
 *
 * `title` 渲染走插值，禁止 v-html。
 */
const props = defineProps<{
  items: TopKnowledgeItem[]
  loading: boolean
  error: string | null
}>()

const emit = defineEmits<{
  retry: []
  'row-click': [item: TopKnowledgeItem]
}>()

/**
 * `Array.isArray` 兜底：同 `TopQuestionsTable`，端点响应形状尚无权威来源（spec §9-1）。
 * 形状对不上时退化成空态，而不是静默什么都不显示。
 */
const list = computed<TopKnowledgeItem[]>(() => (Array.isArray(props.items) ? props.items : []))
</script>

<template>
  <PanelCard title="知识热度榜 TOP" hint="按被引用次数" bordered>
    <ChartSkeleton v-if="loading" />
    <PanelError v-else-if="error" :message="error" @retry="emit('retry')" />
    <p v-else-if="list.length === 0" class="rank__empty">{{ DASHBOARD_EMPTY_TEXT }}</p>
    <ul v-else class="rank">
      <li v-for="(item, index) in list" :key="`${index}-${item.document_id}`">
        <button type="button" class="rank__row" @click="emit('row-click', item)">
          <span class="rank__badge" :class="{ 'rank__badge--top': index < RANK_HIGHLIGHT_COUNT }">
            {{ index + 1 }}
          </span>
          <span class="rank__name">{{ item.title }}</span>
          <span class="rank__value">{{ formatInteger(item.cite_count) }}</span>
        </button>
      </li>
    </ul>
  </PanelCard>
</template>

<style scoped>
.rank {
  flex: 1 1 auto;
  min-height: 0;
  margin: 0;
  padding: 0;
  list-style: none;
  overflow: auto;
}

.rank__row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  height: 40px;
  padding: 0 14px;
  font-family: inherit;
  text-align: left;
  background: transparent;
  border: none;
  border-bottom: 1px solid var(--kb-border-soft);
  cursor: pointer;
}

.rank__row:hover {
  background: var(--kb-bg-subtle);
}

.rank__badge {
  flex: none;
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  color: var(--kb-text-muted);
  background: var(--kb-border-soft);
  border-radius: 5px;
}

.rank__badge--top {
  color: var(--kb-brand);
  background: var(--kb-brand-soft);
}

.rank__name {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 12.5px;
  color: var(--kb-text-primary);
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.rank__value {
  flex: none;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--kb-brand);
}

.rank__empty {
  margin: 0;
  padding: 24px 14px;
  font-size: 12.5px;
  text-align: center;
  color: var(--kb-text-muted);
}
</style>
