"""组织管理路由（be-02）。

15 条路径**全部出自 PRD §8.1**，无一自创。权限码 13 个**全部出自字典 §5.6**
（2026-10-14 冻结的展开写法），与后端种子 `seed_org.py`、前端 `PERM` 常量逐字一致 ——
本模块**不引入任何新的权限码**。

分层铁律：本文件只做「声明端点 → 挂权限依赖 → 转发 service → `ok()` 包装」，
**不写 SQL、不做业务判断**。环校验、引用校验、缓存失效全在 `org_service`。

🔴 安全红线 1（PRD §11.3 R3）：操作人身份一律来自 `Depends(get_current_user)`
   解析的 JWT。**本文件的所有 DTO 里都没有任何身份字段** ——
   不接受 `operator_id` / `created_by` / `user_context` 这类入参。

🔴 安全红线 7：**每个写接口独立挂自己的权限码**，不用「按 method 粗判」的中间件
   （不同写接口的码本就不同：`org:user:create` ≠ `org:role:grant`）。
   ⚠️ `GET /departments/tree` 按 PRD §8.1 只要求**登录** ——
   即任何登录用户都能拉到完整部门树。这是 PRD 的既定取舍（部门结构在公司内不算敏感），
   **不要顺手给它加权限码**，那会改变契约。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.deps import get_current_user, get_db, require_permission
from backend.core.response import ok, page_result
from backend.schemas.auth import AuthContext
from backend.schemas.org import (
    DepartmentCreate,
    DepartmentUpdate,
    RoleCreate,
    RolePermissionGrant,
    RoleUpdate,
    UserCreate,
    UserListQuery,
    UserRoleAssign,
    UserUpdate,
)
from backend.service import org_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/org", tags=["组织管理"])


# ═══════════════════════════════════════════════════════════════
# 部门
# ═══════════════════════════════════════════════════════════════


@router.get("/departments/tree", summary="部门树", response_model=None)
async def list_departments(
    ctx: AuthContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """全量部门树。只要求登录态，**无权限码**（PRD §8.1）。"""
    return ok(await org_service.list_departments_tree(db))


@router.post("/departments", summary="新建部门", response_model=None)
async def create_department(
    payload: DepartmentCreate,
    ctx: AuthContext = Depends(require_permission("org:department:create")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await org_service.create_department(db, payload, ctx))


@router.put("/departments/{dept_id}", summary="修改部门", response_model=None)
async def update_department(
    dept_id: str,
    payload: DepartmentUpdate,
    ctx: AuthContext = Depends(require_permission("org:department:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """改部门。请求体里**显式传入 `parent_id`** 即触发移动。

    注意判据是「传没传」而不是「是不是 null」—— 传 `null` 表示移到顶层。
    """
    return ok(await org_service.update_department(db, dept_id, payload, ctx))


@router.delete("/departments/{dept_id}", summary="删除部门", response_model=None)
async def delete_department(
    dept_id: str,
    ctx: AuthContext = Depends(require_permission("org:department:delete")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删部门。有子部门 / 有用户挂靠 / 有文档授权引用即拒绝（3002）。"""
    await org_service.delete_department(db, dept_id, ctx)
    return ok()


# ═══════════════════════════════════════════════════════════════
# 用户
# ═══════════════════════════════════════════════════════════════


@router.get("/users", summary="用户分页列表", response_model=None)
async def list_users(
    query: UserListQuery = Depends(),
    ctx: AuthContext = Depends(require_permission("org:user:list")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """用户分页列表。`department_id` 筛选**含其全部子孙**部门。"""
    items, total = await org_service.list_users(db, query)
    return ok(page_result(items, total, query.page, query.page_size))


@router.post("/users", summary="新建用户", response_model=None)
async def create_user(
    payload: UserCreate,
    ctx: AuthContext = Depends(require_permission("org:user:create")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """新建用户。密码在此只以明文过一遍，落库前经 be-03 `hash_password`。"""
    return ok(await org_service.create_user(db, payload, ctx))


@router.put("/users/{user_id}", summary="修改用户", response_model=None)
async def update_user(
    user_id: str,
    payload: UserUpdate,
    ctx: AuthContext = Depends(require_permission("org:user:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """改用户。停用或换部门会失效该用户的会话缓存（权限即时生效）。"""
    return ok(await org_service.update_user(db, user_id, payload, ctx))


@router.delete("/users/{user_id}", summary="删除用户", response_model=None)
async def delete_user(
    user_id: str,
    ctx: AuthContext = Depends(require_permission("org:user:delete")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await org_service.delete_user(db, user_id, ctx)
    return ok()


@router.put("/users/{user_id}/roles", summary="分配角色", response_model=None)
async def assign_user_roles(
    user_id: str,
    payload: UserRoleAssign,
    ctx: AuthContext = Depends(require_permission("org:user:grant")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """分配角色 —— **整体替换** `sys_user_role`，不是增量。"""
    await org_service.assign_user_roles(db, user_id, payload.role_ids, ctx)
    return ok()


# ═══════════════════════════════════════════════════════════════
# 角色与权限
# ═══════════════════════════════════════════════════════════════


@router.get("/roles", summary="角色列表", response_model=None)
async def list_roles(
    ctx: AuthContext = Depends(require_permission("org:role:list")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await org_service.list_roles(db))


@router.post("/roles", summary="新建角色", response_model=None)
async def create_role(
    payload: RoleCreate,
    ctx: AuthContext = Depends(require_permission("org:role:create")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await org_service.create_role(db, payload, ctx))


@router.put("/roles/{role_id}", summary="修改角色", response_model=None)
async def update_role(
    role_id: str,
    payload: RoleUpdate,
    ctx: AuthContext = Depends(require_permission("org:role:update")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """改角色。`code` 不在 DTO 内 —— 它是权限契约的一部分，创建后不可改。"""
    return ok(await org_service.update_role(db, role_id, payload, ctx))


@router.delete("/roles/{role_id}", summary="删除角色", response_model=None)
async def delete_role(
    role_id: str,
    ctx: AuthContext = Depends(require_permission("org:role:delete")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """删角色。内置角色、仍被用户持有、有文档授权引用，三种情况都拒绝（3002）。"""
    await org_service.delete_role(db, role_id, ctx)
    return ok()


@router.get("/permissions/tree", summary="功能权限树", response_model=None)
async def get_permission_tree(
    ctx: AuthContext = Depends(require_permission("org:role:list")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """全量功能权限树（两层 `menu` → `button`）。

    ⚠️ 返回的是**全量**树，**不含**「某角色当前已勾选哪些」——
    该读取接口缺失（字典 §9 #75），前端授权弹窗回显会缺数据。
    """
    return ok(await org_service.get_permission_tree(db))


@router.put("/roles/{role_id}/permissions", summary="角色授权", response_model=None)
async def grant_role_permissions(
    role_id: str,
    payload: RolePermissionGrant,
    ctx: AuthContext = Depends(require_permission("org:role:grant")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """角色授权 —— **整体替换** `sys_role_permission`。会失效持该角色用户的会话。"""
    await org_service.grant_role_permissions(db, role_id, payload.permission_ids, ctx)
    return ok()
