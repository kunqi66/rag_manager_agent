<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import type { FormInstance, FormRules } from 'element-plus'

import type { DepartmentNode, UserItem, UserPayload, UserStatus } from '@/types/org'

/**
 * 用户新增/编辑表单。只做校验 + `emit('submit', payload)`，不调 store、不发请求。
 *
 * 安全（CLAUDE.md 红线 3）：
 * - 密码框 `type="password"` + `autocomplete="new-password"`；
 * - **前端不做任何哈希**（后端用 `pwdlib[argon2]`）；
 * - 密码不进 localStorage / 不打日志 / 不埋点；
 * - 弹窗关闭（无论成功还是取消）即清空表单里的密码；
 * - `UserItem` 类型里**没有** `password_hash`，从类型层面杜绝它被消费。
 *
 * 本表单**不含角色**：分配角色走独立的 `PUT /users/{id}/roles`（全量覆盖语义），
 * 与 be-02 §3.1 对齐。设计 §4.2/§4.3② 对此自相矛盾，见 spec §9-18。
 */
const props = defineProps<{
  visible: boolean
  mode: 'create' | 'edit'
  initial: UserItem | null
  tree: DepartmentNode[]
  loading?: boolean
}>()

const emit = defineEmits<{
  'update:visible': [value: boolean]
  submit: [payload: UserPayload]
}>()

const TREE_SELECT_PROPS = { label: 'name', children: 'children' } as const

interface UserForm {
  username: string
  real_name: string
  department_id: string | null
  email: string
  status: UserStatus
  /** 仅新建时使用；编辑态该字段不渲染也不提交 */
  password: string
}

const formRef = ref<FormInstance>()

const form = reactive<UserForm>({
  username: '',
  real_name: '',
  department_id: null,
  email: '',
  status: 'active',
  password: '',
})

const isEdit = computed(() => props.mode === 'edit')

const rules = computed<FormRules<UserForm>>(() => ({
  username: [
    { required: true, message: '请输入登录名', trigger: 'blur' },
    { max: 64, message: '登录名不能超过 64 个字符', trigger: 'blur' },
  ],
  real_name: [{ required: true, message: '请输入姓名', trigger: 'blur' }],
  department_id: [{ required: true, message: '请选择所属部门', trigger: 'change' }],
  email: [{ type: 'email', message: '邮箱格式不正确', trigger: 'blur' }],
  // 密码规则是前端默认值——PRD 未定义密码策略（spec §9-5）。后端若有更强策略以其为准。
  password: isEdit.value
    ? []
    : [
        { required: true, message: '请输入初始密码', trigger: 'blur' },
        { min: 8, max: 64, message: '密码长度 8–64 位', trigger: 'blur' },
      ],
}))

watch(
  () => props.visible,
  (open) => {
    if (!open) {
      // 关闭即清空密码：明文在内存里待着的每一秒都是风险，且下次打开不该带出上一次的输入
      form.password = ''
      return
    }
    formRef.value?.clearValidate()

    if (props.mode === 'edit' && props.initial) {
      form.username = props.initial.username
      form.real_name = props.initial.real_name
      form.department_id = props.initial.department_id
      form.email = props.initial.email ?? ''
      form.status = props.initial.status
    } else {
      form.username = ''
      form.real_name = ''
      form.department_id = null
      form.email = ''
      form.status = 'active'
    }
    form.password = ''
  },
)

async function onSubmit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  const payload: UserPayload = {
    username: form.username.trim(),
    real_name: form.real_name.trim(),
    department_id: form.department_id,
    email: form.email.trim() || null,
    status: form.status,
  }

  // 编辑态**不带 password 键**：改密是另一条（尚未定义的）路径，见 spec §9-5
  if (!isEdit.value) payload.password = form.password

  emit('submit', payload)
}
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    :title="isEdit ? '编辑用户' : '新建用户'"
    width="480px"
    append-to-body
    @update:model-value="emit('update:visible', $event)"
  >
    <el-form
      ref="formRef"
      class="user-form"
      :model="form"
      :rules="rules"
      label-position="top"
      hide-required-asterisk
    >
      <el-form-item label="登录名" prop="username">
        <el-input
          v-model="form.username"
          placeholder="用于登录，创建后不可修改"
          :disabled="isEdit"
          autocomplete="off"
          maxlength="64"
        />
        <!-- 登录名不可改：改了会让审计日志对不上人（fe-04 §4.3② 第 3 条） -->
        <p v-if="isEdit" class="user-form__tip">登录名是审计日志的关联键，创建后不可修改。</p>
      </el-form-item>

      <el-form-item label="姓名" prop="real_name">
        <el-input v-model="form.real_name" placeholder="请输入姓名" maxlength="64" />
      </el-form-item>

      <el-form-item label="所属部门" prop="department_id">
        <el-tree-select
          v-model="form.department_id"
          class="user-form__control"
          :data="props.tree"
          node-key="id"
          :props="TREE_SELECT_PROPS"
          check-strictly
          :render-after-expand="false"
          placeholder="请选择所属部门"
        />
      </el-form-item>

      <el-form-item label="邮箱" prop="email">
        <el-input v-model="form.email" placeholder="选填" maxlength="128" autocomplete="off" />
      </el-form-item>

      <el-form-item label="状态" prop="status">
        <el-select v-model="form.status" class="user-form__control">
          <el-option label="启用" value="active" />
          <el-option label="停用" value="disabled" />
        </el-select>
      </el-form-item>

      <el-form-item v-if="!isEdit" label="初始密码" prop="password">
        <el-input
          v-model="form.password"
          type="password"
          placeholder="8–64 位，由用户首次登录后自行修改"
          autocomplete="new-password"
          show-password
          maxlength="64"
        />
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="emit('update:visible', false)">取消</el-button>
      <el-button type="primary" :loading="props.loading" @click="onSubmit">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.user-form__control {
  width: 100%;
}

.user-form__tip {
  margin: 6px 0 0;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--kb-text-muted);
}
</style>
