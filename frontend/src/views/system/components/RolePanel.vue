<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { onMounted, ref } from 'vue'

import { DELETE_TEXT, PERM } from '@/constants/system'
import { useOrgStore } from '@/stores/org'
import type { RoleItem, RolePayload } from '@/types/org'

import PermissionTreeDialog from './PermissionTreeDialog.vue'
import RoleFormDialog from './RoleFormDialog.vue'

/**
 * 「角色权限」面板：角色列表 + 配置权限入口。
 *
 * 与「用户管理」里的 `RoleOverviewCard` 是**同一份数据**（`org.roles`）、不同视角：
 * 概览卡片用于扫一眼配比，本面板用于操作。故此处只读 store，不另起请求。
 */
const org = useOrgStore()

const formVisible = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const formTarget = ref<RoleItem | null>(null)

const permVisible = ref(false)
const permTarget = ref<RoleItem | null>(null)

const submitting = ref(false)

async function guard(action: () => Promise<void>): Promise<void> {
  try {
    await action()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '操作失败，请稍后重试')
  }
}

onMounted(() => {
  // 面板懒挂载；角色列表通常已被 bootstrap 拉过，这里只在为空时补一次
  if (org.roles.length === 0) void guard(() => org.fetchRoles())
})

/* ---- 角色增删改 ---- */

function openCreate(): void {
  formMode.value = 'create'
  formTarget.value = null
  formVisible.value = true
}

function openEdit(role: RoleItem): void {
  formMode.value = 'edit'
  formTarget.value = role
  formVisible.value = true
}

async function onSubmitForm(payload: RolePayload): Promise<void> {
  submitting.value = true
  const isEdit = formMode.value === 'edit' && formTarget.value !== null
  try {
    if (isEdit && formTarget.value) {
      await org.updateRole(formTarget.value.id, payload)
    } else {
      await org.createRole(payload)
    }
    formVisible.value = false
    ElMessage.success(isEdit ? '角色已更新' : '角色已创建')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '保存失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}

async function onDelete(role: RoleItem): Promise<void> {
  if (role.is_builtin) return

  try {
    await ElMessageBox.confirm(
      `确定删除角色「${role.name}」吗？持有该角色的用户会立即失去对应权限。`,
      DELETE_TEXT.roleTitle,
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  await guard(async () => {
    await org.deleteRole(role.id)
    ElMessage.success('角色已删除')
  })
}

/* ---- 配置权限 ---- */

function openPermission(role: RoleItem): void {
  permTarget.value = role
  permVisible.value = true
}

/**
 * 回显源：`RoleItem.permission_codes`。
 * 后端没回带该字段时为 `null`（= 未知）—— 传 `null` 进去让弹窗显示警示条，
 * **绝不能退化成空数组**，那等于「保存即清空该角色全部权限」。
 */
function grantedCodesOf(role: RoleItem | null): string[] | null {
  return role?.permission_codes ?? null
}

async function onSubmitPermission(codes: string[]): Promise<void> {
  const target = permTarget.value
  if (!target) return

  submitting.value = true
  try {
    // 全量覆盖语义：codes 即该角色的最终权限集合
    await org.grantRolePermissions(target.id, codes)
    permVisible.value = false
    ElMessage.success('权限已更新，持有该角色的用户需重新登录后生效')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '保存失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}

/**
 * `el-table` 默认插槽把 `row` 声明成 `DefaultRow = Record<PropertyKey, any>`：
 * **读属性没事**（模板里 `row.name` 看着一切正常），
 * 但**不能直接传给形参是具体实体的函数** —— TS 报 `DefaultRow is not assignable to RoleItem`
 * （只有 `vue-tsc` 查得出来，SFC 语法层查不出来）。
 * 用收窄函数而不是散落的 `as RoleItem`：`as` 写错不报错，这里收口成一处。
 */
function asRole(row: unknown): RoleItem {
  return row as RoleItem
}

function permLabel(role: RoleItem): string {
  if (!role.permission_codes) return '—'
  return `${role.permission_codes.length} / ${org.permissionCodeCount}`
}

function userLabel(role: RoleItem): string {
  return role.user_count === undefined ? '—' : `${role.user_count} 人`
}

defineExpose({ openCreate })
</script>

<template>
  <div class="role-panel">
    <section class="rpc">
      <header class="rpc__head">
        <h2 class="rpc__title">角色与权限</h2>
        <span class="rpc__count">共 {{ org.roles.length }} 个角色</span>
      </header>

      <div class="rpc__body">
        <el-table
          :data="org.roles"
          class="rpc__table"
          row-key="id"
          :empty-text="org.loading.role ? '加载中…' : '暂无角色'"
        >
          <el-table-column label="角色" min-width="180">
            <template #default="{ row }">
              <span class="rc">
                <span class="rc__dot" :class="{ 'is-builtin': row.is_builtin }" />
                <span class="rc__col">
                  <span class="rc__name">
                    {{ row.name }}
                    <span v-if="row.is_builtin" class="rc__chip">内置</span>
                  </span>
                  <span class="rc__code">{{ row.code }}</span>
                </span>
              </span>
            </template>
          </el-table-column>

          <el-table-column label="描述" min-width="200">
            <template #default="{ row }">
              <span class="rpc__cell">{{ row.description || '—' }}</span>
            </template>
          </el-table-column>

          <el-table-column label="已授权限" width="120">
            <template #default="{ row }">
              <span class="rpc__cell">{{ permLabel(asRole(row)) }}</span>
            </template>
          </el-table-column>

          <el-table-column label="用户数" width="100">
            <template #default="{ row }">
              <span class="rpc__cell rpc__cell--muted">{{ userLabel(asRole(row)) }}</span>
            </template>
          </el-table-column>

          <!--
            v-permission 只是显隐体验，不是安全边界：后端每个写接口独立校验（红线 7）。
            内置角色删除置灰也同理——后端必须独立拒绝。
          -->
          <el-table-column label="操作" width="210" fixed="right">
            <template #default="{ row }">
              <span class="rpc__acts">
                <button
                  v-permission="PERM.roleGrant"
                  class="rpc__act"
                  type="button"
                  @click="openPermission(asRole(row))"
                >
                  配置权限
                </button>
                <button
                  v-permission="PERM.roleUpdate"
                  class="rpc__act"
                  type="button"
                  @click="openEdit(asRole(row))"
                >
                  编辑
                </button>
                <button
                  v-permission="PERM.roleDelete"
                  class="rpc__act rpc__act--danger"
                  type="button"
                  :disabled="row.is_builtin"
                  :title="row.is_builtin ? '内置角色不可删除' : '删除'"
                  @click="onDelete(asRole(row))"
                >
                  删除
                </button>
              </span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </section>

    <RoleFormDialog
      v-model:visible="formVisible"
      :mode="formMode"
      :initial="formTarget"
      :loading="submitting"
      @submit="onSubmitForm"
    />

    <PermissionTreeDialog
      v-model:visible="permVisible"
      :role-name="permTarget?.name ?? ''"
      :role-id="permTarget?.id ?? ''"
      :tree="org.permissionTree"
      :checked-codes="grantedCodesOf(permTarget)"
      :loading="submitting"
      @submit="onSubmitPermission"
    />
  </div>
</template>

<style scoped>
.role-panel {
  flex: 1 1 auto;
  min-height: 0;
  min-width: 0;
  display: flex;
}

.rpc {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--kb-bg-card);
  border: 1px solid var(--kb-border);
  border-radius: 10px;
  overflow: hidden;
}

.rpc__head {
  flex: none;
  height: 46px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 14px;
  border-bottom: 1px solid var(--kb-border);
}

.rpc__title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.rpc__count {
  flex: 1 1 auto;
  font-size: 11.5px;
  color: var(--kb-text-muted);
}

.rpc__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
}

.rpc__table {
  width: 100%;
}

.rpc__cell {
  font-size: 12px;
  color: var(--kb-text-secondary);
}

.rpc__cell--muted {
  color: var(--kb-text-muted);
}

.rc {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.rc__dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 4px;
  background: var(--kb-text-secondary);
}

.rc__dot.is-builtin {
  background: var(--kb-danger);
}

.rc__col {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.rc__name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--kb-text-primary);
}

.rc__chip {
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--kb-warning-soft);
  font-size: 10px;
  font-weight: 400;
  color: var(--kb-warning-text);
}

.rc__code {
  font-size: 11px;
  color: var(--kb-text-muted);
}

.rpc__acts {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.rpc__act {
  padding: 0;
  border: 0;
  background: transparent;
  font-family: inherit;
  font-size: 11.5px;
  color: var(--kb-brand);
  cursor: pointer;
}

.rpc__act--danger {
  color: var(--kb-danger);
}

.rpc__act:disabled {
  color: var(--kb-text-placeholder);
  cursor: not-allowed;
}
</style>
