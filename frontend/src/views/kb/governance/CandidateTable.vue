<script setup lang="ts">
import { ref, watch } from 'vue'

import StatusPill from './StatusPill.vue'
import { formatScore } from './format'

import { CANDIDATE_CARD_HINT, CANDIDATE_STATUS_META } from '@/constants/governance'
import type { CandidateQuery, CandidateStatus, FaqCandidateItem } from '@/types/governance'

/**
 * Tab1 左栏：候选 FAQ 队列（原型 `VOwfE/uw8Zt`）。
 *
 * 纯展示 + emit，**不发请求、不碰 store**（fe-06 §2 的分层：跨 Tab 编排归页面）。
 * 行内「发布 / 忽略」是原型画的快捷入口：直接按该行的原值提交，
 * 不走右侧草稿——后者是「先润色再发布」的路径，两条并存不冲突。
 */
const props = defineProps<{
  items: FaqCandidateItem[]
  total: number
  loading: boolean
  query: CandidateQuery
  selectedId: string | null
}>()

const emit = defineEmits<{
  select: [id: string]
  search: [query: Pick<CandidateQuery, 'status' | 'min_confidence'>]
  'page-change': [page: number]
  publish: [item: FaqCandidateItem]
  reject: [item: FaqCandidateItem]
}>()

/**
 * 筛选值本地持有，再 emit 出去；父级写回 query 后回灌（单一数据源仍在 store）。
 *
 * ⚠️ 必须**逐字段 watch**，不能 `watch(() => props.query, ...)`：`query` 是 store 里
 * 那个对象的**同一个引用**，父级改的是它的属性而非换引用，浅 watch 永远不触发。
 */
const status = ref<CandidateStatus>(props.query.status)
const minConfidence = ref<number>(props.query.min_confidence)

watch(
  () => props.query.status,
  (next) => {
    status.value = next
  },
)

watch(
  () => props.query.min_confidence,
  (next) => {
    minConfidence.value = next
  },
)

const STATUS_OPTIONS: readonly { value: CandidateStatus; label: string }[] = [
  { value: 'pending', label: '待审核' },
  { value: 'published', label: '已发布' },
  { value: 'rejected', label: '已驳回' },
]

/** 置信度是「辅助人工判断该不该发布」的筛子（PRD R8），给几个常用档位而不是自由输入 */
const CONFIDENCE_OPTIONS: readonly { value: number; label: string }[] = [
  { value: 0, label: '全部置信度' },
  { value: 0.6, label: '≥ 0.6' },
  { value: 0.8, label: '≥ 0.8' },
  { value: 0.9, label: '≥ 0.9' },
]

function applyFilter(): void {
  emit('search', { status: status.value, min_confidence: minConfidence.value })
}

/** 选中行加底色。用 row-class-name 而不是 setCurrentRow，省一个 table ref */
function rowClass({ row }: { row: FaqCandidateItem }): string {
  return row.id === props.selectedId ? 'is-selected' : ''
}

function onRowClick(row: FaqCandidateItem): void {
  emit('select', row.id)
}

function onPageChange(page: number): void {
  emit('page-change', page)
}

/** 表格插槽里的 row 是 `any`（EP 未泛型化），用这个函数把状态收敛回字面量联合 */
function statusMeta(status: CandidateStatus): (typeof CANDIDATE_STATUS_META)[CandidateStatus] {
  return CANDIDATE_STATUS_META[status]
}

/**
 * `el-table` 插槽给的 `row` 类型是 `Record<PropertyKey, any>`：读 `row.question` 没问题，
 * 但**不能**直接传给形参是 `FaqCandidateItem` 的函数（TS 认为它缺 id / question 等必填字段，
 * 报 `DefaultRow is not assignable to FaqCandidateItem`）。这里统一收窄一次，
 * 免得模板里到处写 `as`。
 */
function asCandidate(row: unknown): FaqCandidateItem {
  return row as FaqCandidateItem
}
</script>

<template>
  <section class="card">
    <header class="card__head">
      <h2 class="card__title">候选 FAQ（按频次降序）</h2>
      <span class="card__hint">{{ CANDIDATE_CARD_HINT }}</span>
      <div class="card__spacer" />

      <el-select
        v-model="status"
        class="card__filter card__filter--status"
        size="small"
        @change="applyFilter"
      >
        <el-option
          v-for="option in STATUS_OPTIONS"
          :key="option.value"
          :label="option.label"
          :value="option.value"
        />
      </el-select>

      <el-select
        v-model="minConfidence"
        class="card__filter card__filter--conf"
        size="small"
        @change="applyFilter"
      >
        <el-option
          v-for="option in CONFIDENCE_OPTIONS"
          :key="option.value"
          :label="option.label"
          :value="option.value"
        />
      </el-select>
    </header>

    <el-table
      class="table"
      :data="items"
      :row-class-name="rowClass"
      :empty-text="loading ? '加载中…' : '暂无候选'"
      @row-click="onRowClick"
    >
      <el-table-column prop="question" label="代表性问题" :min-width="200">
        <template #default="{ row }">
          <span class="cell-question">{{ row.question }}</span>
        </template>
      </el-table-column>

      <el-table-column label="频次" width="76">
        <template #default="{ row }">
          <span class="cell-freq">{{ row.frequency }}</span>
        </template>
      </el-table-column>

      <el-table-column label="置信度" width="86">
        <template #default="{ row }">
          <span class="cell-conf">{{ formatScore(row.confidence) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="状态" width="92">
        <template #default="{ row }">
          <StatusPill :tone="statusMeta(row.status).tone" :label="statusMeta(row.status).label" />
        </template>
      </el-table-column>

      <el-table-column label="操作" width="110">
        <template #default="{ row }">
          <div class="cell-actions">
            <button
              class="link link--brand"
              type="button"
              @click.stop="emit('publish', asCandidate(row))"
            >
              发布
            </button>
            <button class="link" type="button" @click.stop="emit('reject', asCandidate(row))">
              忽略
            </button>
          </div>
        </template>
      </el-table-column>
    </el-table>

    <!-- 原型没画分页；`GET /faq/candidates` 也还没声明分页参数（spec §9-5）。
         total 为 0 时不显示，免得空表下面挂一条无意义的分页条。 -->
    <footer v-if="total > 0" class="card__foot">
      <el-pagination
        layout="prev, pager, next"
        size="small"
        :current-page="query.page"
        :page-size="query.page_size"
        :total="total"
        @current-change="onPageChange"
      />
    </footer>
  </section>
</template>

<style scoped>
/* `flex: 1 0 auto`（grow 但不 shrink）：内容少时撑满剩余高度，内容多时保持自身高度、
   由 `.app-page__body` 的 overflow 去滚。写成 `1 1 auto` 会在长列表时被压缩导致行被裁掉 */
.card {
  flex: 1 0 auto;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--kb-border);
  border-radius: 10px;
  background: var(--kb-bg-card);
  overflow: hidden;
}

/* 原型 uw8Zt/CardHead：高 46 / 左右 16 / 底边 1px */
.card__head {
  flex: none;
  height: 46px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 16px;
  border-bottom: 1px solid var(--kb-border);
}

.card__title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.card__hint {
  font-size: 11.5px;
  color: var(--kb-text-muted);
}

.card__spacer {
  flex: 1 1 auto;
}

.card__filter--status {
  width: 106px;
}

.card__filter--conf {
  width: 118px;
}

.card__foot {
  flex: none;
  display: flex;
  justify-content: flex-end;
  padding: 8px 16px;
  border-top: 1px solid var(--kb-border-soft);
}

.table {
  flex: 1 0 auto;
  min-height: 0;
}

/* el-table 的默认观感与原型差得远，这里逐项对齐原型（表头 40 / 行 50 / 无外框） */
.table :deep(.el-table__inner-wrapper::before),
.table :deep(.el-table__border-left-patch) {
  display: none;
}

.table :deep(th.el-table__cell) {
  height: 40px;
  padding: 0 16px;
  background: var(--kb-bg-subtle);
  border-bottom: 1px solid var(--kb-border);
}

.table :deep(th.el-table__cell .cell) {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--kb-text-secondary);
}

.table :deep(td.el-table__cell) {
  height: 50px;
  padding: 0 16px;
  border-bottom: 1px solid var(--kb-border-soft);
}

/* 选中行 / 悬停行都用品牌浅底，与原型 CRow 的 fill:$brand-softer 一致 */
.table :deep(.el-table__row.is-selected > td.el-table__cell),
.table :deep(.el-table__body tr:hover > td.el-table__cell) {
  background: var(--kb-brand-softer);
}

.cell-question {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.cell-freq {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--kb-brand);
}

.cell-conf {
  font-size: 12.5px;
  color: var(--kb-success);
}

.cell-actions {
  display: flex;
  align-items: center;
  gap: 11px;
}

/* 行内操作用文字链而不是 el-button：原型画的是纯文字，且 el-button 会撑高行 */
.link {
  padding: 0;
  border: 0;
  background: transparent;
  font-family: inherit;
  font-size: 12px;
  color: var(--kb-text-muted);
  cursor: pointer;
}

.link:hover {
  color: var(--kb-text-secondary);
}

.link--brand {
  color: var(--kb-brand);
}

.link--brand:hover {
  color: var(--kb-brand-hover);
}
</style>
