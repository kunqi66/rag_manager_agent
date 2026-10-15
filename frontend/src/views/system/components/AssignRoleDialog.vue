<script setup lang="ts">
import { Info } from 'lucide-vue-next'
import { computed, ref, watch } from 'vue'

import type { RoleItem } from '@/types/org'

/**
 * 分配角色弹窗。**全量覆盖**语义（`PUT /api/org/users/{id}/roles`）——
 * 提交的数组即该用户的最终角色集合，不是增量追加（fe-04 §4.3② 第 4 条）。
 *
 * ⚠️ `initialRoleIds` 为 `null` 表示**当前角色未知**（拿不到 `UserItem.role_ids`）。
 * 此时不能假装「他没角色」——那样保存就等于把所有角色清空。
 * 弹窗顶部会给出警示条，让管理员知道这次提交的后果。同 spec §9-2 的处理纪律。
 *
 * ✅ 2026-10-14：后端**恒回** `role_ids`（`schemas/org.py` 的 `UserItem`），
 * 所以正常情况下这个分支走不到，警示条只会在异常时才出现 —— 这正是它该有的样子。
 */
const props = defineProps<{
  visible: boolean
  /** 展示用（当前操作的是谁），不做逻辑判断 */
  userName: string
  roles: RoleItem[]
  /** null = 后端未返回当前角色，无法回显 */
  initialRoleIds: string[] | null
  loading?: boolean
}>()

const emit = defineEmits<{
  'update:visible': [value: boolean]
  submit: [roleIds: string[]]
}>()

const selected = ref<string[]>([])

const selectionUnknown = computed(() => props.initialRoleIds === null)

watch(
  () => props.visible,
  (open) => {
    if (!open) return
    selected.value = props.initialRoleIds ? [...props.initialRoleIds] : []
  },
)

function onSubmit(): void {
  emit('submit', [...selected.value])
}
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    :title="`分配角色 · ${props.userName}`"
    width="440px"
    append-to-body
    @update:model-value="emit('update:visible', $event)"
  >
    <div v-if="selectionUnknown" class="assign__warn">
      <Info :size="14" />
      <span>后端未返回该用户的当前角色，下方勾选为空。提交为全量覆盖语义，即清空其现有角色。</span>
    </div>

    <el-select
      v-model="selected"
      class="assign__select"
      multiple
      collapse-tags
      collapse-tags-tooltip
      placeholder="选择角色（可多选）"
    >
      <el-option v-for="role in props.roles" :key="role.id" :label="role.name" :value="role.id">
        <span class="assign__option">{{ role.name }}</span>
        <span class="assign__code">{{ role.code }}</span>
      </el-option>
    </el-select>

    <p class="assign__tip">提交为全量覆盖语义：未勾选的角色会被移除。</p>

    <template #footer>
      <el-button @click="emit('update:visible', false)">取消</el-button>
      <el-button type="primary" :loading="props.loading" @click="onSubmit">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.assign__select {
  width: 100%;
}

.assign__warn {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--kb-warning-soft);
  font-size: 11.5px;
  line-height: 1.6;
  /* 警示底上的正文用 --kb-warning-text（深色版），--kb-warning 在浅橙底上读不清 */
  color: var(--kb-warning-text);
}

.assign__warn svg {
  flex: none;
  margin-top: 2px;
}

.assign__tip {
  margin: 8px 0 0;
  font-size: 11.5px;
  color: var(--kb-text-muted);
}

.assign__option {
  margin-right: 8px;
}

.assign__code {
  font-size: 11px;
  color: var(--kb-text-muted);
}
</style>
