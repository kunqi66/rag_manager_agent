<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { computed, ref } from 'vue'

import { DELETE_TEXT } from '@/constants/system'
import { deptSubtreeIdSet, useOrgStore } from '@/stores/org'
import type { DepartmentNode, DepartmentPayload } from '@/types/org'

import DepartmentFormDialog from './DepartmentFormDialog.vue'
import DeptTreeCard from './DeptTreeCard.vue'

/**
 * 「部门管理」面板：部门树 + 增删改。
 *
 * 面板**自治**：自己持有对话框状态、自己调 store。
 * 若把对话框状态全部上提到 `SystemView`，那个 setup 会变成四个面板的状态堆——
 * 正是 fe-04 §2 拆分面板时要避免的事。
 *
 * 删除策略（fe-04 §4.3①）：有子部门时按钮禁用（前端），后端仍会以 3002 独立拒绝。
 */
const org = useOrgStore()

const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const dialogInitial = ref<DepartmentNode | null>(null)
const defaultParentId = ref<string | null>(null)
const submitting = ref(false)

/**
 * 编辑态禁选自身与全部子孙——选中自己的后代即为「环」，
 * 后端环校验会以 1001 拒绝（be-02 §4.2.1 不变式 I3）。前端禁选只为少一次无效往返。
 */
const disabledIds = computed(() =>
  dialogMode.value === 'edit' && dialogInitial.value
    ? [...deptSubtreeIdSet(org.deptTree, dialogInitial.value.id)]
    : [],
)

/** 点节点只更新高亮；不碰用户筛选条件（那是「用户管理」面板的事） */
function onSelect(node: DepartmentNode): void {
  org.selectedDeptId = node.id
}

function openCreate(): void {
  dialogMode.value = 'create'
  dialogInitial.value = null
  defaultParentId.value = null
  dialogVisible.value = true
}

function openCreateChild(node: DepartmentNode): void {
  dialogMode.value = 'create'
  dialogInitial.value = null
  defaultParentId.value = node.id
  dialogVisible.value = true
}

function openEdit(node: DepartmentNode): void {
  dialogMode.value = 'edit'
  dialogInitial.value = node
  defaultParentId.value = null
  dialogVisible.value = true
}

async function onSubmit(payload: DepartmentPayload): Promise<void> {
  submitting.value = true
  const isEdit = dialogMode.value === 'edit' && dialogInitial.value !== null
  try {
    if (isEdit && dialogInitial.value) {
      await org.updateDept(dialogInitial.value.id, payload)
    } else {
      await org.createDept(payload)
    }
    dialogVisible.value = false
    ElMessage.success(isEdit ? '部门已更新' : '部门已创建')
  } catch (error) {
    // 3002 冲突 / 1001 环校验：展示后端原文，**不关窗**，用户改完还能重试
    ElMessage.error(error instanceof Error ? error.message : '保存失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}

async function onDelete(node: DepartmentNode): Promise<void> {
  // 有子部门时按钮已置灰，这里是双保险（键盘可达等异常路径）
  if (node.children?.length) return

  try {
    await ElMessageBox.confirm(
      `确定删除部门「${node.name}」吗？该操作不可撤销。`,
      DELETE_TEXT.departmentTitle,
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }

  try {
    await org.deleteDept(node.id)
    ElMessage.success('部门已删除')
  } catch (error) {
    // 具体是哪一类引用（子部门 / 用户挂靠 / kb_document_permission 悬挂授权行）只有后端知道，
    // 前端**不预判**——预判错了会给出「看起来能删」的误导。原样展示后端 message。
    ElMessage.error(error instanceof Error ? error.message : DELETE_TEXT.conflictFallback)
  }
}

// SystemView 顶栏的「新建部门」按钮调它（原型按钮在顶栏，见 spec §9-11）
defineExpose({ openCreate })
</script>

<template>
  <div class="dept-panel">
    <DeptTreeCard
      :tree="org.deptTree"
      :selected-id="org.selectedDeptId"
      :loading="org.loading.department"
      manageable
      @select="onSelect"
      @create-root="openCreate"
      @create-child="openCreateChild"
      @edit="openEdit"
      @delete="onDelete"
    />

    <DepartmentFormDialog
      v-model:visible="dialogVisible"
      :mode="dialogMode"
      :initial="dialogInitial"
      :default-parent-id="defaultParentId"
      :tree="org.deptTree"
      :disabled-ids="disabledIds"
      :loading="submitting"
      @submit="onSubmit"
    />
  </div>
</template>

<style scoped>
.dept-panel {
  flex: 1 1 auto;
  min-height: 0;
  min-width: 0;
  display: flex;
}
</style>
