<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import type { FormInstance, FormRules } from 'element-plus'

import type { RoleItem, RolePayload } from '@/types/org'

/**
 * 角色新增/编辑表单。只做校验 + `emit('submit', payload)`。
 *
 * `code` 是权限判定与后端 `require_permission` 的比对键（fe-04 §5 注），
 * **编辑态与内置角色一律置灰**：改 code 等于让所有已授权的引用悬空。
 */
const props = defineProps<{
  visible: boolean
  mode: 'create' | 'edit'
  initial: RoleItem | null
  loading?: boolean
}>()

const emit = defineEmits<{
  'update:visible': [value: boolean]
  submit: [payload: RolePayload]
}>()

interface RoleForm {
  code: string
  name: string
  description: string
}

const formRef = ref<FormInstance>()

const form = reactive<RoleForm>({ code: '', name: '', description: '' })

const isEdit = computed(() => props.mode === 'edit')
/** 内置角色连名字都不建议改（审计里靠它认人），这里只锁 code，name 放开给业务改名 */
const codeLocked = computed(() => isEdit.value || Boolean(props.initial?.is_builtin))

const rules: FormRules<RoleForm> = {
  code: [
    { required: true, message: '请输入角色编码', trigger: 'blur' },
    { max: 64, message: '编码不能超过 64 个字符', trigger: 'blur' },
    // 编码会进 JWT 与日志，限定字符集免掉转义与大小写歧义
    {
      pattern: /^[a-z][a-z0-9_]*$/,
      message: '小写字母开头，仅可含小写字母、数字与下划线',
      trigger: 'blur',
    },
  ],
  name: [{ required: true, message: '请输入角色名称', trigger: 'blur' }],
}

watch(
  () => props.visible,
  (open) => {
    if (!open) return
    formRef.value?.clearValidate()

    if (props.mode === 'edit' && props.initial) {
      form.code = props.initial.code
      form.name = props.initial.name
      form.description = props.initial.description ?? ''
    } else {
      form.code = ''
      form.name = ''
      form.description = ''
    }
  },
)

async function onSubmit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  emit('submit', {
    code: form.code.trim(),
    name: form.name.trim(),
    description: form.description.trim() || null,
  })
}
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    :title="isEdit ? '编辑角色' : '新建角色'"
    width="460px"
    append-to-body
    @update:model-value="emit('update:visible', $event)"
  >
    <el-form
      ref="formRef"
      :model="form"
      :rules="rules"
      label-position="top"
      hide-required-asterisk
    >
      <el-form-item label="角色编码" prop="code">
        <el-input
          v-model="form.code"
          placeholder="如 kb_admin，小写字母开头"
          :disabled="codeLocked"
          maxlength="64"
        />
        <p v-if="codeLocked" class="role-form__tip">
          编码是权限判定的比对键，创建后不可修改。
        </p>
      </el-form-item>

      <el-form-item label="角色名称" prop="name">
        <el-input v-model="form.name" placeholder="如 系统管理员" maxlength="64" />
      </el-form-item>

      <el-form-item label="描述" prop="description">
        <el-input
          v-model="form.description"
          type="textarea"
          :rows="3"
          placeholder="选填，说明该角色的职责范围"
          maxlength="255"
          show-word-limit
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
.role-form__tip {
  margin: 6px 0 0;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--kb-text-muted);
}
</style>
