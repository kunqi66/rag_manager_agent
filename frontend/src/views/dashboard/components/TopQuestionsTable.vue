<script setup lang="ts">
import { computed } from 'vue'

import { DASHBOARD_EMPTY_TEXT, RANK_HIGHLIGHT_COUNT, formatInteger } from '@/constants/dashboard'
import type { TopQuestionItem } from '@/types/stats'

import ChartSkeleton from './ChartSkeleton.vue'
import PanelCard from './PanelCard.vue'
import PanelError from './PanelError.vue'

/**
 * 常见问题 TOP 榜。
 *
 * ⚠️ `item.question` 是**用户原始输入**、未经脱敏（be-07 §3.1）。渲染一律走插值 `{{ }}`，
 * **禁止任何形式的 v-html** —— 否则一条精心构造的提问就能变成 XSS 载荷（安全红线 2 的精神）。
 *
 * 用自定义行而不是 `el-table`：原型是「44 高表头 + 40 高排行 + 20×20 名次徽标」，
 * el-table 的表头/单元格内边距体系与之差得远，且本表**只读、无排序**，
 * 覆写 CSS 的代码量大于直接写标记。
 */
const props = defineProps<{
  items: TopQuestionItem[]
  loading: boolean
  error: string | null
}>()

const emit = defineEmits<{
  retry: []
  'row-click': [item: TopQuestionItem]
}>()

/**
 * `Array.isArray` 兜底：be-07 已实现、字段名已回核（原 spec §9-1 已销）；
 * 留着是防「后端某天不回数组了」把空态变成白屏。
 * 若后端实际返回 `{items:[...]}` 而我们按裸数组解，`items.length` 会是 `undefined` ——
 * 不报错、也不显示数据，最难查。退化成空态至少是「看得见的错」。
 */
const list = computed<TopQuestionItem[]>(() => (Array.isArray(props.items) ? props.items : []))
</script>

<template>
  <PanelCard title="常见问题 TOP" hint="按提问次数" bordered>
    <ChartSkeleton v-if="loading" />
    <PanelError v-else-if="error" :message="error" @retry="emit('retry')" />
    <p v-else-if="list.length === 0" class="rank__empty">{{ DASHBOARD_EMPTY_TEXT }}</p>
    <ul v-else class="rank">
      <li v-for="(item, index) in list" :key="`${index}-${item.question}`">
        <button type="button" class="rank__row" @click="emit('row-click', item)">
          <span class="rank__badge" :class="{ 'rank__badge--top': index < RANK_HIGHLIGHT_COUNT }">
            {{ index + 1 }}
          </span>
          <span class="rank__name">{{ item.question }}</span>
          <span class="rank__value">{{ formatInteger(item.count) }}</span>
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
