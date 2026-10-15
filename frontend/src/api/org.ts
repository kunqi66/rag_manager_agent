import { del, get, post, put } from './http'

import type { PageResult } from '@/types/api'
import type {
  DepartmentNode,
  DepartmentPayload,
  PermissionNode,
  RoleItem,
  RolePayload,
  UserItem,
  UserPayload,
  UserQuery,
} from '@/types/org'

/**
 * `/api/org/*` 请求封装（PRD §8.1，契约见 be-02 §3.1）。
 *
 * 约定：
 * 1. 组件与 store **不直接 import axios**，一律经这里（CLAUDE.md 前端规范）。
 * 2. 路径里的 id 一律 `encodeURIComponent` —— id 由后端生成、理论上安全，
 *    但拼进 URL 的路径段属于「用户可控输入」，编码一次是零成本的纵深防御（红线 8 的精神）。
 * 3. **不存在** `GET /api/org/roles/{id}/permissions`：PRD §8.1 只定义了 PUT，
 *    回显源缺失（fe-04 §10-2 / 字典 §9 #75）。故此处不提供该方法——
 *    角色已授权限改从 `RoleItem.permission_codes` 取。后端补齐前不要加。
 */

/** URL 路径段编码。id 为空时由调用方保证不会走到这里 */
function seg(id: string): string {
  return encodeURIComponent(id)
}

export const orgApi = {
  /* ---- 部门 ---- */

  deptTree: () => get<DepartmentNode[]>('/api/org/departments/tree'),

  createDept: (payload: DepartmentPayload) =>
    post<DepartmentNode>('/api/org/departments', payload),

  updateDept: (id: string, payload: Partial<DepartmentPayload>) =>
    put<DepartmentNode>(`/api/org/departments/${seg(id)}`, payload),

  deleteDept: (id: string) => del<null>(`/api/org/departments/${seg(id)}`),

  /* ---- 用户 ---- */

  users: (query: UserQuery) =>
    get<PageResult<UserItem>>('/api/org/users', {
      page: query.page,
      page_size: query.page_size,
      keyword: query.keyword || undefined,
      department_id: query.department_id ?? undefined,
      include_children: query.include_children,
    }),

  createUser: (payload: UserPayload) => post<UserItem>('/api/org/users', payload),

  // 编辑不带 password：改密是另一条（尚未定义的）路径，见 spec §9-5
  updateUser: (id: string, payload: Omit<UserPayload, 'password'>) =>
    put<UserItem>(`/api/org/users/${seg(id)}`, payload),

  deleteUser: (id: string) => del<null>(`/api/org/users/${seg(id)}`),

  /** 分配角色。**全量覆盖**语义（与 be-02 §3.1 一致），不是增量追加 */
  assignRoles: (id: string, roleIds: string[]) =>
    put<null>(`/api/org/users/${seg(id)}/roles`, { role_ids: roleIds }),

  /* ---- 角色 ---- */

  roles: () => get<RoleItem[]>('/api/org/roles'),

  createRole: (payload: RolePayload) => post<RoleItem>('/api/org/roles', payload),

  updateRole: (id: string, payload: RolePayload) =>
    put<RoleItem>(`/api/org/roles/${seg(id)}`, payload),

  deleteRole: (id: string) => del<null>(`/api/org/roles/${seg(id)}`),

  /* ---- 权限 ---- */

  permissionTree: () => get<PermissionNode[]>('/api/org/permissions/tree'),

  /** 角色授权。**全量覆盖**：提交的 `codes` 即该角色的最终权限集合 */
  grantRolePermissions: (id: string, codes: string[]) =>
    put<null>(`/api/org/roles/${seg(id)}/permissions`, { codes }),
}
