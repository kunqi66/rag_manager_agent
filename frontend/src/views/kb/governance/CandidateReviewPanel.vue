<script setup lang="ts">
import { CircleCheckBig, FileText, Info, X } from 'lucide-vue-next'
import { computed } from 'vue'

import { formatScore, formatShortDate } from './format'
import type { ReviewDraft } from './types'

import { PERM } from '@/constants/governance'
import type { FaqCandidateItem } from '@/types/governance'

/**
 * Tab1 右栏：候选详情与审核（原型 `VOwfE/JTlT6`，宽 360 固定）。
 *
 * 🔒 **推荐答案 `candidate.answer` 是 DeepSeek 生成的文本（LLM 输出）**——
 * 红线 2 要求它渲染前过 DOMPurify。本组件**有意采用纯文本**：展示用 `<textarea>`
 * 的原生文本值、关联项用插值，**全页无 `v-html`**，因而天然安全。
 * **若将来改为 Markdown 富文本渲染，必须走 `@/utils/markdown` 的 `renderSafe()`，
 * 禁止裸 `v-html`**（那是全项目唯一的富文本出口）。
 */

const props = defineProps<{
  candidate: FaqCandidateItem | null
  draft: ReviewDraft
  submitting: boolean
}>()

const emit = defineEmits<{
  'update:draft': [draft: ReviewDraft]
  publish: []
  reject: []
  close: []
}>()

// 用可写 computed 而不是直接在模板里 emit：el-input 的 v-model 需要一个可读写值，
// 而草稿的真身在父级的 store 里（单一数据源）。
const question = computed({
  get: () => props.draft.question,
  set: (value: string) => emit('update:draft', { ...props.draft, question: value }),
})

const answer = computed({
  get: () => props.draft.answer,
  set: (value: string) => emit('update:draft', { ...props.draft, answer: value }),
})

/** 关联知识单元：后端内联了标题就逐条列，否则只报数量（spec §9-10）。**都不展示正文** */
const relatedDocs = computed(() => props.candidate?.related_docs ?? [])

const relatedCount = computed(() => props.candidate?.related_doc_ids.length ?? 0)
</script>

<template>
  <aside class="panel">
    <header class="panel__head">
      <h2 class="panel__title">候选详情</h2>
      <button class="panel__close" type="button" aria-label="关闭详情" @click="emit('close')">
        <X :size="16" />
      </button>
    </header>

    <!-- 未选中时的空态。原型只画了「已选中」的样子，这里补一个不骗人的占位 -->
    <div v-if="!candidate" class="panel__empty">从左侧选择一条候选查看详情</div>

    <template v-else>
      <div class="panel__body">
        <section class="block">
          <span class="block__label">代表性问题</span>
          <el-input
            v-model="question"
            class="block__question"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 4 }"
            resize="none"
          />
          <div class="meta">
            <span class="meta__pill meta__pill--brand">频次 {{ candidate.frequency }}</span>
            <span class="meta__pill meta__pill--success">
              置信度 {{ formatScore(candidate.confidence) }}
            </span>
            <span class="meta__pill meta__pill--muted">
              首次 {{ formatShortDate(candidate.created_at) }}
            </span>
          </div>
        </section>

        <section class="block">
          <span class="block__label">推荐答案（可编辑）</span>
          <el-input
            v-model="answer"
            class="block__answer"
            type="textarea"
            :autosize="{ minRows: 4, maxRows: 10 }"
            resize="none"
          />
        </section>

        <section class="block">
          <span class="block__label">关联知识单元</span>

          <ul v-if="relatedDocs.length" class="related">
            <li v-for="doc in relatedDocs" :key="doc.id" class="related__item">
              <FileText :size="15" class="related__icon" />
              <span class="related__name">{{ doc.title }}</span>
            </li>
          </ul>

          <!-- 只有 ID 时（后端未内联标题，spec §9-10）只报数量：
               正文与标题的展示属 fe-02 台账的职责，且台账有独立的功能权限 -->
          <p v-else class="related__count">
            关联 {{ relatedCount }} 篇知识单元（标题需后端内联返回，见 spec §9-10）
          </p>
        </section>

        <div class="note">
          <Info :size="14" class="note__icon" />
          <span class="note__text">
            由 {{ candidate.frequency }} 条变体提问聚类而成；发布后写入缓存，再次提问将直接命中、不调用生成模型。
          </span>
        </div>
      </div>

      <footer class="panel__foot">
        <!-- ⚠️ v-permission 只控制显隐，不是安全边界：后端 POST .../review 必须独立校验
             faq:candidate:publish，绕过前端隐藏直接调接口应得 403（红线 7） -->
        <el-button
          v-permission="PERM.CANDIDATE_PUBLISH"
          class="panel__btn"
          :disabled="submitting"
          @click="emit('reject')"
        >
          忽略
        </el-button>
        <el-button
          v-permission="PERM.CANDIDATE_PUBLISH"
          class="panel__btn"
          type="primary"
          :loading="submitting"
          @click="emit('publish')"
        >
          <CircleCheckBig v-if="!submitting" :size="15" />
          <span class="panel__btn-text">保存并发布</span>
        </el-button>
      </footer>
    </template>
  </aside>
</template>

<style scoped>
.panel {
  flex: none;
  width: 360px;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--kb-border);
  border-radius: 10px;
  background: var(--kb-bg-card);
  overflow: hidden;
}

.panel__head {
  flex: none;
  height: 46px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 16px;
  border-bottom: 1px solid var(--kb-border);
}

.panel__title {
  flex: 1 1 auto;
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.panel__close {
  flex: none;
  display: flex;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--kb-text-muted);
  cursor: pointer;
}

.panel__close:hover {
  color: var(--kb-text-secondary);
}

.panel__empty {
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  font-size: 12.5px;
  color: var(--kb-text-muted);
}

/* 原型 Ly87X：padding 16 / 纵向 gap 15 / 可滚动 */
.panel__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 15px;
  padding: 16px;
}

.block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.block__label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.4px;
  color: var(--kb-text-muted);
}

/* 问题与答案的编辑框：原型是 --kb-bg-subtle 底 + 圆角 8 的纯文本块。
   padding 取 [11,12]、行高 1.7，均照原型；el-textarea 默认的 5px 8px 会让文字贴着边框。 */
.block__question :deep(.el-textarea__inner),
.block__answer :deep(.el-textarea__inner) {
  padding: 11px 12px;
  border-radius: 8px;
  background: var(--kb-bg-subtle);
  box-shadow: 0 0 0 1px var(--kb-border) inset;
  font-family: inherit;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--kb-text-primary);
}

.block__question :deep(.el-textarea__inner) {
  font-weight: 600;
}

.block__question :deep(.el-textarea__inner:focus),
.block__answer :deep(.el-textarea__inner:focus) {
  background: var(--kb-bg-card);
  box-shadow: 0 0 0 1px var(--kb-brand) inset;
}

.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding-bottom: 2px;
}

.meta__pill {
  padding: 3px 8px;
  border-radius: 5px;
  font-size: 11px;
  font-weight: 500;
}

.meta__pill--brand {
  background: var(--kb-brand-softer);
  color: var(--kb-brand);
}

.meta__pill--success {
  background: var(--kb-success-soft);
  color: var(--kb-success);
}

.meta__pill--muted {
  background: var(--kb-border-soft);
  color: var(--kb-text-secondary);
}

.related {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.related__item {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 9px 11px;
  border: 1px solid var(--kb-border);
  border-radius: 8px;
}

.related__icon {
  flex: none;
  color: var(--kb-brand);
}

.related__name {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--kb-text-primary);
}

.related__count {
  margin: 0;
  font-size: 12px;
  color: var(--kb-text-muted);
}

.note {
  display: flex;
  gap: 8px;
  padding: 9px 11px;
  border-radius: 8px;
  background: var(--kb-brand-softer);
}

.note__icon {
  flex: none;
  margin-top: 1px;
  color: var(--kb-brand);
}

.note__text {
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--kb-brand);
}

.panel__foot {
  flex: none;
  display: flex;
  gap: 10px;
  padding: 12px 16px;
  border-top: 1px solid var(--kb-border);
}

.panel__btn {
  flex: 1 1 0;
  height: 34px;
}

.panel__btn-text {
  margin-left: 6px;
}
</style>
