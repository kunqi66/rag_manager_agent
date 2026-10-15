<script setup lang="ts">
import { computed } from 'vue'
import { Globe, Shield } from 'lucide-vue-next'

import { permissionScopeLabel } from '@/utils/format'

import type { PermissionScope } from '@/types/kb'

/**
 * 权限范围列的图标标签。尺寸对齐原型 `ubgxB` 的 `PermCell`：icon 13 + 文字 12，gap 6。
 *
 * 全局用 `globe` + 品牌色；其余维度一律 `shield` + 次级灰——**不按维度换图标**，
 * 因为原型只画了这两种，多造图标会与视觉稿分叉（spec §9-16）。
 *
 * 文案优先级：`summary`（后端合成的实体名，如「财务部 + 指定人」，对齐原型观感）
 * → 按 `scopes` 的兜底短标签（spec §9-7）。
 */
const props = defineProps<{
  /** 已启用的授权维度集合。空数组 = 未配置任何权限 */
  scopes: PermissionScope[]
  /** 后端合成的标签文案。缺失时走兜底 */
  summary?: string | null
}>()

const isGlobal = computed(() => props.scopes.includes('global'))

const label = computed(() => {
  if (props.summary) return props.summary
  if (props.scopes.length === 0) return '未配置'
  // 全局本身就覆盖了一切，不必再列其它维度
  if (isGlobal.value) return permissionScopeLabel('global')
  return props.scopes.map(permissionScopeLabel).join(' / ')
})
</script>

<template>
  <span class="perm-cell" :class="{ 'perm-cell--global': isGlobal }">
    <component :is="isGlobal ? Globe : Shield" :size="13" class="perm-cell__icon" />
    <span class="perm-cell__text">{{ label }}</span>
  </span>
</template>

<style scoped>
.perm-cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  /* 原型：非全局用 $text-muted 的 shield + $text-secondary 的文字 */
  color: var(--kb-text-secondary);
}

.perm-cell__icon {
  flex: none;
  color: var(--kb-text-muted);
}

.perm-cell__text {
  font-size: 12px;
  color: inherit;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 原型：全局态图标与文字都用 $brand */
.perm-cell--global,
.perm-cell--global .perm-cell__icon {
  color: var(--kb-brand);
}
</style>
