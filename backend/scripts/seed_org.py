"""组织与权限种子数据：5 部门 / 3 角色 / 20 用户 / 21 权限码。

用法（项目根目录）：
    uv run python -m backend.scripts.seed_org            # 重建并写入
    uv run python -m backend.scripts.seed_org --check    # 只核对现有数据，不写
    uv run python -m backend.scripts.seed_org --password 'YourPassw0rd!'

⚠️ 幂等策略是「先清空这 6 张表再写」——这是**开发种子**，
不是生产迁移。生产环境的数据导入不在本脚本职责内。

═══════════════════════════════════════════════════════════════
权限码的取值纪律（本文件最容易改错的地方）
═══════════════════════════════════════════════════════════════
`PERMISSIONS` 里的 28 条来自 `核心数据字典.md` §5.6，**与前端常量逐字一致**，不要单方面增删。
注意前端常量**分在三个文件**：`org:*` 与 `stats:*` 在 `frontend/src/constants/system.ts` 的 `PERM`，
`kb:*` 在 `frontend/src/constants/kb.ts` 的 `KB_PERMS`（外加 `constants/chat.ts` 的
`CITE_JUMP_PERMISSION` = `kb:document:list`），
`faq:*` 在 `frontend/src/constants/governance.ts` 的 `PERM`。

    org:user:list          org:user:create     org:user:update
    org:user:delete        org:user:grant
    org:role:list          org:role:create     org:role:update
    org:role:delete        org:role:grant
    org:department:create  org:department:update  org:department:delete
    kb:document:list       kb:document:upload  kb:document:update
    kb:document:status     kb:document:delete  kb:permission:update
    stats:dashboard:view   stats:audit:list
    faq:candidate:list     faq:candidate:mine  faq:candidate:publish
    faq:list               faq:cache:update

> ✅ **2026-10-14 拍板：通配写法一律展开为独立码**
> （原 `org:user:*` / `org:role:*` / `org:department:*` 三个通配码**作废**，见字典 §5.6）。
> PRD §8.1 里的 `*` 是「示意多个动作」的简写，**不是字面权限码**。
>
> 早先本脚本按字面种了 `org:user:*`，后果是：前端 `PERM` 的 13 个码里
> **9 个在库里根本不存在** —— 这 9 个按钮对**所有人（含系统管理员）静默不可见**，
> 前端以为后端没实现、后端以为前端没接，两边都自洽、谁都不报错。
> 这正是下面 🔴 那条警告要防的病，当时防住了未冻结的域，却漏在同域的粒度上。

部门一族只有 `create/update/delete` 三个码，**没有 `:list`** ——
部门树接口按 PRD §8.1 只要求「登录」，所以这三个码平铺、无父节点。

知识一族 `kb:*` 已在 2026-10-14 冻结（字典 §5.6.1 / §9 #11），共 6 条；
`kb:document:list` 是 `menu`（`path=/kb/documents`），其余 5 条是它的 `button` 子节点。

统计一族 `stats:*` 已在 2026-10-14 冻结（字典 §5.6.2 / §5.6.3，§9 #46/#49），共 2 条，
**平铺无父节点**（运营看板不是左侧菜单项）。两者对 `kb_admin` 的分配**刻意不同**：
给看板、不给审计 —— 见 `ROLE_PERMISSIONS` 里的理由。

FAQ 一族 `faq:*` 已在 2026-10-14 冻结（字典 §5.6.4 / §9 #61、#78），共 5 条；
`faq:candidate:list` 是 `menu`（`path=/kb/governance`），其余 4 条是它的 `button` 子节点。
🔴 **`faq:candidate:publish` 的码名是拍板结果，不是沿用**：PRD §3.2 逐字写了它，
且 `frontend/src/constants/governance.ts` 的 `PERM.CANDIDATE_PUBLISH` 已按它落地。
字典 §9 #78 曾建议改叫 `faq:candidate:review`，**不采纳** —— 改名就是下面 🔴 那条病的形状。
⚠️ 与 `kb:*` 另一处不同：**两个读列表接口也挂了码**（`faq:candidate:list` / `faq:list`），
不是"登录即可" —— 候选列表含**别人部门**的提问原文（聚类窗口是全局的），
而侧栏不按权限过滤菜单，staff 会真的打开这个页面。见字典 §5.6.4。

缺口一族 `gap:*` 已在 2026-10-14 冻结（字典 §5.6.5 / §9 #68），共 2 条，
**平铺无父节点**：治理中心是 `faq:candidate:list` 这个 `menu` 撑起来的，
缺口 Tab 只是同一页面里的一个分区，**不是独立菜单项**，所以 `gap:*` 不该挂在它下面。
🔴 **两条码名都取自字典 #68 的原建议、一字未改**，且前端 `PERM` 已按同样的名字落地 ——
沿用即不产生下面 🔴 那条病的形状。

🔴 `faq:*` / `kb:*` / `stats:*` / `org:*` **一条都不许出现在别处**，以本节为准 ——
权限码是**前端常量与本种子之间的隐式契约**。两边各写各的码名，后果是
「前端按钮不显示 / 接口永远 403，且不报任何错」这种前后端各自自洽的静默失效。
本项目已经踩过一次（前端 13 个码、库里 7 个、只重合 4 个）。
→ **改码名必须三处同动**：字典 §5.6 + 本文件的 `PERMISSIONS`/`ROLE_PERMISSIONS` + 前端 `PERM`。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.db import SessionLocal, engine
from backend.core.security import hash_password
from backend.models import (
    SysDepartment,
    SysPermission,
    SysRole,
    SysRolePermission,
    SysUser,
    SysUserRole,
)

# ═══════════════════════════════════════════════════════════════
# 部门：5 个，3 层
#   集团总部
#   ├── 财务部
#   │   └── 成本组
#   └── 技术部
#       └── 平台组
#
# 刻意做成 3 层 —— PRD 验收 A4 要求「成本组账号登录后 effective_depts 含三层 id」，
# 2 层的树验证不出「全部祖先」与「只有父级」的区别。
#
# path 是物化路径（PRD §6.1.2）：strip('/').split('/') 即得自身 + 全部祖先，不做递归。
# ═══════════════════════════════════════════════════════════════
DEPARTMENTS: list[dict] = [
    {"id": "dept_01", "name": "集团总部", "parent_id": None, "path": "/dept_01/", "sort": 1},
    {"id": "dept_02", "name": "财务部", "parent_id": "dept_01", "path": "/dept_01/dept_02/", "sort": 1},
    {"id": "dept_03", "name": "技术部", "parent_id": "dept_01", "path": "/dept_01/dept_03/", "sort": 2},
    {"id": "dept_04", "name": "成本组", "parent_id": "dept_02", "path": "/dept_01/dept_02/dept_04/", "sort": 1},
    {"id": "dept_05", "name": "平台组", "parent_id": "dept_03", "path": "/dept_01/dept_03/dept_05/", "sort": 1},
]

# ═══════════════════════════════════════════════════════════════
# 权限：28 条，逐字来自字典 §5.6。改动前先读文件头的「权限码取值纪律」。
#
# parent_id 只指向同样来自 §5.6 的 `:list` 码 —— 部门一族在 §5.6 里没有
# `:list`（部门树按 PRD §8.1 只要求「登录」），所以那三条没有父节点、平铺。
# 菜单层级在本项目里没有定义处，**不发明**。
# ═══════════════════════════════════════════════════════════════
PERMISSIONS: list[dict] = [
    # 用户（5 条：`:list` 是菜单节点，其余四个是它的按钮子节点）
    {"id": "perm_01", "parent_id": None, "code": "org:user:list", "name": "用户管理-查看",
     "type": "menu", "path": "/system", "sort": 10},
    {"id": "perm_02", "parent_id": "perm_01", "code": "org:user:create", "name": "用户管理-新增",
     "type": "button", "path": None, "sort": 11},
    {"id": "perm_03", "parent_id": "perm_01", "code": "org:user:update", "name": "用户管理-编辑",
     "type": "button", "path": None, "sort": 12},
    {"id": "perm_04", "parent_id": "perm_01", "code": "org:user:delete", "name": "用户管理-删除",
     "type": "button", "path": None, "sort": 13},
    {"id": "perm_05", "parent_id": "perm_01", "code": "org:user:grant", "name": "用户管理-分配角色",
     "type": "button", "path": None, "sort": 14},
    # 角色（5 条，同上）
    {"id": "perm_06", "parent_id": None, "code": "org:role:list", "name": "角色管理-查看",
     "type": "menu", "path": "/system", "sort": 20},
    {"id": "perm_07", "parent_id": "perm_06", "code": "org:role:create", "name": "角色管理-新增",
     "type": "button", "path": None, "sort": 21},
    {"id": "perm_08", "parent_id": "perm_06", "code": "org:role:update", "name": "角色管理-编辑",
     "type": "button", "path": None, "sort": 22},
    {"id": "perm_09", "parent_id": "perm_06", "code": "org:role:delete", "name": "角色管理-删除",
     "type": "button", "path": None, "sort": 23},
    {"id": "perm_10", "parent_id": "perm_06", "code": "org:role:grant", "name": "角色管理-授权",
     "type": "button", "path": None, "sort": 24},
    # 部门（§5.6 无 :list 码，三条平铺）
    {"id": "perm_11", "parent_id": None, "code": "org:department:create", "name": "部门管理-新增",
     "type": "button", "path": None, "sort": 30},
    {"id": "perm_12", "parent_id": None, "code": "org:department:update", "name": "部门管理-编辑",
     "type": "button", "path": None, "sort": 31},
    {"id": "perm_13", "parent_id": None, "code": "org:department:delete", "name": "部门管理-删除",
     "type": "button", "path": None, "sort": 32},
    # 知识（6 条，2026-10-14 冻结于字典 §5.6.1 / §9 #11）
    # 形状同用户 / 角色两族：`:list` 是 menu 兼路由入口，其余是它的 button 子节点。
    # `kb:permission:update` 的域是 `kb` 而非 `kb:document` —— PRD §3.2 逐字如此，别「顺手改齐」。
    {"id": "perm_14", "parent_id": None, "code": "kb:document:list", "name": "知识维护与导入-查看",
     "type": "menu", "path": "/kb/documents", "sort": 40},
    {"id": "perm_15", "parent_id": "perm_14", "code": "kb:document:upload", "name": "知识维护与导入-导入",
     "type": "button", "path": None, "sort": 41},
    {"id": "perm_16", "parent_id": "perm_14", "code": "kb:document:update", "name": "知识维护与导入-编辑",
     "type": "button", "path": None, "sort": 42},
    {"id": "perm_17", "parent_id": "perm_14", "code": "kb:document:status", "name": "知识维护与导入-启停",
     "type": "button", "path": None, "sort": 43},
    {"id": "perm_18", "parent_id": "perm_14", "code": "kb:document:delete", "name": "知识维护与导入-删除",
     "type": "button", "path": None, "sort": 44},
    {"id": "perm_19", "parent_id": "perm_14", "code": "kb:permission:update", "name": "知识维护与导入-权限配置",
     "type": "button", "path": None, "sort": 45},
    # 统计（2 条，2026-10-14 冻结于字典 §5.6.2 / §5.6.3）
    # ⚠️ **平铺无父节点**：运营看板不是左侧菜单项，它是顶栏入口下的一个页面，
    #    没有 menu 节点可挂（同 `org:department:*` 的形状）。
    {"id": "perm_20", "parent_id": None, "code": "stats:dashboard:view", "name": "运营看板-查看",
     "type": "button", "path": None, "sort": 50},
    {"id": "perm_21", "parent_id": None, "code": "stats:audit:list", "name": "运营看板-审计流水",
     "type": "button", "path": None, "sort": 51},
    # FAQ 沉淀（5 条，2026-10-14 冻结于字典 §5.6.4 / §9 #61、#78）
    # 形状同知识一族但**父节点是 `faq:candidate:list`**：治理中心（`/kb/governance`）
    # 的入口是「候选审核」Tab，已发布 FAQ 与缓存开关都挂在它下面。
    # 🔴 `faq:candidate:publish` 的码名是拍板结果：PRD §3.2 逐字如此，且前端
    #    `constants/governance.ts` 的 `PERM.CANDIDATE_PUBLISH` 已按它落地。
    #    字典 §9 #78 曾建议改叫 `review`，**不采纳** —— 改名就是「按钮对所有人静默不可见」。
    {"id": "perm_22", "parent_id": None, "code": "faq:candidate:list", "name": "知识沉淀与运营-候选审核",
     "type": "menu", "path": "/kb/governance", "sort": 60},
    {"id": "perm_23", "parent_id": "perm_22", "code": "faq:candidate:mine", "name": "知识沉淀与运营-手动挖掘",
     "type": "button", "path": None, "sort": 61},
    {"id": "perm_24", "parent_id": "perm_22", "code": "faq:candidate:publish", "name": "知识沉淀与运营-发布/驳回",
     "type": "button", "path": None, "sort": 62},
    {"id": "perm_25", "parent_id": "perm_22", "code": "faq:list", "name": "知识沉淀与运营-已发布 FAQ",
     "type": "button", "path": None, "sort": 63},
    {"id": "perm_26", "parent_id": "perm_22", "code": "faq:cache:update", "name": "知识沉淀与运营-缓存开关",
     "type": "button", "path": None, "sort": 64},
    # 知识缺口（2 条，2026-10-14 冻结于字典 §5.6.5 / §9 #68）
    # ⚠️ **平铺无父节点**：缺口的入口是治理中心页面里的一个 Tab，
    #    页面本身已由 `faq:candidate:list` 这个 menu 撑起，没有第二个 menu 可挂。
    #    挂到 `perm_22` 下是**错的**：父节点语义是「这个按钮属于哪个菜单」，
    #    而缺口 Tab 不是菜单项 —— 挂上去会让权限树多出一层不存在的层级。
    {"id": "perm_27", "parent_id": None, "code": "gap:list", "name": "知识缺口-查看",
     "type": "button", "path": None, "sort": 70},
    {"id": "perm_28", "parent_id": None, "code": "gap:task:create", "name": "知识缺口-一键转建",
     "type": "button", "path": None, "sort": 71},
]

# ═══════════════════════════════════════════════════════════════
# 角色：3 个，均内置（is_builtin=true，不可删）
# ═══════════════════════════════════════════════════════════════
ROLES: list[dict] = [
    {"id": "role_01", "code": "sys_admin", "name": "系统管理员",
     "description": "拥有全部组织与权限管理能力", "is_builtin": True},
    {"id": "role_02", "code": "kb_admin", "name": "知识管理员",
     "description": "可查看组织与角色（用于挑选授权对象），不改组织数据", "is_builtin": True},
    {"id": "role_03", "code": "staff", "name": "普通员工",
     "description": "无组织管理权限，仅有知识库访问", "is_builtin": True},
]

# 角色 → 权限。
# kb_admin 在**组织侧**只给两个 `:list`：知识管理员配 kb_document_permission 时要选部门和角色，
# 需要能读，但不需要能改组织数据 —— 这正是最小权限的落点。
# 在**知识侧**则给满 6 条：文档台账归它管（PRD §3.3）。
ROLE_PERMISSIONS: dict[str, list[str]] = {
    # sys_admin：**显式**挂满 28 条。字典 §9 待确认 41 已定「不引入 `*` 魔法值」——
    # 内置管理员也走权限行，这样「谁有哪些权限」在 sys_role_permission 里看得见、可审计。
    "role_01": [
        "perm_01", "perm_02", "perm_03", "perm_04", "perm_05",
        "perm_06", "perm_07", "perm_08", "perm_09", "perm_10",
        "perm_11", "perm_12", "perm_13",
        "perm_14", "perm_15", "perm_16", "perm_17", "perm_18", "perm_19",
        "perm_20", "perm_21",
        "perm_22", "perm_23", "perm_24", "perm_25", "perm_26",
        "perm_27", "perm_28",
    ],
    # kb_admin：组织侧只给用户 / 角色两个 `:list`（perm_01 / perm_06），知识侧给满 6 条。
    # 统计侧给看板（perm_20）**不给审计**（perm_21）—— 字典 §5.6.3 的理由：
    # 看板是聚合后的运营数据、不可回溯到个人；审计是逐条原始操作轨迹。
    # FAQ 侧给满 5 条（perm_22..26）：知识沉淀与运营正是知识管理员的日常职责（PRD §3.3）。
    # 缺口侧给满 2 条（perm_27/28）：与 FAQ 同理 —— 「哪个部门缺哪块知识、要不要补料」
    # 正是知识管理员的日常工作，而不是只管发布 FAQ。
    "role_02": [
        "perm_01", "perm_06",
        "perm_14", "perm_15", "perm_16", "perm_17", "perm_18", "perm_19",
        "perm_20",
        "perm_22", "perm_23", "perm_24", "perm_25", "perm_26",
        "perm_27", "perm_28",
    ],
    # 🔴 staff **一条 kb / faq / gap 码都不给**（字典 §5.6.1 / §5.6.4 / §5.6.5）。
    # 台账接口按 PRD §3.3 对管理员不受数据权限约束、返回全量文档，给了 staff
    # 等于全员可读全部标题（含受限文档）—— 违反 CLAUDE.md 安全红线 4。
    # FAQ 侧同理且更重：候选列表含**别人部门**的提问原文（聚类窗口是全局的），
    # 而 `constants/nav.ts` 的侧栏**不按权限过滤菜单**，staff 会真的打开这个页面。
    # 代价是普通员工点问答引用卡片「只读不跳」，那是 fe-03 §9-4 已定的行为。
    # 若将来要让员工读到自己有权的那几篇，另开一个**按数据权限过滤**的只读端点，
    # 不要放宽本行 —— 一个接口不能同时对管理员不过滤、对员工过滤。
    "role_03": [],
}

# ═══════════════════════════════════════════════════════════════
# 用户：20 个，按部门分布 3 / 3 / 3 / 6 / 5
#
# 几个刻意安排（都对应验收点，别随手改）：
#   u_1010  dept_04 成本组  → A4：登录后 effective_depts 应是
#                             {dept_01, dept_02, dept_04} 三层
#   u_1019  status=disabled → A5：停用账号登录返回 2001，且**不计入失败计数**
#   u_1001  sys_admin       → 权限齐全，用来验证「前端按钮不被 v-permission 藏掉」
#   u_1020  双角色          → 验证 sys_user_role 的多对多
# 除 u_1019 外全部 active。
# ═══════════════════════════════════════════════════════════════
USERS: list[dict] = [
    {"id": "u_1001", "username": "admin", "real_name": "系统管理员", "department_id": "dept_01", "email": "admin@example.com"},
    {"id": "u_1002", "username": "sysadmin2", "real_name": "王管理", "department_id": "dept_01", "email": "sysadmin2@example.com"},
    {"id": "u_1003", "username": "kb_hq", "real_name": "李知识", "department_id": "dept_01", "email": "kb_hq@example.com"},
    {"id": "u_1004", "username": "fin_kb", "real_name": "张财务", "department_id": "dept_02", "email": "fin_kb@example.com"},
    {"id": "u_1005", "username": "chenjing", "real_name": "陈静", "department_id": "dept_02", "email": "chenjing@example.com"},
    {"id": "u_1006", "username": "zhaolei", "real_name": "赵磊", "department_id": "dept_02", "email": "zhaolei@example.com"},
    {"id": "u_1007", "username": "tech_kb", "real_name": "孙技术", "department_id": "dept_03", "email": "tech_kb@example.com"},
    {"id": "u_1008", "username": "zhouqi", "real_name": "周琦", "department_id": "dept_03", "email": "zhouqi@example.com"},
    {"id": "u_1009", "username": "wupeng", "real_name": "吴鹏", "department_id": "dept_03", "email": "wupeng@example.com"},
    {"id": "u_1010", "username": "cost01", "real_name": "郑成本", "department_id": "dept_04", "email": "cost01@example.com"},
    {"id": "u_1011", "username": "cost02", "real_name": "冯核算", "department_id": "dept_04", "email": "cost02@example.com"},
    {"id": "u_1012", "username": "cost03", "real_name": "褚预算", "department_id": "dept_04", "email": "cost03@example.com"},
    {"id": "u_1013", "username": "cost04", "real_name": "卫对账", "department_id": "dept_04", "email": "cost04@example.com"},
    {"id": "u_1014", "username": "cost05", "real_name": "蒋报销", "department_id": "dept_04", "email": "cost05@example.com"},
    {"id": "u_1015", "username": "cost06", "real_name": "沈稽核", "department_id": "dept_04", "email": "cost06@example.com"},
    {"id": "u_1016", "username": "plat01", "real_name": "韩平台", "department_id": "dept_05", "email": "plat01@example.com"},
    {"id": "u_1017", "username": "plat02", "real_name": "杨运维", "department_id": "dept_05", "email": "plat02@example.com"},
    {"id": "u_1018", "username": "plat03", "real_name": "朱开发", "department_id": "dept_05", "email": "plat03@example.com"},
    {"id": "u_1019", "username": "plat04", "real_name": "秦测试", "department_id": "dept_05", "email": "plat04@example.com",
     "status": "disabled"},
    {"id": "u_1020", "username": "plat05", "real_name": "尤发布", "department_id": "dept_05", "email": "plat05@example.com"},
]

# 用户 → 角色。全员都有 staff（基础角色），再按身份叠加：
#   2 个 sys_admin、4 个 kb_admin（u_1020 因此是双角色）
BASE_ROLE = "role_03"
EXTRA_ROLES: dict[str, list[str]] = {
    "u_1001": ["role_01"],
    "u_1002": ["role_01"],
    "u_1003": ["role_02"],
    "u_1004": ["role_02"],
    "u_1007": ["role_02"],
    "u_1020": ["role_02"],
}

DEFAULT_PASSWORD = "Rag@12345"

EXPECTED_COUNTS: dict[str, int] = {
    "sys_department": len(DEPARTMENTS),
    "sys_permission": len(PERMISSIONS),
    "sys_role": len(ROLES),
    "sys_user": len(USERS),
    "sys_user_role": len(USERS) + sum(len(v) for v in EXTRA_ROLES.values()),
    "sys_role_permission": sum(len(v) for v in ROLE_PERMISSIONS.values()),
}


async def _wipe(session: AsyncSession) -> None:
    """按外键反序清空这 6 张表。"""
    for model in (
        SysRolePermission,
        SysUserRole,
        SysUser,
        SysRole,
        SysPermission,
        SysDepartment,
    ):
        await session.execute(delete(model))


async def seed(password: str) -> None:
    async with SessionLocal() as session:
        await _wipe(session)

        session.add_all(SysDepartment(**d) for d in DEPARTMENTS)
        session.add_all(SysPermission(**p) for p in PERMISSIONS)
        session.add_all(SysRole(**r) for r in ROLES)
        # 先 flush 保证被引用的行已在会话里，避免 FK 顺序问题
        await session.flush()

        # 每个用户单独算哈希。argon2 本身带随机 salt，共用一份哈希虽然省时间，
        # 但会让「密码哈希相同」这件事在库里看起来像异常，不值得省这点开销。
        #
        # 🔴 哈希**必须**走 `core/security.hash_password`，不许在这里自己
        # `from pwdlib import PasswordHash`。be-03 §7.2 把 security.py 定为
        # 全项目唯一允许 import pwdlib 的地方；这里再建一个 hasher，
        # 就是「同一个语义两份实现」——将来算法参数一改，种子密码与登录校验
        # 会走不同的参数组合，症状极难定位。
        for u in USERS:
            # status 要先 pop 出来再显式传 —— 直接 `**u` 配 `status=` 的话，
            # 对带 status 键的 u_1019 会重复传关键字，抛 TypeError。
            fields = dict(u)
            status = fields.pop("status", "active")
            session.add(
                SysUser(
                    **fields,
                    password_hash=hash_password(password),
                    status=status,
                    last_login_at=None,
                )
            )
        await session.flush()

        session.add_all(
            SysRolePermission(role_id=rid, permission_id=pid)
            for rid, pids in ROLE_PERMISSIONS.items()
            for pid in pids
        )
        session.add_all(
            SysUserRole(user_id=u["id"], role_id=BASE_ROLE) for u in USERS
        )
        session.add_all(
            SysUserRole(user_id=uid, role_id=rid)
            for uid, rids in EXTRA_ROLES.items()
            for rid in rids
        )

        await session.commit()


async def sync_permissions() -> tuple[int, int]:
    """把 `PERMISSIONS` / `ROLE_PERMISSIONS` 里**库里还缺**的行补进去。只增不删。

    返回 `(新增权限行数, 新增角色-权限关联数)`。

    🔴 **为什么需要这条路径**：`seed()` 是 **wipe + 重灌** —— `_wipe` 会把 6 张表
       清空，`sys_user` / `sys_department` 一起没。于是「只想补 9 条权限码」
       这件事的代价变成了「抹掉所有经接口建的用户和部门」。没人愿意在已经用起来的
       库上跑它，**权限码就一直是旧的**。

       而那种失效的症状是**最安静的一类**：相关接口一律 403、前端按钮一律不显示，
       前后端各自都自洽、控制台干干净净（本文件头部那段「已踩过一次」讲的就是它）。
       实测：库里停在 `perm_19`，`perm_20…perm_28`（`stats:*` / `faq:*` / `gap:*`）
       一条都没有 ⇒ be-06…be-09 的**全部**接口对**所有人**（含 `sys_admin`）403。

    ⚠️ 本函数**不碰** `sys_user` / `sys_user_role` / `sys_department`，也不改已有行的
       内容（改了码名不会自动跟随 —— 那属于「三处同动」的范畴，见下方冲突检查）。
    """
    async with SessionLocal() as session:
        existing = (
            await session.execute(select(SysPermission.id, SysPermission.code))
        ).all()
        code_by_id = {row[0]: row[1] for row in existing}

        # 🔴 同一个 code 落在**不同的 id** 上：说明库里的码名与种子已分叉
        #    （改过码名、或只灌过一半）。这种情况**插入解决不了** ——
        #    补进去会变成同一个权限码两行，而 `require_permission` 按 code 查，
        #    两行都会命中、看起来"好了"，直到有人按 id 删掉其中一行。
        #    宁可在这里停住，让人的眼睛看一眼。
        seed_id_of_code = {p["code"]: p["id"] for p in PERMISSIONS}
        conflicts = sorted(
            (pid, code)
            for pid, code in code_by_id.items()
            if code in seed_id_of_code and seed_id_of_code[code] != pid
        )
        if conflicts:
            raise RuntimeError(
                f"权限码与库中 id 冲突，无法自动同步：{conflicts}。"
                "请人工确认码名后重灌（`python -m backend.scripts.seed_org`）。"
            )

        new_perms = [p for p in PERMISSIONS if p["id"] not in code_by_id]
        if new_perms:
            session.add_all(SysPermission(**p) for p in new_perms)
            await session.flush()

        linked = {
            (row[0], row[1])
            for row in (
                await session.execute(
                    select(SysRolePermission.role_id, SysRolePermission.permission_id)
                )
            ).all()
        }
        new_links = [
            SysRolePermission(role_id=role_id, permission_id=permission_id)
            for role_id, permission_ids in ROLE_PERMISSIONS.items()
            for permission_id in permission_ids
            if (role_id, permission_id) not in linked
        ]
        if new_links:
            session.add_all(new_links)

        await session.commit()
        return len(new_perms), len(new_links)


async def check() -> bool:
    """核对各表行数是否为预期值。返回是否全部吻合。"""
    tables = {
        "sys_department": SysDepartment,
        "sys_permission": SysPermission,
        "sys_role": SysRole,
        "sys_user": SysUser,
        "sys_user_role": SysUserRole,
        "sys_role_permission": SysRolePermission,
    }

    ok = True
    async with SessionLocal() as session:
        for name, model in tables.items():
            actual = await session.scalar(select(func.count()).select_from(model))
            want = EXPECTED_COUNTS[name]
            flag = "OK  " if actual == want else "FAIL"
            if actual != want:
                ok = False
            # 只用 ASCII 输出 —— Windows 控制台是 GBK，中文会乱码
            print(f"  [{flag}] {name:<22} actual={actual:<4} expected={want}")
    return ok


async def _run(password: str | None, sync: bool = False) -> bool:
    """在**一个**事件循环里跑完 seed / sync + check。

    ⚠️ 必须单循环。`engine` 是模块级对象，它的连接绑定在创建它的事件循环上。
    写成 `asyncio.run(seed(...)); asyncio.run(check())` 会开两个循环，
    第二次复用上个循环遗留的连接，报
    `AttributeError: 'NoneType' object has no attribute 'send'`，
    收尾时再叠一个 `RuntimeError: Event loop is closed`。
    本脚本首跑踩过，别改回去。
    """
    try:
        if sync:
            print("syncing permissions (additive) ...")
            added_perms, added_links = await sync_permissions()
            print(f"  done. +{added_perms} permissions, +{added_links} role links")
        if password is not None:
            print("seeding ...")
            await seed(password)
            print(f"  done. login password = {password}")
        print("verifying ...")
        return await check()
    finally:
        # 显式释放连接池 —— 否则连接交给 GC 回收时事件循环已关闭
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="组织与权限种子数据")
    parser.add_argument("--check", action="store_true", help="只核对行数，不写入")
    parser.add_argument(
        "--sync-permissions",
        action="store_true",
        help="把缺失的权限行与角色授权补进去，**不删除任何数据**（推荐）",
    )
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="所有种子用户的登录密码")
    args = parser.parse_args()

    if args.sync_permissions:
        # 只增不删，所以单独走一条路：既不 wipe，也不重设密码。
        ok = asyncio.run(_run(None, sync=True))
    else:
        ok = asyncio.run(_run(None if args.check else args.password))
    if ok:
        print("ALL COUNTS MATCH")
        if not args.check:
            print("")
            print("  可用账号（密码见上）：")
            print("    admin  / sysadmin2     -> sys_admin （组织管理全权）")
            print("    kb_hq  / fin_kb / tech_kb / plat05 -> kb_admin （只读组织）")
            print("    cost01                 -> staff     （A4: effective_depts 三层）")
            print("    plat04                 -> staff     （A5: 已停用）")
        return 0

    print("COUNT MISMATCH")
    return 1


if __name__ == "__main__":
    sys.exit(main())
