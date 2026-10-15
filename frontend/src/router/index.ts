import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

declare module 'vue-router' {
  interface RouteMeta {
    /** false = 白名单路由，未登录也可访问。**缺省视为需要登录**（默认拒绝）。 */
    requiresAuth?: boolean
    title?: string
    /** 侧栏高亮用的路由名。子路由不总与导航项一一对应时用它对齐（如详情页归属列表页）。 */
    nav?: string
  }
}

/**
 * 已登录页面一律挂在 `AppLayout` 下（原型里 5 个屏共用同一套侧栏）。
 * 侧栏高亮靠 `router-link-active`，它由「当前路由是否匹配该 link 的目标」决定，
 * 因此子路由的 `name` 必须与 `constants/nav.ts` 的 `NAV_ITEMS[].name` 一致。
 */
const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/login/LoginView.vue'),
    meta: { requiresAuth: false, title: '登录' },
  },
  {
    path: '/',
    component: () => import('@/layouts/AppLayout.vue'),
    children: [
      // 空路径子路由：让 `/` 本身可匹配并重定向。不能把 redirect 写成第二个 `path:'/'`
      // 顶层路由——那样与布局路由路径重复，vue-router 会判定冲突。
      { path: '', redirect: '/chat' },
      {
        path: 'chat',
        name: 'chat',
        component: () => import('@/views/chat/ChatView.vue'),
        meta: { title: 'AI 智能问答' },
      },
      {
        path: 'kb/documents',
        name: 'kb-documents',
        component: () => import('@/views/kb/DocumentCenterView.vue'),
        meta: { title: '知识维护与导入' },
      },
      {
        path: 'kb/governance',
        name: 'kb-governance',
        component: () => import('@/views/kb/GovernanceView.vue'),
        meta: { title: '知识沉淀与运营' },
      },
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/views/dashboard/DashboardView.vue'),
        meta: { title: '运营看板' },
      },
      {
        path: 'system',
        name: 'system',
        component: () => import('@/views/system/SystemView.vue'),
        meta: { title: '组织与系统配置' },
      },
    ],
  },
  {
    // 未定义的路径一律送回主界面，由守卫决定是否再跳到登录页
    path: '/:pathMatch(.*)*',
    redirect: '/chat',
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  document.title = to.meta.title
    ? `${to.meta.title} · 企业知识库管理平台`
    : '企业知识库管理平台'
})

export default router
