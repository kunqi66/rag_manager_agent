"""组织管理 DTO（be-02）。

字段集依据 `P1-08-be-org` §4.4（字典 §9 #87）与字典 §2.1 的列定义。
长度约束与 `backend/models/*.py` 的 `String(n)` **逐列对齐** —— 两边不一致时，
表现为「Pydantic 放行、MySQL 报 1406 Data too long」，错误发生在离原因很远的地方。

🔴 **所有 ID 都是 `str`**（字典 §2.1 把三个主键全定为 VARCHAR）。
写成 `int` 从类型上看完全说得通，本项目已因此错过两次。

🔴 **密码只进不出**：`UserCreate` 收 `password`，但 `UserItem` 里**没有** `password_hash`
（字典 §2.1 严禁回传）。两个类之间没有任何字段可让哈希串漏出去。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# ═══════════════════════════════════════════════════════════════
# 部门
# ═══════════════════════════════════════════════════════════════


class DepartmentNode(BaseModel):
    """部门树节点（递归）。

    `path` 是物化路径（`/dept_01/dept_02/`，首尾都有斜杠 —— `P1-08` §5.1 的 I1）。
    它由服务端维护，**响应里回给前端只为排障与展示，前端不得用它拼权限判断**。
    """

    id: str = Field(..., description="部门 ID")
    name: str = Field(..., description="部门名称")
    parent_id: str | None = Field(None, description="父部门 ID，顶层为 null")
    path: str = Field(..., description="物化路径，形如 /dept_01/dept_02/")
    sort: int = Field(0, description="同级排序，升序")
    status: str = Field("active", description="状态：active / disabled")
    children: list[DepartmentNode] = Field(
        default_factory=list, description="子部门（递归）"
    )


class DepartmentCreate(BaseModel):
    """新建部门。

    刻意**不收 `path`** —— 它由服务端按 `父.path + 自己的 id + '/'` 算出。
    让客户端传 path 等于把 I1（首尾斜杠）与 I2（前缀性质）交给不可信输入去满足。
    """

    name: str = Field(..., min_length=1, max_length=64, description="部门名称")
    parent_id: str | None = Field(None, description="父部门 ID，顶层传 null")
    sort: int = Field(0, description="同级排序")


class DepartmentUpdate(BaseModel):
    """修改部门。**未出现在请求体里的字段 = 不改**。

    ⚠️ 判据是 `"parent_id" in payload.model_fields_set`，**不是** `payload.parent_id is not None`。
    因为 `parent_id` 的 `None` 有实义 —— 它表示「移到顶层」（顶层部门的 `parent_id` 就是 NULL）。
    用 `is not None` 判会让「移到顶层」永远无法表达，
    且症状是接口静默不生效（不报错、也不移动），排查时极难想到是判空写法的问题。
    `model_fields_set` 是 Pydantic v2 记录「本次请求**显式传了**哪些字段」的集合，
    正好区分「没传」与「传了 null」。

    `parent_id` 变化即触发**移动**（重写整棵子树的 path，见 `P1-08` §5.2）。
    """

    name: str | None = Field(None, min_length=1, max_length=64)
    parent_id: str | None = Field(
        None, description="新父部门 ID；显式传 null = 移到顶层；不传 = 不改"
    )
    sort: int | None = None
    status: str | None = Field(None, description="active / disabled")


# ═══════════════════════════════════════════════════════════════
# 用户
# ═══════════════════════════════════════════════════════════════


class UserItem(BaseModel):
    """用户列表项 / 详情。

    🔴 **不含 `password_hash`**（字典 §2.1「严禁明文/回传」）。
    `department_name` 是 JOIN 出来的冗余字段 —— 列表页要显示部门名，
    逐个再查一次会造成 N+1。
    """

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(..., description="用户 ID")
    username: str = Field(..., description="登录名")
    real_name: str = Field(..., description="姓名")
    email: str | None = Field(None, description="邮箱")
    department_id: str | None = Field(None, description="所属部门 ID（自身，不含祖先）")
    department_name: str | None = Field(None, description="所属部门名（显示用）")
    status: str = Field("active", description="active / disabled")
    role_ids: list[str] = Field(default_factory=list, description="角色 ID 列表")
    last_login_at: datetime | None = Field(None, description="最后登录时间")
    created_at: datetime = Field(..., description="创建时间")


class UserCreate(BaseModel):
    """新建用户。

    `password` 由管理员在表单里指定（`P1-08` §10-90 的暂定选择）。
    明文只在此 DTO 与服务层之间传递，**落库前经 be-03 `hash_password`**。
    """

    username: str = Field(..., min_length=1, max_length=64, description="登录名，唯一")
    password: str = Field(..., min_length=1, max_length=128, description="初始密码")
    real_name: str = Field(..., min_length=1, max_length=64, description="姓名")
    email: str | None = Field(None, max_length=128, description="邮箱")
    department_id: str | None = Field(None, description="所属部门 ID")
    role_ids: list[str] = Field(default_factory=list, description="初始角色")


class UserUpdate(BaseModel):
    """修改用户。字段为 `None` 表示「不改」。

    **不含 `password`** —— 改密是独立语义（要校验强度、可能要验旧密码、
    要记审计），塞进这个「部分更新」DTO 会让它变得既可空又敏感。见 `P1-08` §10-90。
    """

    real_name: str | None = Field(None, min_length=1, max_length=64)
    email: str | None = Field(None, max_length=128)
    department_id: str | None = Field(None, description="改动即失效该用户会话")
    status: str | None = Field(None, description="active / disabled，停用即踢下线")
    role_ids: list[str] | None = Field(None, description="null = 不改角色")


class UserListQuery(BaseModel):
    """用户列表的过滤条件（`P1-08` §10-88，暂定集）。

    `department_id` 是**含子孙**的语义 —— 选「财务部」要能看到「成本组」的人。
    实现走物化路径前缀匹配（`P1-08` §5.1 的 I2），不是 `=` 等值。
    """

    page: int = Field(1, ge=1, description="页码，从 1 起")
    page_size: int = Field(20, ge=1, le=100, description="每页条数")
    department_id: str | None = Field(None, description="按部门筛选，含其全部子孙")
    keyword: str | None = Field(None, max_length=64, description="匹配登录名或姓名")
    status: str | None = Field(None, description="active / disabled")


class UserRoleAssign(BaseModel):
    """分配角色 —— **整体替换** `sys_user_role`，不是增量。

    整体替换让「最终状态」在请求里完全可见，避免增量接口在重试/并发下
    产生「多授了一个角色」这种静默的权限扩大。
    """

    role_ids: list[str] = Field(default_factory=list, description="替换后的角色 ID 全集")


# ═══════════════════════════════════════════════════════════════
# 角色与权限
# ═══════════════════════════════════════════════════════════════


class RoleItem(BaseModel):
    """角色。`is_builtin = true` 的角色**不可删除**。"""

    id: str = Field(..., description="角色 ID")
    code: str = Field(..., description="角色码，唯一")
    name: str = Field(..., description="角色名")
    description: str | None = Field(None, description="描述")
    is_builtin: bool = Field(False, description="内置角色（不可删）")
    permission_ids: list[str] = Field(
        default_factory=list, description="已授权的权限 ID 列表"
    )
    created_at: datetime = Field(..., description="创建时间")


class RoleCreate(BaseModel):
    """新建角色。`code` 唯一，且**创建后不可改** —— 它是权限契约的一部分。"""

    code: str = Field(..., min_length=1, max_length=64, description="角色码，唯一")
    name: str = Field(..., min_length=1, max_length=64, description="角色名")
    description: str | None = Field(None, max_length=255, description="描述")


class RoleUpdate(BaseModel):
    """修改角色。**不含 `code`** —— 改码等于让所有引用它的地方失效。"""

    name: str | None = Field(None, min_length=1, max_length=64)
    description: str | None = Field(None, max_length=255)


class RolePermissionGrant(BaseModel):
    """角色授权 —— **整体替换** `sys_role_permission`。"""

    permission_ids: list[str] = Field(
        default_factory=list, description="替换后的权限 ID 全集"
    )


class PermissionNode(BaseModel):
    """功能权限树节点（两层：`menu` → `button`）。

    `code` 就是字典 §5.6 的 13 个 `org:*` 码 —— 前端 `v-permission` 用它判按钮显隐，
    是本项目**最需要三处一致**的字段（字典 / 后端种子 / 前端 `PERM` 常量）。
    """

    id: str = Field(..., description="权限 ID")
    parent_id: str | None = Field(None, description="父权限 ID")
    code: str = Field(..., description="权限码，如 org:user:create")
    name: str = Field(..., description="权限名")
    type: str = Field(..., description="menu / button")
    sort: int = Field(0, description="同级排序")
    children: list[PermissionNode] = Field(
        default_factory=list, description="子权限（递归）"
    )


__all__ = [
    "DepartmentCreate",
    "DepartmentNode",
    "DepartmentUpdate",
    "PermissionNode",
    "RoleCreate",
    "RoleItem",
    "RolePermissionGrant",
    "RoleUpdate",
    "UserCreate",
    "UserItem",
    "UserListQuery",
    "UserRoleAssign",
    "UserUpdate",
]
