/**
 * 问答工作台 DTO。与后端 `backend/schemas/query.py`（be-05）+ 字典 §2.3 / §7 一一对应。
 *
 * ✅ **2026-10-14 已回核**：be-05 已实现（`api/query_router.py` + `schemas/query.py`
 * + `engines/search_engine/`），原先挂的两处已销：
 *    1. ✅ `SessionDetail` 形状已按后端核对（原 spec §9-14）；
 *    2. ✅ `suggestions` 的元素形状已按后端核对（原 spec §9-15）。
 *
 * 🔴 但**别把「类型对上了」当成「接口通了」**：本机连不上云端 MySQL ⇒
 *    `verify_search.py` 的 T2（接口层）14 项 SKIP，`session_id` 换人提问那个
 *    横向越权守门用例（T2-4）**一次都没跑过**。首次联调先跑它。
 */

/** `qa_message.role`——字典 §2.3 枚举 `user` / `assistant` */
export type ChatRole = 'user' | 'assistant'

/**
 * 会话摘要（`GET /api/query/sessions` 的元素，对应 `qa_session`）。
 * 该接口按 CLAUDE.md 统一分页返回 `{list,total,page,page_size}`（spec §9-13）。
 */
export interface ChatSessionVo {
  id: string
  /** 取首问前 20 字（字典 §2.3）。新会话尚未落库时为空 */
  title: string
  message_count: number
  created_at: string
  updated_at: string
}

/** 单条引用溯源（SSE `citations.items[]`，字典 §7） */
export interface CitationItem {
  document_id: string
  title: string
  chunk_id: string
  /** 标题路径，形如 `差旅制度 > 报销标准 > 住宿费`（字典 §2.3 `heading_path`） */
  heading_path: string
  /** 相似度分数，展示时保留 2 位小数 */
  score: number
}

/**
 * 消息在**前端会话内**的终态。历史消息一律 `done`。
 *
 * `stopped` / `interrupted` / `failed` 三种非正常结束必须留下可见标记——
 * 「静默吞掉错误」会让用户以为模型就是这么答的（fe-03 §4.2.6）。
 */
export type MessageStatus =
  | 'pending' // 占位气泡已建，尚未收到 start
  | 'streaming' // 正在逐字接收 delta
  | 'done' // 正常结束
  | 'stopped' // 用户主动停止
  | 'interrupted' // 流中途断开（未收到 done）
  | 'failed' // 收到 error 事件或 HTTP 层失败

/**
 * 消息流中的一条消息 = 后端 `qa_message` + **前端会话内状态**。
 * 下方标注为「前端会话内状态」的字段不来自后端、不入库，纯 UI 用。
 */
export interface ChatMessageVo {
  id: string
  session_id: string
  role: ChatRole
  /** 原始 Markdown 文本。渲染时才过 `renderSafe()`——存原文、渲染时清洗（fe-03 §4.2.4） */
  content: string
  citations: CitationItem[] | null
  blocked_count: number
  created_at: string

  /* ---- 以下为前端会话内状态，不来自后端 ---- */
  /** 本轮流式状态 */
  status: MessageStatus
  /**
   * 仅当后端开了 `ACL_REVEAL_BLOCKED_TITLES`（默认 false）的演示开关时才有值。
   * 历史轮次回放拿不到它——`qa_message` 只存 `blocked_count`（字典 §2.3）。
   */
  blocked_titles?: string[]
  /** `done` 帧的 `latency_ms`；历史消息没有 */
  latency_ms?: number
  /** `done` 帧的 `token_out`。**仅用于展示**，不再用于推断是否调用了模型 */
  token_out?: number
  /** `done` 帧的 `faq_hit`。true 时挂「毫秒级直出」徽标 */
  faq_hit?: boolean
  /**
   * `done` 帧的 `llm_called`（be-05 §10-107）。
   * 取代原先「`token_out === 0 && !faq_hit` → 未调用模型」的推断（见 §9-10）。
   */
  llm_called?: boolean
  /** 失败/中断的摘要文案（正文保留，不覆盖） */
  error_message?: string
}

/* ==================== SSE 事件载荷（字典 §7 / be-05 §4.3） ==================== */

export interface SseStart {
  session_id: string
  message_id: string
}

export interface SseDelta {
  content: string
}

export interface SseCitations {
  items: CitationItem[]
}

export interface SseBlocked {
  count: number
  /** 默认不返回（`ACL_REVEAL_BLOCKED_TITLES=false`）。红线 4：无值即只报数量 */
  titles?: string[]
}

export interface SseDone {
  latency_ms: number
  token_in: number
  token_out: number
  faq_hit: boolean
  /**
   * 本轮**是否真的调用了生成模型**（be-05 §10-107 冻结）。
   *
   * 🔴 不要再用 `token_out === 0 && !faq_hit` 推断：模型正常应答但产出 0 token
   *    （或 usage 未随末帧下发）时，那个推断会误报成「未调用模型」。
   *    后端 `stream_options.include_usage` 已开，token 计数有值时以本字段为准。
   */
  llm_called: boolean
}

export interface SseError {
  code: number
  message: string
}

/**
 * 事件名 → 载荷的类型字典。分发器用 `keyof SseEventMap` 收窄，避免 `any`（fe-03 §5）。
 */
export interface SseEventMap {
  start: SseStart
  delta: SseDelta
  citations: SseCitations
  blocked: SseBlocked
  done: SseDone
  error: SseError
}

export type SseEventName = keyof SseEventMap

/* ==================== 请求 / 其它响应 ==================== */

/**
 * `POST /api/query/ask` 与 `/ask/stream` 的请求体。
 *
 * 🔴 **红线 1（PRD §11.3 R3）**：`user_context` 曾是**客户端传参**，属越权漏洞。
 *    它现在**已经从后端删除了**（不是「被 JWT 覆盖」——覆盖的话字段还在，
 *    下一个人照着旧文档写客户端就把口子开回来），前端也**物理上不定义该字段**。
 *    身份只能来自 `Authorization` 头里的 JWT。**不要**在这里加回 `user_context`。
 *    守门用例：`backend/scripts/verify_search.py` 的 T1-40a / T1-40b。
 */
export interface AskRequest {
  query: string
  /** 为空 = 新会话，由后端在首问时创建 `qa_session` */
  session_id: string | null
  /** 召回条数。前端不自行调整，用后端默认值（fe-03 §8：避免看板召回口径不一致） */
  top_k: number
}

/** `GET /api/query/sessions/{id}` 的 data。形状未定稿，见 spec §9-14 */
export interface SessionDetailVo {
  id: string
  title: string
  message_count: number
  messages: ChatMessageVo[]
}

/**
 * `GET /api/query/suggestions` 的元素。
 * 后端未定稿（be-05 §10 #23），连「纯字符串 or 对象」都未定——`api/chat.ts` 做归一化容错。
 */
export interface SuggestionItem {
  question: string
}

/**
 * `POST /api/query/ask`（非流式，降级路径）的 data。形状未定稿，见 spec §9-17。
 * 按 SSE 事件的并集定义，降级路径上做宽松取值。
 */
export interface AskResponseVo {
  session_id: string
  message_id: string
  /** 完整回答（等价于把多帧 delta 拼起来） */
  content: string
  citations: CitationItem[] | null
  blocked_count: number
  blocked_titles?: string[]
  latency_ms: number
  token_out: number
  faq_hit: boolean
  /** 见 `SseDone.llm_called`（两处同义，别只改一个） */
  llm_called?: boolean
}
