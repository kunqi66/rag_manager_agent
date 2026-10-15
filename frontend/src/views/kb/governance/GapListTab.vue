<script setup lang="ts">
import { Send } from 'lucide-vue-next'
import { ref, watch } from 'vue'

import StatusPill from './StatusPill.vue'
import { formatDateTime, formatScore } from './format'

import { GAP_EMPTY_TEXT, GAP_MIN_FREQUENCY, GAP_STATUS_META, PERM } from '@/constants/governance'
import type { GapQuery, GapStatus, KnowledgeGapItem } from '@/types/governance'

/**
 * Tab3：知识缺口清单（fe-06 §4.3③，A8 的核心）。纯展示 + emit。
 *
 * 🔒 **隐私边界**：缺口的 `question` 来自 `qa_log` 里用户的**真实提问原文**。
 * 本页只展示问题文本与**部门**，容器里**没有 `user_id` 字段**（字典 §2.4），
 * 因此不可能、也不允许在前端拼出提问人身份（红线 4）。
 */
const props = defineProps<{
  items: KnowledgeGapItem[]
  total: number
  loading: boolean
  query: GapQuery
}>()

const emit = defineEmits<{
  search: [minFrequency: number]
  'page-change': [page: number]
  'create-task': [item: KnowledgeGapItem]
}>()

// 逐字段 watch：`props.query` 是 store 里那个对象的同一引用，浅 watch 整个对象不会触发
const minFrequency = ref<number>(props.query.min_frequency)

watch(
  () => props.query.min_frequency,
  (next) => {
    minFrequency.value = next
  },
)

/** 频次下限的常用档位。给档位而不是自由输入，避免管理员把清单拉成空表还以为是没数据 */
const FREQUENCY_OPTIONS: readonly { value: number; label: string }[] = [
  { value: GAP_MIN_FREQUENCY, label: '全部频次' },
  { value: 5, label: '频次 ≥ 5' },
  { value: 10, label: '频次 ≥ 10' },
  { value: 20, label: '频次 ≥ 20' },
]

/** 状态胶囊的 hover 说明。「已建补充任务」必须讲清不等于已解决（fe-06 §10.5） */
const STATUS_TIP: Record<GapStatus, string> = {
  open: '窗口内多次未命中，且知识库里没有对应内容——需要补料',
  tasked: '已创建知识补充任务，但补料文档还没导入。补料后再次提问命中，才会自动关闭',
  closed: '补料后已再次命中，闭环完成',
}

/** 表格插槽里的 row 是 `any`，用这两个函数把状态收敛回字面量联合（免得在模板里写断言） */
function statusTip(status: GapStatus): string {
  return STATUS_TIP[status]
}

function statusMeta(status: GapStatus): (typeof GAP_STATUS_META)[GapStatus] {
  return GAP_STATUS_META[status]
}

function applyFilter(): void {
  emit('search', minFrequency.value)
}

function onPageChange(page: number): void {
  emit('page-change', page)
}

/** 只有 `open` 能转建：`tasked`/`closed` 再点会被后端以 409 挡回（fe-06 §6），前端先禁用 */
function canCreateTask(item: KnowledgeGapItem): boolean {
  return item.status === 'open'
}

function departmentOf(item: KnowledgeGapItem): string {
  // 🔴 **只显示 `department_id`，不要再读 `department_name`**（be-09 实现期定稿）。
  // 该字段已在字典 §9 #146 **撤回**：缺口可能来自**别的**部门，而前端只有自己那条
  // 部门链，拿不到它的名字 —— 后端不内联返回，这个字段就**永远**是 `undefined`。
  //
  // ⚠️ 所以原来的 `item.department_name || ...` 会一直走右边那支，**而它不会报错**：
  //    单元测试、类型收窄、人工点检都看不出问题（显示的就是 ID，看着像"降级成功"）。
  //    现在 TS 会在编译期直接拦下来 —— 这正是「字段撤回要连消费方一起改」的意义。
  //
  // 要显示「财务部」而不是 `dept_02`，需要一个**部门 id→名 的批量取名接口**，
  // 那是 be-02 的活（§9 #146 已登记）。在那之前显示 ID 也好过显示「—」，
  // 管理员能拿它去 fe-04 的组织管理里对照。
  return item.department_id || '—'
}

/**
 * `el-table` 插槽给的 `row` 是 `Record<PropertyKey, any>`：读属性没问题，但**不能**直接
 * 传给形参是 `KnowledgeGapItem` 的函数（TS 报 `DefaultRow is not assignable to ...`）。
 * 统一在这里收窄一次，免得模板里到处写 `as`。
 */
function asGap(row: unknown): KnowledgeGapItem {
  return row as KnowledgeGapItem
}
</script>

<template>
  <section class="card">
    <header class="card__head">
      <h2 class="card__title">知识缺口清单（按频次降序）</h2>
      <div class="card__spacer" />

      <el-select
        v-model="minFrequency"
        class="card__filter"
        size="small"
        @change="applyFilter"
      >
        <el-option
          v-for="option in FREQUENCY_OPTIONS"
          :key="option.value"
          :label="option.label"
          :value="option.value"
        />
      </el-select>
    </header>

    <el-table class="table" :data="items" :empty-text="loading ? '加载中…' : GAP_EMPTY_TEXT">
      <el-table-column label="问题" :min-width="220">
        <template #default="{ row }">
          <span class="cell-strong">{{ row.question }}</span>
        </template>
      </el-table-column>

      <el-table-column label="提问部门" width="120">
        <template #default="{ row }">
          <span class="cell-dim">{{ departmentOf(asGap(row)) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="频次" width="72">
        <template #default="{ row }">
          <span class="cell-brand">{{ row.frequency }}</span>
        </template>
      </el-table-column>

      <el-table-column label="最高相似度" width="100">
        <template #default="{ row }">
          <span class="cell-dim">{{ formatScore(row.max_score) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="建议分类" width="120">
        <template #default="{ row }">
          <span class="cell-dim">{{ row.suggested_category || '—' }}</span>
        </template>
      </el-table-column>

      <el-table-column label="状态" width="130">
        <template #default="{ row }">
          <el-tooltip :content="statusTip(row.status)" placement="top" :show-after="120">
            <StatusPill :tone="statusMeta(row.status).tone" :label="statusMeta(row.status).label" />
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column label="最近出现" width="150">
        <template #default="{ row }">
          <span class="cell-dim">{{ formatDateTime(row.last_seen_at) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="操作" width="98">
        <template #default="{ row }">
          <!-- ⚠️ v-permission 只控制显隐；后端 POST .../task 必须独立校验
               gap:task:create（红线 7） -->
          <el-button
            v-permission="PERM.GAP_TASK_CREATE"
            class="create-btn"
            size="small"
            :disabled="!canCreateTask(asGap(row))"
            @click="emit('create-task', asGap(row))"
          >
            <Send :size="13" />
            <span class="create-btn__text">一键转建</span>
          </el-button>
        </template>
      </el-table-column>
    </el-table>

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
/* `flex: 1 0 auto`：长列表不被压缩（见 CandidateTable 同名注释） */
.card {
  flex: 1 0 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--kb-border);
  border-radius: 10px;
  background: var(--kb-bg-card);
  overflow: hidden;
}

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

.card__spacer {
  flex: 1 1 auto;
}

.card__filter {
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

.table :deep(.el-table__body tr:hover > td.el-table__cell) {
  background: var(--kb-brand-softer);
}

.cell-strong {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.cell-dim {
  font-size: 12.5px;
  color: var(--kb-text-secondary);
}

.cell-brand {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--kb-brand);
}

/* 行只有 50 高，按钮压到 26 高才不会把行撑开；el-button 的 size=small 是 32，再收一点 */
.create-btn {
  height: 26px;
  padding: 0 9px;
  font-size: 12px;
}

.create-btn__text {
  margin-left: 4px;
}
</style>
