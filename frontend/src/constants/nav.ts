import { Database, LayoutDashboard, MessageSquare, Settings, Sparkles } from 'lucide-vue-next'
import type { Component } from 'vue'

/**
 * 全局导航配置。取自 pencil 原型复用件 `S5mE0Z`（NavSidebar）的 NavList 五项，
 * 文案与图标**逐字对齐**，不要改写。
 *
 * 用 `name`（路由名）而非 `path` 作跳转目标：路径将来若调整（如 `/kb/documents`
 * 改成 `/knowledge`），这里一行不用改。side bar 与 router 靠这个字符串对齐，
 * 写错会在渲染时直接报「没有名为 xxx 的路由」，不会静默变成死链。
 */
export interface NavItem {
  /** 路由名，必须与 `router/index.ts` 里注册的 `name` 一致 */
  name: string
  label: string
  icon: Component
}

export const NAV_ITEMS: readonly NavItem[] = [
  { name: 'chat', label: '问答工作台', icon: MessageSquare },
  { name: 'kb-documents', label: '知识维护与导入', icon: Database },
  { name: 'kb-governance', label: '知识沉淀与运营', icon: Sparkles },
  { name: 'dashboard', label: '运营看板', icon: LayoutDashboard },
  { name: 'system', label: '组织与系统配置', icon: Settings },
]

/** 侧栏顶部品牌区文案，取自原型 `S5mE0Z/MvgbB`。与登录页的 `BRAND` 是两套——勿混用。 */
export const APP_BRAND = {
  name: 'RAG 知识库',
  sub: '企业知识管理平台',
} as const
