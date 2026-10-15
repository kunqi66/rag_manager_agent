<script setup lang="ts">
import { Search } from 'lucide-vue-next'
import { computed, ref, watch } from 'vue'

import { formatCount, formatDateTime, truncate } from './format'

import { CACHE_SWITCH_TOOLTIP, FAQ_EMPTY_TEXT, PERM } from '@/constants/governance'
import type { FaqItem, FaqQuery } from '@/types/governance'

/**
 * Tab2：已发布 FAQ（fe-06 §4.3②）。纯展示 + emit，编排在 `GovernanceView`。
 *
 * ⚠️ 原型只画了 Tab1，本屏的图元（卡片 / 表头 40 / 行 50 / 字号）全部沿用 Tab1 的那套，
 * 不另立样式——spec §3.4 已说明这是预期内的差异。
 */
const props = defineProps<{
  items: FaqItem[]
  total: number
  loading: boolean
  query: FaqQuery
}>()

const emit = defineEmits<{
  search: [keyword: string]
  'page-change': [page: number]
  'toggle-cache': [id: string, enabled: boolean]
}>()

// 逐字段 watch：`props.query` 是 store 里那个对象的同一引用，浅 watch 整个对象不会触发
const keyword = ref<string>(props.query.keyword)

watch(
  () => props.query.keyword,
  (next) => {
    keyword.value = next
  },
)

/**
 * 卡片副标题的「累计命中 N 次」。
 *
 * ⚠️ **口径是「当前页命中数之和」，不是全库累计**：`GET /api/mining/faq` 只回
 * `{list,total,page,page_size}`，没有任何聚合字段（spec §9-22）。文案刻意写「本页」
 * 而不是设计稿建议的「累计」，避免管理员把 20 条的和当成全库效果。
 */
const pageHits = computed(() => props.items.reduce((sum, item) => sum + (item.hit_count ?? 0), 0))

function applySearch(): void {
  emit('search', keyword.value.trim())
}

function onPageChange(page: number): void {
  emit('page-change', page)
}

/**
 * 缓存开关。**不做乐观切换**（fe-06 §6）：模板绑的是 `row.cache_enabled`，
 * 失败时 store 不改值 → 开关自然回弹，界面与后端始终一致。
 */
function onCacheChange(row: FaqItem): void {
  emit('toggle-cache', row.id, !row.cache_enabled)
}

function publisherOf(row: FaqItem): string {
  return row.published_by || '—'
}

/**
 * `el-table` 插槽给的 `row` 是 `Record<PropertyKey, any>`：读属性没问题，但**不能**直接
 * 传给形参是 `FaqItem` 的函数（TS 报 `DefaultRow is not assignable to FaqItem`）。
 * 统一在这里收窄一次，免得模板里到处写 `as`。
 */
function asFaq(row: unknown): FaqItem {
  return row as FaqItem
}
</script>

<template>
  <section class="card">
    <header class="card__head">
      <h2 class="card__title">已发布 FAQ</h2>
      <span class="card__hint">本页命中 {{ formatCount(pageHits) }} 次</span>
      <div class="card__spacer" />

      <el-input
        v-model="keyword"
        class="card__search"
        size="small"
        placeholder="搜索标准问题"
        clearable
        @keyup.enter="applySearch"
        @clear="applySearch"
      >
        <template #prefix>
          <Search :size="14" />
        </template>
      </el-input>
    </header>

    <el-table
      class="table"
      :data="items"
      :empty-text="loading ? '加载中…' : FAQ_EMPTY_TEXT"
    >
      <el-table-column label="标准问题" :min-width="200">
        <template #default="{ row }">
          <span class="cell-strong">{{ row.question }}</span>
        </template>
      </el-table-column>

      <el-table-column label="答案摘要" :min-width="240">
        <template #default="{ row }">
          <!-- 纯文本插值：`answer` 是 LLM 生成的文本，这里**不放富文本渲染**，
               所以不需要 renderSafe（红线 2 的适用条件是 v-html，本页全页无 v-html） -->
          <span class="cell-dim">{{ truncate(row.answer, 42) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="缓存" width="88">
        <template #default="{ row }">
          <!-- ⚠️ v-permission 只控制显隐；后端 PUT .../cache 必须独立校验
               faq:cache:update（红线 7）。注释放在 el-tooltip 外层：它只接受单个子节点 -->
          <el-tooltip :content="CACHE_SWITCH_TOOLTIP" placement="top" :show-after="120">
            <el-switch
              v-permission="PERM.CACHE_UPDATE"
              :model-value="row.cache_enabled"
              size="small"
              @change="onCacheChange(asFaq(row))"
            />
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column label="命中次数" width="96">
        <template #default="{ row }">
          <span class="cell-brand">{{ formatCount(row.hit_count) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="发布时间" width="150">
        <template #default="{ row }">
          <span class="cell-dim">{{ formatDateTime(row.published_at) }}</span>
        </template>
      </el-table-column>

      <!-- 发布人来自 `faq.published_by`，是**审核动作的留痕**（治理职责），
           不是提问者身份——与缺口清单的隐私边界是两回事（spec §7 红线 4） -->
      <el-table-column label="发布人" width="120">
        <template #default="{ row }">
          <span class="cell-dim">{{ publisherOf(asFaq(row)) }}</span>
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

/* 与 Tab1 的 CardHead 同高同距，保证切换 Tab 时头部不跳动 */
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

.card__search {
  width: 220px;
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
</style>
