<script setup lang="ts">
import type { PillTone } from '@/constants/governance'

/**
 * 状态胶囊。三个 Tab 都要用（候选 `pending/published/rejected`、缺口 `open/tasked/closed`），
 * 所以抽成一处；`tone` 只做语义映射，颜色全走 `--kb-*` 令牌，组件里不出现十六进制。
 *
 * 尺寸取自原型 `VOwfE/NnwEP/CPill`：圆角 5、内边距 [3,9]、字号 11.5/500。
 */
defineProps<{
  tone: PillTone
  label: string
}>()
</script>

<template>
  <span class="pill" :class="`pill--${tone}`">{{ label }}</span>
</template>

<style scoped>
.pill {
  display: inline-flex;
  align-items: center;
  padding: 3px 9px;
  border-radius: 5px;
  font-size: 11.5px;
  font-weight: 500;
  line-height: 1.3;
  white-space: nowrap;
}

/* 令牌选用说明：
   warning 的文字色用 --kb-warning-text（原型里的 #8A5A12）而不是 --kb-warning——
   橙色 #F59E0B 压在浅橙底上对比度不够，原型自己也把文字加深了。 */
.pill--brand {
  background: var(--kb-brand-soft);
  color: var(--kb-brand);
}

.pill--success {
  background: var(--kb-success-soft);
  color: var(--kb-success);
}

.pill--warning {
  background: var(--kb-warning-soft);
  color: var(--kb-warning-text);
}

.pill--danger {
  background: var(--kb-danger-soft);
  color: var(--kb-danger);
}

.pill--muted {
  background: var(--kb-border-soft);
  color: var(--kb-text-muted);
}
</style>
