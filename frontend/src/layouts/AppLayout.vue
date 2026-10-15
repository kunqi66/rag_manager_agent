<script setup lang="ts">
import NavSidebar from './components/NavSidebar.vue'

/**
 * 已登录页面的公共外壳：左侧导航 + 右侧内容区。
 *
 * 原型里每个已登录屏（fe-02…fe-06）的主区都是同一结构——`NavSidebar` 以 `ref`
 * 复用件的形式出现，右侧是各自的 `Main`。所以这层做成布局路由，
 * 5 个页面作为子路由渲染进 `<router-view>`，各自只写自己的顶栏与内容。
 */
</script>

<template>
  <div class="layout">
    <NavSidebar />
    <main class="layout__main">
      <router-view />
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  height: 100%;
  background: var(--kb-bg-page);
}

.layout__main {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
</style>
