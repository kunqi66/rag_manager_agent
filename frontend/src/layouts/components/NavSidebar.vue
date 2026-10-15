<script setup lang="ts">
import { BookOpen, LogOut } from 'lucide-vue-next'
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { APP_BRAND, NAV_ITEMS } from '@/constants/nav'
import { useAuthStore } from '@/stores/auth'

/**
 * 全局导航侧栏 —— pencil 原型里的**复用件** `S5mE0Z`，被 5 个已登录页面共同引用。
 * 尺寸逐项对齐原型：宽 240 / Logo 64 高 / NavItem 42 高圆角 8 / UserFoot 64 高。
 *
 * 选中态取自原型对 fe-02 那次实例化：`NavItem/知识维护与导入` 被覆写成
 * `fill:$brand` 且图标与文字均转 `#FFFFFF`——即「当前页 = 品牌色底 + 白字」。
 */
const auth = useAuthStore()
const router = useRouter()

/**
 * 头像取姓名首字。
 * ⚠️ 原型里写死的「李」是演示数据，**不是**默认值——真实用户可能叫任何名字。
 */
const avatarInitial = computed(() => {
  const label = auth.user?.real_name || auth.user?.username || ''
  return label.charAt(0) || '?'
})

const displayName = computed(() => auth.user?.real_name || auth.user?.username || '未登录')

/**
 * 原型副标题是「财务部 · 成本组」（**父部门 · 自身部门**）。
 *
 * ✅ 2026-10-14 已还原：be-01 在 `/api/auth/me` 补了 `dept_names`（spec §10-86）。
 * 「取末两段拼 `·`」这条规则由 `stores/auth.ts` 的 `deptLabel` getter **统一实现一次**，
 * 本组件只读它 —— 问答页的检索范围 chip 读的是同一个值，
 * 两处都是原型上同一句话的变体，各拼一遍迟早只改一处、显示不一致。
 *
 * 无部门归属（`deptLabel` 为空）时才退回角色码，保证用户区不会空一行。
 */
const orgLabel = computed(() => auth.deptLabel || auth.roles.join(' · '))

async function onLogout(): Promise<void> {
  // 后端登出失败也必须清本地态并离开，否则用户卡在点不动的界面里（stores/auth.ts 同此约定）
  await auth.logout()
  await router.replace({ name: 'login' })
}

/** el-dropdown 的 command 类型是宽泛联合，收敛后再分发。 */
function onCommand(command: unknown): void {
  if (command === 'logout') void onLogout()
}
</script>

<template>
  <aside class="side">
    <div class="side__logo">
      <div class="side__mark"><BookOpen :size="16" /></div>
      <div class="side__brand">
        <span class="side__brand-name">{{ APP_BRAND.name }}</span>
        <span class="side__brand-sub">{{ APP_BRAND.sub }}</span>
      </div>
    </div>

    <nav class="side__nav">
      <router-link
        v-for="item in NAV_ITEMS"
        :key="item.name"
        class="side__item"
        :to="{ name: item.name }"
      >
        <component :is="item.icon" :size="18" class="side__item-icon" />
        <span class="side__item-label">{{ item.label }}</span>
      </router-link>
    </nav>

    <div class="side__spacer" />
    <div class="side__divider" />

    <!--
      原型没画登出入口，但 fe-01 §4.3 的「登出」流程必须有落点。
      挂在用户区（点开 → 退出登录）是改动最小、且不破坏原型版式的做法。
    -->
    <el-dropdown
      class="side__dropdown"
      trigger="click"
      placement="top-start"
      @command="onCommand"
    >
      <button class="side__user" type="button">
        <span class="side__avatar">{{ avatarInitial }}</span>
        <span class="side__meta">
          <span class="side__name">{{ displayName }}</span>
          <span v-if="orgLabel" class="side__org">{{ orgLabel }}</span>
        </span>
      </button>
      <template #dropdown>
        <el-dropdown-menu>
          <el-dropdown-item command="logout">
            <LogOut :size="14" />
            <span class="side__logout">退出登录</span>
          </el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>
  </aside>
</template>

<style scoped>
.side {
  flex: none;
  width: 240px;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--kb-sidebar);
}

/* ---- Logo 区（原型 S5mE0Z/MvgbB，高 64） ---- */
.side__logo {
  flex: none;
  height: 64px;
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 0 20px;
}

.side__mark {
  flex: none;
  width: 30px;
  height: 30px;
  border-radius: 9px;
  background: var(--kb-brand);
  color: var(--kb-sidebar-active);
  display: flex;
  align-items: center;
  justify-content: center;
}

.side__brand {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.side__brand-name {
  font-size: 14.5px;
  font-weight: 600;
  color: var(--kb-sidebar-active);
}

.side__brand-sub {
  font-size: 10.5px;
  letter-spacing: 0.2px;
  color: var(--kb-sidebar-text);
}

/* ---- 导航项（原型 NavList：gap 4，padding [8,12]） ---- */
.side__nav {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 12px;
}

.side__item {
  height: 42px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 14px;
  border-radius: 8px;
  color: var(--kb-sidebar-text);
  text-decoration: none;
  transition: background-color 0.15s ease, color 0.15s ease;
}

.side__item-icon {
  flex: none;
}

.side__item-label {
  font-size: 13.5px;
}

.side__item:hover {
  background: var(--kb-sidebar-hover);
}

/* 当前页：品牌色底 + 白字（原型对 fe-02 的覆写值） */
.side__item.router-link-active {
  background: var(--kb-brand);
  color: var(--kb-sidebar-active);
}

/* ---- 底部用户区 ---- */
.side__spacer {
  flex: 1 1 auto;
}

.side__divider {
  flex: none;
  height: 1px;
  background: var(--kb-sidebar-divider);
}

.side__dropdown {
  flex: none;
  display: block;
}

.side__user {
  width: 100%;
  height: 64px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 16px;
  border: 0;
  background: transparent;
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: background-color 0.15s ease;
}

.side__user:hover {
  background: var(--kb-sidebar-hover);
}

.side__avatar {
  flex: none;
  width: 32px;
  height: 32px;
  border-radius: 16px;
  background: var(--kb-brand);
  color: var(--kb-sidebar-active);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
}

.side__meta {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.side__name {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--kb-sidebar-active);
}

.side__org {
  font-size: 10.5px;
  color: var(--kb-sidebar-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.side__logout {
  margin-left: 6px;
}
</style>
