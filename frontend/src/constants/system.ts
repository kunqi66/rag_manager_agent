/**
 * 组织架构与系统配置页（fe-04）的常量。
 *
 * 常量集中在此而不是散在组件里：CLAUDE.md「常量进配置」，
 * 且权限码与分页这类值评审后一定会调，散落会漏改。
 */

/**
 * 分段子导航（原型 `zUyFC/SubTabs/Y55ZC` 的四个 Seg，文案逐字对齐）。
 * `key` 同时用作懒挂载的 `v-if` 判断与面板 loading 的键。
 */
export const PANELS = [
  { key: 'department', label: '部门管理' },
  { key: 'user', label: '用户管理' },
  { key: 'role', label: '角色权限' },
  { key: 'model', label: '模型配置' },
] as const

export type PanelKey = (typeof PANELS)[number]['key']

/**
 * 功能权限码。**必须与后端 `require_permission` 的入参、与 `sys_permission` 表逐字一致**——
 * 不一致就会「按钮永远不显示」或「按钮显示但接口 403」（fe-04 §3）。
 *
 * ✅ **已冻结（2026-10-14）**：`create` / `update` / `delete` 展开写法已拍板，
 * 通配码 `org:user:*` 等**作废**（字典 §5.6；原 §9 #8、#34）。
 * 共 15 条（`org:*` 13 + `stats:*` 2），与 `backend/scripts/seed_org.py` 的
 * `PERMISSIONS` 一一对应。⚠️ 本节**只放 `org:*` 与 `stats:*`** ——
 * `kb:*` 在 `constants/kb.ts` 的 `KB_PERMS`，别把两族的条数混在一起数。
 *
 * ⚠️ 权限码是前端常量与后端种子之间的**隐式契约**：两边各写各的码名，
 * 后果是按钮对**所有人（含 `sys_admin`）静默不可见**，且前后端都不报错。
 * 改这里务必同改种子脚本并重灌——这就是把这些串集中定义的原因。
 */
export const PERM = {
  deptCreate: 'org:department:create',
  deptUpdate: 'org:department:update',
  deptDelete: 'org:department:delete',

  userList: 'org:user:list',
  userCreate: 'org:user:create',
  userUpdate: 'org:user:update',
  userDelete: 'org:user:delete',
  userGrant: 'org:user:grant',

  roleList: 'org:role:list',
  roleCreate: 'org:role:create',
  roleUpdate: 'org:role:update',
  roleDelete: 'org:role:delete',
  roleGrant: 'org:role:grant',

  // 运营看板（2026-10-14 冻结，字典 §5.6.2 / §5.6.3）。
  // ⚠️ 两者对 `kb_admin` 的分配**刻意不同**：给看板、不给审计 ——
  // 看板是聚合后的运营数据；审计是逐条原始操作轨迹。
  statsDashboardView: 'stats:dashboard:view',
  statsAuditList: 'stats:audit:list',
} as const

/** 用户表格分页默认值（与后端 `{list,total,page,page_size}` 对齐） */
export const DEFAULT_PAGE_SIZE = 20
/** 不加 `as const`：`el-pagination` 的 `page-sizes` 要的是可变 `number[]`，只读元组传不进去 */
export const PAGE_SIZE_OPTIONS: number[] = [10, 20, 50, 100]

/** `el-tree` 的字段映射：节点数据用 `name` / `children`（字典 §2.1 的字段名） */
export const DEPT_TREE_PROPS = { label: 'name', children: 'children' } as const

/**
 * 「授权部门自动覆盖其子孙部门」提示，**在两个面板都常驻**（fe-04 §4.3① 第 6 条）。
 * 原型 `zUyFC/EZbfO/TreeFoot/Cj79S` 原文。
 */
export const DEPT_INHERIT_HINT = '授权部门自动覆盖其子孙部门'

/** 「数据权限范围」列的悬停说明。必须与「功能权限」区分开（PRD §3.3 二者正交） */
export const DATA_SCOPE_HINT =
  '有效部门集合 = 该部门 + 其全部祖先，用于 AI 问答的数据权限判定；这不是功能权限。'

/** 「数据权限范围」列在无部门归属（如超级管理员）时的展示 */
export const GLOBAL_SCOPE_LABEL = '全局'

/** 「含下级」后缀（原型 `ScopeText` 原文：「财务部（含下级）」） */
export const SCOPE_WITH_CHILDREN_SUFFIX = '（含下级）'

/**
 * 删除类文案集中在常量里。
 * 🟡 若字典 §9 #4 拍板「软删除」，此处文案要整体换成「停用/移除」（fe-04 §10-4）——
 * 集中一处就是为了那时只改这里。
 */
export const DELETE_TEXT = {
  departmentTitle: '删除部门',
  userTitle: '删除用户',
  roleTitle: '删除角色',
  /** 409/3002 时的兜底提示；具体原因以**后端 message 原文**为准（fe-04 §9-9 不前端预判） */
  conflictFallback: '该对象仍被引用，无法删除',
} as const

/**
 * 模型配置面板的只读展示项。取自 PRD §0 决策摘要（D4 / D5 / D17）。
 * **这些不是可编辑配置**——全项目无模型配置接口（fe-04 §10-1 / 字典 §9 #73）。
 * 服务地址一律不展示真实值，密钥更不经前端（PRD §11.2）。
 */
export const MODEL_INFO = [
  { label: '生成模型', value: 'DeepSeek deepseek-chat', note: 'PRD D4' },
  { label: '向量模型', value: 'BAAI/bge-base-zh-v1.5（768 维）', note: 'PRD D5' },
  { label: 'Embedding 服务地址', value: '由服务端环境变量配置', note: 'PRD D17 / D15' },
  { label: '解析服务', value: 'MinerU 官方 API', note: 'PRD D17' },
] as const

export const MODEL_READONLY_NOTE =
  '当前为只读展示。模型与阈值配置改动需修改服务端环境变量后重启服务——密钥自检失败会拒绝启动，这是刻意设计（PRD §11.2）。'
