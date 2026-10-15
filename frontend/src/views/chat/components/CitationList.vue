<script setup lang="ts">
import { ChevronDown, ChevronRight } from 'lucide-vue-next'
import { computed, ref } from 'vue'

import { CITATION_COLLAPSE_THRESHOLD, CITATION_SUMMARY_TEMPLATE } from '@/constants/chat'
import type { CitationItem } from '@/types/chat'

import CitationCard from './CitationCard.vue'

/**
 * 引用溯源卡片列表。原型 `LoIHB/AiBody/Citations`（`SMArQ`）。
 *
 * 「共 N 条来源」汇总行**原型里没有**，按 fe-03 §2「含『共 N 条来源』折叠」补
 * （设计有、原型无 → 按设计实现，视觉用原型图元拼，spec §9 同类处置）。
 * 条数少时不出折叠开关，避免为一个装饰性按钮增加交互。
 */
const props = defineProps<{
  items: CitationItem[]
}>()

const emit = defineEmits<{
  'cite-click': [item: CitationItem]
}>()

const collapsed = ref(false)

const summary = computed(() => CITATION_SUMMARY_TEMPLATE.replace('{count}', String(props.items.length)))

/** 超过阈值才给折叠开关；否则汇总行纯粹是计数 */
const collapsible = computed(() => props.items.length > CITATION_COLLAPSE_THRESHOLD)

const visibleItems = computed(() => (collapsed.value ? [] : props.items))

function toggle(): void {
  if (!collapsible.value) return
  collapsed.value = !collapsed.value
}
</script>

<template>
  <div class="cites">
    <button
      class="cites__summary"
      :class="{ 'cites__summary--clickable': collapsible }"
      type="button"
      @click="toggle"
    >
      <component :is="collapsed ? ChevronRight : ChevronDown" v-if="collapsible" :size="13" />
      <span>{{ summary }}</span>
    </button>

    <div v-if="visibleItems.length" class="cites__grid">
      <CitationCard
        v-for="item in visibleItems"
        :key="`${item.document_id}-${item.chunk_id}`"
        :item="item"
        @cite-click="emit('cite-click', $event)"
      />
    </div>
  </div>
</template>

<style scoped>
.cites {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.cites__summary {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  align-self: flex-start;
  padding: 0;
  border: 0;
  background: transparent;
  font-family: inherit;
  font-size: 11px;
  color: var(--kb-text-muted);
}

.cites__summary--clickable {
  cursor: pointer;
}

.cites__summary--clickable:hover {
  color: var(--kb-brand);
}

/* Citations：横排 gap 10（原型 SMArQ），卡片放不下就换行 */
.cites__grid {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
</style>
