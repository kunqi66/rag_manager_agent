<script setup lang="ts">
/**
 * 页面顶栏（60 高）。原型里每个屏的 `Main/Topbar` 节点结构一致：
 * 左侧标题 + 右侧操作按钮组。尺寸取自 fe-02 原型 `OXIKJ/xQ8tG`。
 *
 * 只负责排版，不持有任何业务状态——按钮由各页通过默认插槽塞进来，
 * 因为「有哪些按钮、各自什么权限」是页面自己的事。
 */
defineProps<{
  /** 顶栏标题。原型各屏 `PageTitle` 的原文，逐字对齐 */
  title: string
}>()
</script>

<template>
  <header class="topbar">
    <h1 class="topbar__title">{{ title }}</h1>
    <div class="topbar__spacer" />
    <div class="topbar__actions">
      <slot />
    </div>
  </header>
</template>

<style scoped>
.topbar {
  flex: none;
  height: 60px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 20px;
  background: var(--kb-bg-card);
  border-bottom: 1px solid var(--kb-border);
}

.topbar__title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.topbar__spacer {
  flex: 1 1 auto;
}

.topbar__actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
</style>
