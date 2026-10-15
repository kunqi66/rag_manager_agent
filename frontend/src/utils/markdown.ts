import DOMPurify from 'dompurify'
import { marked } from 'marked'

/**
 * 全项目渲染富文本的**唯一出口**（CLAUDE.md 安全红线 2）。
 *
 * LLM 输出与知识正文在 `v-html` 之前必须先过这里。
 * 组件不允许绕过它直接 v-html，也不允许各自 import DOMPurify —— 净化规则
 * 散成多份，迟早有一份忘记跟着收紧。
 *
 * 登录页本身不用 v-html（无富文本），此函数在此建立，供 fe-03 问答页、fe-06 治理中心复用。
 */
export function renderSafe(markdown: string): string {
  // marked 在 async:false 下同步返回 string，但其类型签名是 string | Promise<string>
  const html = marked.parse(markdown, { async: false }) as string
  return DOMPurify.sanitize(html)
}

/**
 * 纯文本出口：只做转义，不走 markdown 解析。
 * 用于标题、引用来源、后端返回的 message 等**本就是纯文本**的地方。
 */
export function escapeText(text: string): string {
  return DOMPurify.sanitize(text, { ALLOWED_TAGS: [], ALLOWED_ATTR: [] })
}
