/**
 * 组织架构相关 DTO。**fe-02（权限弹窗的部门/角色/用户选项）与 fe-04（组织管理）共用**
 * （fe-02 模块概要设计 §5 指定放这里，避免两页各定义一套「部门」）。
 *
 * ✅ **2026-10-14 已回核**：be-02 已实现（`api/org_router.py` + `service/org_service.py` +
 *    `schemas/org.py`），本文件按**实际响应 DTO** 校对过。原先挂着的两处，逐条销账：
 *    1. ✅ ID 确实是 **VARCHAR 字符串**（字典 §2.1）—— 前端全按 `string` 收，**一致**。
 *    2. ✅ `status` 后端 `UserItem.status` 是 `str`、默认 `active`；种子数据只用
 *       `active` / `disabled` ⇒ 前端 `UserStatus` 的收窄**与数据一致**。
 *
 * 🔴 本次回核改掉一个**恒为 undefined 的幽灵字段**：`UserItem.roles`。
 *    后端发的是 **`role_ids: string[]`**（只有 id，没有名字），见 `UserItem` 的注释。
 */

/** sys_department.status —— 字典标 🟡，先按建议值收 */
export type DepartmentStatus = 'active' | 'disabled'

/** sys_user.status —— 字典标 🟡（§9 #5 提出是否含 locked），锁定态走 Redis 不入枚举 */
export type UserStatus = 'active' | 'disabled'

/**
 * 部门树节点（`GET /api/org/departments/tree` 的元素）。
 *
 * `path` 是物化路径（形如 `/1/4/9/`），**前端不做继承推导**——
 * 有效部门集合（自身 + 全部祖先）由后端算好经 `/api/auth/me` 下发（PRD D10）。
 * 前端只在需要「展示层级 / 判断祖先关系」时读它。
 */
export interface DepartmentNode {
  id: string
  name: string
  /** 根节点为 null */
  parent_id: string | null
  path: string
  sort: number
  status: DepartmentStatus
  /** 树接口返回；扁平接口无此字段 */
  children?: DepartmentNode[]
}

/** 角色（`GET /api/org/roles` 的元素，权限弹窗与角色管理共用） */
export interface RoleBrief {
  id: string
  /** 角色编码，如 `kb_admin`。**权限判定用 code，不用 name** */
  code: string
  name: string
  description: string | null
  /** 内置角色不可删除 */
  is_builtin: boolean
}

/** 用户摘要（`GET /api/org/users` 的元素，权限弹窗的「指定用户」选择器用） */
export interface UserBrief {
  id: string
  username: string
  real_name: string
  department_id: string | null
  /** 列表接口顺带回的部门名，用于下拉里消歧（同名的人靠部门区分）。可空但**恒在** */
  department_name: string | null
  status: UserStatus
}

/* ==========================================================================================
 * 以下为 **fe-04 组织架构与系统配置页** 追加（2026-10-14）。
 * 既有类型一律不改写——fe-02 同时在消费上面的 `DepartmentNode` / `RoleBrief` / `UserBrief`。
 * ======================================================================================== */

/** 功能权限节点的类型（字典 §2.1 `sys_permission.type`）：菜单控制路由可见性，按钮控制页内动作 */
export type PermissionType = 'menu' | 'button'

/**
 * 功能权限树节点（`GET /api/org/permissions/tree` 的元素，字典 §2.1 `sys_permission`）。
 *
 * ⚠️ 是**全量**权限树（渲染可选项用），**不是**某角色已授权的码集合——
 * 后者在 PRD §8.1 无对应接口（fe-04 §10-2 / 字典 §9 #75）。
 * `code` 是全站 `v-permission` 的比对键，改名会让所有页面的权限判定失效。
 */
export interface PermissionNode {
  id: string
  parent_id: string | null
  code: string
  name: string
  type: PermissionType
  /** `menu` 类型对应的前端路由；`button` 为 null */
  path: string | null
  sort: number
  children?: PermissionNode[]
}

/**
 * 用户分页列表元素（`GET /api/org/users`）。
 * 命名对齐 be-02 §3.1 的响应 DTO `UserItem`——**不叫 `UserDetail`**，
 * 因为 `src/types/auth.ts` 的 `/api/auth/me` 已占用该名（两者字段不同，同名会误用）。
 *
 * ⚠️ **不定义 `password_hash`**：字典 §2.1 标记为「严禁明文/回传」，
 * 从类型层面杜绝它被消费（CLAUDE.md 红线 3）。
 */
export interface UserItem {
  id: string
  username: string
  real_name: string
  department_id: string | null
  /** 列表接口回带的部门名（消歧用）。后端 `UserItem.department_name` 可空但**恒在** */
  department_name: string | null
  email: string | null
  status: UserStatus
  last_login_at: string | null
  created_at: string

  /**
   * 该用户持有的**角色 ID 列表**（后端 `UserItem.role_ids`）。
   *
   * ✅ **2026-10-14 定案**（原 spec §9-2 的「后端是否回带角色」已销）：
   * 后端回的是 **id 数组**，不是角色对象。原先这里写的是 `roles?: RoleBrief[]`，
   * 那个字段**后端从来没发过** ⇒ 恒为 `undefined`。
   *
   * 🔴 后果是**静默的**（两处都只表现为「没数据」，不报错）：
   *   ① 用户表的「角色」列**永远是空的**；
   *   ② 分配角色弹窗的回显恒为「未知」，于是那个「未知 ≠ 空」的警示条**永远挂着**。
   * 名字要自己用 `org.roles`（已加载的角色列表）按 id 查 —— 见 `UserPanel.vue`。
   */
  role_ids: string[]
}

/**
 * 角色列表元素（`GET /api/org/roles`）。命名对齐 be-02 §3.1 的 `RoleItem`。
 * 继承 `RoleBrief`——fe-02 的权限弹窗按 `RoleBrief` 消费同一份数据，字段完全兼容。
 */
export interface RoleItem extends RoleBrief {
  /**
   * 该角色**已授权**的权限码。用于权限弹窗回显。
   * 🟡 PRD §8.1 无 `GET /roles/{id}/permissions`（字典 §9 #75）；若 be-02 选择内联返回，
   * 就是这个字段；若两处都没有，弹窗将无法回显 —— 见 P1-04-fe-system spec §9-2。
   */
  permission_codes?: string[]
  /** 持有该角色的用户数（「角色与权限概览」展示「N 人」） */
  user_count?: number
}

/** `GET /api/org/users` 的查询参数（分页 + 筛选） */
export interface UserQuery {
  page: number
  page_size: number
  keyword: string
  /** 部门筛选。null = 不按部门过滤 */
  department_id: string | null
  /**
   * 是否含子部门。默认 true（贴 D10 继承语义）。
   * 🟡 be-02 对该参数的语义尚未拍板（fe-04 §10-3）——前端只传参，**不做本地过滤**。
   */
  include_children: boolean
}

/* ---- 写接口的请求负载（表单 → api） ---- */

/** `POST/PUT /api/org/departments` 请求体（字典 §2.1 `sys_department`） */
export interface DepartmentPayload {
  name: string
  /** 根部门为 null */
  parent_id: string | null
  sort: number
  status: DepartmentStatus
}

/**
 * `POST/PUT /api/org/users` 请求体。
 *
 * ⚠️ 不含任何身份字段（`operator_id` / `created_by`）——操作人由后端从 JWT 取（红线 1）。
 * ⚠️ `password` **只在新建时**发送，编辑态不带；前端不做任何哈希（红线 3）。
 */
export interface UserPayload {
  username: string
  real_name: string
  department_id: string | null
  email: string | null
  status: UserStatus
  /** 仅新建时存在。明文提交给后端，由 `pwdlib[argon2]` 哈希 */
  password?: string
}

/** `POST/PUT /api/org/roles` 请求体（字典 §2.1 `sys_role`） */
export interface RolePayload {
  /** 角色编码，全局唯一；权限判定用 code 不用 name */
  code: string
  name: string
  description: string | null
}
