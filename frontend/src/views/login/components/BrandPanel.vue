<script setup lang="ts">
import { BookOpen, Layers, Search, ShieldCheck } from 'lucide-vue-next'

import { BRAND } from '@/constants/brand'

/**
 * 左侧品牌面板。纯展示，**无 props、不发请求**（fe-01 §4.2）。
 * 尺寸 / 间距 / 字号逐项对齐 pencil 原型 c55di，颜色一律走 var(--kb-*）。
 */

// 原型里图标是名字符串，这里显式映射成组件：拼错的图标名会在编译期报错，
// 而不是静默渲染出一个空白方块。
const FEATURE_ICONS = {
  'shield-check': ShieldCheck,
  search: Search,
  layers: Layers,
} as const
</script>

<template>
  <aside class="brand">
    <div class="brand__row">
      <div class="brand__logo">
        <BookOpen :size="24" />
      </div>
      <div class="brand__col">
        <h1 class="brand__name">{{ BRAND.productName }}</h1>
        <p class="brand__sub">{{ BRAND.productSub }}</p>
      </div>
    </div>

    <p class="brand__tagline">{{ BRAND.tagline }}</p>

    <ul class="brand__features">
      <li v-for="feature in BRAND.features" :key="feature.text">
        <component :is="FEATURE_ICONS[feature.icon]" :size="17" />
        <span>{{ feature.text }}</span>
      </li>
    </ul>

    <div class="brand__stats">
      <div v-for="stat in BRAND.stats" :key="stat.label">
        <strong>{{ stat.value }}</strong>
        <span>{{ stat.label }}</span>
      </div>
    </div>
  </aside>
</template>

<style scoped>
/* 宽 880 / padding [0,84] / 垂直居中 —— 来自原型 c55di */
.brand {
  flex: none;
  width: 880px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 0 84px;
  background: var(--kb-sidebar);
}

.brand__row {
  display: flex;
  align-items: center;
  gap: 14px;
}

.brand__logo {
  flex: none;
  width: 46px;
  height: 46px;
  border-radius: 12px;
  background: var(--kb-brand);
  color: var(--kb-sidebar-active);
  display: flex;
  align-items: center;
  justify-content: center;
}

.brand__col {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.brand__name {
  margin: 0;
  font-size: 21px;
  font-weight: 600;
  color: var(--kb-sidebar-active);
}

.brand__sub {
  margin: 0;
  font-size: 11.5px;
  letter-spacing: 0.5px;
  color: var(--kb-sidebar-text);
}

.brand__tagline {
  margin: 0;
  padding-top: 14px;
  font-size: 15px;
  line-height: 1.7;
  color: var(--kb-sidebar-text);
}

.brand__features {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin: 0;
  padding: 6px 0 0;
  list-style: none;
}

.brand__features li {
  display: flex;
  gap: 11px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--kb-sidebar-text);
}

.brand__features svg {
  flex: none;
  color: var(--kb-brand-hover);
  margin-top: 1px;
}

.brand__stats {
  display: flex;
  gap: 34px;
  padding-top: 22px;
}

.brand__stats div {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.brand__stats strong {
  font-size: 19px;
  font-weight: 600;
  color: var(--kb-sidebar-active);
}

.brand__stats span {
  font-size: 11.5px;
  color: var(--kb-sidebar-text);
}
</style>
