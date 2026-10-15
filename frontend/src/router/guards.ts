import type { Router } from 'vue-router'

import { setSessionExpiredHandler } from '@/api/http'
import { useAuthStore } from '@/stores/auth'
import { getRefreshToken } from '@/utils/token'

/**
 * 登录守卫与会话失效处理（P1-01-fe-login spec §4）。
 *
 * 在 main.ts 里、`app.mount()` 之前调用。
 */
export function setupGuards(router: Router): void {
  router.beforeEach(async (to) => {
    const auth = useAuthStore()

    // 刷新页面后 access 只存在内存里、必然为空。本地还留着 refresh 就先换回身份，
    // 否则每次 F5 都会被踢回登录页。
    // 失败不在这里处理：http 拦截器已清掉 token，落到下面按未登录走。
    if (!auth.isLoggedIn && getRefreshToken()) {
      try {
        await auth.fetchMe()
      } catch {
        /* 见上 */
      }
    }

    if (auth.isLoggedIn) {
      // 已登录还去登录页（含登录后按浏览器后退）→ 直接送进主界面
      return to.name === 'login' ? { name: 'chat' } : true
    }

    // 白名单路由放行；其余一律回登录页并带上原目标，登录成功后回跳
    if (to.meta.requiresAuth === false) return true

    return { name: 'login', query: { redirect: to.fullPath } }
  })

  // access 与 refresh 双双失效时，由 http 层回调到这里清场并回登录页
  setSessionExpiredHandler(() => {
    const auth = useAuthStore()
    auth.reset()

    const current = router.currentRoute.value
    if (current.name !== 'login') {
      void router.replace({ name: 'login', query: { redirect: current.fullPath } })
    }
  })
}
