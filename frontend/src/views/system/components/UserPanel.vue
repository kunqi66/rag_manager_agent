<script setup lang="ts">
import { Search } from 'lucide-vue-next'
import { ElMessage, ElMessageBox } from 'element-plus'
import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import { computed, onMounted, ref } from 'vue'

import { DELETE_TEXT, PAGE_SIZE_OPTIONS, PERM } from '@/constants/system'
import { useAuthStore } from '@/stores/auth'
import { useOrgStore } from '@/stores/org'
import type { DepartmentNode, UserItem, UserPayload, UserStatus } from '@/types/org'

import AssignRoleDialog from './AssignRoleDialog.vue'
import DataScopeTag from './DataScopeTag.vue'
import DeptTreeCard from './DeptTreeCard.vue'
import RoleOverviewCard from './RoleOverviewCard.vue'
import UserFormDialog from './UserFormDialog.vue'

/**
 * 「用户管理」面板：左部门筛选 + 右用户表格 + 底部角色概览。
 *
 * 布局按原型 `zUyFC/RqMwy`：左栏 250、右栏自适应，右栏纵向 = 表格(自适应) + 角色概览(196)。
 *
 * 表格列口径（spec §9-12）：设计 §4.3② 列了 8 个字段、原型是 6 列且把「用户名 + 姓名」
 * 合成一个头像单元格。实现取**设计字段的全集**，展示上沿用原型的合并单元格 + 保留原型的
 * 「角色」列 → 共 8 列。两边都不丢。
 */
const emit = defineEmits<{
  /** 「角色与权限概览 → 管理权限点 ›」：切到「角色权限」面板 */
  'manage-roles': []
}>()

const org = useOrgStore()
const auth = useAuthStore()

const keyword = ref(org.userQuery.keyword)

const formVisible = ref(false)
const formMode = ref<'create' | 'edit'>('create')
const formTarget = ref<UserItem | null>(null)

const assignVisible = ref(false)
const assignTarget = ref<UserItem | null>(null)

const submitting = ref(false)

/** 统一兜底：http.ts 已把 `code!==0` 转成带 message 的 ApiError，这里原样透出 */
async function guard(action: () => Promise<void>): Promise<void> {
  try {
    await action()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '操作失败，请稍后重试')
  }
}

onMounted(() => {
  // 面板是懒挂载的（`v-if` + `keep-alive`），这里只会跑一次；
  // 部门树/角色/权限树由 SystemView 的 bootstrap 拉，用户列表依赖它们先就位
  void guard(() => org.fetchUsers())
})

/* ---- 部门筛选 ---- */

async function onSelectDept(node: DepartmentNode): Promise<void> {
  org.selectDept(node.id)
  await guard(() => org.fetchUsers())
}

/* ---- 搜索与分页 ---- */

async function onSearch(): Promise<void> {
  await guard(() => org.patchUserQuery({ keyword: keyword.value.trim(), page: 1 }))
}

async function onPageChange(page: number): Promise<void> {
  await guard(() => org.patchUserQuery({ page }))
}

async function onSizeChange(size: number): Promise<void> {
  // 每页条数变了必须回第 1 页，否则可能落在越界页拿回空列表
  await guard(() => org.patchUserQuery({ page: 1, page_size: size }))
}

/* ---- 增删改 ---- */

function openCreate(): void {
  formMode.value = 'create'
  formTarget.value = null
  formVisible.value = true
}

function openEdit(row: UserItem): void {
  formMode.value = 'edit'
  formTarget.value = row
  formVisible.value = true
}

async function onSubmitForm(payload: UserPayload): Promise<void> {
  submitting.value = true
  const isEdit = formMode.value === 'edit' && formTarget.value !== null
  try {
    if (isEdit && formTarget.value) {
      // 编辑态 payload 里没有 password 键（UserFormDialog 保证），类型上正好匹配
      await org.updateUser(formTarget.value.id, payload)
    } else {
      await org.createUser(payload)
    }
    formVisible.value = false
    ElMessage.success(isEdit ? '用户已更新' : '用户已创建')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '保存失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}

/**
 * 是不是当前登录人自己。
 *
 * `auth.user.user_id` 在 fe-01 侧被声明为 `number`，而字典 §2.1 的 `sys_user.id` 是 VARCHAR
 * ——两边口径冲突（spec §9-17）。统一 `String()` 归一后比较，绕开这个不确定性；
 * 归一后仍不相等只会导致「按钮没置灰」，**不构成安全问题**（后端必须独立拒绝删自己，红线 7）。
 */
function isSelf(row: UserItem): boolean {
  return String(auth.user?.user_id ?? '') === row.id
}

async function onDelete(row: UserItem): Promise<void> {
  if (isSelf(row)) return

  try {
    await ElMessageBox.confirm(
      `确定删除用户「${row.real_name || row.username}」吗？删除后其权限立即失效。`,
      DELETE_TEXT.userTitle,
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  await guard(async () => {
    await org.deleteUser(row.id)
    ElMessage.success('用户已删除')
  })
}

/* ---- 分配角色 ---- */

function openAssign(row: UserItem): void {
  assignTarget.value = row
  assignVisible.value = true
}

/**
 * 回显源：`UserItem.role_ids`（后端**恒返回**，见 `types/org.ts`）。
 *
 * ⚠️ 保留 `null` 返回分支：语义是「**未知**」，不是「空」。
 * 弹窗据此显示警示条 —— 把「未知」当成「空」提交，会静默清空用户的全部角色。
 *
 * 📌 2026-10-14 修正：原先读的是 `target.roles`，而**后端从来不发那个字段**
 * ⇒ 恒为 `undefined` ⇒ 恒返回 `null` ⇒ **警示条永远挂着、勾选永远是空的**。
 * 防住了数据丢失，但功能等于没做。改成 `role_ids` 后它才是真的「已知」。
 */
function initialRoleIdsOf(target: UserItem | null): string[] | null {
  if (!target) return null
  // 后端保证了这个字段存在；防御性兜底，避免旧缓存 / 老版本后端下把 undefined 当空数组
  if (!Array.isArray(target.role_ids)) return null
  return [...target.role_ids]
}

/**
 * 用户 id → 角色名数组（**每行只查一次**）。
 *
 * 写成预先算好的 Map 而不是在模板里直接调 `org.roleNames(row.role_ids)`：
 * 那个单元格要读 3 次（长度、第一个、`+n`），直接调就是每行 3 遍 `find`。
 * 20 行 × 3 遍不是性能问题，但**同一个值算 3 次迟早会漏改一处**。
 */
const roleNamesByUser = computed(() => {
  const map = new Map<string, string[]>()
  for (const user of org.users) {
    map.set(user.id, org.roleNames(user.role_ids))
  }
  return map
})

function roleNamesOf(row: UserItem): string[] {
  return roleNamesByUser.value.get(row.id) ?? []
}

async function onSubmitAssign(roleIds: string[]): Promise<void> {
  const target = assignTarget.value
  if (!target) return

  submitting.value = true
  try {
    await org.assignRoles(target.id, roleIds)
    assignVisible.value = false
    ElMessage.success('角色已更新')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '保存失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}

/* ---- 展示工具 ---- */

/**
 * `el-table` 默认插槽把 `row` 声明成 `DefaultRow = Record<PropertyKey, any>`：
 * **读属性没事**（所以模板里 `row.username` 看着一切正常），
 * 但**不能把它直接传给形参是具体实体的函数** —— TS 会报
 * `DefaultRow is not assignable to UserItem`（vue-tsc 才查得出来，SFC 语法层查不出来）。
 *
 * 用一个收窄函数而不是散落的 `as UserItem`：`as` 写错（比如写成别的实体）不报错，
 * 这里收口成一处，改类型时也只改这一个地方。
 */
function asUser(row: unknown): UserItem {
  return row as UserItem
}

function displayName(row: UserItem): string {
  return row.real_name || row.username
}

function avatarText(row: UserItem): string {
  return displayName(row).slice(0, 1) || '·'
}

function statusLabel(status: UserStatus): string {
  return status === 'active' ? '启用' : '停用'
}

/** `2026-10-14T09:30:00Z` → `2026-10-14 09:30`。不引入日期库（本页只做展示，不做时区换算） */
function formatDateTime(value: string | null): string {
  if (!value) return '—'
  return value.replace('T', ' ').slice(0, 16)
}

function deptLabel(row: UserItem): string {
  return row.department_name ?? org.deptPath(row.department_id) ?? '—'
}

defineExpose({ openCreate })
</script>

<template>
  <div class="user-panel">
    <DeptTreeCard
      class="user-panel__tree"
      :tree="org.deptTree"
      :selected-id="org.selectedDeptId"
      :loading="org.loading.department"
      @select="onSelectDept"
    />

    <div class="user-panel__main">
      <section class="utc">
        <header class="utc__head">
          <h2 class="utc__title">用户管理</h2>
          <span class="utc__count">共 {{ org.userTotal }} 人</span>
          <el-input
            v-model="keyword"
            class="utc__search"
            placeholder="搜索姓名或登录名"
            :prefix-icon="Search"
            clearable
            @keyup.enter="onSearch"
            @clear="onSearch"
          />
        </header>

        <div class="utc__body">
          <el-table
            :data="org.users"
            class="utc__table"
            row-key="id"
            :empty-text="org.loading.user ? '加载中…' : '该条件下暂无用户'"
          >
            <!-- 用户名 + 姓名合并成一格（原型 URow 的头像单元格），设计要求的两个字段都在 -->
            <el-table-column label="用户" min-width="170">
              <template #default="{ row }">
                <span class="uc">
                  <span class="uc__avatar">{{ avatarText(asUser(row)) }}</span>
                  <span class="uc__col">
                    <span class="uc__name">{{ displayName(asUser(row)) }}</span>
                    <span class="uc__account">{{ row.username }}</span>
                  </span>
                </span>
              </template>
            </el-table-column>

            <el-table-column label="所属部门" width="120">
              <template #default="{ row }">
                <span class="utc__cell">{{ deptLabel(asUser(row)) }}</span>
              </template>
            </el-table-column>

            <el-table-column label="角色" width="120">
              <template #default="{ row }">
                <!--
                  ⚠️ 名字要用 `org.roleNames` 从 `role_ids` 查（后端只回 id）。
                  原先这里读 `row.roles[0].name` —— 那个字段后端不发，所以这一列
                  **永远是「—」**，且不报任何错。
                -->
                <span v-if="roleNamesOf(asUser(row)).length" class="utc__roles">
                  <span class="utc__pill">{{ roleNamesOf(asUser(row))[0] }}</span>
                  <span v-if="roleNamesOf(asUser(row)).length > 1" class="utc__more">
                    +{{ roleNamesOf(asUser(row)).length - 1 }}
                  </span>
                </span>
                <span v-else class="utc__cell utc__cell--muted">—</span>
              </template>
            </el-table-column>

            <el-table-column label="数据权限范围" width="150">
              <template #default="{ row }">
                <DataScopeTag
                  :department-id="row.department_id"
                  :dept-path="org.deptPath(row.department_id)"
                  :has-children="org.deptHasChildren(row.department_id)"
                />
              </template>
            </el-table-column>

            <el-table-column label="邮箱" width="150">
              <template #default="{ row }">
                <span class="utc__cell">{{ row.email || '—' }}</span>
              </template>
            </el-table-column>

            <el-table-column label="状态" width="78">
              <template #default="{ row }">
                <span class="utc__status" :class="`is-${row.status}`">
                  {{ statusLabel(row.status) }}
                </span>
              </template>
            </el-table-column>

            <el-table-column label="最后登录" width="120">
              <template #default="{ row }">
                <span class="utc__cell utc__cell--muted">{{ formatDateTime(row.last_login_at) }}</span>
              </template>
            </el-table-column>

            <!--
              下面三个按钮的 `v-permission` 只是**显隐体验**，不是安全边界：
              每个写接口后端仍须独立校验功能权限（CLAUDE.md 红线 7）。
              绕过前端隐藏直接调接口必须得 403（验收 A1）。
            -->
            <el-table-column label="操作" width="148" fixed="right">
              <template #default="{ row }">
                <span class="utc__acts">
                  <button
                    v-permission="PERM.userUpdate"
                    class="utc__act"
                    type="button"
                    @click="openEdit(asUser(row))"
                  >
                    编辑
                  </button>
                  <button
                    v-permission="PERM.userGrant"
                    class="utc__act"
                    type="button"
                    @click="openAssign(asUser(row))"
                  >
                    分配角色
                  </button>
                  <button
                    v-permission="PERM.userDelete"
                    class="utc__act utc__act--danger"
                    type="button"
                    :disabled="isSelf(asUser(row))"
                    :title="isSelf(asUser(row)) ? '不能删除当前登录人自己' : '删除'"
                    @click="onDelete(asUser(row))"
                  >
                    删除
                  </button>
                </span>
              </template>
            </el-table-column>
          </el-table>
        </div>

        <footer class="utc__foot">
          <el-pagination
            :current-page="org.userQuery.page"
            :page-size="org.userQuery.page_size"
            :page-sizes="PAGE_SIZE_OPTIONS"
            :total="org.userTotal"
            layout="total, sizes, prev, pager, next"
            background
            @current-change="onPageChange"
            @size-change="onSizeChange"
          />
        </footer>
      </section>

      <RoleOverviewCard
        :roles="org.roles"
        :total-permissions="org.permissionCodeCount"
        :loading="org.loading.role"
        @manage="emit('manage-roles')"
      />
    </div>

    <UserFormDialog
      v-model:visible="formVisible"
      :mode="formMode"
      :initial="formTarget"
      :tree="org.deptTree"
      :loading="submitting"
      @submit="onSubmitForm"
    />

    <AssignRoleDialog
      v-model:visible="assignVisible"
      :user-name="assignTarget ? displayName(assignTarget) : ''"
      :roles="org.roles"
      :initial-role-ids="initialRoleIdsOf(assignTarget)"
      :loading="submitting"
      @submit="onSubmitAssign"
    />
  </div>
</template>

<style scoped>
/* 原型 Content：左 250 树 + 右自适应，gap 14 */
.user-panel {
  flex: 1 1 auto;
  min-height: 0;
  min-width: 0;
  display: flex;
  gap: 14px;
}

/* 用子选择器提一档特异性：`DeptTreeCard` 自己的 `.dtc { flex: 1 1 auto }` 与单类选择器
   权重相同，谁生效取决于两个 SFC 的样式注入顺序（脆弱）。这里 0,3,0 > 0,2,0，恒定生效。 */
.user-panel > .user-panel__tree {
  flex: none;
  width: 250px;
}

/* 原型 RightCol：纵向 gap 14（表格自适应 + 角色卡片 196） */
.user-panel__main {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.utc {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--kb-bg-card);
  border: 1px solid var(--kb-border);
  border-radius: 10px;
  overflow: hidden;
}

/* 原型 TableHead：高 46、gap 10、内边距 14 */
.utc__head {
  flex: none;
  height: 46px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 14px;
  border-bottom: 1px solid var(--kb-border);
}

.utc__title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.utc__count {
  flex: 1 1 auto;
  font-size: 11.5px;
  color: var(--kb-text-muted);
}

.utc__search {
  flex: none;
  width: 200px;
}

.utc__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  display: flex;
  flex-direction: column;
}

.utc__table {
  flex: 1 1 auto;
}

.utc__cell {
  font-size: 12px;
  color: var(--kb-text-secondary);
}

.utc__cell--muted {
  color: var(--kb-text-muted);
}

/* 头像单元格（原型 URow 头部） */
.uc {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.uc__avatar {
  flex: none;
  width: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 13px;
  background: var(--kb-brand-soft);
  font-size: 12px;
  color: var(--kb-brand);
}

.uc__col {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.uc__name {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--kb-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.uc__account {
  font-size: 11px;
  color: var(--kb-text-muted);
}

.utc__roles {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}

.utc__pill {
  max-width: 78px;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--kb-bg-subtle);
  font-size: 11px;
  color: var(--kb-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.utc__more {
  font-size: 11px;
  color: var(--kb-text-muted);
}

.utc__status {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
}

.utc__status.is-active {
  background: var(--kb-success-soft);
  color: var(--kb-success);
}

.utc__status.is-disabled {
  background: var(--kb-bg-subtle);
  color: var(--kb-text-muted);
}

.utc__acts {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.utc__act {
  padding: 0;
  border: 0;
  background: transparent;
  font-family: inherit;
  font-size: 11.5px;
  color: var(--kb-brand);
  cursor: pointer;
}

.utc__act--danger {
  color: var(--kb-danger);
}

.utc__act:disabled {
  color: var(--kb-text-placeholder);
  cursor: not-allowed;
}

.utc__foot {
  flex: none;
  display: flex;
  justify-content: flex-end;
  padding: 10px 14px;
  border-top: 1px solid var(--kb-border);
}
</style>
