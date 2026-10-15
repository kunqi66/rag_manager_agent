<script setup lang="ts">
import { TriangleAlert } from 'lucide-vue-next'

/**
 * 单个分区的错误态：一句后端 message + 「重试」。
 *
 * ⚠️ 只有**真的失败**才用它。看板在系统刚上线、当天没人提问时本来就没有数据，
 * 那种情况是**空态**（`暂无数据`）而**不是错误** —— 给管理员弹红框只会制造恐慌
 * （fe-05 §6 的约定：`data` 为 null/空数组 ≠ 错误）。
 */
defineProps<{
  /** 后端返回的 message，或前端归一化后的提示。用插值渲染，禁止 v-html */
  message: string
}>()

defineEmits<{ retry: [] }>()
</script>

<template>
  <div class="panel-error">
    <TriangleAlert :size="15" class="panel-error__icon" />
    <span class="panel-error__text">{{ message }}</span>
    <button type="button" class="panel-error__retry" @click="$emit('retry')">重试</button>
  </div>
</template>

<style scoped>
.panel-error {
  /* 撑满所在槽位：它会被放进 flex 行（图表槽）或 flex 列（KPI 行），两种都要能占满 */
  flex: 1 1 auto;
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  height: 100%;
  min-height: 80px;
  padding: 12px 14px;
  color: var(--kb-text-secondary);
  background: var(--kb-bg-card);
  border: 1px solid var(--kb-border);
  border-radius: 10px;
}

.panel-error__icon {
  flex: none;
  color: var(--kb-warning);
}

.panel-error__text {
  font-size: 12.5px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.panel-error__retry {
  flex: none;
  padding: 3px 10px;
  font-family: inherit;
  font-size: 12px;
  color: var(--kb-brand);
  background: var(--kb-brand-soft);
  border: none;
  border-radius: 5px;
  cursor: pointer;
}

.panel-error__retry:hover {
  background: var(--kb-brand-softer);
}
</style>
