import { defineStore } from 'pinia'

import { authApi } from '@/api/auth'
import type { LoginRequest, UserDetail } from '@/types/auth'
import { accessToken, clearTokens, saveTokenPair } from '@/utils/token'

/**
 * 登录态的**唯一**持有者。其它页面只读不写（P1-01-fe-login spec §2.1）。
 *
 * accessToken 本身存在 `utils/token.ts`（http 拦截器要同步读它，放这儿会循环依赖），
 * 这里只持有身份与权限。
 */
interface AuthState {
  user: UserDetail | null
  permissions: string[]
  roles: string[]
  /** 有效部门集合（含祖先）。string——`sys_department.id` 是 VARCHAR，见 types/auth.ts 注 */
  depts: string[]
  /** 部门名，与 `depts` 同序同长（根在前、自身在后），仅显示用 */
  deptNames: string[]
}

export const useAuthStore = defineStore('auth', {
  state: (): AuthState => ({
    user: null,
    permissions: [],
    roles: [],
    depts: [],
    deptNames: [],
  }),

  getters: {
    /**
     * 登录成功的唯一判据：**有 token 且有身份**。
     * 只看 token 会放过「token 拿到但 /me 失败」的半登录态。
     */
    isLoggedIn: (state) => accessToken.value !== null && state.user !== null,

    hasPermission: (state) => (code: string) => state.permissions.includes(code),

    hasAnyPermission: (state) => (codes: string[]) =>
      codes.some((code) => state.permissions.includes(code)),

    /**
     * 组织归属标签，形如「财务部 · 成本组」（**父级 · 自身**）。
     *
     * 🔴 **「取末两段拼 `·`」这条规则只在这里实现一次** —— 侧栏副标题
     * （`NavSidebar.vue`）与问答页检索范围 chip（`constants/chat.ts` 的
     * `scopeChipText`）都读它。原型上这两处文案是同一句话的两种说法，
     * 各写一份必然有一天只改一处，同一个用户在两处看到不同的部门。
     *
     * 取自 `deptNames` 而非 `depts`：后者是 id，显示不出来。
     * `deptNames` 的顺序是「根在前、自身在后」（后端保证），所以末两段正好是
     * 「父级 · 自身」；只有一级时就只显示自身，`slice(-2)` 天然不会越界。
     *
     * 无部门归属时返回空串，由调用方决定兜底（侧栏退回角色码、chip 退回等义表述）。
     */
    deptLabel: (state) => state.deptNames.slice(-2).join(' · '),
  },

  actions: {
    async login(payload: LoginRequest): Promise<void> {
      const pair = await authApi.login(payload)
      saveTokenPair(pair)
      try {
        await this.fetchMe()
      } catch (error) {
        // 拿到 token 却拿不到身份 → 半登录态。必须回滚，否则用户停在空白主界面
        this.reset()
        throw error
      }
    },

    /**
     * 拉取身份与权限。
     * 无 access 时也可调用：请求会 401 → 拦截器用 refresh 换新 access 并重放。
     * **刷新页面恢复登录态走的就是这条路**（见 router/guards.ts）。
     */
    async fetchMe(): Promise<void> {
      const me = await authApi.me()
      this.user = me
      // DTO 上的字段是 `perms`（后端原名）。本 store 的 state 字段名保持
      // `permissions` 不动 —— 改它会波及 hasPermission / hasAnyPermission
      // 和所有 v-permission 调用点，而那是纯前端命名，与后端契约无关。
      this.permissions = me.perms ?? []
      this.roles = me.roles ?? []
      this.depts = me.depts ?? []
      this.deptNames = me.dept_names ?? []
    },

    async logout(): Promise<void> {
      try {
        await authApi.logout()
      } catch {
        // 后端登出失败也必须清本地态，否则用户卡在「点不动」的已登录界面出不来
      } finally {
        this.reset()
      }
    },

    reset(): void {
      clearTokens()
      this.user = null
      this.permissions = []
      this.roles = []
      this.depts = []
      this.deptNames = []
    },
  },
})
