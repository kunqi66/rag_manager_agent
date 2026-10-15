import { SSE_FRAME_SEPARATOR } from '@/constants/chat'

/**
 * SSE 分帧与单帧解析——**纯函数，与 Vue 无关**，可单测
 * （喂一段含跨包的 `\n\n` 文本，断言帧边界正确）。
 *
 * 抽出来单独放的理由：`fetch` + `ReadableStream` 的两个最容易写错的地方
 * （跨包残余缓冲、UTF-8 半个汉字）都发生在这里，混在组件/组合式函数里没法验。
 */

export interface SseFrame {
  /** 事件名。没有 `event:` 行时为空串（SSE 规范默认事件名是 `message`） */
  event: string
  /** `data:` 行拼接后的原始载荷。**未做 JSON.parse** */
  data: string
}

/**
 * 从缓冲区切出**完整帧**，返回完整帧与剩余缓冲。
 *
 * TCP 分片不会尊重事件边界：一帧可能被切成两次 `read()` 返回。
 * 因此必须维护残余 `buffer`，只在找到分隔符时才切出整帧，剩下的留到下一包。
 *
 * ⚠️ **对每个 chunk 直接 `split('\n\n')` 是常见错误**——最后一个元素永远是被截断的半帧，
 * 把它当完整帧去 `JSON.parse` 必然失败，表现为「回答时断时续」。
 */
export function splitFrames(buffer: string): { frames: string[]; rest: string } {
  // 事件流规范允许 CRLF 作为行分隔；sse-starlette 用 LF，但上游换成 CRLF 时不能崩
  const normalized = buffer.replace(/\r\n/g, '\n')
  const parts = normalized.split(SSE_FRAME_SEPARATOR)
  // 最后一段要么是空串（正好以分隔符结尾），要么是被截断的半帧——两种情况都留作残余
  const rest = parts.pop() ?? ''
  return { frames: parts.filter((frame) => frame.trim() !== ''), rest }
}

/**
 * 解析单个帧。
 *
 * 规则（对应 SSE 规范与本项目约定）：
 * - `:` 开头的行是注释/心跳（keep-alive），**忽略且不报错**——Nginx `proxy_read_timeout` 期间
 *   后端可能发心跳，把心跳当未知事件会让整条流全程报错；
 * - `event:` → 事件名（去前导空格）；
 * - `data:` → 载荷，**多行用 `\n` 拼接**（本项目载荷是单行 JSON，但解析器要支持换行，
 *   否则后端一改格式就崩）；
 * - 无 `data` 的帧返回 `null`（空帧忽略）。
 */
export function parseFrame(raw: string): SseFrame | null {
  let event = ''
  const dataLines: string[] = []

  for (const line of raw.split('\n')) {
    if (line.startsWith(':')) continue
    if (line.startsWith('event:')) {
      event = line.slice('event:'.length).trim()
    } else if (line.startsWith('data:')) {
      // 冒号后允许一个空格，去掉它但不吃掉载荷本身的前导空白
      dataLines.push(line.slice('data:'.length).replace(/^ /, ''))
    }
    // 其余字段（id: / retry:）本项目不使用，忽略
  }

  if (dataLines.length === 0) return null
  return { event, data: dataLines.join('\n') }
}
