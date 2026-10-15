<script setup lang="ts">
import CandidateReviewPanel from './CandidateReviewPanel.vue'
import CandidateTable from './CandidateTable.vue'
import type { ReviewDraft, ReviewSubmit } from './types'

import type { CandidateQuery, FaqCandidateItem } from '@/types/governance'

/**
 * Tab1 容器（原型 `VOwfE/ZDnRi`）：左候选队列 + 右审核详情的两栏布局。
 *
 * 本组件**只做布局与转发**，是个纯粹的「哑容器」——真实动作（确认框、Toast、
 * store 调用）全在 `GovernanceView` 编排。这样三个 Tab 的编排点只有一个，
 * 也符合 dev-frontend「表单/展示组件只 emit，由页面组件编排」的分层纪律。
 */
defineProps<{
  items: FaqCandidateItem[]
  total: number
  loading: boolean
  query: CandidateQuery
  selectedId: string | null
  candidate: FaqCandidateItem | null
  draft: ReviewDraft
  submitting: boolean
}>()

const emit = defineEmits<{
  select: [id: string]
  search: [query: Pick<CandidateQuery, 'status' | 'min_confidence'>]
  'page-change': [page: number]
  review: [payload: ReviewSubmit]
  'update:draft': [draft: ReviewDraft]
  close: []
}>()

/** 面板上的「保存并发布 / 忽略」：提交的是**编辑框里的当前值**（可能已被润色） */
function onPanelReview(action: ReviewSubmit['action'], candidate: FaqCandidateItem | null, draft: ReviewDraft): void {
  if (!candidate) return
  emit('review', {
    id: candidate.id,
    action,
    question: draft.question,
    answer: draft.answer,
  })
}

/** 表格行内的快捷发布/忽略：提交该行自身的原值，与右侧草稿互不干扰 */
function onRowReview(action: ReviewSubmit['action'], item: FaqCandidateItem): void {
  emit('review', {
    id: item.id,
    action,
    question: item.question,
    answer: item.answer,
  })
}
</script>

<template>
  <div class="candidate">
    <CandidateTable
      :items="items"
      :total="total"
      :loading="loading"
      :query="query"
      :selected-id="selectedId"
      @select="(id: string) => emit('select', id)"
      @search="(query) => emit('search', query)"
      @page-change="(page: number) => emit('page-change', page)"
      @publish="(item: FaqCandidateItem) => onRowReview('publish', item)"
      @reject="(item: FaqCandidateItem) => onRowReview('reject', item)"
    />

    <CandidateReviewPanel
      :candidate="candidate"
      :draft="draft"
      :submitting="submitting"
      @update:draft="(next: ReviewDraft) => emit('update:draft', next)"
      @publish="onPanelReview('publish', candidate, draft)"
      @reject="onPanelReview('reject', candidate, draft)"
      @close="emit('close')"
    />
  </div>
</template>

<style scoped>
/* 原型 ZDnRi：左右两栏、gap 16、撑满剩余高度。
   `1 0 auto`：长列表时不被压缩（压缩会裁掉行），短列表时撑满 */
.candidate {
  flex: 1 0 auto;
  min-height: 0;
  display: flex;
  align-items: stretch;
  gap: 16px;
}
</style>
