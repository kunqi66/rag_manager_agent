<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import type { FormInstance, FormRules } from 'element-plus'

import type { DepartmentNode, DepartmentPayload } from '@/types/org'

/**
 * 部门新增/编辑表单。只做校验 + `emit('submit', payload)`——
 * **不调 store、不发请求、不关弹窗**（发请求与关窗由 `DepartmentPanel` 编排，同登录页约定）。
 *
 * 「移动部门」就是本表单改「上级部门」字段（fe-04 §4.3①）：
 * 项目**不用** `el-tree` 的 `draggable`——拖拽移动会重写整棵子树的 `path`，
 * 影响 D10 的继承语义与检索鉴权，值得一次显式确认而不是一次误拖。
 */
const props = defineProps<{
  visible: boolean
  mode: 'create' | 'edit'
  /** 编辑态的原始节点 */
  initial: DepartmentNode | null
  /** 新建下级时预选的上级部门 */
  defaultParentId: string | null
  tree: DepartmentNode[]
  /**
   * 编辑态需要**禁选**的部门 id（自身 + 全部子孙）。
   * 选中自己的子孙 = 形成环，后端会以 1001 拒绝（be-02 §4.2.1 不变式 I3）；
   * 前端先禁掉是为了少一次无效往返，**不是**安全校验。
   */
  disabledIds: string[]
  loading?: boolean
}>()

const emit = defineEmits<{
  'update:visible': [value: boolean]
  submit: [payload: DepartmentPayload]
}>()

/** `el-tree-select` 的字段映射。`disabled` 是 Element Plus 约定的键名 */
const TREE_SELECT_PROPS = { label: 'name', children: 'children', disabled: 'disabled' } as const

interface DeptOption {
  id: string
  name: string
  disabled: boolean
  children?: DeptOption[]
}

const formRef = ref<FormInstance>()

const form = reactive<DepartmentPayload>({
  name: '',
  parent_id: null,
  sort: 0,
  status: 'active',
})

const rules: FormRules<DepartmentPayload> = {
  name: [
    { required: true, message: '请输入部门名称', trigger: 'blur' },
    { max: 64, message: '部门名称不能超过 64 个字符', trigger: 'blur' },
  ],
}

const disabledSet = computed(() => new Set(props.disabledIds))

/**
 * 复制一份带 `disabled` 标记的选项树。
 * **不能就地改 `props.tree`**——那是 store 里的同一份引用，改了会影响部门树本身的渲染。
 */
function decorate(nodes: DepartmentNode[]): DeptOption[] {
  return nodes.map((node) => ({
    id: node.id,
    name: node.name,
    disabled: disabledSet.value.has(node.id),
    children: node.children?.length ? decorate(node.children) : undefined,
  }))
}

const options = computed(() => decorate(props.tree))

// 每次打开都按当前模式重新填表，避免上一次的残留值串到这一次
watch(
  () => props.visible,
  (open) => {
    if (!open) return
    formRef.value?.clearValidate()

    if (props.mode === 'edit' && props.initial) {
      form.name = props.initial.name
      form.parent_id = props.initial.parent_id
      form.sort = props.initial.sort
      form.status = props.initial.status
    } else {
      form.name = ''
      form.parent_id = props.defaultParentId
      form.sort = 0
      form.status = 'active'
    }
  },
)

async function onSubmit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  emit('submit', {
    name: form.name.trim(),
    parent_id: form.parent_id,
    sort: form.sort,
    status: form.status,
  })
}
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    :title="props.mode === 'edit' ? '编辑部门' : '新建部门'"
    width="440px"
    append-to-body
    @update:model-value="emit('update:visible', $event)"
  >
    <el-form
      ref="formRef"
      class="dept-form"
      :model="form"
      :rules="rules"
      label-position="top"
      hide-required-asterisk
    >
      <el-form-item label="部门名称" prop="name">
        <el-input v-model="form.name" placeholder="请输入部门名称" maxlength="64" />
      </el-form-item>

      <el-form-item label="上级部门" prop="parent_id">
        <el-tree-select
          v-model="form.parent_id"
          class="dept-form__control"
          :data="options"
          node-key="id"
          :props="TREE_SELECT_PROPS"
          check-strictly
          :render-after-expand="false"
          clearable
          placeholder="不选则为根部门"
        />
        <p class="dept-form__tip">改上级即为「移动部门」，会重算整棵子树的路径并影响权限继承。</p>
      </el-form-item>

      <el-form-item label="排序" prop="sort">
        <el-input-number v-model="form.sort" :min="0" :max="9999" controls-position="right" />
      </el-form-item>

      <el-form-item label="状态" prop="status">
        <el-select v-model="form.status" class="dept-form__control">
          <el-option label="启用" value="active" />
          <el-option label="停用" value="disabled" />
        </el-select>
      </el-form-item>
    </el-form>

    <template #footer>
      <el-button @click="emit('update:visible', false)">取消</el-button>
      <el-button type="primary" :loading="props.loading" @click="onSubmit">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.dept-form__control {
  width: 100%;
}

.dept-form__tip {
  margin: 6px 0 0;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--kb-text-muted);
}
</style>
