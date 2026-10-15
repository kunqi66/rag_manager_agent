import { get, post } from './http'

import type { LoginRequest, TokenPair, UserDetail } from '@/types/auth'

/**
 * 认证接口封装。路径对齐 PRD §8.1。
 *
 * 注意：**不存在** `GET /api/auth/captcha`（核心数据字典 §9 #31：PRD §4.2 要求图形验证码，
 * 但 §8.1 无接口、§7.2 无存储位）。故此处不提供 captcha 方法——后端补齐前不要加。
 */
export const authApi = {
  login: (payload: LoginRequest) => post<TokenPair>('/api/auth/login', payload),

  me: () => get<UserDetail>('/api/auth/me'),

  refresh: (refreshToken: string) =>
    post<TokenPair>('/api/auth/refresh', { refresh_token: refreshToken }),

  logout: () => post<null>('/api/auth/logout'),
}
