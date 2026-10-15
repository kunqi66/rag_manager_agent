<script setup lang="ts">
import { Folder, Info, Plus } from 'lucide-vue-next'

import { DEPT_INHERIT_HINT, DEPT_TREE_PROPS, PERM } from '@/constants/system'
import type { DepartmentNode } from '@/types/org'

/**
 * 部门树卡片。**两种用法共用一份渲染**，靠 `manageable` 区分：
 * - `manageable = true`  「部门管理」面板：节点悬停出「新增下级 / 编辑 / 删除」，头部有「新增根部门」
 * - `manageable = false` 「用户管理」面板的左栏筛选器：只读，点节点即筛选
 *
 * 为什么不拆成两个组件：树的渲染、选中高亮、脚注提示三处完全一样，
 * 拆两份必然有人改歪其中一份（`DEPT_INHERIT_HINT` 是 D10 的关键提示，两边都必须常驻）。
 *
 * 原型：`zUyFC/I5cp4K/hNgDF/EZbfO`（宽 250、头高 46、节点高 34、圆角 6、脚注 10.5）。
 */
const props = withDefaults(
  defineProps<{
    tree: DepartmentNode[]
    /** 当前选中部门。null = 未选（用户表格显示全部） */
    selectedId: string | null
    loading?: boolean
    /** 是否可增删改。false 时只作筛选器（用户管理面板的左栏） */
    manageable?: boolean
  }>(),
  { loading: false, manageable: false },
)

const emit = defineEmits<{
  select: [node: DepartmentNode]
  'create-root': []
  'create-child': [node: DepartmentNode]
  edit: [node: DepartmentNode]
  delete: [node: DepartmentNode]
}>()

/*
 * 下面所有 `v-permission` 只是**显隐体验**，不是安全边界——每个写接口后端仍独立校验功能权限
 * （CLAUDE.md 红线 7）。绕过前端隐藏直接调接口必须得 403（A1 的验收点）。
 * 权限码取自 `constants/system.ts` 的 `PERM`，与 be-02 `require_permission` 逐字对齐。
 */

/**
 * `data` 在 el-tree 的插槽里是宽泛的索引类型，用 `unknown` 收口后再断言，
 * 避免把 any 顺着事件签名传出去。
 */
function onNodeClick(data: unknown): void {
  emit('select', data as DepartmentNode)
}

/** 有子部门的节点不能直接删（be-02 §4.2.1 的引用校验），前端先把按钮禁掉少一次往返 */
function isBranch(data: unknown): boolean {
  return Boolean((data as DepartmentNode).children?.length)
}
</script>

<template>
  <section class="dtc">
    <header class="dtc__head">
      <h2 class="dtc__title">组织架构</h2>
      <!-- 只在管理态给「新增根部门」入口；筛选态这是别人的事 -->
      <button
        v-if="props.manageable"
        v-permission="PERM.deptCreate"
        class="dtc__icon-btn"
        type="button"
        title="新增根部门"
        @click="emit('create-root')"
      >
        <Plus :size="15" />
      </button>
    </header>

    <div class="dtc__body">
      <el-tree
        class="dtc__tree"
        :data="props.tree"
        node-key="id"
        :props="DEPT_TREE_PROPS"
        :current-node-key="props.selectedId ?? undefined"
        highlight-current
        default-expand-all
        :expand-on-click-node="false"
        @node-click="onNodeClick"
      >
        <template #default="{ data }">
          <span class="dn">
            <Folder :size="14" class="dn__icon" />
            <span class="dn__name">{{ data.name }}</span>
            <span v-if="data.children?.length" class="dn__count">{{ data.children.length }}</span>

            <span v-if="props.manageable" class="dn__acts">
              <button
                v-permission="PERM.deptCreate"
                class="dn__act"
                type="button"
                @click.stop="emit('create-child', data)"
              >
                新增下级
              </button>
              <button
                v-permission="PERM.deptUpdate"
                class="dn__act"
                type="button"
                @click.stop="emit('edit', data)"
              >
                编辑
              </button>
              <!-- 有子部门时禁用：删不掉，先迁移/清空下级（后端也会以 3002 拒绝） -->
              <button
                v-permission="PERM.deptDelete"
                class="dn__act dn__act--danger"
                type="button"
                :disabled="isBranch(data)"
                :title="isBranch(data) ? '请先处理下级部门' : '删除'"
                @click.stop="emit('delete', data)"
              >
                删除
              </button>
            </span>
          </span>
        </template>
      </el-tree>

      <p v-if="props.loading && props.tree.length === 0" class="dtc__empty">加载中…</p>
      <p v-else-if="props.tree.length === 0" class="dtc__empty">暂无部门，先新建一个根部门</p>
    </div>

    <!-- 常驻提示（原型 QG1Nv）：D10 向下继承的理解成本全在这一行上 -->
    <footer class="dtc__foot">
      <Info :size="13" />
      <span>{{ DEPT_INHERIT_HINT }}</span>
    </footer>
  </section>
</template>

<style scoped>
/* 原型 EZbfO：宽 250（由父级给）、圆角 10、描边 1 */
.dtc {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--kb-bg-card);
  border: 1px solid var(--kb-border);
  border-radius: 10px;
  overflow: hidden;
}

/* 原型 TreeHead/gO4dE：高 46、底部 1px 分隔、内边距 14、gap 8 */
.dtc__head {
  flex: none;
  height: 46px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 14px;
  border-bottom: 1px solid var(--kb-border);
}

.dtc__title {
  flex: 1 1 auto;
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.dtc__icon-btn {
  flex: none;
  width: 22px;
  height: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--kb-text-muted);
  cursor: pointer;
}

.dtc__icon-btn:hover {
  background: var(--kb-bg-subtle);
  color: var(--kb-brand);
}

/* 原型 TreeBody/U7hTJ7：内边距 8、gap 2 */
.dtc__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding: 8px;
}

.dtc__tree {
  background: transparent;
}

.dtc__empty {
  margin: 12px 4px;
  font-size: 11.5px;
  color: var(--kb-text-muted);
}

/* 节点（原型 TreeNode/*：高 34、圆角 6、gap 6、内边距 [0,8]） */
.dtc__tree :deep(.el-tree-node__content) {
  height: 34px;
  border-radius: 6px;
  gap: 6px;
  padding-right: 6px;
}

.dtc__tree :deep(.el-tree-node.is-current > .el-tree-node__content) {
  background: var(--kb-brand-soft);
}

.dtc__tree :deep(.el-tree-node.is-current > .el-tree-node__content .dn__name) {
  color: var(--kb-brand);
  font-weight: 600;
}

.dtc__tree :deep(.el-tree-node__expand-icon) {
  color: var(--kb-text-muted);
}

.dn {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
}

.dn__icon {
  flex: none;
  color: var(--kb-text-muted);
}

.dtc__tree :deep(.el-tree-node.is-current) .dn__icon {
  color: var(--kb-brand);
}

.dn__name {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 12.5px;
  color: var(--kb-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 原型 DeptCount：11，右对齐的浅灰小字 */
.dn__count {
  flex: none;
  font-size: 11px;
  color: var(--kb-text-muted);
}

/* 悬停才出现动作，避免树看起来太吵 */
.dn__acts {
  flex: none;
  display: none;
  align-items: center;
  gap: 6px;
}

.dtc__tree :deep(.el-tree-node__content:hover) .dn__acts {
  display: flex;
}

.dn__act {
  padding: 0;
  border: 0;
  background: transparent;
  font-family: inherit;
  font-size: 11.5px;
  color: var(--kb-brand);
  cursor: pointer;
}

.dn__act--danger {
  color: var(--kb-danger);
}

.dn__act:disabled {
  color: var(--kb-text-placeholder);
  cursor: not-allowed;
}

/* 脚注（原型 TreeFoot/QG1Nv：顶部 1px、内边距 [10,12]、图标 13、字号 10.5） */
.dtc__foot {
  flex: none;
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border-top: 1px solid var(--kb-border);
  font-size: 10.5px;
  line-height: 1.45;
  color: var(--kb-text-muted);
}

.dtc__foot svg {
  flex: none;
  margin-top: 1px;
}
</style>
