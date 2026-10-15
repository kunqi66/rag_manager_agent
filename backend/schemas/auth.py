"""认证与身份的 DTO。

本文件同时承载 be-01（认证）与 be-03（安全依赖）两侧的模型 ——
be-03 §2 把 `AuthContext` 定在这里，be-01 的请求/响应 DTO 与它同域，放一起避免割裂。

🔴 全部 ID 字段一律 `str`。字典 §1「主键一律字符串」，`sys_user.id` /
`sys_department.id` 在库里是 **VARCHAR**，写成 `int` 会在前后端交界处静默出错
（前端已因此错过两次：`depts: number[]`、`user_id: number`）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# ═══════════════════════════════════════════════════════════════
# 身份（服务端权威）
# ═══════════════════════════════════════════════════════════════


class AuthContext(BaseModel):
    """服务端权威身份上下文（be-03 §4.1）。

    🔴 **必须 `frozen=True`** —— 引擎拿到之后不得改写。若某个引擎「顺手」往
    `roles` 里补一项，越权修复的合围就破了，而且不留任何日志。
    """

    model_config = ConfigDict(frozen=True)

    user_id: str = Field(..., description="用户 ID（= JWT 的 sub）")
    username: str = Field(..., description="登录名")
    department_id: str | None = Field(None, description="所属部门（自身，不含祖先）")
    # ⚠️ `depts` **不是**「所属部门」，是 effective_depts（自身 + 全部祖先）。
    # 字段名沿用 PRD §8.1 的 JWT 载荷键名，语义以 PRD §6.1.2 为准。
    # 这是本模块最容易写错的一处：它直接喂给 be-05 拼 ACL 表达式，
    # 少一层祖先 = 该部门下所有文档静默不可见。
    depts: list[str] = Field(default_factory=list, description="有效部门集合（含全部祖先）")
    roles: list[str] = Field(default_factory=list, description="角色 ID 列表")
    perms: list[str] = Field(default_factory=list, description="功能权限码列表")

    # ⚠️ `ip` **不是身份字段** —— 它不参与任何鉴权判定，只是给审计留痕用
    #   （be-06 / 字典 §9 #115）。放这里是因为它是「请求上下文」中唯一
    #   需要跟着身份走到 service 层的值：引擎写审计时拿不到 `Request` 对象。
    #
    #   取值口径见 `core/deps.py::_client_ip`：**只信 `X-Real-IP`**，
    #   绝不解析 `X-Forwarded-For`（那是追加语义，左半段客户端可控）。
    ip: str | None = Field(None, description="来源 IP（审计用，非身份字段）")


class UserIdentity(AuthContext):
    """`load_user_identity` 的返回值（be-02 §3.2）。

    字段与 `AuthContext` **完全一致**（be-03 §4.1 的 `AuthContext` 本就不含 `exp`），
    因此用继承而非重新定义 —— be-02 §3.2 要求两者「一致」，
    两份独立定义必然漂移，而漂移的表现形式是「登录时算出的部门集合 ≠
    依赖注入时算出的部门集合」，属最难查的越权/漏权 bug。
    """


# ═══════════════════════════════════════════════════════════════
# 请求 DTO（全部出自 PRD §8.1）
# ═══════════════════════════════════════════════════════════════


class LoginRequest(BaseModel):
    """登录请求。

    🔴 **只有 `username` + `password`**。绝不接受任何形如 `user_id` / `roles` /
    `depts` 的入参 —— 那正是 PRD §11.3 R3 的越权口子（安全红线 1）。
    登录是身份的**源头**，本请求的产物 `AuthContext` 是此后所有请求的唯一权威身份。
    """

    username: str = Field(..., min_length=1, max_length=64, description="登录名")
    password: str = Field(..., min_length=1, max_length=128, description="密码")


class RefreshRequest(BaseModel):
    """刷新令牌请求。"""

    refresh_token: str = Field(..., min_length=1, description="refresh token")


class LogoutRequest(BaseModel):
    """登出请求（be-01 §10-27）。

    `refresh_token` 可选：带上才能吊销 refresh 白名单条目；不带则只清会话缓存并告警。
    """

    refresh_token: str | None = Field(None, description="可选，用于吊销 refresh token")


# ═══════════════════════════════════════════════════════════════
# 响应 DTO
# ═══════════════════════════════════════════════════════════════


class TokenPair(BaseModel):
    """令牌对（PRD §8.1）。"""

    access_token: str = Field(..., description="访问令牌")
    refresh_token: str = Field(..., description="刷新令牌")
    token_type: str = Field("bearer", description="令牌类型，固定 bearer")
    expires_in: int = Field(..., description="access token 有效期（秒）")


class DepartmentBrief(BaseModel):
    """部门简要信息，供前端显示部门**名**（`P1-00-fe-app-shell` §9-4 的侧栏依赖它）。"""

    id: str = Field(..., description="部门 ID")
    name: str = Field(..., description="部门名称")
    path: str = Field(..., description="物化路径，形如 /dept_01/dept_02/")


class UserDetail(BaseModel):
    """当前用户画像（`GET /api/auth/me`）。

    🔴 **字段集已冻结**，出处 be-01 spec §10-79（✅ 2026-10-14 拍板）+ §10-86（`dept_names`）。
    该节曾记录四处互不一致的定义（be-01 `UserInfo` / 字典 §8 命名 / be-03 `AuthContext` /
    前端自定），现已收敛到本类。

    🔴 **三个部门字段并存、语义不同、不可互替** —— 这是本 DTO 最容易混用的一处：

    | 字段 | 内容 | 用途 |
    |---|---|---|
    | `department` | `{id, name, path}`，仅**自身**一级 | 显示 |
    | `depts` | 自身 + **全部祖先**的 id | **ACL**（be-05 拼 Milvus 过滤表达式） |
    | `dept_names` | 与 `depts` **同序同长**的部门名 | 显示 |

    `dept_names` 不能省：前端要显示「财务部 · 成本组」（父级 · 自身），
    `department` 只有自身一级、`depts` 只有 id 没名字，两者都凑不出父级。

    ⚠️ **不含 `password_hash`** —— 安全红线 3，任何路径都不得回传。
    """

    user_id: str = Field(..., description="用户 ID")
    username: str = Field(..., description="登录名")
    real_name: str = Field(..., description="姓名")
    email: str | None = Field(None, description="邮箱")
    department: DepartmentBrief | None = Field(None, description="所属部门（仅自身，显示用）")
    depts: list[str] = Field(default_factory=list, description="有效部门集合（ACL 用，含祖先）")
    # 🔴 `dept_names[i]` 必须是 `depts[i]` 的名字 —— 由 auth_service.get_me **遍历 depts 回填**保证。
    # 若改成遍历查询结果拼接，顺序由数据库决定，会出现「名字与 id 对错位」这种不报错、
    # 只静默显示错名字的 bug。`IN` 查询不保证返回顺序，这正是不能图省事的原因。
    dept_names: list[str] = Field(
        default_factory=list,
        description="部门名，与 depts **同序同长**（根在前、自身在后；仅显示用）",
    )
    roles: list[str] = Field(default_factory=list, description="角色 ID 列表")
    perms: list[str] = Field(default_factory=list, description="功能权限码列表")
