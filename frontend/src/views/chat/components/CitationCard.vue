<script setup lang="ts">
import { FileText } from 'lucide-vue-next'
import { computed } from 'vue'

import { CITE_JUMP_PERMISSION } from '@/constants/chat'
import { useAuthStore } from '@/stores/auth'
import type { CitationItem } from '@/types/chat'

/**
 * 单条引用溯源卡片。原型 `LoIHB/AiBody/Citations/Citation/*`（`H3n5xb` / `JydIW`）。
 *
 * 原型里还有一行 `CiteScope`「权限来源：全局可见 / 财务部」——**不实现**：
 * SSE `citations.items[]` 没有该字段，且它会把「这份文档靠哪个维度授权才让我看到」
 * 暴露给用户，与红线 4 的克制精神相悖（spec §9-8）。
 *
 * **权限判断的边界**：卡片展示什么完全由后端决定（红线 3/4），前端不做权限判断。
 * 这里唯一的 `hasPermission` 是「能不能跳到台账」的**功能权限**（与检索的数据权限正交，PRD §3.3），
 * 且它只是个显隐体验，**不是安全边界**——目标页后端仍会独立校验。
 */
const props = defineProps<{
  item: CitationItem
}>()

const emit = defineEmits<{
  'cite-click': [item: CitationItem]
}>()

const auth = useAuthStore()

/**
 * 无台账查看权限时**只读不跳**——点进去看到一片空白比不能点体验更差（fe-03 §4.7③）。
 */
const canJump = computed(() => auth.hasPermission(CITE_JUMP_PERMISSION))

/** `heading_path` 是「一份数据两处收益」的第二处（第一处是 BM25 召回信号），必须真正用起来 */
const pathSegments = computed(() =>
  props.item.heading_path
    .split(/[>›]/)
    .map((seg) => seg.trim())
    .filter((seg) => seg !== ''),
)

const score = computed(() => {
  const value = props.item.score
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '—'
})

function onClick(): void {
  if (!canJump.value) return
  emit('cite-click', props.item)
}
</script>

<template>
  <component
    :is="canJump ? 'button' : 'div'"
    class="cite"
    :class="{ 'cite--jumpable': canJump }"
    :type="canJump ? 'button' : undefined"
    @click="onClick"
  >
    <div class="cite__top">
      <FileText :size="14" class="cite__icon" />
      <span class="cite__name">{{ item.title }}</span>
      <span class="cite__score">{{ score }}</span>
    </div>

    <!-- 面包屑用 › 连接（原型用 ›，字典 §2.3 的 heading_path 用 >） -->
    <p v-if="pathSegments.length" class="cite__path">
      {{ pathSegments.join(' › ') }}
    </p>
  </component>
</template>

<style scoped>
/* Citation：bg $bg-card，圆角 9，描边 1 border，纵向 gap 7，padding 12 */
.cite {
  /* 每张卡最小 240px，超过容器宽度自动换行到下一行 */
  flex: 1 1 240px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 12px;
  border: 1px solid var(--kb-border);
  border-radius: 9px;
  background: var(--kb-bg-card);
  text-align: left;
  font-family: inherit;
}

.cite--jumpable {
  cursor: pointer;
}

.cite--jumpable:hover {
  border-color: var(--kb-brand);
}

.cite__top {
  display: flex;
  align-items: center;
  gap: 7px;
}

.cite__icon {
  flex: none;
  color: var(--kb-brand);
}

.cite__name {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--kb-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* CiteScore：11/600 $success */
.cite__score {
  flex: none;
  font-size: 11px;
  font-weight: 600;
  color: var(--kb-success);
}

/* CitePath：11 $text-secondary */
.cite__path {
  margin: 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--kb-text-secondary);
  word-break: break-word;
}
</style>
