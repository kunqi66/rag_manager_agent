<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus'

import type { DepartmentNode, RoleBrief, UserBrief } from '@/types/org'
import type { PermissionPayload } from '@/types/kb'

import 'element-plus/es/components/message-box/style/css'

/**
 * 四维数据权限配置弹窗（A3 的核心，fe-02 §4.3 ②）。
 *
 * 原型**没有画**这个弹窗（spec §9-16），视觉按 fe-02 §4.3 自拟。
 *
 * **本组件不发请求**（CLAUDE.md 前端规范：表单组件只 emit，由页面编排）：
 * - 打开时 emit `open`，页面据此调 `GET .../permissions` 回显；
 * - 「指定用户」的远程搜索 emit `search-users`，页面把结果经 `userOptions` 回灌。
 *
 * ⚠️ 回显接口 `GET /api/import/documents/{id}/permissions` **后端未定义**
 * （fe-02 §10 #1，字典 §9 #70）。fe-02 明确「盲写会覆盖原权限，是数据破坏性缺陷，
 * 不建议先上再补」——因此**回显失败时提交按钮置灰**，宁可暂时不可用也不做破坏性写入。
 */
const props = defineProps<{
  visible: boolean
  /** 文档标题，放在弹窗副标题里，避免用户配错文档 */
  documentTitle: string
  departments: DepartmentNode[]
  roles: RoleBrief[]
  /** 远程搜索到的用户候选 */
  userOptions: UserBrief[]
  /** 回显结果。null = 尚未拿到 */
  initial: PermissionPayload | null
  /** 回显请求进行中 */
  loading: boolean
  /** 回显失败（接口缺失或报错）→ 禁用提交 */
  loadError: boolean
  /** 提交中 */
  submitting: boolean
}>()

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'open'): void
  (e: 'search-users', keyword: string): void
  (e: 'submit', payload: PermissionPayload): void
}>()

const globalPublic = ref(false)
const selectedDepartments = ref<string[]>([])
const selectedRoles = ref<string[]>([])
const selectedUsers = ref<string[]>([])

function resetFromInitial(): void {
  const source = props.initial
  globalPublic.value = source?.global_public ?? false
  selectedDepartments.value = [...(source?.departments ?? [])]
  selectedRoles.value = [...(source?.roles ?? [])]
  selectedUsers.value = [...(source?.users ?? [])]
}

// 回显结果到达后灌入表单。不放在 onMounted：弹窗是复用实例，每次打开都要重置。
watch(() => props.initial, resetFromInitial)

watch(
  () => props.visible,
  (visible) => {
    if (!visible) return
    // 先清空再等页面回填：残留上一次的勾选会让用户以为已经读到了新文档的权限
    resetFromInitial()
    emit('open')
  },
)

/** 四组全空 = 除知识管理员外无人可检索（PRD §6.1.1），不是「公开」 */
const isEmptyPermission = computed(
  () =>
    !globalPublic.value &&
    selectedDepartments.value.length === 0 &&
    selectedRoles.value.length === 0 &&
    selectedUsers.value.length === 0,
)

/**
 * 权限 ID → 展示名的兜底解析。
 * `PermissionPayload` 只有 ID（没有实体名），所以回显出来、又不在选项里的 ID
 * 只能显示原始 ID —— **不假装知道名字**，好过显示一个错误的部门名。
 * （部门由 `el-tree-select` 自己按树数据渲染标签，不需要在这里解析。）
 */
function labelOfRole(id: string): string {
  return props.roles.find((role) => role.id === id)?.name ?? id
}

function labelOfUser(id: string): string {
  const hit = props.userOptions.find((user) => user.id === id)
  if (!hit) return id
  return hit.department_name ? `${hit.real_name}（${hit.department_name}）` : hit.real_name
}

function onSearchUsers(keyword: string): void {
  emit('search-users', keyword)
}

async function onSubmit(): Promise<void> {
  const payload: PermissionPayload = {
    global_public: globalPublic.value,
    departments: [...selectedDepartments.value],
    roles: [...selectedRoles.value],
    users: [...selectedUsers.value],
  }

  // 二次确认：管理员最容易误以为「不配就是公开」，实际是「不配就是独占」（fe-02 §4.3 ② 第 5 步）
  if (isEmptyPermission.value) {
    try {
      await ElMessageBox.confirm(
        '四组权限全部为空时，除知识管理员外任何人都无法在问答中检索到该文档——这不是「公开」。确认保存吗？',
        '确认不授予任何权限？',
        { type: 'warning', confirmButtonText: '确认保存', cancelButtonText: '返回修改' },
      )
    } catch {
      return // 用户取消
    }
  }

  emit('submit', payload)
}
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    title="配置数据权限"
    width="560px"
    :close-on-click-modal="false"
    @update:model-value="emit('update:visible', $event)"
  >
    <div class="perm-dialog">
      <p class="perm-dialog__doc">
        <span class="perm-dialog__doc-label">知识单元：</span>
        <span class="perm-dialog__doc-title">{{ props.documentTitle }}</span>
      </p>

      <!-- 回显失败：明确说明原因并**禁用提交**，不产生盲写覆盖（spec §9-1） -->
      <el-alert
        v-if="props.loadError"
        type="warning"
        :closable="false"
        show-icon
        title="当前权限读取失败，无法回显"
        description="为避免把该文档原有权限覆盖为空，提交已禁用。后端补齐「读取权限」接口后自动恢复（见 spec P1-02 §9-1）。"
      />

      <div v-loading="props.loading" class="perm-dialog__body">
        <!-- ① 全局 -->
        <section class="perm-group">
          <div class="perm-group__head">
            <span class="perm-group__title">全局公开</span>
            <el-switch v-model="globalPublic" />
          </div>
          <p class="perm-group__hint">
            开启后全体登录用户均可检索到该文档；其余三维无需再配。
          </p>
        </section>

        <!-- ② 部门 -->
        <section class="perm-group">
          <div class="perm-group__head">
            <span class="perm-group__title">指定部门</span>
          </div>
          <el-tree-select
            v-model="selectedDepartments"
            class="perm-group__control"
            :data="props.departments"
            :props="{ label: 'name', children: 'children' }"
            node-key="id"
            value-key="id"
            multiple
            check-strictly
            show-checkbox
            filterable
            clearable
            :render-after-expand="false"
            placeholder="选择可见部门"
          />
          <!-- D10：向下继承。必须显式提示，否则管理员会以为「授权财务部」只覆盖财务部本身 -->
          <p class="perm-group__hint">
            授权某个部门，将自动覆盖其全部子孙部门（部门权限向下继承，PRD §6.1.2 D10）。
          </p>
        </section>

        <!-- ③ 角色 -->
        <section class="perm-group">
          <div class="perm-group__head">
            <span class="perm-group__title">指定角色</span>
          </div>
          <el-select
            v-model="selectedRoles"
            class="perm-group__control"
            multiple
            filterable
            clearable
            placeholder="选择可见角色"
          >
            <el-option
              v-for="role in props.roles"
              :key="role.id"
              :label="role.name"
              :value="role.id"
            />
          </el-select>
          <!-- 选中但选项里还没有的名称（回显只有 ID）用只读提示补出来，避免"看不见选了什么" -->
          <p v-if="selectedRoles.length" class="perm-group__hint perm-group__hint--soft">
            已选：{{ selectedRoles.map(labelOfRole).join('、') }}
          </p>
        </section>

        <!-- ④ 指定用户 -->
        <section class="perm-group">
          <div class="perm-group__head">
            <span class="perm-group__title">指定用户</span>
          </div>
          <el-select
            v-model="selectedUsers"
            class="perm-group__control"
            multiple
            filterable
            remote
            reserve-keyword
            clearable
            :remote-method="onSearchUsers"
            placeholder="输入姓名搜索用户"
          >
            <el-option
              v-for="user in props.userOptions"
              :key="user.id"
              :label="labelOfUser(user.id)"
              :value="user.id"
            />
          </el-select>
          <p v-if="selectedUsers.length" class="perm-group__hint perm-group__hint--soft">
            已选：{{ selectedUsers.map(labelOfUser).join('、') }}
          </p>
        </section>
      </div>
    </div>

    <template #footer>
      <el-button @click="emit('update:visible', false)">取消</el-button>
      <el-button
        type="primary"
        :loading="props.submitting"
        :disabled="props.loadError || props.loading"
        @click="onSubmit"
      >
        保存
      </el-button>
      <!-- ⚠️ 保存走后端独立校验（红线 7）：v-permission 只在这里的入口按钮上做显隐，
           手工直接调 PUT .../permissions 必须 403。 -->
    </template>
  </el-dialog>
</template>

<style scoped>
.perm-dialog {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.perm-dialog__doc {
  margin: 0;
  font-size: 12.5px;
  color: var(--kb-text-secondary);
}

.perm-dialog__doc-label {
  color: var(--kb-text-muted);
}

.perm-dialog__doc-title {
  font-weight: 500;
  color: var(--kb-text-primary);
}

.perm-dialog__body {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: 120px;
}

.perm-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.perm-group__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.perm-group__title {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--kb-text-primary);
}

.perm-group__control {
  width: 100%;
}

.perm-group__hint {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--kb-text-muted);
}

.perm-group__hint--soft {
  color: var(--kb-text-secondary);
}
</style>
