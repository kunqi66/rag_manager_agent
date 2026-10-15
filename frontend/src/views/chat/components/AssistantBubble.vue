<script setup lang="ts">
import { RefreshCw, Sparkles } from 'lucide-vue-next'
import { computed } from 'vue'

import {
  ACTION_RETRY,
  ASSISTANT_NAME,
  FAQ_HIT_BADGE,
  META_BLOCKED_NO_LLM,
  META_FAILED,
  META_INTERRUPTED,
  META_STOPPED,
  META_STREAMING,
} from '@/constants/chat'
import { renderSafe } from '@/utils/markdown'
import type { ChatMessageVo, CitationItem } from '@/types/chat'

import { formatLatency } from '../utils/time'
import BlockedBubble from './BlockedBubble.vue'
import CitationList from './CitationList.vue'

/**
 * AI 回答气泡。原型 `NtZEu/AiMsg`（`LoIHB` 正常回答 / `WTK0N` 受限回答）。
 *
 * 🔴 **红线 2（PRD R7）**：本组件是全页**唯一**的 `v-html`，其值恒为
 *    `renderSafe(message.content)`。`renderSafe` 是 `utils/markdown.ts` 里的全项目唯一富文本出口
 *    （DOMPurify + marked 两道闸门），**不允许在别处 import DOMPurify**。
 *    除正文外的一切（标题、路径、状态文案、后端 message）一律走插值 `{{ }}`。
 *
 * 「存原文、渲染时清洗」：`delta` 只往 `message.content` 追加原始 Markdown，
 * 清洗发生在 `computed` 里——对已清洗的 HTML 追加会让每次 delta 都重新破坏清洗边界。
 */
const props = defineProps<{
  message: ChatMessageVo
  /** 全局流状态。用于决定光标是否闪烁（本消息可能已结束而别的还在跑） */
  streaming: boolean
}>()

const emit = defineEmits<{
  retry: [messageId: string]
  'cite-click': [item: CitationItem]
}>()

const isActive = computed(
  () => props.message.status === 'pending' || props.message.status === 'streaming',
)

const hasContent = computed(() => props.message.content !== '')

const rendered = computed(() => renderSafe(props.message.content))

/** 流式光标：仅在「本消息还在接收」且已有内容时闪，空内容时显示等待点 */
const showCursor = computed(() => props.streaming && isActive.value && hasContent.value)
const showThinking = computed(() => isActive.value && !hasContent.value)

/**
 * 本轮是否调用了生成模型。
 *
 * 🔴 优先用后端下发的 `done.llm_called`（be-05 §10-107）。
 *    **历史轮次回放拿不到它** —— `qa_message` 不存这一列（字典 §2.3），
 *    只有本轮流式的消息才有值；此时退回旧的 `token_out` 推断，
 *    宁可显示不出徽标，也不要给一个错的结论。
 */
const llmCalled = computed<boolean | undefined>(() => {
  const m = props.message
  if (m.llm_called !== undefined) return m.llm_called
  if (m.faq_hit) return false
  if (m.token_out === undefined) return undefined
  return m.token_out > 0
})

/**
 * 元信息（原型 `AiHead/AiMeta`）。
 */
const meta = computed(() => {
  const m = props.message

  if (m.status === 'pending' || m.status === 'streaming') return META_STREAMING
  if (m.status === 'stopped') return META_STOPPED
  if (m.status === 'interrupted') return META_INTERRUPTED
  if (m.status === 'failed') return META_FAILED

  const latency = formatLatency(m.latency_ms ?? 0)
  if (m.faq_hit) return `${FAQ_HIT_BADGE} · ${latency}`
  if (m.blocked_count > 0 && llmCalled.value === false) return META_BLOCKED_NO_LLM

  const count = m.citations?.length ?? 0
  return count > 0 ? `检索到 ${count} 篇 · 耗时 ${latency}` : `耗时 ${latency}`
})

/** 只有「失败 / 生成中断」给重试入口；「已停止」是用户意图，不该劝他重来 */
const canRetry = computed(
  () => props.message.status === 'failed' || props.message.status === 'interrupted',
)

const citations = computed(() => props.message.citations ?? [])
</script>

<template>
  <div class="ai-msg">
    <div class="ai-msg__avatar"><Sparkles :size="15" /></div>

    <div class="ai-msg__body">
      <div class="ai-msg__head">
        <span class="ai-msg__name">{{ ASSISTANT_NAME }}</span>
        <span class="ai-msg__meta">{{ meta }}</span>
      </div>

      <!-- 🔴 全页唯一的 v-html，值恒为 renderSafe(...)：LLM 输出是不可信数据 -->
      <div v-if="hasContent" class="ai-msg__md md" v-html="rendered" />

      <div v-else-if="showThinking" class="ai-msg__thinking">
        <span /><span /><span />
      </div>

      <span v-if="showCursor" class="ai-msg__caret" aria-hidden="true" />

      <CitationList
        v-if="citations.length"
        :items="citations"
        @cite-click="emit('cite-click', $event)"
      />

      <!-- 红线 4：titles 缺省时只渲染数量 -->
      <BlockedBubble
        v-if="message.blocked_count > 0"
        :count="message.blocked_count"
        :titles="message.blocked_titles"
      />

      <div v-if="canRetry" class="ai-msg__actions">
        <span v-if="message.error_message" class="ai-msg__error">{{ message.error_message }}</span>
        <button class="ai-msg__retry" type="button" @click="emit('retry', message.id)">
          <RefreshCw :size="13" />
          <span>{{ ACTION_RETRY }}</span>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* AiMsg：横向 gap 12 */
.ai-msg {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}

/* AiAvatar：30×30 圆，bg $brand-soft，图标 $brand */
.ai-msg__avatar {
  flex: none;
  width: 30px;
  height: 30px;
  border-radius: 15px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--kb-brand-soft);
  color: var(--kb-brand);
}

/* AiBody：纵向 gap 10 */
.ai-msg__body {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  align-items: flex-start;
}

.ai-msg__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ai-msg__name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

/* AiMeta：11 $text-muted */
.ai-msg__meta {
  font-size: 11px;
  color: var(--kb-text-muted);
}

/* AiText：13.5 行高 1.7 */
.ai-msg__md {
  align-self: stretch;
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--kb-text-primary);
  word-break: break-word;
}

/* v-html 的内容不带 scoped 属性，必须用 :deep() 才吃得到样式 */
.md :deep(p) {
  margin: 0 0 8px;
}

.md :deep(p:last-child) {
  margin-bottom: 0;
}

.md :deep(ul),
.md :deep(ol) {
  margin: 0 0 8px;
  padding-left: 20px;
}

.md :deep(li) {
  margin-bottom: 2px;
}

.md :deep(code) {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--kb-bg-subtle);
  border: 1px solid var(--kb-border-soft);
  font-size: 12.5px;
}

.md :deep(pre) {
  margin: 0 0 8px;
  padding: 10px 12px;
  overflow-x: auto;
  border-radius: 8px;
  background: var(--kb-bg-subtle);
  border: 1px solid var(--kb-border-soft);
}

.md :deep(pre code) {
  padding: 0;
  border: 0;
  background: transparent;
}

.md :deep(blockquote) {
  margin: 0 0 8px;
  padding: 2px 0 2px 12px;
  border-left: 3px solid var(--kb-border);
  color: var(--kb-text-secondary);
}

.md :deep(a) {
  color: var(--kb-brand);
}

.md :deep(table) {
  width: 100%;
  margin: 0 0 8px;
  border-collapse: collapse;
  font-size: 12.5px;
}

.md :deep(th),
.md :deep(td) {
  padding: 6px 10px;
  border: 1px solid var(--kb-border);
  text-align: left;
}

.md :deep(th) {
  background: var(--kb-bg-subtle);
}

/* 等待中的三点：没有内容先给个「在动」的信号，别让气泡看起来是空的 */
.ai-msg__thinking {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 22px;
}

.ai-msg__thinking span {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--kb-text-placeholder);
  animation: kb-blink 1.2s infinite ease-in-out;
}

.ai-msg__thinking span:nth-child(2) {
  animation-delay: 0.15s;
}

.ai-msg__thinking span:nth-child(3) {
  animation-delay: 0.3s;
}

/* 流式光标 */
.ai-msg__caret {
  display: inline-block;
  width: 2px;
  height: 14px;
  margin-top: -6px;
  background: var(--kb-brand);
  animation: kb-blink 1s steps(1) infinite;
}

@keyframes kb-blink {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.2;
  }
}

.ai-msg__actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.ai-msg__error {
  font-size: 11.5px;
  color: var(--kb-danger);
}

.ai-msg__retry {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  border: 1px solid var(--kb-border);
  border-radius: 6px;
  background: var(--kb-bg-card);
  font-family: inherit;
  font-size: 11.5px;
  color: var(--kb-brand);
  cursor: pointer;
}

.ai-msg__retry:hover {
  border-color: var(--kb-brand);
  background: var(--kb-brand-softer);
}
</style>
