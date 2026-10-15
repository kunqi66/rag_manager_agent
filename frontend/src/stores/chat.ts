import { defineStore } from 'pinia'

import { chatApi } from '@/api/chat'
import { ACTIVE_SESSION_KEY, SUGGESTION_LIMIT } from '@/constants/chat'
import type {
  ChatMessageVo,
  ChatSessionVo,
  CitationItem,
  SseBlocked,
  SseDone,
  SseStart,
} from '@/types/chat'

/**
 * 问答工作台的状态持有者（fe-03 §4.1）。
 *
 * 组件之间**不用 event bus**，共享的只有这个 store；纵向传递一律 props/emits。
 * 本文件只做「状态收敛」，不发流式请求——网络与分发在 `views/chat/composables/useAskStream.ts`。
 *
 * **偏离 fe-03 §4.1 两处**（已在 spec §9-19 / §9-20 记录）：
 * 1. `send()` / `stop()` 放组合式函数（store 调组合式函数会形成循环依赖）；
 * 2. 不再有全局 `streamError`——错误挂在**消息自身**上，一屏多条失败消息才不会串台。
 */

interface ChatState {
  /** 侧边栏列表，后端按 updated_at 倒序 */
  sessions: ChatSessionVo[]
  /** 当前会话；null = 新会话（尚未在库里） */
  activeSessionId: string | null
  /** 当前会话的消息流（user/assistant 交替） */
  messages: ChatMessageVo[]
  /** 是否正在流式接收 */
  streaming: boolean
  /**
   * 当前流的取消句柄。
   * 放 state 是安全的：Vue 的 reactive() 只代理普通对象/数组/Map/Set，
   * `AbortController` 不在其列，取到的仍是原始实例，`fetch` 可正常消费其 signal。
   */
  abortController: AbortController | null
  /** 联想词 */
  suggestions: string[]
  /** 侧边栏加载中 */
  listLoading: boolean
  /** 历史轮次加载中 */
  historyLoading: boolean
}

let seq = 0

/** 本地临时 id。仅用于乐观渲染的气泡，收到 start 后由后端 message_id 覆盖 */
function tmpId(prefix: string): string {
  seq += 1
  return `${prefix}_tmp_${Date.now().toString(36)}_${seq}`
}

/** 历史轮次回放：后端只存了 role/content/citations/blocked_count，其余是前端状态 */
function normalizeHistoryMessage(raw: ChatMessageVo): ChatMessageVo {
  return {
    ...raw,
    citations: raw.citations ?? [],
    blocked_count: raw.blocked_count ?? 0,
    // 历史消息一律是已结束的
    status: 'done',
  }
}

/**
 * `activeSessionId` 的记忆（fe-03 §4.2.6：刷新后能回到上次那个会话）。
 * **存的是会话 id，不是任何凭证**——token 仍然只走 `utils/token.ts`。
 * 隐私模式下 sessionStorage 会抛，不能让它把应用崩掉。
 */
function readStoredSessionId(): string | null {
  try {
    return sessionStorage.getItem(ACTIVE_SESSION_KEY)
  } catch {
    return null
  }
}

function writeStoredSessionId(id: string | null): void {
  try {
    if (id === null) sessionStorage.removeItem(ACTIVE_SESSION_KEY)
    else sessionStorage.setItem(ACTIVE_SESSION_KEY, id)
  } catch {
    /* 存不进去就只当次会话有效，不影响功能 */
  }
}

export const useChatStore = defineStore('chat', {
  state: (): ChatState => ({
    sessions: [],
    activeSessionId: null,
    messages: [],
    streaming: false,
    abortController: null,
    suggestions: [],
    listLoading: false,
    historyLoading: false,
  }),

  getters: {
    activeSession(state): ChatSessionVo | null {
      return state.sessions.find((s) => s.id === state.activeSessionId) ?? null
    },

    /** 最后一条 AI 消息——`delta` 增量更新它 */
    lastAssistantMessage(state): ChatMessageVo | null {
      for (let i = state.messages.length - 1; i >= 0; i -= 1) {
        const msg = state.messages[i]
        if (msg && msg.role === 'assistant') return msg
      }
      return null
    },

    /** 消息流中是否存在受限气泡（决定是否要额外汇总提示） */
    hasBlockedHint(state): boolean {
      return state.messages.some((m) => m.role === 'assistant' && m.blocked_count > 0)
    },

    hasMessages(state): boolean {
      return state.messages.length > 0
    },
  },

  actions: {
    /* ==================== 会话管理 ==================== */

    async fetchSessions(): Promise<void> {
      this.listLoading = true
      try {
        const page = await chatApi.listSessions()
        this.sessions = page?.list ?? []
      } finally {
        this.listLoading = false
      }
    },

    /** 纯前端动作：会话实体在首次 ask 时由后端创建（`session_id` 传 null） */
    newSession(): void {
      this.stopStream()
      this.activeSessionId = null
      this.messages = []
      writeStoredSessionId(null)
    },

    /**
     * 切换会话。
     * ⚠️ 第一步必须中断旧流——不取消的话，旧流的 `delta` 会追加进新会话的消息流，
     * 表现为「消息串台」（fe-03 §4.3②）。
     */
    async switchSession(sessionId: string): Promise<void> {
      this.stopStream()
      this.activeSessionId = sessionId
      writeStoredSessionId(sessionId)
      this.messages = []
      this.historyLoading = true
      try {
        const detail = await chatApi.getSession(sessionId)
        this.messages = (detail?.messages ?? []).map(normalizeHistoryMessage)
      } finally {
        this.historyLoading = false
      }
    },

    /**
     * 删除会话。
     * ⚠️ 语义待 be-05 明确（spec §9-2）：本页按 fe-03 §4.1 实现——成功后**从侧边栏移除**；
     * 若后端最终采用「清空上下文」语义，改本 action 即可（只影响此处）。
     * 请求失败必须抛出，让调用方提示且**不**动本地列表（否则界面与库不一致）。
     */
    async deleteSession(sessionId: string): Promise<void> {
      await chatApi.deleteSession(sessionId)
      this.sessions = this.sessions.filter((s) => s.id !== sessionId)

      if (this.activeSessionId === sessionId) {
        const next = this.sessions[0]
        if (next) await this.switchSession(next.id)
        else this.newSession()
      }
    },

    /**
     * 进入页面时决定打开哪个会话：优先恢复上次那个，否则开新会话（空态）。
     * 上次的会话可能已被删——不在列表里就当没有。
     */
    restoreOrNewSession(): void {
      const stored = readStoredSessionId()
      if (stored && this.sessions.some((s) => s.id === stored)) {
        void this.switchSession(stored).catch(() => {
          // 历史拉取失败：保留空态由页面展示重试入口，不阻断其它功能
        })
        return
      }
      this.newSession()
    },

    /** 联想词：拉取失败静默降级为「不显示 chips」，**不阻断提问** */
    async fetchSuggestions(): Promise<void> {
      try {
        const list = await chatApi.suggestions()
        this.suggestions = list.slice(0, SUGGESTION_LIMIT)
      } catch {
        this.suggestions = []
      }
    },

    /* ==================== 流的开关 ==================== */

    startStream(controller: AbortController): void {
      this.abortController = controller
      this.streaming = true
    },

    stopStream(): void {
      this.abortController?.abort()
      this.abortController = null
      this.streaming = false
    },

    /* ==================== 消息与六事件收敛 ==================== */

    /** 追加用户气泡 + 空的 AI 占位气泡（fe-03 §4.3① 第 3 步） */
    beginExchange(question: string): void {
      const now = new Date().toISOString()
      this.messages.push({
        id: tmpId('u'),
        session_id: this.activeSessionId ?? '',
        role: 'user',
        content: question,
        citations: null,
        blocked_count: 0,
        created_at: now,
        status: 'done',
      })
      this.pushAssistantPlaceholder()
    },

    /** 补建 AI 占位气泡。用于兜底「收到 delta 但还没有占位」的乱序情形 */
    pushAssistantPlaceholder(): void {
      this.messages.push({
        id: tmpId('a'),
        session_id: this.activeSessionId ?? '',
        role: 'assistant',
        content: '',
        citations: null,
        blocked_count: 0,
        created_at: new Date().toISOString(),
        status: 'pending',
      })
    },

    /**
     * 取当前正在接收的 AI 消息；若它已终态则补建一个占位。
     * 分发器**不假设事件顺序**——先到 delta 后到 start 也要能工作（fe-03 §4.2.4）。
     */
    ensureStreamingAssistant(): ChatMessageVo {
      const last = this.lastAssistantMessage
      if (last && (last.status === 'pending' || last.status === 'streaming')) return last
      this.pushAssistantPlaceholder()
      // push 之后 getter 必然能取到刚建的那个
      return this.lastAssistantMessage as ChatMessageVo
    },

    applyStart(payload: SseStart): void {
      // 新会话：后端此刻才建出 qa_session，把 id 写入并让调用方刷一次侧边栏
      if (!this.activeSessionId) {
        this.activeSessionId = payload.session_id
        writeStoredSessionId(payload.session_id)
      }
      const msg = this.ensureStreamingAssistant()
      msg.id = payload.message_id
      msg.session_id = payload.session_id
      msg.status = 'streaming'
      this.streaming = true
    },

    /** 追加而非替换：存原始 Markdown，渲染时才过 renderSafe（fe-03 §4.2.4） */
    appendDelta(content: string): void {
      const msg = this.ensureStreamingAssistant()
      if (msg.status === 'pending') msg.status = 'streaming'
      msg.content += content
    },

    setCitations(items: CitationItem[]): void {
      const msg = this.ensureStreamingAssistant()
      msg.citations = items
    },

    /** 红线 4：`titles` 通常不存在，此时只保留数量 */
    setBlocked(payload: SseBlocked): void {
      const msg = this.ensureStreamingAssistant()
      msg.blocked_count = payload.count
      if (payload.titles && payload.titles.length > 0) msg.blocked_titles = payload.titles
    },

    applyDone(payload: SseDone): void {
      const msg = this.lastAssistantMessage
      // 只有还在接收中的消息才认这帧 done；已 stopped/failed 的不回退成「成功」
      if (msg && (msg.status === 'pending' || msg.status === 'streaming')) {
        msg.status = 'done'
        msg.latency_ms = payload.latency_ms
        msg.token_out = payload.token_out
        msg.faq_hit = payload.faq_hit
        // be-05 §10-107：以后端为准，不再由 token_out 反推
        msg.llm_called = payload.llm_called
      }
      this.streaming = false
      this.abortController = null
    },

    /**
     * 失败（`error` 事件或 HTTP 层异常）。
     * **保留已收到的文本**——用户已经看到的内容不能凭空消失（fe-03 §4.2.4）。
     */
    failAssistant(message: string): void {
      const msg = this.lastAssistantMessage
      if (msg && (msg.status === 'pending' || msg.status === 'streaming')) {
        msg.status = 'failed'
        msg.error_message = message
      }
      this.streaming = false
      this.abortController = null
    },

    /** 用户主动停止：静默，不算错误（§4.2.5） */
    stopAssistant(): void {
      const msg = this.lastAssistantMessage
      if (msg && (msg.status === 'pending' || msg.status === 'streaming')) {
        msg.status = 'stopped'
      }
      this.streaming = false
      this.abortController = null
    },

    /** 流中途断开：没有 done 帧，**不得**当完整回答（不补元信息、不显示秒答徽标） */
    interruptAssistant(): void {
      const msg = this.lastAssistantMessage
      if (msg && (msg.status === 'pending' || msg.status === 'streaming')) {
        msg.status = 'interrupted'
      }
      this.streaming = false
      this.abortController = null
    },
  },
})
