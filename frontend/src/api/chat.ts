import { authApi } from './auth'
import { del, get, post } from './http'

import type { PageResult } from '@/types/api'
import type {
  AskRequest,
  AskResponseVo,
  ChatSessionVo,
  SessionDetailVo,
  SuggestionItem,
} from '@/types/chat'
import { getAccessToken } from '@/utils/token'

/**
 * 问答工作台的接口封装（`/api/query/*`）。
 *
 * 分层：组件只 emit，页面编排，本文件是**唯一**发请求的地方。
 * 流式接口内部用 `fetch`（非 axios）——原因见 `openAskStream` 的注释。
 */

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || ''

export const chatApi = {
  /**
   * 会话列表。
   * CLAUDE.md 统一分页 → 取 `data.list`（spec §9-13：be-05 若改为裸数组，只改这里）。
   */
  listSessions: () => get<PageResult<ChatSessionVo>>('/api/query/sessions'),

  /** 单会话历史轮次。`limit` 用后端默认（不传），保证与后端组 Prompt 的口径一致（spec §9-6）。 */
  getSession: (sessionId: string) => get<SessionDetailVo>(`/api/query/sessions/${sessionId}`),

  /**
   * 删除会话。
   * ⚠️ 语义待 be-05 明确（spec §9-2）：fe-03 §4.1 说删会话，而现状 docstring 写的是「清空上下文」。
   * 本页按 fe-03 §4.1 实现（删后从侧边栏移除）。
   */
  deleteSession: (sessionId: string) => del<null>(`/api/query/sessions/${sessionId}`),

  /**
   * 智能联想。无参调用（spec §9-1：接口参数未定义，不猜 `?prefix=`）。
   *
   * 元素形状后端未定稿（spec §9-15），这里做一次归一化容错：
   * 纯字符串与 `{question}` 两种都收，输出统一为 `string[]`。
   */
  async suggestions(): Promise<string[]> {
    const raw = await get<Array<string | SuggestionItem>>('/api/query/suggestions')
    if (!Array.isArray(raw)) return []
    return raw
      .map((item) => (typeof item === 'string' ? item : item?.question))
      .filter((q): q is string => typeof q === 'string' && q.trim() !== '')
  },

  /** 非流式问答。**仅**在浏览器不支持流时降级使用（spec §9-5）。 */
  ask: (payload: AskRequest) => post<AskResponseVo>('/api/query/ask', payload),

  openAskStream,
}

/* ==================== 流式请求 ==================== */

function sendAskStream(payload: AskRequest, signal: AbortSignal): Promise<Response> {
  const access = getAccessToken()
  return fetch(`${API_BASE_URL}/api/query/ask/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      // 显式声明，便于后端与 Nginx 正确识别为事件流（不会被当普通响应缓冲）
      Accept: 'text/event-stream',
      // 身份只在这里。payload 里**没有** user_context（红线 1 / PRD §11.3 R3）
      ...(access ? { Authorization: `Bearer ${access}` } : {}),
    },
    body: JSON.stringify(payload),
    // 用户点「停止生成」/ 切会话 / 组件卸载时 abort，让 reader.read() 抛 AbortError
    signal,
  })
}

/**
 * 借道一次普通受保护请求，触发 `http.ts` 的**单飞刷新**。
 *
 * 为什么不能直接刷新：`http.ts` 的单飞刷新是私有的（只导出 `get/post/put/del` 与
 * `setSessionExpiredHandler`），而 SSE 走裸 `fetch`，够不到 axios 拦截器（spec §9-18）。
 * 借道的好处是**刷新逻辑仍然只有 http.ts 一份**，不另起一套会产生两套真相。
 *
 * 刷新失败时，http.ts 会清 token 并回调 `setSessionExpiredHandler` → 已注册的守卫跳登录页。
 */
async function refreshAccessViaAxios(): Promise<boolean> {
  try {
    await authApi.me()
    return getAccessToken() !== null
  } catch {
    return false
  }
}

/**
 * 发起 SSE 问答流，返回原始 `Response` 交给 `useAskStream` 消费。
 *
 * **为什么必须 `fetch` 而不是 `EventSource`**（CLAUDE.md「检索实现要点」/ PRD §6.2.5）：
 * `EventSource` 只支持 GET、不能设请求头（带不了 `Authorization`）、也无法携带 POST body。
 * 因此全项目**不允许出现 `new EventSource(...)`**。
 */
export async function openAskStream(payload: AskRequest, signal: AbortSignal): Promise<Response> {
  const first = await sendAskStream(payload, signal)
  if (first.status !== 401) return first

  // access 过期。刷新后**重发整个问答**——SSE 请求是有副作用的（会落会话与问答日志），
  // 不能只重放「半个」。最多重试一次，避免刷新失败时无限循环。
  const refreshed = await refreshAccessViaAxios()
  if (!refreshed) return first

  return sendAskStream(payload, signal)
}
