import { chatApi } from '@/api/chat'
import { ASK_TOP_K } from '@/constants/chat'
import { useChatStore } from '@/stores/chat'
import type { AskRequest, SseEventMap, SseEventName } from '@/types/chat'

import { parseFrame, splitFrames } from '../utils/sse'

/**
 * SSE 消费核心（fe-03 §4.2）：`fetch` + `ReadableStream` + 分帧解析 + 六事件分发 + `AbortController`。
 *
 * 🔴 **为什么不是 `EventSource`**：它只支持 GET、不能设请求头（带不了 `Authorization`）、
 *    也无法携带 POST body。全项目**不允许出现 `new EventSource(...)`**。
 *
 * 分层：本文件负责「网络 + 分发」，状态收敛全在 `stores/chat.ts`。
 * 纯解析逻辑在 `../utils/sse.ts`（与 Vue 解耦，可单测）。
 */

/** 契约里的六种事件（字典 §7 / be-05 §4.3）。未知事件名一律忽略，不影响其它帧 */
type Handler<K extends SseEventName> = (payload: SseEventMap[K]) => void

export interface AskStreamApi {
  /** 发送一问。空串、或已有流在跑时直接返回 */
  send: (question: string) => Promise<void>
  /** 停止生成。等价于中止当前流，气泡会被标记为「已停止」 */
  stop: () => void
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    (error as { name?: unknown }).name === 'AbortError'
  )
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message
  return '生成失败，请重试'
}

/** `response.ok === false` 时读后端 `{code,message}`；不是 JSON 时退回状态码文案 */
async function readHttpError(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json()
    if (body && typeof body === 'object' && typeof (body as { message?: unknown }).message === 'string') {
      return (body as { message: string }).message
    }
  } catch {
    /* 响应体不是 JSON（如 Nginx 直接返 502 HTML），走下面的兜底 */
  }
  if (response.status === 401) return '登录已过期，请重新登录'
  if (response.status === 403) return '没有访问该内容的权限'
  return `请求失败（HTTP ${response.status}）`
}

export function useAskStream(): AskStreamApi {
  const store = useChatStore()

  /** 事件分发表。用映射表而不是 if-else，事件名收窄后载荷类型自动对上（fe-03 §5） */
  const handlers: { [K in SseEventName]: Handler<K> } = {
    start: (payload) => {
      const isNewSession = store.activeSessionId === null
      store.applyStart(payload)
      // 新会话：后端此刻才建出 qa_session，侧边栏需要重新拉一次才有这条记录
      if (isNewSession) void store.fetchSessions().catch(() => undefined)
    },
    delta: (payload) => store.appendDelta(payload.content),
    citations: (payload) => store.setCitations(payload.items ?? []),
    blocked: (payload) => store.setBlocked(payload),
    done: (payload) => {
      store.applyDone(payload)
      // done 后会话的 message_count / updated_at 变了，刷新侧边栏
      void store.fetchSessions().catch(() => undefined)
    },
    error: (payload) => store.failAssistant(payload.message || '生成失败'),
  }

  function dispatch(raw: string): void {
    const frame = parseFrame(raw)
    if (!frame) return

    const name = frame.event as SseEventName
    if (!(name in handlers)) {
      // 未知事件名 / 心跳：忽略。后端新增事件不能让前端整条流报错
      return
    }

    let payload: unknown
    try {
      payload = JSON.parse(frame.data)
    } catch {
      // 一帧畸形不应导致用户已经看到的内容消失
      console.warn('[chat] 跳过无法解析的 SSE 帧:', frame.event)
      return
    }

    // 收窄点仅此一处：`name` 已由上面的 `in handlers` 判定为合法事件名
    const handler = handlers[name] as (data: unknown) => void
    handler(payload)
  }

  /**
   * 读取循环。帧只在这里被消费，状态一律交给 store 收敛。
   * 是否收到过 `done` **不靠返回值判断**——`done` 会把 store 的 `streaming` 置 false，
   * 读完后若它还是 true，就是「流断了但没收到 done」。
   */
  async function consume(body: ReadableStream<Uint8Array>, stillCurrent: () => boolean): Promise<void> {
    const reader = body.getReader()
    // 必须 stream:true —— 中文一字 3 字节，分包边界大概率落在字符中间；
    // 不带该参数会解出 `�`（fe-03 §4.2.2 细节 2）
    const decoder = new TextDecoder('utf-8')
    let buffer = ''

    try {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })

        // 只在找到分隔符时才切出整帧，残余留到下一包（细节 1：跨包半帧）
        const { frames, rest } = splitFrames(buffer)
        buffer = rest

        for (const frame of frames) {
          // 用户切了会话 / 开了新会话：旧流作废，不能再往新消息流里写
          if (!stillCurrent()) return
          dispatch(frame)
        }
      }
      // 流正常结束，冲刷解码器里可能残留的半个字符
      decoder.decode()
    } finally {
      reader.releaseLock()
    }
  }

  /**
   * 降级路径：浏览器没有 `ReadableStream`（极老环境）时走非流式（fe-03 §4.5.2）。
   * 没有打字机效果，但引用与受限信息照常渲染——复用同一套状态收敛动作。
   */
  async function fallbackNonStream(payload: AskRequest, stillCurrent: () => boolean): Promise<void> {
    try {
      const res = await chatApi.ask(payload)
      if (!stillCurrent()) return

      // `AskResponse` 形状后端未定稿（spec §9-17），这里逐字段宽松取值。
      // 缺 session_id 时不调 applyStart（否则会把 activeSessionId 写成空串）；
      // 代价是新会话的侧边栏刷新会漏一次，属该降级路径的可接受损失。
      if (res.session_id && res.message_id) {
        store.applyStart({ session_id: res.session_id, message_id: res.message_id })
      }
      if (res.content) store.appendDelta(res.content)
      store.setCitations(res.citations ?? [])
      if (res.blocked_count > 0) {
        store.setBlocked({ count: res.blocked_count, titles: res.blocked_titles })
      }
      store.applyDone({
        latency_ms: res.latency_ms ?? 0,
        // 非流式端点不返回 token_in（`AskResponseVo` 里没有这个字段），
        // 但 `applyDone` 收的是 `SseDone`，形状必须齐 —— 补 0 而不是省掉。
        token_in: 0,
        token_out: res.token_out ?? 0,
        faq_hit: res.faq_hit ?? false,
        // 缺字段时按「调用了」兜底：P1 的 FAQ 是桩，走内容生成路径必然调用模型。
        // 兜成 false 会让气泡挂上「未调用生成模型」的错误徽标 —— 宁可少一个徽标，
        // 不可给用户一个错的结论。
        llm_called: res.llm_called ?? true,
      })
    } catch (error) {
      if (!stillCurrent()) return
      store.failAssistant(errorMessage(error))
    }
  }

  async function send(question: string): Promise<void> {
    const text = question.trim()
    if (text === '' || store.streaming) return

    store.beginExchange(text)
    // 记下这次提问对应的那条 AI 占位气泡：切会话/新会话后它就「不属于当前会话」了，
    // 用它挡住迟到的帧，避免「消息串台」
    const assistant = store.lastAssistantMessage
    const stillCurrent = (): boolean => assistant !== null && store.lastAssistantMessage === assistant

    // 每次发送都**新建** AbortController：复用旧实例会让上一次的 cancel 掐掉这一次的流
    const controller = new AbortController()
    store.startStream(controller)

    const payload: AskRequest = {
      query: text,
      // 为 null 表示新会话，由后端在首问时创建 qa_session
      session_id: store.activeSessionId,
      // 用后端默认召回条数，前端不自行调整（fe-03 §8：避免看板召回口径不一致）
      top_k: ASK_TOP_K,
      // 🔴 这里**没有** user_context：它是客户端传参、属越权漏洞（红线 1 / PRD §11.3 R3）。
      //    身份只来自 Authorization 头里的 JWT，由后端 get_current_user 解析并覆盖。
      //    前端不传是配合动作，**不把「前端不传」当安全措施**。
    }

    try {
      const response = await chatApi.openAskStream(payload, controller.signal)

      if (!response.ok) {
        if (!stillCurrent()) return
        store.failAssistant(await readHttpError(response))
        return
      }

      if (!response.body) {
        await fallbackNonStream(payload, stillCurrent)
        return
      }

      await consume(response.body, stillCurrent)

      // 走到这里说明流结束了。若 `streaming` 还是 true，说明**没收到 `done`**——
      // 结论是「生成中断」，保留已收到的文本但不补元信息（fe-03 §4.2.6）
      if (stillCurrent() && store.streaming) store.interruptAssistant()
    } catch (error) {
      if (!stillCurrent()) return

      if (isAbortError(error)) {
        // 用户主动停止：静默处理，不算错误。已生成的文本保留
        store.stopAssistant()
        return
      }
      // 网络中断等：同样保留已收到的文本，标「生成中断」而不是「失败」
      store.interruptAssistant()
    }
  }

  function stop(): void {
    // 只中止；标记「已停止」由 send 的 AbortError 分支统一做，避免两处状态写入
    store.stopStream()
  }

  return { send, stop }
}
