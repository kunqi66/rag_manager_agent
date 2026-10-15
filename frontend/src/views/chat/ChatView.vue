<script setup lang="ts">
import { ElMessage } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import { Bell, Info, Sparkles } from 'lucide-vue-next'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import PageTopbar from '@/components/PageTopbar.vue'
import {
  CITE_JUMP_QUERY_KEY,
  CITE_JUMP_ROUTE,
  EMPTY_STATE,
  PRESET_QUESTION_QUERY_KEY,
} from '@/constants/chat'
import { useAuthStore } from '@/stores/auth'
import { useChatStore } from '@/stores/chat'
import type { CitationItem } from '@/types/chat'

import ComposerInput from './components/ComposerInput.vue'
import MessageList from './components/MessageList.vue'
import SessionSidebar from './components/SessionSidebar.vue'
import { useAskStream } from './composables/useAskStream'

/**
 * fe-03「AI 智能问答工作台」。原型 `XA7T8`（`Main` = Topbar + Body，
 * Body = `SessionList` 264 + `ChatCol` 936）；最左侧的 `NavSidebar` 240 由 `AppLayout` 提供。
 *
 * **页面外壳**：`PageTopbar` 是项目统一的屏内顶栏（`src/components/PageTopbar.vue`），
 * 它的 padding/gap 固定为 20/12，与原型 `JsPoG` 的 24/14 有 4px、2px 出入——
 * 「要页面长得一样」的优先级低于「五个页面顶栏必须一致」，故以共用组件为准（spec §3.4）。
 *
 * **布局例外**：`ChatCol` 是「左会话栏（定宽 264）+ 右对话列」的两栏结构，
 * 两栏各自贴边、中间一条分隔线，**不能**套 `.app-page__body`（它自带 18/20 内边距与 16 间距，
 * 会把 SessionList 从屏幕左边缘推进来，也不再满高）。故这里用页面私有的 `.chat-body`
 * 顶替 `.app-page__body`（spec §3.3 已记录该偏离及理由）。
 *
 * 本组件只做编排：状态在 `stores/chat.ts`，SSE 在 `composables/useAskStream.ts`。
 */
const auth = useAuthStore()
const store = useChatStore()
const route = useRoute()
const router = useRouter()
const { send, stop } = useAskStream()

const displayName = computed(() => auth.user?.real_name || auth.user?.username || '未登录')
const initial = computed(() => displayName.value.slice(0, 1))

/**
 * 外部带进来的预填提问（fe-05「热门问题」下钻：`router.push({name:'chat', query:{q}})`）。
 *
 * **只填充、不自动发送**。理由三条：
 * 1. 替用户发一条**他自己没打的**提问，与「我点了下钻看看」的意图不符，是越权代理用户操作；
 * 2. 一次提问会真的消耗生成模型调用（`token_out` 计入成本与看板口径）；
 * 3. 与全页一致的纪律：联想 chip 也只是填进输入框（`ComposerInput.onSuggestion`），
 *    同一个「帮你起个头」的语义不该有两种行为。
 * 设计文档 fe-03 未规定该入参，故按「最小惊讶」自行定调。
 */
const presetQuestion = ref('')

function consumePresetQuestion(): void {
  const raw = route.query[PRESET_QUESTION_QUERY_KEY]
  // 同名参数出现多次时 Vue Router 给的是数组，只看单个字符串
  const question = typeof raw === 'string' ? raw.trim() : ''
  if (question === '') return

  presetQuestion.value = question
  // 抹掉 query：否则用户刷新会重复填充，带链接分享也会把别人的提问带出去。
  // 放进 nextTick 是因为本函数在「首次进入」时会于组件 setup 阶段同步执行，
  // 此刻应用的首个导航可能尚未落定，立刻 replace 会与之打架（表现为白屏或路由告警）。
  void nextTick(() => {
    void router.replace({ query: {} }).catch(() => undefined)
  })
}

// immediate 覆盖首次进入；非 immediate 的那半覆盖「已在本页时又被 push 了一次 ?q=」
// （同路由仅 query 变化不会重新挂载组件，onMounted 不会再跑）
watch(() => route.query[PRESET_QUESTION_QUERY_KEY], consumePresetQuestion, { immediate: true })

function toastError(error: unknown, fallback: string): void {
  ElMessage.error(error instanceof Error && error.message ? error.message : fallback)
}

onMounted(async () => {
  // 先有列表才知道「上次那个会话」还在不在，所以必须先 await
  try {
    await store.fetchSessions()
  } catch (error) {
    toastError(error, '会话列表加载失败')
  }
  store.restoreOrNewSession()
  // 联想词是锦上添花：store 内部已吞异常，失败就不显示 chips，绝不阻断提问
  void store.fetchSuggestions()
})

onBeforeUnmount(() => {
  // 离开页面必须掐断在途的流：否则后台还在收字节，store 里的消息永远停在「正在生成」
  stop()
})

function onSelectSession(sessionId: string): void {
  if (sessionId === store.activeSessionId) return
  void store.switchSession(sessionId).catch((error: unknown) => {
    toastError(error, '会话历史加载失败')
  })
}

function onCreateSession(): void {
  store.newSession()
}

function onRemoveSession(sessionId: string): void {
  void store.deleteSession(sessionId).catch((error: unknown) => {
    toastError(error, '删除失败，请稍后重试')
  })
}

function onSend(question: string): void {
  void send(question)
}

/** 重试 = 用同一个问题**重新发一条**（不原地覆盖失败的那条，便于对照两次结果） */
function onRetry(messageId: string): void {
  const index = store.messages.findIndex((m) => m.id === messageId)
  if (index < 0) return

  for (let i = index - 1; i >= 0; i -= 1) {
    const previous = store.messages[i]
    if (previous && previous.role === 'user') {
      void send(previous.content)
      return
    }
  }
}

/**
 * 引用跳转。前端只带 `document_id` 过去，**目标页会自己再校验一次权限**——
 * 这里的跳转是个便捷入口，不是授权判定（红线 3）。
 *
 * 参数名走常量（`CITE_JUMP_QUERY_KEY`）：它同时被 fe-02（台账页的入参）与 fe-05（下钻）
 * 使用，是跨页契约，散成字面量必然有一天只剩一边对得上。
 */
function onCiteClick(item: CitationItem): void {
  void router
    .push({ name: CITE_JUMP_ROUTE, query: { [CITE_JUMP_QUERY_KEY]: item.document_id } })
    .catch(() => undefined)
}
</script>

<template>
  <div class="app-page">
    <PageTopbar title="AI 问答工作台">
      <!-- 原型 JsPoG 右端的 info / bell：需求未定义点击行为（spec §9-11），
           按装饰渲染并 aria-hidden，不做成看起来能点但点了没反应的假按钮 -->
      <Info :size="18" class="topbar__icon" aria-hidden="true" />
      <Bell :size="18" class="topbar__icon" aria-hidden="true" />

      <span class="topbar__divider" aria-hidden="true" />

      <div class="topbar__user">
        <span class="topbar__avatar">{{ initial }}</span>
        <span class="topbar__name">{{ displayName }}</span>
      </div>
    </PageTopbar>

    <div class="chat-body">
      <SessionSidebar
        :sessions="store.sessions"
        :active-session-id="store.activeSessionId"
        :loading="store.listLoading"
        @select="onSelectSession"
        @create="onCreateSession"
        @remove="onRemoveSession"
      />

      <div class="chat-main">
        <MessageList
          :messages="store.messages"
          :streaming="store.streaming"
          @retry="onRetry"
          @cite-click="onCiteClick"
        >
          <template #empty>
            <div class="empty">
              <span class="empty__icon"><Sparkles :size="22" /></span>
              <p class="empty__title">{{ EMPTY_STATE.title }}</p>
              <p class="empty__desc">{{ EMPTY_STATE.desc }}</p>
            </div>
          </template>
        </MessageList>

        <ComposerInput
          :streaming="store.streaming"
          :suggestions="store.suggestions"
          :preset="presetQuestion"
          @send="onSend"
          @stop="stop"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
/* Topbar 右端（原型 JsPoG）：图标 18 $text-muted、1×22 分隔线、头像 28 圆 + 姓名 13 */
.topbar__icon {
  color: var(--kb-text-muted);
}

.topbar__divider {
  width: 1px;
  height: 22px;
  background: var(--kb-border);
}

.topbar__user {
  display: flex;
  align-items: center;
  gap: 8px;
}

.topbar__avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 14px;
  background: var(--kb-brand);
  color: var(--kb-text-on-brand);
  font-size: 12px;
  font-weight: 600;
}

.topbar__name {
  font-size: 13px;
  color: var(--kb-text-primary);
}

/* Body（原型 TXrwf）：SessionList 264 定宽 + ChatCol 撑满，两栏各自贴边 */
.chat-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
}

/* ChatCol（原型 NtZEu）：纵向，Messages 撑满、Composer 固定 */
.chat-main {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--kb-bg-page);
}

/* 空态：原型未画，用原型已有图元（品牌色圆形 + 文字层级）拼，尺寸随内容 */
.empty {
  margin: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  max-width: 520px;
  text-align: center;
}

.empty__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  height: 46px;
  border-radius: 23px;
  background: var(--kb-brand-soft);
  color: var(--kb-brand);
}

.empty__title {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.empty__desc {
  margin: 0;
  font-size: 12.5px;
  color: var(--kb-text-secondary);
}
</style>
