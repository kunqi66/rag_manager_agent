"""知识导入的请求与响应 DTO（be-04 §4.2）。

🔴 **所有 ID 都是 `str`** —— 字典 §2.1 把 `sys_user.id` / `sys_department.id` /
   `kb_document.id` 全定为 VARCHAR。前端已因「ID 写成 number」错过两次，
   且从类型上看完全说得通，只有对着字典才发现。这里看到 `int` 就该怀疑。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.core.settings import settings

#: 文档生命周期状态（字典 §5.1）。
#: ⚠️ 与 `kb_import_task.status`（pending/parsing/embedding/success/failed）**是两套枚举**，
#:    不要互相复用 —— 一个是文档的，一个是任务的。
DocumentStatus = Literal["draft", "enabled", "disabled"]
TaskStatus = Literal["pending", "parsing", "embedding", "success", "failed"]
TaskType = Literal["upload", "supplement"]


# ═══════════════════════════════════════════════════════════════
# 数据权限（四维）
# ═══════════════════════════════════════════════════════════════


class PermissionPayload(BaseModel):
    """权限配置入参。

    **写入语义是「全量替换」**（先删后插，§5.6）——
    因此调用方必须先 GET 回显、再在此基础上改，否则会把未传的维度清空。

    ⚠️ 字段名 `global_public` 与字典 §5.3 的枚举值 `global` 不一致，
       这是沿用现有壳子的既有命名（见 spec §10 待确认）。
       别「顺手改齐」——前端 `types/` 里已经按 `global_public` 写好了。
    """

    global_public: bool = Field(False, description="是否全局公开（对应 entity_type=global）")
    departments: list[str] = Field(default_factory=list, description="可见部门 ID 列表")
    roles: list[str] = Field(default_factory=list, description="可见角色 ID 列表")
    users: list[str] = Field(default_factory=list, description="可见用户 ID 列表")


class PermissionView(BaseModel):
    """权限回显（`GET /documents/{id}/permissions`）。

    与 `PermissionPayload` **同构**，前端可直接回填表单。
    缺这个接口的话，权限弹窗每次打开都是空的，用户一保存就把原权限覆盖成全空
    （A3 验收原文要求「权限弹窗回显正确」）。
    """

    global_public: bool
    departments: list[str]
    roles: list[str]
    users: list[str]


class PermissionSummary(BaseModel):
    """台账列表里的「权限标签」列。

    🔴 **只给数量与维度，不给 `entity_id` 集合** ——
       回传完整集合等于把权限快照泄漏给了每一个能打开台账的人，
       而这本来就是该被权限保护的信息（安全红线 4 的精神）。

    ⚠️ **本类是内部值对象，不直接进 HTTP 响应**。`DocumentListItem` 用的是它派生出的
       两样东西：`permission_scopes`（维度集合）与 `permission_summary`（**字符串**文案）。
       2026-10-15 之前 DTO 里放的是**本对象本身**，而前端
       `permission_summary?: string | null` 把它当字符串渲染 —— 对象会显示成
       `[object Object]`；同时 `permission_scopes` 整个字段缺失，
       `PermissionTag` 里的 `props.scopes.includes('global')` 抛 `TypeError`
       ⇒ **台账表格的表体整个渲染不出来**，而接口照回 200。
       与「导入进度卡片」是同一类缺陷（前端声明了后端不发的字段），
       由 `verify_import.py` 的 T1 对账用例一并守。
    """

    global_public: bool
    department_count: int
    role_count: int
    user_count: int

    @property
    def scopes(self) -> list[str]:
        """已启用的授权维度集合。取值与前端 `PermissionScope` **逐字一致**。

        顺序固定为 global → department → role → user，
        便于前端兜底文案稳定（`['role','department']` 与 `['department','role']`
        渲染出的字符串不同，会让人以为数据变了）。
        """
        out: list[str] = []
        if self.global_public:
            out.append("global")
        if self.department_count:
            out.append("department")
        if self.role_count:
            out.append("role")
        if self.user_count:
            out.append("user")
        return out

    @property
    def text(self) -> str | None:
        """合成文案。**只含维度与数量，绝不含实体名**（安全红线 4）。

        原型上写的是「财务部 + 指定人」这种**实体名**，而实体名不能进列表响应 ——
        所以这里退到「维度 + 数量」。前端的兜底文案（`permissionScopeLabel`）
        只说得出「限定部门 / 指定用户」，比这里少一个数量，故优先用后端这份。
        """
        if self.global_public:
            return "全局"
        parts: list[str] = []
        if self.department_count:
            parts.append(f"限定部门 ×{self.department_count}")
        if self.role_count:
            parts.append(f"限定角色 ×{self.role_count}")
        if self.user_count:
            parts.append(f"指定用户 ×{self.user_count}")
        # 一条权限都没配时回 `None` 而**不是**「未配置」：
        # 那句文案归前端（它有 `permissionScopeLabel` 这一整套），后端只说「没有」。
        return " / ".join(parts) or None


# ═══════════════════════════════════════════════════════════════
# 文档
# ═══════════════════════════════════════════════════════════════


class DocumentListQuery(BaseModel):
    """台账列表查询参数（`Depends()` 注入）。"""

    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=settings.kb_page_size_max)
    keyword: str = Field("", description="标题 / 文件名模糊匹配")
    category: str = Field("", description="分类精确匹配")
    # 🔴 **用三态 `status` 而不是壳子里原来的 `enabled: bool`**。
    #    `bool` 表达不了 draft/enabled/disabled 三态：传 false 到底是
    #    「仅看已停用」还是「看未启用的（含草稿）」无从区分。
    status: DocumentStatus | None = None


class DocumentListItem(BaseModel):
    """台账列表项。**不含 `source_markdown`** —— 那是 LONGTEXT，只走详情接口。

    🔴 **字段集以前端 `types/kb.ts::KbDocumentVo` 为准**（spec `P1-02-fe-kb-documents` §9-7）。
       原先这里放的是 `permission_summary: PermissionSummary`（**对象**）且**没有**
       `permission_scopes` —— 两处都与前端声明的契约不符，且**都不会报错**：
       对象被 `{{ label }}` 渲染成 `[object Object]`，
       而缺字段让 `PermissionTag` 抛 `TypeError`、**整个表体空白**。
       已改为「维度集合（必给）+ 字符串文案（可空）」这一对，与 §9-7 的处置一致。
    """

    id: str
    title: str
    file_name: str | None
    file_type: str | None
    category: str | None
    status: DocumentStatus
    chunk_count: int
    #: 已启用的授权维度集合，取值 `global|department|role|user`。
    #: 空数组 = 一条权限都没配（除知识管理员外无人可检索）。
    permission_scopes: list[str]
    #: 合成文案（如「限定部门 ×2 / 指定用户 ×3」）。**不含实体名**（安全红线 4）。
    #: 一条权限都没配时为 `None`，文案交前端兜底。
    permission_summary: str | None
    uploaded_by: str | None
    created_at: datetime
    updated_at: datetime


class DocumentDetail(DocumentListItem):
    """详情（`GET /documents/{id}`）。

    🔴 带正文的理由：`PUT /documents/{id}` 要能编辑正文，而 `source_markdown`
       不在任何列表 DTO 里。不开这个接口，「编辑正文」就无从下手
       （只剩「本期只能改标题与分类」这一条路，spec §10 #71 已按开接口定）。
    """

    source_markdown: str | None


class DocumentUpdatePayload(BaseModel):
    """编辑文档。

    ⚠️ 用 `model_fields_set` 区分「没传」与「传了 null」，**不要**用 `is not None` ——
       后者会让「想清空分类」的请求变成「不改分类」。同 be-02 `DepartmentUpdate`。

    传了 `source_markdown` 才触发切片与向量重建（§5.7），这是本模块最重的一个操作。
    """

    title: str | None = Field(None, max_length=255)
    category: str | None = Field(None, max_length=64)
    source_markdown: str | None = None


class DocumentStatusPayload(BaseModel):
    """启停文档。

    **刻意不含 `draft`**：回到草稿是非法流转（§5.7 状态机）。
    用 `Literal` 而不是 `str`，非法值在入口就被 Pydantic 挡成 1001，
    不会走到引擎里再报错。
    """

    status: Literal["enabled", "disabled"]


# ═══════════════════════════════════════════════════════════════
# 导入任务
# ═══════════════════════════════════════════════════════════════


class ImportTaskRef(BaseModel):
    """任务引用。`create_supplement_task` 的返回（§5.10，be-09 调用）。"""

    task_id: str


class UploadAccepted(BaseModel):
    """上传受理回执。

    受理是**同步**的（只落 `kb_document` + `kb_import_task` 两条记录），
    耗时的解析入库交给 Celery —— 所以这里 200 不代表导入完成，
    前端要拿 `task_id` 去轮询 `GET /tasks/{task_id}`。

    两种形状（用 `batch_id` 区分）：
    - 单篇：`task_id` + `document_id` 都有，`batch_id` 为 None；
    - 批量：三者都有，但**每篇的 `task_id` / `document_id` 不在本响应里** ——
      批量可能几十个文件，逐个回传会让响应体膨胀且顺序无意义。
      前端拿 `batch_id` 去 `GET /tasks?batch_id=...` 拉全批（#100）。
    """

    task_id: str | None = None
    document_id: str | None = None
    batch_id: str | None = None


class ImportTaskListQuery(BaseModel):
    """任务列表查询（`Depends()` 注入）。"""

    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=settings.kb_page_size_max)
    batch_id: str | None = Field(None, description="按批次筛选，批量导入后拉全批进度")
    status: TaskStatus | None = None


class ImportTaskDetail(BaseModel):
    """任务详情，前端轮询用。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    #: 可空：be-09「一键转建」的任务先于文档存在（字典 §9 #64）
    document_id: str | None
    #: 所属批次。单篇为 None。前端靠它把「这一批」的任务归到一起，
    #: 也是 `GET /tasks?batch_id=` 的**回显**（列表里能看出某条属于哪批）。
    batch_id: str | None = None
    file_name: str | None
    file_size: int
    type: TaskType
    status: TaskStatus
    progress: int
    total_chunks: int
    error_msg: str | None
    #: 🔴 **必填**：进度抽屉按它**倒序**排（最新的在最上面）。
    #:    漏了它前端只能拿到「按字典插入顺序」的任务列表 —— 而那取决于
    #:    用户先传了哪一批，不是时间序。
    created_at: datetime
    finished_at: datetime | None


class ImportTaskListItem(ImportTaskDetail):
    """任务列表项。与详情**同构**（spec §4.2：暂不分化）。

    保留独立类名是为了将来真需要分化时不必改调用方 ——
    但**不要**现在就往里加详情没有的字段，那会让「同构」这个前提消失。
    """


# ═══════════════════════════════════════════════════════════════
# 内部类型（不是 HTTP DTO）
# ═══════════════════════════════════════════════════════════════


class ChunkDraft(BaseModel):
    """`chunking.split_markdown` 的返回项。

    内部类型，**不出现在任何接口的响应里** —— 切片对上层是黑盒，
    对外只暴露 `chunk_count`。放这里是因为它跨越了 chunking → pipeline 两个模块。
    """

    model_config = ConfigDict(frozen=True, from_attributes=True)

    content: str
    heading_path: str
    chunk_index: int
    token_count: int
