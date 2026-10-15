import { defineStore } from 'pinia'

import { orgApi } from '@/api/org'
import { DEFAULT_PAGE_SIZE } from '@/constants/system'
import type { PanelKey } from '@/constants/system'
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

/* ==========================================================================================
 * 树工具（纯函数，与 store 同文件具名导出）
 *
 * ⚠️ fe-04 §2 把这些放在 `src/utils/tree.ts`，但该文件**不在本页的文件归属内**
 * （fe-02 可能在并行创建）。故暂放此处，评审后若两页都需要，统一收编到 `src/utils/tree.ts`。
 * 见 P1-04-fe-system spec §9-10。
 * ======================================================================================== */

/** 深度优先展开一棵部门树为扁平列表 */
export function flattenDepts(nodes: DepartmentNode[]): DepartmentNode[] {
  const out: DepartmentNode[] = []
  const walk = (list: DepartmentNode[]): void => {
    for (const node of list) {
      out.push(node)
      if (node.children?.length) walk(node.children)
    }
  }
  walk(nodes)
  return out
}

/**
 * 由物化路径 `path` 拼出部门全路径名，如 `集团总部 / 财务部 / 成本组`。
 *
 * 为什么用 `path` 而不是递归找父节点：`path` 就是「从根到自身的 id 链」
 * （字典 §2.1 / be-02 §4.2.1 的 I2 前缀性），一次 split 即可，与后端算 `effective_depts` 同源。
 */
export function deptPathOf(nodes: DepartmentNode[], id: string | null): string | null {
  if (!id) return null
  const map = new Map<string, DepartmentNode>()
  for (const node of flattenDepts(nodes)) map.set(node.id, node)

  const target = map.get(id)
  if (!target) return null

  const names = target.path
    .split('/')
    .filter(Boolean)
    .map((deptId) => map.get(deptId)?.name)
    .filter((name): name is string => typeof name === 'string')

  return names.length > 0 ? names.join(' / ') : target.name
}

/**
 * 某部门的子树 id 集合（**含自身**）。
 * 用途：部门表单里把自身与全部子孙置为不可选——选中即形成环，后端会报 1001（be-02 §4.2.1 I3）。
 * 前端先禁掉是为了少一次无效往返，**不是**安全校验。
 */
export function deptSubtreeIdSet(nodes: DepartmentNode[], id: string): Set<string> {
  const flat = flattenDepts(nodes)
  const self = flat.find((node) => node.id === id)
  if (!self) return new Set<string>()

  const ids = new Set<string>()
  for (const node of flat) {
    // path 首尾带斜杠，所以前缀匹配不会误命中兄弟节点（/1/4/9/ 不会命中 /1/4/90/）
    if (node.path.startsWith(self.path)) ids.add(node.id)
  }
  return ids
}

/** 权限树的全部权限码（用于「已授权 n / 共 N」的分母） */
export function collectPermissionCodes(nodes: PermissionNode[]): string[] {
  const out: string[] = []
  const walk = (list: PermissionNode[]): void => {
    for (const node of list) {
      out.push(node.code)
      if (node.children?.length) walk(node.children)
    }
  }
  walk(nodes)
  return out
}

/**
 * 权限树里**叶子节点**的权限码。
 *
 * 回显时必须只 set 叶子码：`el-tree` 默认 `check-strictly = false`，
 * 若把父节点也塞进 `checkedKeys`，会联动勾上它的**全部**子节点 —— 权限膨胀。
 * 只给叶子，让 el-tree 自行推导半选的父节点。
 */
export function leafPermissionCodes(nodes: PermissionNode[]): string[] {
  const out: string[] = []
  const walk = (list: PermissionNode[]): void => {
    for (const node of list) {
      if (node.children?.length) walk(node.children)
      else out.push(node.code)
    }
  }
  walk(nodes)
  return out
}

/* ========================================================================================== */

interface OrgState {
  deptTree: DepartmentNode[]
  /** 扁平表：`el-tree-select` 与路径拼装用，避免每次调用都重新展开 */
  deptFlat: DepartmentNode[]
  /** 当前选中的部门（用户表格的筛选条件）。null = 全部 */
  selectedDeptId: string | null

  users: UserItem[]
  userTotal: number
  userQuery: UserQuery

  roles: RoleItem[]

  permissionTree: PermissionNode[]

  /** 按面板粒度的 loading，避免一个全局 loading 把四个面板绑死 */
  loading: Record<PanelKey, boolean>
}

export const useOrgStore = defineStore('org', {
  state: (): OrgState => ({
    deptTree: [],
    deptFlat: [],
    selectedDeptId: null,
    users: [],
    userTotal: 0,
    userQuery: {
      page: 1,
      page_size: DEFAULT_PAGE_SIZE,
      keyword: '',
      department_id: null,
      // 默认含子孙：D10 的继承语义下，点「财务部」看不到「成本组」的人会很反直觉（spec §9-3）
      include_children: true,
    },
    roles: [],
    permissionTree: [],
    loading: { department: false, user: false, role: false, model: false },
  }),

  getters: {
    /** 部门全路径名（面包屑 / 数据权限范围列的悬停说明） */
    deptPath: (state) => (id: string | null) => deptPathOf(state.deptTree, id),

    /** 是否有子部门——「数据权限范围」列据此显示「（含下级）」（原型 `ScopeText`） */
    deptHasChildren: (state) => (id: string | null) =>
      Boolean(id && state.deptFlat.find((node) => node.id === id)?.children?.length),

    /**
     * 角色 id → 角色名。
     *
     * 🔴 **为什么需要这个 getter**：后端 `UserItem` 只回 **`role_ids: string[]`**
     * （不回角色对象），而用户表的「角色」列要显示**名字**。
     * 名字在 `state.roles`（`GET /api/org/roles`）里，两边靠 id 关联。
     *
     * 收在这里而不是写在 `UserPanel.vue` 里：角色列表归本 store 持有，
     * 谁要显示角色名都得走这一份（「同一个语义只准有一份实现」）。
     *
     * 查不到的 id **回退成 id 本身**而不是丢弃 —— 角色被删而用户还引用着它时，
     * 显示 `role_x1y2` 至少能让人看出「这里有个悬空引用」；直接过滤掉会让
     * 计数（`「角色」列只显示第一个 + n`）悄悄少一个。
     */
    roleNames: (state) => (ids: readonly string[] | null | undefined) =>
      (ids ?? []).map((id) => state.roles.find((role) => role.id === id)?.name ?? id),

    /** 权限树全量码的数量：角色概览「n / N」的分母 */
    permissionCodeCount: (state) => collectPermissionCodes(state.permissionTree).length,
  },

  actions: {
    /* ---- 部门 ---- */

    async fetchDeptTree(): Promise<void> {
      this.loading.department = true
      try {
        const tree = await orgApi.deptTree()
        this.deptTree = tree
        this.deptFlat = flattenDepts(tree)
      } finally {
        this.loading.department = false
      }
    },

    async createDept(payload: DepartmentPayload): Promise<void> {
      await orgApi.createDept(payload)
      await this.fetchDeptTree()
    },

    async updateDept(id: string, payload: Partial<DepartmentPayload>): Promise<void> {
      await orgApi.updateDept(id, payload)
      // 移动部门会重写整棵子树的 path，前端这份树必然过期 → 必须重拉
      await this.fetchDeptTree()
    },

    async deleteDept(id: string): Promise<void> {
      await orgApi.deleteDept(id)
      if (this.selectedDeptId === id) this.selectedDeptId = null
      await this.fetchDeptTree()
    },

    /** 点部门树节点：更新筛选条件并回到第 1 页 */
    selectDept(id: string | null): void {
      this.selectedDeptId = id
      this.userQuery.department_id = id
      this.userQuery.page = 1
    },

    /* ---- 用户 ---- */

    async fetchUsers(): Promise<void> {
      this.loading.user = true
      try {
        const page = await orgApi.users(this.userQuery)
        this.users = page.list
        this.userTotal = page.total
        // 后端可能纠正 page（如越界回退），以响应为准，避免分页器与数据不一致
        this.userQuery.page = page.page
        this.userQuery.page_size = page.page_size
      } finally {
        this.loading.user = false
      }
    },

    /** 合并式更新查询条件。只在需要时显式重置 `page`，避免翻页被无意打断 */
    async patchUserQuery(patch: Partial<UserQuery>): Promise<void> {
      this.userQuery = { ...this.userQuery, ...patch }
      await this.fetchUsers()
    },

    async createUser(payload: UserPayload): Promise<void> {
      await orgApi.createUser(payload)
      await this.fetchUsers()
    },

    async updateUser(id: string, payload: Omit<UserPayload, 'password'>): Promise<void> {
      await orgApi.updateUser(id, payload)
      await this.fetchUsers()
    },

    async deleteUser(id: string): Promise<void> {
      await orgApi.deleteUser(id)
      await this.fetchUsers()
    },

    async assignRoles(id: string, roleIds: string[]): Promise<void> {
      await orgApi.assignRoles(id, roleIds)
      await this.fetchUsers()
    },

    /* ---- 角色 ---- */

    async fetchRoles(): Promise<void> {
      this.roles = await orgApi.roles()
    },

    async createRole(payload: RolePayload): Promise<void> {
      await orgApi.createRole(payload)
      await this.fetchRoles()
    },

    async updateRole(id: string, payload: RolePayload): Promise<void> {
      await orgApi.updateRole(id, payload)
      await this.fetchRoles()
    },

    async deleteRole(id: string): Promise<void> {
      await orgApi.deleteRole(id)
      await this.fetchRoles()
    },

    /* ---- 权限 ---- */

    async fetchPermissionTree(): Promise<void> {
      this.permissionTree = await orgApi.permissionTree()
    },

    async grantRolePermissions(roleId: string, codes: string[]): Promise<void> {
      await orgApi.grantRolePermissions(roleId, codes)
      // 重拉角色列表：角色概览的「已授权 n」来自 RoleItem.permission_codes，不重拉会显示旧值
      await this.fetchRoles()
    },

    /* ---- 页面初始化 ---- */

    /**
     * 部门树 / 角色 / 权限树三份基础数据并行拉取。
     * 用户列表另算——它依赖 `selectedDeptId`，由 UserPanel 自行触发。
     */
    async bootstrap(): Promise<void> {
      await Promise.all([this.fetchDeptTree(), this.fetchRoles(), this.fetchPermissionTree()])
    },
  },
})
