<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Search } from 'lucide-vue-next'

import {
  FILTER_DEBOUNCE_MS,
  PERMISSION_FILTER_OPTIONS,
  STATUS_FILTER_OPTIONS,
} from '@/constants/kb'

import type { DocumentQuery, KbDocumentStatus, PermissionScope } from '@/types/kb'

/**
 * 筛选栏。尺寸逐项对齐原型 `P2kxKA`：
 * 搜索框 272、分类 116、状态 116、权限 120，均为 h34 / r8 / 白底 / 1px 描边 / gap 12，
 * 右侧合计文案 12.5 muted 由 spacer 推到最右。
 *
 * 本组件**不发请求**：只 `emit('update:modelValue', patch)`，由页面/store 决定何时拉数据。
 * 关键字额外 `debounce 300ms` 再 emit，避免每个字符打一次接口。
 */
const props = defineProps<{
  modelValue: DocumentQuery
  /** 分类可选值。后端未回聚合时由 store 从已见数据累积（spec §9-3） */
  categoryOptions: string[]
  total: number
  /** 全量切片数。为 null 时合计文案只显示条数（spec §9-14） */
  totalChunks: number | null
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', patch: Partial<DocumentQuery>): void
  (e: 'search'): void
}>()

/**
 * 关键字用本地副本：直接绑 `modelValue.keyword` 会让输入框随防抖回写而抖动，
 * 且用户快速输入时中途的字符会被下一次 emit 覆盖。
 */
const keyword = ref(props.modelValue.keyword)

let debounceTimer: ReturnType<typeof setTimeout> | null = null

function scheduleKeyword(value: string): void {
  if (debounceTimer !== null) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    debounceTimer = null
    emit('update:modelValue', { keyword: value })
    emit('search')
  }, FILTER_DEBOUNCE_MS)
}

watch(keyword, (value) => scheduleKeyword(value))

/** 外部重置筛选（如「清空」）时把本地副本同步回去，否则输入框会与真实条件脱节 */
watch(
  () => props.modelValue.keyword,
  (value) => {
    if (value !== keyword.value) keyword.value = value
  },
)

onBeforeUnmount(() => {
  // 不清定时器的话，用户打完字立刻切页会在组件销毁后仍 emit 一次
  if (debounceTimer !== null) clearTimeout(debounceTimer)
})

const permissionValue = computed<PermissionScope | 'none' | ''>(() => props.modelValue.permission_scope)

const totalText = computed(() =>
  props.totalChunks === null
    ? `共 ${props.total} 个知识单元`
    : `共 ${props.total} 个知识单元 · ${props.totalChunks} 个切片`,
)

function onCategoryChange(value: string): void {
  emit('update:modelValue', { category: value })
  emit('search')
}

function onStatusChange(value: KbDocumentStatus | ''): void {
  emit('update:modelValue', { status: value })
  emit('search')
}

function onPermissionChange(value: PermissionScope | 'none' | ''): void {
  emit('update:modelValue', { permission_scope: value })
  emit('search')
}
</script>

<template>
  <div class="filter-bar">
    <div class="filter-bar__search">
      <Search :size="15" class="filter-bar__search-icon" />
      <input
        v-model="keyword"
        class="filter-bar__search-input"
        type="text"
        placeholder="搜索标题 / 文件名"
      />
    </div>

    <!-- 分类：后端未提供聚合接口时用 allow-create 退化为「历史值补全 + 自由输入」（spec §9-3） -->
    <el-select
      class="filter-bar__select filter-bar__select--category"
      :model-value="props.modelValue.category"
      placeholder="全部分类"
      clearable
      filterable
      allow-create
      default-first-option
      @change="onCategoryChange"
    >
      <el-option v-for="item in props.categoryOptions" :key="item" :label="item" :value="item" />
    </el-select>

    <el-select
      class="filter-bar__select"
      :model-value="props.modelValue.status"
      placeholder="全部状态"
      @change="onStatusChange"
    >
      <el-option
        v-for="item in STATUS_FILTER_OPTIONS"
        :key="item.value"
        :label="item.label"
        :value="item.value"
      />
    </el-select>

    <el-select
      class="filter-bar__select filter-bar__select--permission"
      :model-value="permissionValue"
      placeholder="全部权限"
      @change="onPermissionChange"
    >
      <el-option
        v-for="item in PERMISSION_FILTER_OPTIONS"
        :key="item.value"
        :label="item.label"
        :value="item.value"
      />
    </el-select>

    <div class="filter-bar__spacer" />

    <span class="filter-bar__total">{{ totalText }}</span>
  </div>
</template>

<style scoped>
/* 原型 P2kxKA：width fill_container / gap 12 / alignItems center */
.filter-bar {
  display: flex;
  align-items: center;
  gap: 12px;
}

/* 原型 SearchBox：宽 272 h34 r8 白底 1px 描边 gap8 padding 0/12 */
.filter-bar__search {
  width: 272px;
  flex: none;
  height: 34px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 12px;
  background: var(--kb-bg-card);
  border: 1px solid var(--kb-border);
  border-radius: 8px;
}

.filter-bar__search-icon {
  flex: none;
  color: var(--kb-text-muted);
}

.filter-bar__search-input {
  flex: 1 1 auto;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  font-family: inherit;
  font-size: 12.5px;
  color: var(--kb-text-primary);
}

.filter-bar__search-input::placeholder {
  color: var(--kb-text-placeholder);
}

/* 原型三个 Select：宽 116 / 116 / 120，均 h34 r8 */
.filter-bar__select {
  width: 116px;
  flex: none;
}

.filter-bar__select--category {
  width: 116px;
}

.filter-bar__select--permission {
  width: 120px;
}

.filter-bar__spacer {
  flex: 1 1 auto;
}

.filter-bar__total {
  flex: none;
  font-size: 12.5px;
  color: var(--kb-text-muted);
  white-space: nowrap;
}

/* el-select 默认高度 32 / 圆角 4，与原型不一致，需拉平到 h34 r8。
   用 :deep 是因为它们渲染在 Element 自己的根节点上，scoped 选择器够不到。 */
.filter-bar :deep(.el-select__wrapper) {
  min-height: 34px;
  height: 34px;
  border-radius: 8px;
  background: var(--kb-bg-card);
  box-shadow: 0 0 0 1px var(--kb-border) inset;
  font-size: 12.5px;
}

.filter-bar :deep(.el-select__placeholder) {
  font-size: 12.5px;
  color: var(--kb-text-secondary);
}

.filter-bar :deep(.el-select__placeholder.is-transparent) {
  color: var(--kb-text-secondary);
}
</style>
