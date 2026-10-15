<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import type { ChatMessageVo, CitationItem } from '@/types/chat'

import AssistantBubble from './AssistantBubble.vue'
import UserBubble from './UserBubble.vue'

/**
 * 消息流滚动容器。原型 `NtZEu/Stream`（`sTMdU`，纵向 gap 20）。
 *
 * 自己持有滚动容器而不是交给父组件：**自动跟随底部**必须知道用户此刻在不在这儿，
 * 这个状态只跟这个滚动容器有关。
 *
 * 跟随策略：用户往上翻看历史时**不**把视图拽回底部（`stick=false`），
 * 滑回底部附近自动恢复跟随——否则流式输出时想回看引用会被反复打断。
 */
const props = defineProps<{
  messages: ChatMessageVo[]
  streaming: boolean
}>()

const emit = defineEmits<{
  retry: [messageId: string]
  'cite-click': [item: CitationItem]
}>()

const scroller = ref<HTMLElement | null>(null)

/** 是否跟随到底部。默认跟随（新会话总是从底部开始） */
const stick = ref(true)

/** 判定「已经在底部附近」的容差（px）：留一点余量，避免滚动条亚像素误差把跟随关掉 */
const STICK_TOLERANCE = 48

/**
 * 跟随触发的信号：由「条数 + 末条内容长度 + 末条状态」拼成的字符串原始值。
 * 用字符串而不是对象，是为了让 watch 做值比较——否则流式每个 token 都会触发一次。
 */
const tailKey = computed(() => {
  const last = props.messages[props.messages.length - 1]
  return `${props.messages.length}:${last?.content.length ?? 0}:${last?.status ?? ''}`
})

let lastCount = 0

function scrollToBottom(): void {
  const el = scroller.value
  if (el) el.scrollTop = el.scrollHeight
}

function onScroll(): void {
  const el = scroller.value
  if (!el) return
  const distance = el.scrollHeight - el.scrollTop - el.clientHeight
  stick.value = distance <= STICK_TOLERANCE
}

watch(tailKey, () => {
  // 条数变了（新提问 / 切会话）说明视线应该回到最新处；只是同一条在长高才尊重用户
  if (props.messages.length !== lastCount) {
    lastCount = props.messages.length
    stick.value = true
  }
  if (!stick.value) return
  void nextTick(scrollToBottom)
})

onMounted(() => {
  lastCount = props.messages.length
  scrollToBottom()
})
</script>

<template>
  <div ref="scroller" class="stream" @scroll.passive="onScroll">
    <!-- 空态由父组件给：空态里要放联想词，而联想词属于页面而不是消息流 -->
    <slot v-if="messages.length === 0" name="empty" />

    <template v-else>
      <template v-for="message in messages" :key="message.id">
        <UserBubble v-if="message.role === 'user'" :content="message.content" />
        <AssistantBubble
          v-else
          :message="message"
          :streaming="streaming"
          @retry="emit('retry', $event)"
          @cite-click="emit('cite-click', $event)"
        />
      </template>
    </template>
  </div>
</template>

<style scoped>
/* Messages：flex 纵向、gap 22、padding [24,28]、可滚、撑满（原型 m76vTz） */
.stream {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 22px;
  padding: 24px 28px;
}
</style>
