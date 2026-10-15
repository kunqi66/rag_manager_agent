<script setup lang="ts">
import type { RoleItem } from '@/types/org'

/**
 * 「角色与权限概览」卡片（原型 `zUyFC/RqMwy`：高 196、头 44、行 50）。
 * 纯展示 + 一个「管理权限点 ›」跳转（切到「角色权限」面板）。
 *
 * ⚠️ 分子「已授权 n」依赖 `RoleItem.permission_codes`，该字段的接口来源缺失
 * （fe-04 §10-2 / 字典 §9 #75）——拿不到就显示 `—`，**不猜**。
 * 原型画的权限名 chips（知识导入 / 知识删除…）同理未实现，见 spec §9-16。
 */
const props = defineProps<{
  roles: RoleItem[]
  /** 权限树全量码数：分母 `n / N` 的 N */
  totalPermissions: number
  loading?: boolean
}>()

const emit = defineEmits<{ manage: [] }>()

function permLabel(role: RoleItem): string {
  if (!role.permission_codes) return '—'
  return `${role.permission_codes.length} / ${props.totalPermissions}`
}

function userLabel(role: RoleItem): string {
  return role.user_count === undefined ? '—' : `${role.user_count} 人`
}
</script>

<template>
  <section class="roc">
    <header class="roc__head">
      <h2 class="roc__title">角色与权限概览</h2>
      <button class="roc__link" type="button" @click="emit('manage')">管理权限点 ›</button>
    </header>

    <div class="roc__body">
      <p v-if="props.loading && props.roles.length === 0" class="roc__empty">加载中…</p>
      <p v-else-if="props.roles.length === 0" class="roc__empty">暂无角色</p>

      <div v-for="role in props.roles" :key="role.id" class="roc__row">
        <!-- 色点：内置角色用警告色，普通角色用中性色（原型里系统管理员是红点） -->
        <span class="roc__dot" :class="{ 'is-builtin': role.is_builtin }" />
        <span class="roc__name">{{ role.name }}</span>
        <span class="roc__perm">{{ permLabel(role) }}</span>
        <span class="roc__users">{{ userLabel(role) }}</span>
        <span v-if="role.is_builtin" class="roc__chip">内置</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 原型 RoleCard/RqMwy：高 196、圆角 10、描边 1 */
.roc {
  flex: none;
  height: 196px;
  display: flex;
  flex-direction: column;
  background: var(--kb-bg-card);
  border: 1px solid var(--kb-border);
  border-radius: 10px;
  overflow: hidden;
}

.roc__head {
  flex: none;
  height: 44px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 16px;
  border-bottom: 1px solid var(--kb-border);
}

.roc__title {
  flex: 1 1 auto;
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--kb-text-primary);
}

.roc__link {
  flex: none;
  padding: 0;
  border: 0;
  background: transparent;
  font-family: inherit;
  font-size: 11.5px;
  color: var(--kb-brand);
  cursor: pointer;
}

.roc__body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
}

.roc__empty {
  margin: 16px;
  font-size: 11.5px;
  color: var(--kb-text-muted);
}

/* 原型 RoleRow：高 50、gap 12、内边距 16 */
.roc__row {
  height: 50px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 16px;
  border-bottom: 1px solid var(--kb-border-soft);
}

.roc__dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 4px;
  background: var(--kb-text-secondary);
}

.roc__dot.is-builtin {
  background: var(--kb-danger);
}

.roc__name {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--kb-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.roc__perm {
  flex: none;
  font-size: 12px;
  color: var(--kb-text-secondary);
}

.roc__users {
  flex: none;
  font-size: 12px;
  color: var(--kb-text-muted);
}

.roc__chip {
  flex: none;
  padding: 2px 8px;
  border-radius: 4px;
  background: var(--kb-warning-soft);
  font-size: 10.5px;
  color: var(--kb-warning-text);
}
</style>
