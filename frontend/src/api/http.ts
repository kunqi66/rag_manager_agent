import axios from 'axios'
import type {
  AxiosError,
  AxiosInstance,
  AxiosResponse,
  InternalAxiosRequestConfig,
} from 'axios'

import type { ApiResponse } from '@/types/api'
import type { TokenPair } from '@/types/auth'
import { clearTokens, getAccessToken, getRefreshToken, saveTokenPair } from '@/utils/token'

/**
 * axios 封装：统一拆包 + token 注入 + 401 单飞刷新。
 *
 * 设计约束（P1-01-fe-login spec §4）：
 * 1. 组件不直接 import axios，一律走 `@/api/*`（CLAUDE.md 前端规范）。
 * 2. 本文件**不 import store** —— 那会形成 http → store → api → http 的循环依赖。
 *    需要「会话失效」通知 store/router 时，走 setSessionExpiredHandler 注册回调。
 */

const BASE_URL = import.meta.env.VITE_API_BASE_URL || ''

/** 业务错误：后端返回 code !== 0，或网络/超时被归一化后抛出 */
export class ApiError extends Error {
  readonly code: number

  constructor(code: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.code = code
  }
}

/** 会话彻底失效（refresh 也换不回来）时回调。由 router/guards 注册。 */
let onSessionExpired: (() => void) | null = null

export function setSessionExpiredHandler(handler: () => void): void {
  onSessionExpired = handler
}

const http: AxiosInstance = axios.create({ baseURL: BASE_URL, timeout: 15_000 })

/**
 * 刷新专用实例，**不挂任何拦截器**。
 * 若用 http 发 refresh，refresh 自己 401 时又会触发刷新 → 无限递归。
 */
const refreshClient: AxiosInstance = axios.create({ baseURL: BASE_URL, timeout: 15_000 })

/**
 * 这些路径上的 401 不是「access 过期」，不该触发刷新：
 * - login  ：401 表示账号密码错，去刷新只会把用户硬踢到登录页，且掩盖真实原因
 * - refresh：本身就在刷新，重入即递归
 * - logout ：登出失败无所谓，本地照清
 */
const NO_REFRESH_PATHS = ['/api/auth/login', '/api/auth/refresh', '/api/auth/logout']

function shouldRefresh(url: string | undefined): boolean {
  if (!url) return true
  return !NO_REFRESH_PATHS.some((path) => url.includes(path))
}

function toApiError(error: AxiosError<ApiResponse<unknown>>): ApiError {
  const body = error.response?.data
  if (body && typeof body === 'object' && typeof body.code === 'number') {
    return new ApiError(body.code, body.message || '请求失败')
  }
  if (error.code === 'ECONNABORTED') return new ApiError(5000, '请求超时，请稍后重试')
  if (!error.response) return new ApiError(5000, '网络异常，请检查网络后重试')
  return new ApiError(5000, error.message || '服务异常，请稍后重试')
}

async function doRefresh(): Promise<string> {
  const refreshToken = getRefreshToken()
  if (!refreshToken) throw new ApiError(2001, '登录已过期，请重新登录')

  const res = await refreshClient.post<ApiResponse<TokenPair>>('/api/auth/refresh', {
    refresh_token: refreshToken,
  })
  const body = res.data
  if (body.code !== 0) throw new ApiError(body.code, body.message)

  saveTokenPair(body.data)
  return body.data.access_token
}

/** 在途的刷新 Promise。并发 401 复用它，只发一次 refresh。 */
let refreshing: Promise<string> | null = null

function refreshOnce(): Promise<string> {
  if (!refreshing) {
    refreshing = doRefresh().finally(() => {
      refreshing = null
    })
  }
  return refreshing
}

http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = getAccessToken()
  if (token) config.headers.set('Authorization', `Bearer ${token}`)
  return config
})

http.interceptors.response.use(
  (response: AxiosResponse<ApiResponse<unknown>>) => {
    const body = response.data
    if (body && typeof body === 'object' && typeof body.code === 'number' && body.code !== 0) {
      throw new ApiError(body.code, body.message || '请求失败')
    }
    return response
  },
  async (error: AxiosError<ApiResponse<unknown>>) => {
    const config = error.config as (InternalAxiosRequestConfig & { _retried?: boolean }) | undefined

    const canRetry =
      error.response?.status === 401 &&
      !!config &&
      !config._retried &&
      shouldRefresh(config.url)

    if (!canRetry || !config) throw toApiError(error)

    config._retried = true // 重放后若再 401，不再刷新，避免死循环
    try {
      const token = await refreshOnce()
      config.headers.set('Authorization', `Bearer ${token}`)
      return await http.request(config)
    } catch {
      clearTokens()
      onSessionExpired?.()
      throw new ApiError(2001, '登录已过期，请重新登录')
    }
  },
)

/** 拆掉 {code, message, data} 包装，把 data 交给调用方 */
async function unwrap<T>(promise: Promise<AxiosResponse<ApiResponse<T>>>): Promise<T> {
  const response = await promise
  return response.data.data
}

export function get<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  return unwrap<T>(http.get<ApiResponse<T>>(url, { params }))
}

export function post<T>(url: string, body?: unknown): Promise<T> {
  return unwrap<T>(http.post<ApiResponse<T>>(url, body))
}

export function put<T>(url: string, body?: unknown): Promise<T> {
  return unwrap<T>(http.put<ApiResponse<T>>(url, body))
}

export function del<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  return unwrap<T>(http.delete<ApiResponse<T>>(url, { params }))
}
