<script setup lang="ts">
import { computed } from 'vue'

import { documentStatusLabel } from '@/utils/format'

import type { KbDocumentStatus } from '@/types/kb'

/**
 * 状态胶囊。尺寸逐项对齐原型 `ubgxB` 的 `StatusPill`：
 * 圆角 5、padding 3/9、文字 11.5/500。
 *
 * 单独成组件是为了让配色只定义一次——散在表格/详情/弹窗里迟早会出现
 * 同一状态两种颜色（原型只画了「已启用」绿、「已停用」灰，`draft` 的黄色是补的，
 * 更需要在同一处定义）。
 */
const props = defineProps<{
  status: KbDocumentStatus
}>()

/** 三态 → 配色类。颜色全走令牌，组件里不出现十六进制 */
const STATUS_CLASS: Record<KbDocumentStatus, string> = {
  draft: 'status-tag--draft',
  enabled: 'status-tag--enabled',
  disabled: 'status-tag--disabled',
}

const statusClass = computed(() => STATUS_CLASS[props.status] ?? 'status-tag--disabled')
const label = computed(() => documentStatusLabel(props.status))
</script>

<template>
  <span class="status-tag" :class="statusClass">{{ label }}</span>
</template>

<style scoped>
.status-tag {
  display: inline-flex;
  align-items: center;
  padding: 3px 9px;
  border-radius: 5px;
  font-size: 11.5px;
  font-weight: 500;
  line-height: 1.4;
  white-space: nowrap;
}

/* 已启用 —— 原型 ubgxB/StatusPill：fill $success-soft + text $success */
.status-tag--enabled {
  background: var(--kb-success-soft);
  color: var(--kb-success);
}

/* 已停用 —— 原型 fill $border-soft + text $text-muted */
.status-tag--disabled {
  background: var(--kb-border-soft);
  color: var(--kb-text-muted);
}

/* 草稿 —— 原型未画，沿用既有 warning 令牌（spec §9-8） */
.status-tag--draft {
  background: var(--kb-warning-soft);
  color: var(--kb-warning);
}
</style>
