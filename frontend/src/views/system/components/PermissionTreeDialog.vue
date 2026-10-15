<script setup lang="ts">
import { Info } from 'lucide-vue-next'
import { computed, nextTick, ref, watch } from 'vue'

import { leafPermissionCodes } from '@/stores/org'
import type { PermissionNode } from '@/types/org'

/**
 * 角色功能权限配置（`el-tree` + checkbox）。
 *
 * ## 这里藏着一对经典坑，两边都必须处理（fe-04 §4.3③ 第 4 条）
 *
 * `el-tree` 默认 `check-strictly = false`（父子联动），于是：
 *
 * 1. **提交**若只取 `getCheckedKeys()`，**半选的父节点会丢**。父节点是 `menu` 权限
 *    （控制路由可见性），丢了它 → 用户看不到那个菜单 → 子按钮权限再全也没有入口。
 *    所以必须 `[...getCheckedKeys(), ...getHalfCheckedKeys()]`。
 *
 * 2. **回显**若把父节点的码也塞进 `checkedKeys`，`el-tree` 会**联动勾上它的全部子节点**
 *    —— 权限膨胀，而且管理员看不出来（树上看就是全勾着）。
 *    所以回显只 `setCheckedKeys(叶子码)`，父节点的全选/半选状态交给 `el-tree` 自行推导。
 *
 * ⚠️ `checkedCodes` 为 `null` 表示**后端没有回显源**（PRD §8.1 无
 * `GET /api/org/roles/{id}/permissions`，字典 §9 #75，spec §9-2）。
 * 此时不能假装「这个角色没有任何权限」——那样保存即等于**清空该角色全部权限**。
 * 弹窗会给出警示条，提示提交将覆盖既有配置。
 *
 * ⚠️ 提交到 `PUT /roles/{id}/permissions` 是**全量覆盖**语义。
 */
const props = defineProps<{
  visible: boolean
  roleName: string
  roleId: string
  tree: PermissionNode[]
  /** null = 无回显源（接口缺失） */
  checkedCodes: string[] | null
  loading?: boolean
}>()

const emit = defineEmits<{
  'update:visible': [value: boolean]
  submit: [codes: string[]]
}>()

/** `el-tree` 实例的结构化类型：只声明用到的三个方法，避免 `InstanceType<typeof ElTree>` 的具名导入与自动导入撞名 */
interface TreeApi {
  getCheckedKeys: (leafOnly?: boolean) => string[]
  getHalfCheckedKeys: () => string[]
  setCheckedKeys: (keys: string[], leafOnly?: boolean) => void
}

const TREE_PROPS = { label: 'name', children: 'children' } as const

const treeRef = ref<TreeApi | null>(null)

const unknownState = computed(() => props.checkedCodes === null)

/** 权限树里全部叶子码（回显时的候选：只有叶子才允许被直接 set） */
const allLeafCodes = computed(() => leafPermissionCodes(props.tree))

/** 已授权的码集合。`null` 与空集必须区分开：前者是「不知道」，后者是「确实一个都没有」 */
const checkedSet = computed(() => new Set(props.checkedCodes ?? []))

const checkedCount = computed(() => props.checkedCodes?.length ?? 0)

/**
 * 回显。`el-dialog` 的 body 是首次打开时才挂载的，`treeRef` 可能晚一拍才可用——
 * 拿不到就再等一个 tick 重试（最多 5 次）。
 * 若这里静默失败，用户会看到一棵**全未勾选**的树 → 以为该角色没权限 → 保存即清空，
 * 所以宁可多转两圈也不能跳过。
 */
async function applyChecked(): Promise<void> {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await nextTick()
    const tree = treeRef.value
    if (!tree) continue

    // 只勾「已授权 ∩ 叶子」。父节点的状态由 el-tree 推导，直接塞父节点会全选其子树
    tree.setCheckedKeys(allLeafCodes.value.filter((code) => checkedSet.value.has(code)))
    return
  }
}

/**
 * 同时盯 `visible` 与 `roleId`：前者管「打开时回显」，后者管「弹窗没关就换了个角色」
 * （`roleId` 存在的意义就在此，不是冗余 prop）。
 * 不做 `:key="roleId"` 强制重建树——那会让 `treeRef` 指向旧实例，回显反而可能落到废弃的树上。
 */
watch(
  () => [props.visible, props.roleId] as const,
  ([open]) => {
    if (!open) return
    void applyChecked()
  },
)

function onSubmit(): void {
  const tree = treeRef.value
  if (!tree) return

  // 半选父节点必须一起提交，否则菜单权限丢失（顶部注释第 1 条）
  const codes = [...tree.getCheckedKeys(), ...tree.getHalfCheckedKeys()]
  emit('submit', codes)
}
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    :title="`配置权限 · ${props.roleName}`"
    width="520px"
    append-to-body
    @update:model-value="emit('update:visible', $event)"
  >
    <div v-if="unknownState" class="perm__warn">
      <Info :size="14" />
      <span>
        当前无法读取该角色已授权的权限（后端未提供回显接口）。树中勾选为空白，
        <b>保存会全量覆盖并清空其现有权限</b>，请谨慎操作。
      </span>
    </div>
    <p v-else class="perm__hint">已勾选 {{ checkedCount ?? 0 }} 项。勾选会自动联动其下级权限。</p>

    <div class="perm__body">
      <el-tree
        ref="treeRef"
        :data="props.tree"
        node-key="code"
        :props="TREE_PROPS"
        show-checkbox
        default-expand-all
        :check-on-click-node="true"
        empty-text="权限树为空，请先由后端维护 sys_permission"
      >
        <template #default="{ data }">
          <span class="pn">
            <span class="pn__name">{{ data.name }}</span>
            <!-- 区分菜单/按钮：menu 决定路由可见性，button 只控制页内动作（字典 §2.1） -->
            <span class="pn__tag" :class="`is-${data.type}`">
              {{ data.type === 'menu' ? '菜单' : '按钮' }}
            </span>
            <span class="pn__code">{{ data.code }}</span>
          </span>
        </template>
      </el-tree>
    </div>

    <template #footer>
      <el-button @click="emit('update:visible', false)">取消</el-button>
      <el-button type="primary" :loading="props.loading" @click="onSubmit">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.perm__hint {
  margin: 0 0 10px;
  font-size: 11.5px;
  color: var(--kb-text-muted);
}

.perm__warn {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 10px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--kb-warning-soft);
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--kb-warning-text);
}

.perm__warn svg {
  flex: none;
  margin-top: 2px;
}

.perm__body {
  max-height: 380px;
  overflow: auto;
  padding: 8px;
  border: 1px solid var(--kb-border);
  border-radius: 8px;
}

.pn {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.pn__name {
  font-size: 12.5px;
  color: var(--kb-text-primary);
}

.pn__tag {
  flex: none;
  padding: 1px 6px;
  border-radius: 3px;
  font-size: 10px;
}

.pn__tag.is-menu {
  background: var(--kb-brand-soft);
  color: var(--kb-brand);
}

.pn__tag.is-button {
  background: var(--kb-bg-subtle);
  color: var(--kb-text-muted);
}

.pn__code {
  font-size: 11px;
  color: var(--kb-text-placeholder);
}
</style>
