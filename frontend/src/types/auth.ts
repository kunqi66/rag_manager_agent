/**
 * 认证相关 DTO。与后端 `backend/schemas/auth.py` 一一对应（CLAUDE.md 前端规范）。
 *
 * ✅ **已对齐（2026-10-14）**：be-01 认证已实现，本文件已逐字段对照
 *    `backend/schemas/auth.py` 核对过。`TokenPair` 的蛇形字段名、`depts` 的
 *    「含祖先」口径均确认无误（P1-01-fe-login §9-7 的待办已销）。
 *    同日再补 `dept_names`（be-01 spec §10-86 / 字典 §9 #86）——
 *    `UserDetail` 字段集至此与后端**完全一致**。
 *
 * ⚠️ 后端字段名是 `perms`，**不是** `permissions`。上一轮这里是按字典推定的
 *    `permissions`，与后端不符。store 内部的 state 字段仍叫 `permissions`
 *    （那是前端本地命名，不跨进程），但**读 DTO 时必须用 `perms`** ——
 *    写错 TypeScript 也不报错（接口上是可选地取不到），只会静默拿到 `undefined`，
 *    表现为所有按钮消失、菜单不显。
 */

/** POST /api/auth/login 请求体 */
export interface LoginRequest {
  username: string
  password: string
}

/** access + refresh 令牌对（POST /api/auth/login、POST /api/auth/refresh 的 data） */
export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  /** access 有效期（秒）。PRD §8.1：access 30 分钟 */
  expires_in: number
}

/**
 * 部门简要信息（`UserDetail.department`）。
 * 对应后端 `backend/schemas/auth.py::DepartmentBrief`。
 * 只够显示用（PRD §8.1 的 `/api/auth/me` 只回这三个字段），
 * 要完整部门数据走 `types/org.ts` 的 `DepartmentNode`。
 */
export interface DepartmentBrief {
  id: string
  name: string
  /** 物化路径，形如 `/dept_01/dept_02/` */
  path: string
}

/** GET /api/auth/me 的 data —— 当前登录用户的身份与权限快照 */
export interface UserDetail {
  /**
   * ⚠️ 是 **string**：`sys_user.id` 字典 §2.1 定为 VARCHAR（与 `sys_department.id` 同）。
   * 上一轮误写成 `number`，2026-10-14 生成 fe-04 时按字典纠正。
   * 注意本文件里的 `UserDetail`（`/api/auth/me`）与 `types/org.ts` 的 `UserItem`
   * （`/api/org/users`）是**两个不同的 DTO**，字段并不相同，不要互相套用。
   */
  user_id: string
  username: string
  real_name: string
  email: string | null
  /**
   * 所属部门（**自身，不含祖先**）。
   *
   * 🔴 **不要拿它当数据权限范围**：那要用下面的 `depts`。两者在后端是
   * 并存且**不可互替**的（`schemas/auth.py::UserDetail` 的注释里写死了这条），
   * 混用会导致「只看得到本部门、看不到祖先部门的知识」这类漏权。
   *
   * 无部门归属时为 `null`（如超级管理员），展示走 `constants/system.ts` 的 `GLOBAL_SCOPE_LABEL`。
   */
  department: DepartmentBrief | null
  /**
   * 有效部门集合 = 自身部门 + 全部祖先（部门权限向下继承，PRD D10）。
   * 由后端算好返回，**前端不做继承推导**——两处各算一遍必然算岔。
   *
   * ⚠️ 是 **string**：`sys_department.id` 字典 §2.1 定为 VARCHAR。
   * 上一轮此处误写成 `number[]`，2026-10-14 生成 fe-02/fe-04 时按字典纠正。
   */
  depts: string[]
  /**
   * 部门名，**与 `depts` 同序同长**（`dept_names[i]` 是 `depts[i]` 的名字，
   * 根在前、自身在后）。**显示用**。
   *
   * 它的存在只有一个理由：侧栏副标题与检索范围 chip 要显示「财务部 · 成本组」
   * = **父级 + 自身**两段名字。`department` 只有自身一级、`depts` 只有 id 没名字，
   * 两者都凑不出父级那一级（`P1-00-fe-app-shell` §9-4 / `P1-03-fe-chat` §9-7）。
   *
   * 🔴 **取末两段的路由不要在这里重复实现** —— 用 `stores/auth.ts` 的 `deptLabel`
   * getter（`deptNames.slice(-2).join(' · ')`）。侧栏与 chip 两处共用它一份，
   * 各写一遍必然有一天只改一处，两处显示不一样的部门名。
   *
   * 后端保证与 `depts` 长度一致（缺失部门回退成 id），但**不要依赖它做 ACL 判断**。
   */
  dept_names: string[]
  /** 角色 ID 集合 */
  roles: string[]
  /**
   * 功能权限码集合，驱动 `v-permission` 与菜单显隐。
   *
   * ⚠️ 字段名是 `perms`（后端原名），不是 `permissions`。
   * 取值见 `constants/system.ts` 的 `PERM` —— 13 个 `org:*` 码，字典 §5.6 已冻结。
   */
  perms: string[]
}
