<script setup lang="ts">
import { ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message-box/style/css'
import { SquarePen, Trash2 } from 'lucide-vue-next'

import {
  DELETE_CONFIRM_TEXT,
  DELETE_CONFIRM_TITLE,
  LOADING_TEXT,
  SESSION_EMPTY_TEXT,
  SESSION_LIST_TITLE,
  SESSION_UNTITLED,
} from '@/constants/chat'
import type { ChatSessionVo } from '@/types/chat'

import { formatSessionTime } from '../utils/time'

/**
 * 会话记录栏。原型 `XA7T8/SessionList`（`vQ3rT`，宽 264，bg $bg-card，右侧 1px 描边）。
 *
 * `ElMessageBox` **不**在 `unplugin-auto-import` 的白名单里，必须显式 import 组件与它的
 * 样式文件——只 import 函数不 import 样式，弹窗会以无样式状态裸渲染。
 */
defineProps<{
  sessions: ChatSessionVo[]
  activeSessionId: string | null
  loading: boolean
}>()

const emit = defineEmits<{
  select: [sessionId: string]
  create: []
  remove: [sessionId: string]
}>()

/**
 * 删除前的二次确认：删的是**会话下的全部问答记录**（后端 `DELETE /sessions/{id}`
 * 的语义是清空上下文记忆），误触代价不可逆，必须拦一道（spec §9-21）。
 */
async function onDelete(session: ChatSessionVo): Promise<void> {
  try {
    await ElMessageBox.confirm(DELETE_CONFIRM_TEXT, DELETE_CONFIRM_TITLE, {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    // 用户取消：ElMessageBox 以 reject 表达，不算异常
    return
  }
  emit('remove', session.id)
}
</script>

<template>
  <aside class="sessions">
    <div class="sessions__head">
      <span class="sessions__title">{{ SESSION_LIST_TITLE }}</span>
      <button class="sessions__new" type="button" title="新建会话" @click="emit('create')">
        <SquarePen :size="16" />
      </button>
    </div>

    <div class="sessions__list">
      <p v-if="loading && sessions.length === 0" class="sessions__placeholder">{{ LOADING_TEXT }}</p>
      <p v-else-if="sessions.length === 0" class="sessions__placeholder">{{ SESSION_EMPTY_TEXT }}</p>

      <!-- 用 div 而不是 button：里面还要放删除按钮，button 嵌套 button 是非法 HTML -->
      <div
        v-for="session in sessions"
        :key="session.id"
        class="sessions__item"
        :class="{ 'sessions__item--active': session.id === activeSessionId }"
        role="button"
        tabindex="0"
        @click="emit('select', session.id)"
        @keydown.enter.prevent="emit('select', session.id)"
        @keydown.space.prevent="emit('select', session.id)"
      >
        <div class="sessions__item-body">
          <span class="sessions__item-title">{{ session.title || SESSION_UNTITLED }}</span>
          <span class="sessions__item-time">{{ formatSessionTime(session.updated_at) }}</span>
        </div>

        <button
          class="sessions__del"
          type="button"
          title="删除会话"
          @click.stop="onDelete(session)"
        >
          <Trash2 :size="13" />
        </button>
      </div>
    </div>
  </aside>
</template>

<style scoped>
/* SessionList：宽 264，bg $bg-card，右侧 1px 描边，占满高度 */
.sessions {
  flex: none;
  width: 264px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--kb-bg-card);
  border-right: 1px solid var(--kb-border);
}

/* SessionHead：高 52，横排 gap 8，padding [0,14]（原型 v8BRh） */
.sessions__head {
  flex: none;
  height: 52px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 14px;
}

/* SessionTitle：13/600 $text-primary */
.sessions__title {
  flex: 1 1 auto;
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

/* NewChatIcon：原型是一枚裸 16px 图标（无描边无底），按钮只是给它一个点击热区 */
.sessions__new {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--kb-brand);
  cursor: pointer;
}

.sessions__new:hover {
  color: var(--kb-brand-hover);
}

/* SessionItems：纵向 gap 3，padding [0,10] */
.sessions__list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 0 10px;
}

.sessions__placeholder {
  margin: 8px 2px;
  font-size: 12px;
  color: var(--kb-text-muted);
}

/* SessionItem：纵向 gap 4，padding [10,12]，圆角 8 */
.sessions__item {
  flex: none;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
}

.sessions__item:hover {
  background: var(--kb-bg-subtle);
}

.sessions__item--active {
  background: var(--kb-brand-soft);
}

.sessions__item-body {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

/* SessionName：13 */
.sessions__item-title {
  font-size: 13px;
  color: var(--kb-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sessions__item--active .sessions__item-title {
  font-weight: 600;
  color: var(--kb-brand);
}

.sessions__item-time {
  font-size: 11px;
  color: var(--kb-text-muted);
}

/* 删除按钮平时不可见，hover / 键盘聚焦时才出（避免列表全是垃圾桶图标） */
.sessions__del {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--kb-text-muted);
  opacity: 0;
  cursor: pointer;
}

.sessions__item:hover .sessions__del,
.sessions__del:focus-visible {
  opacity: 1;
}

.sessions__del:hover {
  background: var(--kb-danger-soft);
  color: var(--kb-danger);
}
</style>
