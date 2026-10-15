import { ref } from 'vue'

import type { TokenPair } from '@/types/auth'

/**
 * token 的**唯一**读写点（P1-01-fe-login spec §2.4）。
 *
 * 任何组件 / store / 接口封装都不允许直接碰 localStorage —— 散着写迟早出现
 * 一处清了另一处没清，表现为「登出后刷新又登上了」。
 *
 * 存放策略：
 * - access：**只存内存**，页面刷新即丢。XSS 拿不到长期凭证。
 * - refresh：存 localStorage，7 天有效（PRD §8.1）。
 */

const REFRESH_KEY = 'kb_refresh_token'

/**
 * 用 ref 而不是裸变量：store 的 isLoggedIn 要能响应它。
 * 拦截器里同步读 `.value`，不受影响。
 */
export const accessToken = ref<string | null>(null)

export function getAccessToken(): string | null {
  return accessToken.value
}

export function setAccessToken(token: string | null): void {
  accessToken.value = token
}

export function getRefreshToken(): string | null {
  // 隐私模式 / 禁用存储时 localStorage 会直接抛，不能让它把应用崩在启动阶段
  try {
    return localStorage.getItem(REFRESH_KEY)
  } catch {
    return null
  }
}

export function setRefreshToken(token: string | null): void {
  try {
    if (token === null) localStorage.removeItem(REFRESH_KEY)
    else localStorage.setItem(REFRESH_KEY, token)
  } catch {
    // 存不进去就只保留内存态，不阻断本次登录
  }
}

export function saveTokenPair(pair: TokenPair): void {
  setAccessToken(pair.access_token)
  setRefreshToken(pair.refresh_token)
}

export function clearTokens(): void {
  setAccessToken(null)
  setRefreshToken(null)
}
