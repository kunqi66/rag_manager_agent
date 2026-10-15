<script setup lang="ts">
import { ArrowUp, ShieldCheck, Square } from 'lucide-vue-next'
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import {
  ACTION_SEND,
  ACTION_STOP,
  COMPOSER_COUNTER_THRESHOLD,
  COMPOSER_HINT,
  COMPOSER_MAX_HEIGHT,
  COMPOSER_PLACEHOLDER,
  QUESTION_MAX_LENGTH,
  scopeChipText,
} from '@/constants/chat'
import { useAuthStore } from '@/stores/auth'

/**
 * 提问输入区。原型 `NtZEu/Composer`（`YPbfN`）→ `PFMCf ComposerCard` → `Exngf ComposerTools`。
 *
 * 原型把「检索范围」chip 放在**输入卡的工具条里**（工具条左端），不是单独一条横幅——
 * 它提示的是「这句话会以什么权限去检索」，跟输入框贴在一起才有意义。
 *
 * 输入内容**只留在这个组件里**，不进 Pinia：草稿没有跨页共享的需求，
 * 进 store 反而要处理「切会话时草稿归谁」这种问题。
 */
const props = defineProps<{
  /** 流式进行中。此时输入框锁住、按钮变「停止」 */
  streaming: boolean
  disabled?: boolean
  /** 联想词。空数组 = 不渲染 chips 行（接口失败/无数据时静默降级） */
  suggestions?: string[]
  /**
   * 外部预填的提问原文（如 fe-05「热门问题」下钻带过来的 `?q=`）。
   * **只填进输入框、不自动发送**——见 `ChatView.vue` 里消费 `?q=` 处的说明。
   * 值本身是**普通字符串**，走插值，**与 `renderSafe` 无关**（那是渲染出口，不是入口）。
   */
  preset?: string
}>()

const emit = defineEmits<{
  send: [question: string]
  stop: []
}>()

const text = ref('')
const textarea = ref<HTMLTextAreaElement | null>(null)

const auth = useAuthStore()

const locked = computed(() => props.streaming || props.disabled === true)

/**
 * 检索范围文案。`deptLabel` 取自 store ——
 * 「取末两段拼 `·`」的规则只在那里实现一次（侧栏副标题读的是同一个值），
 * 本组件只负责套上「检索范围：…（含继承）」的外壳。
 */
const scopeText = computed(() => scopeChipText(auth.deptLabel))

const canSend = computed(() => !locked.value && text.value.trim() !== '')

/** 接近上限才显示计数，平时不占视觉噪音 */
const showCounter = computed(
  () => text.value.length >= QUESTION_MAX_LENGTH * COMPOSER_COUNTER_THRESHOLD,
)

/** 自增高：先归零再按 scrollHeight 设，否则删字时高度不会缩回去 */
function resize(): void {
  const el = textarea.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, COMPOSER_MAX_HEIGHT)}px`
}

watch(text, () => {
  void nextTick(resize)
})

function submit(): void {
  if (!canSend.value) return
  const question = text.value.trim()
  text.value = ''
  void nextTick(resize)
  emit('send', question)
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter') return
  // Shift+Enter 换行，原样交给浏览器
  if (event.shiftKey) return

  // 🔴 中文输入法：拼音候选框里敲 Enter 是「上屏」不是「发送」。
  //    `isComposing` 在 Chrome/Edge 可靠；部分输入法（老版搜狗等）在 compositionend
  //    之后才派发这个 Enter，此时 isComposing=false 但 keyCode 仍是 229，必须一起挡。
  //    少这一道判断，用户每选一次候选词就会误发一条提问。
  if (event.isComposing || event.keyCode === 229) return

  event.preventDefault()
  submit()
}

function onPrimary(): void {
  if (props.streaming) {
    emit('stop')
    return
  }
  submit()
}

/**
 * 联想词**只填进输入框**，不代用户发送——他还有机会改一改再发（fe-03 §4）。
 * 放在输入卡上方（spec §9-15）：原型里没有这块 UI，位置按「离输入最近」取。
 */
function onSuggestion(item: string): void {
  if (locked.value) return
  text.value = item
  void nextTick(() => {
    resize()
    textarea.value?.focus()
  })
}

/**
 * 预填（跨页下钻带过来的提问）。**只填不发**：替用户发一条他没打的提问，
 * 既有违直觉，又会真的消耗一次生成模型调用。
 *
 * `immediate: true` 是为了覆盖「页面首次渲染时就带着预填值」的情况——
 * 此时 watcher 在 setup 阶段就跑，`textarea` 还没挂上，所以聚焦要放进 `nextTick`。
 */
watch(
  () => props.preset,
  (value) => {
    if (!value || value.trim() === '') return
    text.value = value
    void nextTick(() => {
      resize()
      textarea.value?.focus()
    })
  },
  { immediate: true },
)

onMounted(() => {
  // 这是对话页，进来就该能直接打字（与是否带 `?q=` 无关）
  if (!locked.value) textarea.value?.focus()
})
</script>

<template>
  <div class="composer">
    <!-- 联想词：接口失败或无数据时整行不渲染（不报错、不阻断提问） -->
    <div v-if="suggestions && suggestions.length" class="composer__chips">
      <button
        v-for="item in suggestions"
        :key="item"
        class="composer__chip"
        type="button"
        @click="onSuggestion(item)"
      >
        {{ item }}
      </button>
    </div>

    <div class="composer__card">
      <textarea
        ref="textarea"
        v-model="text"
        class="composer__input"
        :placeholder="COMPOSER_PLACEHOLDER"
        :maxlength="QUESTION_MAX_LENGTH"
        :disabled="locked"
        rows="1"
        @keydown="onKeydown"
        @input="resize"
      />

      <div class="composer__tools">
        <span class="composer__scope">
          <ShieldCheck :size="13" />
          <span>{{ scopeText }}</span>
        </span>

        <span class="composer__spacer" />

        <span v-if="showCounter" class="composer__counter">
          {{ text.length }}/{{ QUESTION_MAX_LENGTH }}
        </span>

        <span class="composer__hint">{{ COMPOSER_HINT }}</span>

        <button
          class="composer__send"
          :class="{ 'composer__send--stop': streaming }"
          type="button"
          :title="streaming ? ACTION_STOP : ACTION_SEND"
          :aria-label="streaming ? ACTION_STOP : ACTION_SEND"
          :disabled="!streaming && !canSend"
          @click="onPrimary"
        >
          <Square v-if="streaming" :size="14" />
          <ArrowUp v-else :size="16" />
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* Composer：外层只给边距 padding [0,28,18,28]（原型 YPbfN），纵向 gap 8 */
.composer {
  flex: none;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0 28px 18px;
}

.composer__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

/* 复用 ScopeChip 的胶囊形态（原型无此 UI，图元取自同屏已有元素） */
.composer__chip {
  padding: 5px 11px;
  border: 1px solid var(--kb-border);
  border-radius: 13px;
  background: var(--kb-bg-card);
  font-family: inherit;
  font-size: 11.5px;
  color: var(--kb-text-secondary);
  cursor: pointer;
}

.composer__chip:hover {
  border-color: var(--kb-brand);
  color: var(--kb-brand);
  background: var(--kb-brand-softer);
}

/* ComposerCard：bg $bg-card，圆角 12，描边 1 border，padding [13,15]，纵向 gap 11 */
.composer__card {
  display: flex;
  flex-direction: column;
  gap: 11px;
  padding: 13px 15px;
  border: 1px solid var(--kb-border);
  border-radius: 12px;
  background: var(--kb-bg-card);
}

/* ComposerInput：13.5，占位色 $text-placeholder */
.composer__input {
  width: 100%;
  min-height: 20px;
  padding: 0;
  border: 0;
  outline: none;
  resize: none;
  overflow-y: auto;
  background: transparent;
  font-family: inherit;
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--kb-text-primary);
}

.composer__input::placeholder {
  color: var(--kb-text-placeholder);
}

.composer__input:disabled {
  color: var(--kb-text-muted);
}

/* ComposerTools：横向 gap 8，居中（原型 Exngf） */
.composer__tools {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 30px;
}

/* ScopeChip：bg $brand-softer，圆角 6，gap 6，padding [5,10]，文字 11 $brand */
.composer__scope {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-radius: 6px;
  background: var(--kb-brand-softer);
  color: var(--kb-brand);
  font-size: 11px;
}

.composer__spacer {
  flex: 1 1 auto;
}

.composer__counter {
  flex: none;
  font-size: 11px;
  color: var(--kb-text-muted);
}

/* ComposerHint：11 $text-muted */
.composer__hint {
  flex: none;
  font-size: 11px;
  color: var(--kb-text-muted);
}

/* SendBtn：30×30，圆角 8，bg $brand，图标 $text-on-brand */
.composer__send {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  padding: 0;
  border: 0;
  border-radius: 8px;
  background: var(--kb-brand);
  color: var(--kb-text-on-brand);
  cursor: pointer;
}

.composer__send:disabled {
  background: var(--kb-border);
  color: var(--kb-text-muted);
  cursor: not-allowed;
}

/* 流式中：同一个按钮位换成「停止」。原型没有这个状态（设计补的行为） */
.composer__send--stop {
  background: var(--kb-danger);
  color: var(--kb-text-on-brand);
}
</style>
