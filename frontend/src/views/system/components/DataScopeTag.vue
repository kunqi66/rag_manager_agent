<script setup lang="ts">
import { Building2, Globe } from 'lucide-vue-next'
import { computed } from 'vue'

import { DATA_SCOPE_HINT, GLOBAL_SCOPE_LABEL, SCOPE_WITH_CHILDREN_SUFFIX } from '@/constants/system'

/**
 * 用户表格「数据权限范围」列的渲染（原型 `zUyFC/URow` 行内的 `ScopeCell`）。
 *
 * ⚠️ 注释里写原型节点路径时**不要出现 `星号 + 斜杠`**——那会提前闭合块注释，
 *    后续整段中文被当成代码解析，报出来的是「Unexpected character」这种风马牛不相及的错误。
 *
 * **这一列不是功能权限**——PRD §3.3 已明确「数据权限」与「功能权限」正交：
 * 前者决定能检索到哪些知识，后者决定能用哪些按钮。悬停文案必须把这点说清，
 * 否则管理员会拿它当权限码用（fe-04 §4.3②）。
 *
 * 纯展示组件，不发请求、不 emit。
 */
const props = withDefaults(
  defineProps<{
    /** 用户所属部门 id。null = 无部门归属（如超级管理员），展示为「全局」 */
    departmentId: string | null
    /** 部门全路径名，如 `集团总部 / 财务部 / 成本组`；由调用方用部门树算出（fe-04 §4.2） */
    deptPath: string | null
    /** 该部门是否有下级。有则展示「（含下级）」，对应 D10 的向下继承 */
    hasChildren?: boolean
  }>(),
  { hasChildren: false },
)

/** 只取路径最后一段作为展示名——整条路径放在悬停里，列宽只有 152px 放不下 */
const name = computed(() => {
  if (!props.deptPath) return null
  const segments = props.deptPath.split(' / ')
  return segments[segments.length - 1] || props.deptPath
})

const isGlobal = computed(() => !props.departmentId)

const text = computed(() => {
  if (isGlobal.value) return GLOBAL_SCOPE_LABEL
  if (!name.value) return '—'
  return props.hasChildren ? `${name.value}${SCOPE_WITH_CHILDREN_SUFFIX}` : name.value
})
</script>

<template>
  <el-tooltip placement="top" effect="dark" :show-after="120">
    <template #content>
      <div class="scope-tip">
        <p class="scope-tip__text">{{ DATA_SCOPE_HINT }}</p>
        <p v-if="props.deptPath" class="scope-tip__path">所属路径：{{ props.deptPath }}</p>
      </div>
    </template>

    <span class="scope" :class="{ 'is-global': isGlobal }">
      <Globe v-if="isGlobal" :size="13" class="scope__icon scope__icon--brand" />
      <Building2 v-else :size="13" class="scope__icon" />
      <span class="scope__text">{{ text }}</span>
    </span>
  </el-tooltip>
</template>

<style scoped>
/* 原型 ScopeCell：gap 6、图标 13、字号 12 */
.scope {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  font-size: 12px;
  color: var(--kb-text-secondary);
}

.scope.is-global {
  color: var(--kb-brand);
}

.scope__icon {
  flex: none;
  color: var(--kb-text-muted);
}

.scope__icon--brand {
  color: var(--kb-brand);
}

.scope__text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.scope-tip {
  max-width: 260px;
}

.scope-tip__text {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
}

.scope-tip__path {
  margin: 4px 0 0;
  font-size: 11.5px;
  opacity: 0.85;
}
</style>
