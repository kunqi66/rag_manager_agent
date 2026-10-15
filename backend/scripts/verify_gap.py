"""be-09 知识缺口闭环的验收脚本（spec `P3-15-be-gap-closure` §8 的可执行版本）。

跑法（项目根目录）：

    uv run python -m backend.scripts.verify_gap

脚本分四层，**能跑的就跑，跑不了的明确报 SKIP 并说明原因** ——
绝不把「环境不具备」伪装成「通过」（那是验收脚本最坏的失败模式）。

| 层 | 需要什么 | 覆盖 |
|---|---|---|
| **T1 离线** | 什么都不需要 | `is_miss` 的四分支（含两条反例）、`_unit_vector` 的归一化契约、`_Matcher` 的**部门双键**、DTO ↔ 前端类型逐字段对齐、路由注册与权限码、红线 1/4/7/8、配置与 beat 排班、迁移 0007 与模型一致 |
| **T1.5 假会话** | 什么都不需要 | 用**脚本化假 `AsyncSession`** 驱动 `gap`：30 条同问题 → 一条缺口 `frequency=30`、**幂等**（第二轮 `updated=0`）、新建缺口立刻可匹配、**升序**处理、部门双键、`skipped_by_acl`、自动关闭（含 `faq_hit=True` 路径）、转建状态机（3001/3002）与复用 |
| **T2 接口** | MySQL + **Redis** | 2 端点鉴权门禁、权限码 403、分页结构、清单真跑、转建真跑 |
| **T3 链路** | ＋ Milvus ＋ BGE ＋ DeepSeek ＋ worker | A8/A9：未命中 → 缺口 → 转建 → 补料 → 再问命中 → `closed` |

**本机跑不了 T2/T3**：云端 MySQL 不可达、本机没有 Docker（Milvus 单机版依赖它）。
每一项都标注了它依赖什么，供在 ECS 上跑时对照。

⚠️ **T1.5 为什么要用假会话**：本模块最容易错的四处 —— 幂等水位线、
新建缺口是否立刻进匹配池、处理顺序、部门双键 —— 都不需要真实数据库就能验证。
等到 T2 才测的话，在本机（MySQL 不可达时）这四条**永远测不到**，
而它们全都是**不报错的静默失效**：频次虚高、同一问题两条缺口、跨部门合并。

⚠️ Windows 控制台是 GBK —— 输出一律 **ASCII 标签 + 中文说明**，断言**布尔值与具体值**，
不靠肉眼读中文。与 `verify_org.py` / `verify_import.py` / `verify_search.py` /
`verify_faq.py` 同一约定。

⚠️ T2 会真的连 `.env` 里的 MySQL（开发期直连 ECS），只增删自己造的数据
（标记前缀 `ZZVFYGAP`）。**绝不动种子数据**，也**不碰 `rag_kb` 以外的库**。
"""

from __future__ import annotations

import asyncio
import ast
import inspect
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect as sa_inspect

import main
from backend.core.db import engine
from backend.core.exceptions import ConflictError, DependencyError, NotFoundError
from backend.core.settings import settings
from backend.engines.data_mining_engine import gap as gap_engine
from backend.engines.data_mining_engine import similarity as sim
from backend.models.audit_log import SYSTEM_USER_ID
from backend.models.kb_import_task import KbImportTask
from backend.models.knowledge_gap import (
    ACTIVE_STATUSES,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_TASKED,
    KnowledgeGap,
)
from backend.models.qa_log import pack_embedding
from backend.schemas import mining as mining_schemas
from backend.schemas.auth import AuthContext
from backend.service import audit_service

PASSWORD = "Rag@12345"
#: 自造数据的标记前缀。清理一律靠它，不靠「记住 id」。
#: 🔴 **不含下划线** —— `LIKE 'zzvfy_%'` 里的 `_` 是通配符（CLAUDE.md 已知坑第一条）。
MARK = "ZZVFYGAP"

ROOT = Path(__file__).resolve().parents[2]

# 🔴 Windows 控制台默认是 GBK，本脚本标签里有中文与 🔴 —— 不重配的话
#    `print` 直接抛 UnicodeEncodeError，脚本在中途崩掉，而前面的 OK 行
#    已经打出来，看起来像「跑到一半莫名退出」。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 —— 重配失败不该让脚本起不来
        pass

_results: list[bool] = []
_skipped: list[str] = []


def check(passed: bool, label: str) -> None:
    _results.append(bool(passed))
    print(f"  [{'OK  ' if passed else 'FAIL'}] {label}")


def skip(label: str, reason: str) -> None:
    """环境不具备 —— 记一笔，**不计入通过数**。"""
    _skipped.append(label)
    print(f"  [SKIP] {label} —— {reason}")


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def ctx_for(
    user_id: str = "u_test",
    depts: list[str] | None = None,
) -> AuthContext:
    return AuthContext(
        user_id=user_id,
        username=user_id,
        department_id=(depts or ["dept_self"])[-1],
        depts=depts if depts is not None else ["dept_root", "dept_self"],
        roles=["role_02"],
        perms=["gap:list", "gap:task:create"],
    )


def _dt(*args: int) -> datetime:
    return datetime(*args)


def _blank(source: str) -> str:
    """去掉 docstring 与注释，只留代码本体。

    🔴 本仓库的 docstring **刻意写着**「原先会丢弃 `gap_id`，现已落库」这类说明。
       直接对源码做子串检查，会把这段解释本身当成「还是错的」，
       于是断言永远红着 —— 而永远红着的用例最终会被人注释掉，那才是真损失。
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    spans: list[tuple[int, int, int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            spans.append(
                (
                    first.value.lineno,
                    first.value.col_offset,
                    first.value.end_lineno,
                    first.value.end_col_offset,
                )
            )
    for line1, col1, line2, col2 in sorted(spans, reverse=True):
        if line1 == line2:
            lines[line1 - 1] = lines[line1 - 1][:col1] + lines[line1 - 1][col2:]
        else:
            lines[line1 - 1] = lines[line1 - 1][:col1] + "\n"
            for index in range(line1, line2 - 1):
                lines[index] = ""
            lines[line2 - 1] = lines[line2 - 1][col2:]
    text = "".join(lines)
    return "\n".join(re.sub(r"#.*$", "", line) for line in text.splitlines())


def _gap_src() -> str:
    return (ROOT / "backend" / "engines" / "data_mining_engine" / "gap.py").read_text(
        encoding="utf-8"
    )


def _router_src() -> str:
    return (ROOT / "backend" / "api" / "data_mining_router.py").read_text(encoding="utf-8")


def _ts_interfaces(source: str) -> dict[str, list[str]]:
    """从 `frontend/src/types/governance.ts` 抽出 `interface X { ... }` 的字段名。

    🔴 **前端类型文件是真正的契约**：字段名不一致不会编译报错，只会让前端
       `data.xxx` 取到 `undefined`，而**接口照回 200**。所以这条对账必须自动化。
    """
    result: dict[str, list[str]] = {}
    for match in re.finditer(r"export interface (\w+)\s*\{(.*?)\n\}", source, re.S):
        name, body = match.group(1), match.group(2)
        fields = []
        for line in body.splitlines():
            line = re.sub(r"/\*.*?\*/", "", line).strip()
            line = re.sub(r"//.*$", "", line).strip()
            if not line or line.startswith("*") or line.startswith("/*"):
                continue
            field = re.match(r"(\w+)\??\s*:", line)
            if field:
                fields.append(field.group(1))
        result[name] = fields
    return result


# ═══════════════════════════════════════════════════════════════
# 假会话：不需要数据库就能驱动 gap 引擎
# ═══════════════════════════════════════════════════════════════


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _ScriptedSession:
    """**按调用顺序**给定返回值的假会话。

    ⚠️ 不做 `add` / `commit` 的语义模拟 —— 那会变成一个半吊子 ORM，
       测的是假实现而不是真代码。这里只覆盖「构造了什么 SQL」+
       「按什么顺序返回什么」+「add 进去的是谁」。

    为什么需要「按顺序」：`aggregate_gaps` 一轮里按固定顺序取四批东西 ——
    ① 窗口内未命中行 ② 活跃缺口 ③ `tasked` 缺口 ④ 命中行（自动关闭用）。
    返回同一批会让「哪一步取到了什么」无法验证。
    """

    def __init__(
        self,
        *,
        scalars_seq: list[list] | None = None,
        scalar_seq: list | None = None,
        get_map: dict | None = None,
    ) -> None:
        self._scalars = list(scalars_seq or [])
        self._scalar = list(scalar_seq or [])
        self._get_map = get_map or {}
        self.statements: list = []
        self.added: list = []
        self.commits = 0

    async def scalars(self, stmt):
        self.statements.append(stmt)
        return _FakeResult(self._scalars.pop(0) if self._scalars else [])

    async def scalar(self, stmt):
        self.statements.append(stmt)
        return self._scalar.pop(0) if self._scalar else 0

    async def get(self, model, key):
        self.statements.append(("get", model, key))
        return self._get_map.get(key)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1


class _Obj:
    """通用假 ORM 对象。"""

    def __init__(self, **kw) -> None:
        for key, value in kw.items():
            setattr(self, key, value)


def _vec(seed: int, dim: int = sim.EMBED_DIM) -> np.ndarray:
    """确定性伪随机**单位**向量。用 `default_rng` 而不是 `np.random.rand`：
    后者受全局状态影响，同一份脚本两次运行会得到不同数据。"""
    rng = np.random.default_rng(seed)
    raw = rng.random(dim).astype(np.float32)
    return raw / np.linalg.norm(raw)


def _row(
    row_id: str,
    *,
    vector: np.ndarray,
    department_id: str | None = "dept_a",
    faq_hit: bool = False,
    recalled: list[str] | None = None,
    allowed: list[str] | None = None,
    top_score: float | None = None,
    created_at: datetime | None = None,
) -> _Obj:
    """一条假的 `qa_log` 行。字段名与 `models/qa_log.QaLog` 逐字一致。"""
    return _Obj(
        id=row_id,
        question=f"{MARK} 提问 {row_id}",
        question_embedding=pack_embedding(vector.tolist()),
        department_id=department_id,
        faq_hit=faq_hit,
        recalled_ids=list(recalled or []),
        allowed_ids=list(allowed or []),
        top_score=top_score,
        created_at=created_at or _dt(2026, 10, 14, 10, 0, 0),
    )


def _gap(
    gap_id: str,
    *,
    vector: np.ndarray,
    department_id: str | None = "dept_a",
    status: str = STATUS_OPEN,
    frequency: int = 1,
    last_seen_at: datetime | None = None,
) -> KnowledgeGap:
    return KnowledgeGap(
        id=gap_id,
        question=f"{MARK} 已存在的缺口",
        question_embedding=pack_embedding(vector.tolist()),
        department_id=department_id,
        frequency=frequency,
        max_score=None,
        suggested_category=None,
        status=status,
        last_seen_at=last_seen_at or _dt(2026, 10, 14, 9, 0, 0),
    )


def _compiled(stmt) -> str:
    """把 ORM 语句编译成 MySQL 方言的文本（离线可做，不连库）。"""
    from sqlalchemy.dialects import mysql as mysql_dialect

    return str(stmt.compile(dialect=mysql_dialect.dialect()))


def _seeded_permission_codes() -> set[str]:
    path = ROOT / "backend" / "scripts" / "seed_org.py"
    if not path.exists():
        return set()
    src = path.read_text(encoding="utf-8")
    return set(re.findall(r'"code":\s*"([a-z]+:[a-z_:]+)"', src))


def _seeded_roles() -> tuple[dict[str, str], dict[str, list[str]]]:
    """从 `seed_org.py` 解析出 `{perm_id: code}` 与 `{role_id: [perm_id]}`。

    用 AST 而不是正则：`ROLE_PERMISSIONS` 的值是**跨行**的字符串列表，
    正则要么漏要么把相邻的块粘起来，而「粘起来」这种错**照样能得出一个结果**
    —— 于是断言在错误的输入上「正常工作」。
    """
    path = ROOT / "backend" / "scripts" / "seed_org.py"
    if not path.exists():
        return {}, {}
    id_to_code: dict[str, str] = {}
    roles: dict[str, list[str]] = {}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        # ⚠️ `PERMISSIONS: list[dict] = [...]` 是 **AnnAssign** 不是 Assign
        #    （`node.targets` 只在 Assign 上有，AnnAssign 用 `node.target`）。
        #    只认 Assign 的话两个常量都解析不出来，函数静默返回空字典 ——
        #    于是本脚本会打印一条 SKIP，看起来像「环境不具备」而不是「我解析错了」。
        if isinstance(node, ast.Assign):
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(value, (ast.List, ast.Dict)):
            continue
        name = getattr(target, "id", None)
        if name == "PERMISSIONS" and isinstance(value, ast.List):
            for item in value.elts:
                if not isinstance(item, ast.Dict):
                    continue
                pairs = {
                    key.value: inner.value
                    for key, inner in zip(item.keys, item.values)
                    if isinstance(key, ast.Constant) and isinstance(inner, ast.Constant)
                }
                if "id" in pairs and "code" in pairs:
                    id_to_code[pairs["id"]] = pairs["code"]
        elif name == "ROLE_PERMISSIONS" and isinstance(value, ast.Dict):
            for key, inner in zip(value.keys, value.values):
                if isinstance(key, ast.Constant) and isinstance(inner, ast.List):
                    roles[key.value] = [
                        elt.value for elt in inner.elts if isinstance(elt, ast.Constant)
                    ]
    return id_to_code, roles


#: 身份字段名。出现在**接口签名**里就意味着「身份可以由调用方指定」。
_IDENTITY_PARAMS = {
    "user_id",
    "userid",
    "operator_id",
    "reviewer_id",
    "uploaded_by",
    "user_context",
    "current_user",
}


def _router_identity_params() -> tuple[list[tuple[str, str]], int]:
    """扫出 router 里带身份入参的端点 → `([(函数名, 参数名)], 扫过的端点数)`。

    返回扫过的端点数是为了**证明扫描真的跑了**：只回一个空列表时，
    「没有违规」与「AST 没解析出任何函数」是同一个结果，
    而后者会让这条红线断言变成一句永远为真的空话。

    🔴 反过来的写法（在本文件里 grep `ctx.user_id`）是**错的**，而且错得很隐蔽：
       `reviewer_id=ctx.user_id` 这类**从 JWT 读**的写法才是红线 1 **要求**的做法，
       它同样含 `ctx.user_id` 子串。上一版就是这么写的，结果
       「be-08 的审核端点从 ctx 取操作人」被判成了违规 —— 用例在正确的代码上报错。
       判据必须是「身份能不能由**调用方传进来**」，不是「代码里有没有出现 ctx」。
    """
    found: list[tuple[str, str]] = []
    scanned = 0
    for node in ast.walk(ast.parse(_router_src())):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not any(_is_route_decorator(d) for d in node.decorator_list):
            continue
        scanned += 1
        for arg in node.args.args + node.args.kwonlyargs:
            if arg.arg in _IDENTITY_PARAMS:
                found.append((node.name, arg.arg))
    return found, scanned


def _is_route_decorator(node: ast.expr) -> bool:
    """`@router.get("/gaps")` 是 **`ast.Call`**，不是 `ast.Attribute`。

    ⚠️ 直接 `isinstance(d, ast.Attribute)` 永远为假 ⇒ 扫到 0 个端点 ⇒
       「没有任何端点接受身份入参」变成一句**永远为真**的空话。
       本脚本第一次跑就是这么错的，靠「扫到几个端点」这条非空断言才暴露出来。
       过滤掉非 Call 就是过滤掉 `@staticmethod` 这类无形参装饰器 —— 那是对的。
    """
    inner = node.func if isinstance(node, ast.Call) else node
    return isinstance(inner, ast.Attribute) and inner.attr in {
        "get",
        "post",
        "put",
        "patch",
        "delete",
    }


# ═══════════════════════════════════════════════════════════════
# T1 离线
# ═══════════════════════════════════════════════════════════════


def t1_is_miss() -> None:
    print("\n== T1 is_miss 的四条分支与两条反例 ==")

    # --- 反例 ①：FAQ 直接作答 ---
    # 这条路径根本没检索，`allowed_ids` / `recalled_ids` 天然为空。
    # 若不短路掉，**每一条被 FAQ 答上的提问都会进缺口池** ——
    # 而 FAQ 恰恰最不可能是知识缺口（它就是从提问里沉淀出来的）。
    check(
        gap_engine.is_miss(
            faq_hit=True, recalled_ids=[], allowed_ids=[], top_score=None
        )
        is False,
        "反例 ①: faq_hit=True 即使无召回也**不是**缺口",
    )
    check(
        gap_engine.is_miss(
            faq_hit=True, recalled_ids=["c1"], allowed_ids=[], top_score=0.1
        )
        is False,
        "反例 ①: faq_hit=True 优先于其它一切条件（短路在第一位）",
    )

    # --- 真缺口：完全没有召回 ---
    check(
        gap_engine.is_miss(
            faq_hit=False, recalled_ids=[], allowed_ids=[], top_score=None
        )
        is True,
        "is_miss: 一条候选都没召回 ⇒ 缺口",
    )

    # --- 反例 ②：检索到了、但候选全被权限挡住 ---
    # 知识**存在**，缺的是权限。判成缺口会让运营去补一份已经有的材料，
    # 并把「本部门不该看到的话题」以提问文本的形式堆进治理页。
    check(
        gap_engine.is_miss(
            faq_hit=False,
            recalled_ids=["c1", "c2"],
            allowed_ids=[],
            top_score=None,
        )
        is False,
        "反例 ②: 有召回但全被权限挡下 ⇒ **不是**知识缺口（缺的是权限）",
    )
    # 🔴 这一条是本轮扩参的**直接证据**：`top_score` 只统计**准入集合**
    #    （`retrieval.py:268`），所以「高分但被挡」在它上面是 `None`，
    #    与「低分但准入」长得完全一样。只看 `top_score` + `allowed_ids`
    #    无法把「准许入的都很低分」与「高分但被挡」分开。
    check(
        gap_engine.is_miss(
            faq_hit=False, recalled_ids=["c1"], allowed_ids=[], top_score=0.95
        )
        is False,
        "反例 ②: 即使「看起来分很高」也不算缺口（分只统计准入集合，不能反推）",
    )

    # --- 低置信度：准入集合的最高分低于阈值 ---
    check(
        gap_engine.is_miss(
            faq_hit=False,
            recalled_ids=["c1"],
            allowed_ids=["c1"],
            top_score=settings.kb_min_score - 0.01,
        )
        is True,
        "is_miss: 有准入但最高分 < KB_MIN_SCORE ⇒ 低置信缺口",
    )
    check(
        gap_engine.is_miss(
            faq_hit=False,
            recalled_ids=["c1"],
            allowed_ids=["c1"],
            top_score=settings.kb_min_score,
        )
        is False,
        "is_miss: 分数**等于**阈值不算缺口（边界含在「答得上」一侧）",
    )
    # `top_score is None` 而准入集合非空：be-05 极少数路径可能不落分。
    # 判 False 是**保守**选择 —— 宁可漏记一条缺口，也不要凭「没数据」造出缺口。
    check(
        gap_engine.is_miss(
            faq_hit=False, recalled_ids=["c1"], allowed_ids=["c1"], top_score=None
        )
        is False,
        "is_miss: 有准入但无分数 ⇒ 不判缺口（不凭缺失数据造缺口）",
    )

    # --- 签名契约：四个入参，全 keyword-only ---
    params = inspect.signature(gap_engine.is_miss).parameters
    check(
        set(params) == {"faq_hit", "recalled_ids", "allowed_ids", "top_score"},
        f"is_miss: 签名是 4 个入参（实测 {sorted(params)}）—— "
        "冻结设计里的 2 参版本表达不了两条反例",
    )
    check(
        all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values()),
        "is_miss: 全 keyword-only（4 个 bool/list 位置传参必错，且不会报错）",
    )
    check(
        "kb_min_score" in _gap_src() and "faq_cluster_similarity" in _gap_src(),
        "is_miss: 阈值复用既有配置，**没有**新造重复的重复配置项",
    )


def t1_unit_vector_and_matcher() -> None:
    print("\n== T1 归一化契约与部门双键 ==")

    # --- `_unit_vector` 必须返回**已归一化**的向量 ---
    # 🔴 归一化有没有做，直接决定点积是不是余弦。漏做时相似度会整体偏移
    #    （模长越大分越高），而阈值看起来「只是有点不准」——
    #    **这个错不会报错，只会让阈值失效**。
    rng = np.random.default_rng(7)
    raw = (rng.random(sim.EMBED_DIM).astype(np.float32) * 10.0)  # 刻意非单位长
    unit = gap_engine._unit_vector(pack_embedding(raw.tolist()))
    check(unit is not None, "_unit_vector: 合法 BLOB 返回向量")
    if unit is not None:
        check(
            abs(float(np.linalg.norm(unit)) - 1.0) < 1e-5,
            f"_unit_vector: 模长归一化为 1（实测 {float(np.linalg.norm(unit)):.6f}）",
        )
        check(
            abs(float(unit @ unit) - 1.0) < 1e-5,
            "_unit_vector: 自点积 = 1（点积真的是余弦）",
        )
    check(
        gap_engine._unit_vector(None) is None,
        "_unit_vector: None 返回 None",
    )
    check(
        gap_engine._unit_vector(b"short") is None,
        "_unit_vector: 长度不符的 BLOB 返回 None（不抛）",
    )
    # 一处实现：它必须复用 similarity，不得自己 from_blob + l2_normalize
    body = _blank(inspect.getsource(gap_engine._unit_vector))
    check(
        "build_matrix" in body and "l2_normalize" not in body,
        "_unit_vector: 归一化复用 similarity.build_matrix（全项目只有一处归一化实现）",
    )

    # --- `_Matcher`：部门双键 ---
    threshold = settings.faq_cluster_similarity
    shared = _vec(11)
    matcher = gap_engine._Matcher(threshold)
    existing = _gap("g1", vector=shared, department_id="dept_a")
    matcher.add(existing, gap_engine._unit_vector(existing.question_embedding))

    hit_same = matcher.best("dept_a", shared)
    check(
        hit_same is not None and hit_same[0].id == "g1",
        "_Matcher: 同部门的相同问题匹配得上",
    )
    # 🔴 这一条是双键的**守门用例**。若实现写成「全量算完再按部门过滤」，
    #    跨部门的同问题会被判成同一条缺口 —— 而「部门」这个维度
    #    一旦塌掉**无法从数据里还原**（提问原文里没有部门信息）。
    check(
        matcher.best("dept_b", shared) is None,
        "_Matcher: **跨部门**的相同问题匹配不上（部门是双键的一半）",
    )
    check(matcher.best(None, shared) is None, "_Matcher: department_id=None 是独立的一格")

    # --- 脏行不进池 ---
    dirty = gap_engine._Matcher(threshold)
    bad = _gap("g_dirty", vector=shared)
    bad.question_embedding = b"broken"
    dirty.add(bad, None)
    check(
        dirty.best("dept_a", shared) is None,
        "_Matcher: 向量不可解析的缺口不进池（留着会让 argmax 的下标指错人）",
    )

    # --- 阈值边界 ---
    far = gap_engine._Matcher(threshold)
    other = _gap("g2", vector=_vec(999), department_id="dept_a")
    far.add(other, gap_engine._unit_vector(other.question_embedding))
    check(
        far.best("dept_a", shared) is None,
        "_Matcher: 相似度低于阈值的**不算命中**（否则会把不相干的问题并成一条）",
    )


def t1_contract_alignment() -> None:
    print("\n== T1 DTO ↔ 前端类型逐字段对齐（前端是真正的契约）==")

    ts_path = ROOT / "frontend" / "src" / "types" / "governance.ts"
    if not ts_path.exists():
        skip("DTO ↔ 前端类型对齐", "找不到 frontend/src/types/governance.ts")
        return
    ts = _ts_interfaces(ts_path.read_text(encoding="utf-8"))

    # (后端 DTO, 前端 interface)。**名字可以不同，字段必须逐字相同。**
    pairs = [
        (mining_schemas.KnowledgeGapItemDTO, "KnowledgeGapItem"),
        (mining_schemas.GapQuery, "GapQuery"),
        (mining_schemas.GapTaskResult, "GapTaskResult"),
    ]
    for dto, ts_name in pairs:
        if ts_name not in ts:
            check(False, f"{ts_name}: 前端类型里找不到该 interface")
            continue
        missing = set(ts[ts_name]) - set(dto.model_fields)
        check(
            not missing,
            f"{dto.__name__} ⊇ {ts_name}（前端要而后端缺的：{sorted(missing) or '无'}）",
        )

    # 🔴 `department_name` 已在字典 §9 #146 撤回：缺口可能来自**别的**部门，
    #    前端只有自己那条部门链，后端不内联名字就永远查不到 ——
    #    这个字段只会是 `undefined`，留着比删掉更糟（看起来像「后端没实现」）。
    check(
        "department_name" not in mining_schemas.KnowledgeGapItemDTO.model_fields,
        "KnowledgeGapItemDTO: 不含 department_name（§9-146 撤回，缺了只能是 undefined）",
    )
    check(
        "department_name" not in ts.get("KnowledgeGapItem", []),
        "KnowledgeGapItem: 前端也已撤掉 department_name（两处同动）",
    )

    # 向量不进 DTO
    check(
        "question_embedding" not in mining_schemas.KnowledgeGapItemDTO.model_fields,
        "KnowledgeGapItemDTO: 不含 question_embedding（与前端契约一致）",
    )

    # `task_id` 由可选收紧为必回
    check(
        mining_schemas.GapTaskResult.model_fields["task_id"].is_required(),
        "GapTaskResult.task_id: 必回（前端拿它跳导入中心，少回只能给一句「操作成功」）",
    )

    # `min_frequency` 下界是 1 而不是 0（§9-147）：0 与 1 效果相同，
    # 允许 0 只会让「0 是不是表示不过滤」变成下一个人要猜的问题。
    min_freq = mining_schemas.GapQuery.model_fields["min_frequency"]
    check(
        [getattr(m, "ge", None) for m in min_freq.metadata] == [1],
        "GapQuery.min_frequency: 下界 = 1（§9-147）",
    )
    # 待确认编号销账
    for num in (144, 145, 146, 147):
        check(
            f"# {num} " in (ROOT / "vibecoding_doc" / "核心数据字典.md").read_text(
                encoding="utf-8"
            )
            or f"| {num} |" in (ROOT / "vibecoding_doc" / "核心数据字典.md").read_text(
                encoding="utf-8"
            ),
            f"字典 §9: #{num} 已登记（本轮新增的待确认都有编号）",
        )


def t1_routes() -> None:
    print("\n== T1 路由注册与权限码 ==")

    paths = main.app.openapi()["paths"]
    # ⚠️ Starlette 1.7 的 `include_router` 不再把子路由摊平进 `app.routes`
    #    （包成 `_IncludedRouter`），数 `app.routes` 会漏报 —— 用 openapi。
    check("/api/mining/gaps" in paths, "路由: GET /api/mining/gaps 已注册")
    check(
        "/api/mining/gaps/{gap_id}/task" in paths,
        "路由: POST /api/mining/gaps/{gap_id}/task 已注册",
    )
    check(
        "get" in paths.get("/api/mining/gaps", {}),
        "路由: 清单是 GET",
    )
    check(
        "post" in paths.get("/api/mining/gaps/{gap_id}/task", {}),
        "路由: 转建是 POST",
    )
    # 壳子里的 `data: None` 占位必须已被真实现替换
    router_code = _blank(_router_src())
    check(
        "接口待实现" not in _router_src(),
        "路由: 壳子的「接口待实现」占位已清除",
    )
    check(
        "TODO(be-09)" not in _router_src(),
        "路由: 壳子的 TODO(be-09) 已清除",
    )

    # --- 权限码 ---
    gap_region = _router_src().split("知识缺口（be-09）", 1)[-1]
    codes = re.findall(r'require_permission\("([^"]+)"\)', gap_region)
    check(
        sorted(codes) == ["gap:list", "gap:task:create"],
        f"权限码: 两个端点各挂**自己的**码（实测 {sorted(codes)}）",
    )
    check(
        "gap:list" in gap_region and "gap:task:create" in gap_region,
        "权限码: gap:list（读）/ gap:task:create（写）",
    )
    # 读端点也必须挂码 —— 缺口清单含**别人部门**的提问原文，
    # 而侧栏不按权限过滤菜单，staff 会真的打开治理中心并发这个请求（红线 4 / 7）。
    get_block = gap_region.split('@router.get("/gaps"', 1)[-1].split("@router.post", 1)[0]
    check(
        "require_permission" in get_block,
        "权限码: 读端点也挂了码（不是「登录即可」——清单含跨部门提问原文）",
    )
    check(
        "user_context" not in router_code,
        "红线 1: 路由里不存在 user_context（PRD §11.3 R3）",
    )
    # 🔴 判据是「身份**能不能由调用方传进来**」，不是「代码里有没有出现 ctx」。
    #    见 `_router_identity_params` 的注释：反着写的版本会把 be-08 的
    #    `reviewer_id=ctx.user_id`（**正确的**写法）判成违规。
    intruders, scanned = _router_identity_params()
    check(
        scanned >= 8,
        f"红线 1: 扫描器真的解析到了 {scanned} 个端点（本文件 be-08 六 + be-09 二）"
        " —— 否则下面的「无违规」只是一句空话",
    )
    check(
        not intruders,
        f"红线 1: router **没有任何**端点接受身份入参（实测 {intruders or '无'}）",
    )
    check(
        "operator=ctx" in router_code,
        "红线 1: 转建的操作人直接传 `ctx`（引擎只认 AuthContext，不认裸 id）",
    )
    check(
        'ctx: AuthContext = Depends(require_permission("gap:task:create"))' in router_code,
        "红线 1: 操作人来自 JWT 依赖注入，不来自 query/path/body",
    )

    # 入参 DTO 里不得有身份字段
    identity_names = {"user_id", "operator_id", "reviewer_id", "user_context", "ctx"}
    for payload in (mining_schemas.GapQuery, mining_schemas.GapTaskResult):
        overlap = identity_names & set(payload.model_fields)
        check(
            not overlap,
            f"红线 1: {payload.__name__} 无身份入参字段（实测多出 {sorted(overlap) or '无'}）",
        )

    # --- 三处同动：字典 §5.6.5 + 种子 + 前端常量 ---
    id_to_code, roles = _seeded_roles()
    seeded = _seeded_permission_codes()
    if not id_to_code or not roles:
        skip("权限码: 种子里的 gap:* 与角色分配", "解析不出 seed_org.py 的权限/角色结构")
    else:
        check(
            {"gap:list", "gap:task:create"} <= seeded,
            f"权限码: 两条 gap 码都在 sys_permission 种子里（实测缺 "
            f"{sorted({'gap:list', 'gap:task:create'} - seeded) or '无'}）",
        )
        # 路由里出现的每个码都必须在种子里 —— 否则按钮对**所有人**（含 sys_admin）
        # 静默不可见，且前后端都不报错。本项目上一轮就是这么错的
        # （前端 13 个码、库里 7 个、只重合 4 个）。
        invented = set(codes) - seeded
        check(not invented, f"权限码: 路由的码全部存在于种子（自造的：{sorted(invented) or '无'}）")

        def _granted(role_id: str) -> set[str]:
            return {id_to_code.get(pid, pid) for pid in roles.get(role_id, [])}

        check(
            {"gap:list", "gap:task:create"} <= _granted("role_01"),
            "权限码: sys_admin(role_01) 有两条",
        )
        check(
            {"gap:list", "gap:task:create"} <= _granted("role_02"),
            "权限码: kb_admin(role_02) 有两条（缺口治理正是它的日常职责）",
        )
        check(
            not ({"gap:list", "gap:task:create"} & _granted("role_03")),
            "权限码: staff(role_03) **一条不给**（清单含跨部门提问原文 + 转建是写接口）",
        )

    fe_path = ROOT / "frontend" / "src" / "constants" / "governance.ts"
    if fe_path.exists():
        fe_src = fe_path.read_text(encoding="utf-8")
        check(
            "GAP_LIST: 'gap:list'" in fe_src,
            "权限码: 前端 PERM.GAP_LIST = 'gap:list'（与后端种子逐字一致）",
        )
        check(
            "GAP_TASK_CREATE: 'gap:task:create'" in fe_src,
            "权限码: 前端 PERM.GAP_TASK_CREATE = 'gap:task:create'",
        )
    else:
        skip("权限码: 前端常量一致", "找不到 frontend/src/constants/governance.ts")

    dict_src = (ROOT / "vibecoding_doc" / "核心数据字典.md").read_text(encoding="utf-8")
    for code in ("gap:list", "gap:task:create"):
        check(f"`{code}`" in dict_src, f"权限码: 字典 §5.6 记了 {code}（三处的第三处）")


async def t1_redlines() -> None:
    print("\n== T1 安全红线静态断言 ==")

    src = _gap_src()
    code = _blank(src)

    # --- 分层铁律 ---
    check(
        "fastapi" not in code.lower(),
        "分层: engines/gap.py **不 import FastAPI**（CLAUDE.md 分层铁律）",
    )
    # 🔴 本模块**不直接 INSERT `kb_import_task`**：建任务走 be-04 的门面。
    #    直接 INSERT 会让「补充任务」这个语义有两份实现，
    #    而两份的不一致（比如一边写 batch_id 一边不写）不会有任何报错。
    check(
        "create_supplement_task" in code,
        "分层: 建任务经 be-04 的门面 `create_supplement_task`",
    )
    check(
        "KbImportTask(" not in code,
        "分层: 不直接构造 `kb_import_task`（只 select 它做幂等判定）",
    )
    check(
        "from backend.engines.import_engine import create_supplement_task" in src,
        "分层: 走 import_engine 的门面，**不**直接 import lifecycle（门面是唯一入口）",
    )

    # --- 红线 4：未授权内容不返回正文/标题 ---
    check(
        "question_embedding" not in mining_schemas.KnowledgeGapItemDTO.model_fields,
        "红线 4: 缺口 DTO 不带向量",
    )
    # 日志里不得出现提问原文。⚠️ 用 `\.question\b` 而不是子串 `question` ——
    #    `question_embedding` 里也含 "question"，而那一处记的是**行 id**、是正当的。
    #    （`\b` 在 `_` 前不成立，所以两者能干净地分开。）
    log_calls = re.findall(r"logger\.\w+\((?:[^()]|\([^()]*\))*\)", code, re.S)
    leaked = [call for call in log_calls if re.search(r"\.\s*question\b", call)]
    check(
        not leaked,
        f"红线 4: 日志**只记数量与 id**，不记提问原文（泄漏的调用：{leaked or '无'}）",
    )
    check(
        len(log_calls) >= 3,
        f"红线 4: 日志调用被真的解析到了（{len(log_calls)} 处）—— 用例不是在空集上通过",
    )
    check(
        "AggregateResult" in code and "skipped_by_acl" in code,
        "红线 4: 被权限挡下的行只**计数**（进 skipped_by_acl），不落提问原文",
    )

    # --- 红线 6 / 8 相关：不拼 SQL ---
    check(
        "execute(text(" not in code and 'f"SELECT' not in code,
        "红线 8: 没有裸 SQL 拼接",
    )
    # 「参数化」必须**真的编一次语句**才知道，不能靠形状判断。
    # 塞一个注入串进窗口参数，看它有没有落进 SQL 文本。
    from sqlalchemy import select as sa_select

    from backend.models.qa_log import QaLog

    probe = f"{MARK}' OR 1=1 -- "
    compiled = _compiled(
        sa_select(QaLog.id).where(QaLog.question.like(f"%{probe}%"))
    )
    check(probe not in compiled, "红线 8: 含注入串的值不进 SQL 文本")
    check("LIKE %s" in compiled, "红线 8: LIKE 右侧是绑定参数 %s")

    # --- 聚合 SQL 的三条硬约束（都是「反了不报错」的那种）---
    # ⚠️ 这一小段必须 `await` 而不是自建事件循环 —— 本函数已经跑在 `run()` 的
    #    循环里，`loop.run_until_complete` 会直接抛
    #    `Cannot run the event loop while another loop is running`。
    session = _ScriptedSession(scalars_seq=[[], []])
    await gap_engine.aggregate_gaps(
        session, since=_dt(2026, 10, 14, 9, 0, 0), until=_dt(2026, 10, 14, 12, 0, 0)
    )
    miss_sql = _compiled(session.statements[0])
    active_sql = _compiled(session.statements[1])

    # ⚠️ 编译出来是 `qa_log.faq_hit IS false` —— SQLAlchemy 对
    #    `Column.is_(False)` 生成 `IS false`，不是 `= false`。
    #    断言写成 `= false` 会永久红着，而「永久红的用例最终会被注释掉」。
    check(
        "qa_log.faq_hit IS false" in miss_sql,
        "SQL: 未命中行在 **SQL 侧**就排除 faq_hit（FAQ 命中行占多数，"
        "全捞出来再逐条短路是白读）",
    )
    # 🔴 **必须升序**。写成 DESC 会让「同一问题在窗口内的两条提问」中较早的那条
    #    碰到较晚那条刚抬高的水位线，被误判「已计过」而**静默少计**
    #    （频次偏低不会触发任何告警，只会让运营觉得「这问题没多少人问」）。
    check(
        "ORDER BY qa_log.created_at ASC" in miss_sql,
        "SQL: 未命中行按 created_at **升序**（反了会静默少计）",
    )
    check("LIMIT" in miss_sql, "SQL: 单轮扫描有上限（窗口算错时不会把全表读进内存）")
    check(
        "status IN" in active_sql,
        "SQL: 只有活跃状态（open/tasked）的缺口参与匹配",
    )
    check(
        set(ACTIVE_STATUSES) == {STATUS_OPEN, STATUS_TASKED},
        f"SQL: ACTIVE_STATUSES = {{open, tasked}}（实测 {sorted(ACTIVE_STATUSES)}）",
    )

    # --- 自动关闭的两条口径 ---
    close_body = _blank(inspect.getsource(gap_engine._auto_close))
    # 🔴 「已命中」必须包含 `faq_hit=True` —— 「补料后已被沉淀成 FAQ」
    #    是最强的闭环证据。而 `faq_hit` 恰好是 `is_miss` 反例 ① **排除**的那一类：
    #    **两处口径必须成对地想**，不能一处排除、另一处又指望它。
    check(
        "row.faq_hit or" in close_body,
        "自动关闭: 「已命中」**包含** faq_hit=True（补料后沉淀成 FAQ 是最强证据）",
    )
    check(
        "kb_min_score" in close_body,
        "自动关闭: 走检索那条路的判据与 is_miss 同一个阈值",
    )
    close_params = inspect.signature(gap_engine._auto_close).parameters
    # 🔴 扫描下界**不能**用聚合窗口的 `since`：缺口被转建的时刻可能**晚于**
    #    命中提问发生的时刻（管理员隔了半天才处理），那条提问落在更早的窗口里，
    #    而之后每一轮又都扫不到它 ⇒ 这条缺口**永远关不上**，且没有任何报错。
    check(
        "since" not in close_params,
        f"自动关闭: 签名里**没有** since（实测 {sorted(close_params)}）—— "
        "用聚合窗口下界会漏掉「转建晚于命中」的收口",
    )
    check(
        "floor = min(" in close_body,
        "自动关闭: 扫描下界取「这批缺口里最早的水位线」",
    )
    check(
        "created_at.desc()" in close_body,
        "自动关闭: 取**最近**的行（截断时丢掉最老的历史比丢掉最新证据代价小）",
    )

    # --- 审计：状态流转与转建都要留痕 ---
    check(
        "GAP_STATUS_UPDATE" in code,
        "审计: 自动关闭记 `gap.status.update`（be-06 §5.4 把这条列在 be-09 名下）",
    )
    check(
        "GAP_TASK_CREATE" in code,
        "审计: 转建记 `gap.task.create`",
    )
    check(
        "_SYSTEM_CTX" in code and "SYSTEM_USER_ID" in src,
        "审计: 自动关闭的 user_id 记 `system`（不新造「auto」之类的第二个系统 id）",
    )
    check(
        gap_engine._SYSTEM_CTX.user_id == SYSTEM_USER_ID == "system",
        f"审计: 系统上下文 user_id = 'system'（实测 {gap_engine._SYSTEM_CTX.user_id!r}）",
    )
    # `record()` 搭调用方事务、**不 commit**；本模块末尾有 commit。
    check(
        "await db.commit()" in code,
        "审计: 本模块自己有 commit（`record()` 不 commit，搭的就是它）",
    )
    check(
        "record_detached" not in code,
        "审计: 不用 `record_detached`（那会自开一条连接白多一次写）",
    )

    # --- 分层：跨模块契约的归属 ---
    check(
        "_find_open_task" in code,
        "幂等: 转建前先查「是否已有在跑的补充任务」（`_find_open_task`）",
    )
    check(
        "KbImportTask.status.in_(KbImportTask.ACTIVE_STATUSES)" in code,
        "幂等: 复用判定用 be-04 的 ACTIVE_STATUSES（同一个语义只有一份定义）",
    )


def t1_config_and_beat() -> None:
    print("\n== T1 配置与 beat 排班 ==")

    check(
        settings.gap_aggregate_lookback_hours >= 1,
        f"配置: GAP_AGGREGATE_LOOKBACK_HOURS ≥ 1（实测 {settings.gap_aggregate_lookback_hours}）",
    )
    check(
        isinstance(settings.gap_auto_close_enabled, bool),
        "配置: GAP_AUTO_CLOSE_ENABLED 是布尔（自动关闭不可逆，必须能关掉）",
    )
    # 🔴 上限的**唯一**执行点是 `GapQuery` 的字段约束，而不是路由签名上的
    #    `Query(..., le=200)`（壳子那行已被 `GapQuery` 取代）。
    #    ⚠️ 这里刻意**不**去 router 里 grep `le=200`：那个子串在实现后就不存在了，
    #    断言它会永久红着。
    #    要守的是「上限的值没被悄悄改小」+「越界是**被拒**而不是被静默截断」这两件事。
    gap_query_body = _blank(inspect.getsource(mining_schemas.GapQuery))
    check(
        "le=settings.gap_page_size_max" in gap_query_body,
        "配置: GapQuery 的 page_size 上限**真的**接到配置上（不是硬编码数字）",
    )
    # 🔴 `min(value, ...)` 的静默截断与「越界被拒」是**两种不同的契约**，
    #    而它们对客户端的表现只差一个状态码 —— 本脚本的 T2-4 指望的是「被拒」。
    #    曾经实现是截断，于是 T1 绿、T2 红，同一条约束两处口径打架。
    #    这里把「不许再出现截断」写成反断言，免得它悄悄回来。
    check(
        "min(value, settings.gap_page_size_max)" not in gap_query_body,
        "配置: 上限是**拒绝**（le=）而非静默截断（min）—— 与其它五域同口径",
    )
    check(
        "ge=1" in gap_query_body,
        "配置: page_size 下界 1（0 会让 LIMIT 0 静默返回空列表）",
    )
    # 200 是路由壳子冻结的值，实现期不得擅自改动。
    check(
        settings.gap_page_size_max == 200,
        f"配置: GAP_PAGE_SIZE_MAX = 200，与壳子冻结的 `le=200` 同值"
        f"（实测 {settings.gap_page_size_max}）",
    )
    # 🔴 这个超时必须**真的传下去**。只定义不传等于没有，且不会报错 ——
    #    只表现为「偶尔一整轮聚合慢了 60 秒」，没人能解释。
    check(
        0 < settings.gap_llm_suggest_timeout_seconds < settings.deepseek_timeout_seconds,
        f"配置: GAP_LLM_SUGGEST_TIMEOUT_SECONDS({settings.gap_llm_suggest_timeout_seconds}s)"
        f" < DEEPSEEK_TIMEOUT_SECONDS({settings.deepseek_timeout_seconds}s)"
        "（分类推荐是可选项，不该占满整轮预算）",
    )
    call = _blank(inspect.getsource(gap_engine.suggest_category))
    check(
        "timeout_seconds=settings.gap_llm_suggest_timeout_seconds" in call,
        "配置: 该超时**真的**传给了 llm_client.chat（只定义不传等于没有）",
    )
    check(
        "timeout_seconds" in _blank(
            (ROOT / "backend" / "clients" / "llm_client.py").read_text(encoding="utf-8")
        ),
        "配置: llm_client.chat **支持**按调用方传超时（否则那个配置无处可施）",
    )
    check(
        "GAP_AGGREGATE_LOOKBACK_HOURS" in (ROOT / ".env.example").read_text(encoding="utf-8"),
        "配置: 四个 GAP_* 已进 .env.example（环境变量的唯一清单）",
    )

    celery_src = (ROOT / "backend" / "tasks" / "celery_app.py").read_text(encoding="utf-8")
    tasks_src = (ROOT / "backend" / "tasks" / "mining_tasks.py").read_text(encoding="utf-8")
    check(
        "mining.aggregate_knowledge_gaps" in tasks_src
        and "mining.aggregate_knowledge_gaps" in celery_src,
        "beat: 任务名在 **beat 计划**与**任务定义**处逐字一致（对不上 beat 静默跳过）",
    )
    beat_block = celery_src.split('"aggregate-knowledge-gaps"', 1)
    check(len(beat_block) == 2, "beat: 存在 aggregate-knowledge-gaps 计划")
    if len(beat_block) == 2:
        block = beat_block[1].split("},", 1)[0]
        check('minute="*/10"' in block, "beat: 每 10 分钟一轮")
    check(
        "backend.tasks.mining_tasks" in celery_src,
        "beat: mining_tasks 在 celery include 列表里（漏写 = Received unregistered task，任务丢且不报错）",
    )
    # 🔴 `bind=True` 是 `self.retry()` 的前提。壳子原本没有它，
    #    少了它会直接 `TypeError`（而 `retry_backoff` 那类配置只是被静默忽略）。
    task_src = tasks_src.split("def aggregate_knowledge_gaps", 1)
    check(len(task_src) == 2, "Celery: 找到 aggregate_knowledge_gaps 定义")
    if len(task_src) == 2:
        decorator = task_src[0].rsplit("@celery_app.task", 1)[-1].split("\n)")[0]
        check("bind=True" in decorator, "Celery: 任务带 `bind=True`（否则 self.retry() 报 TypeError）")
    check(
        "countdown = min(" in tasks_src,
        "Celery: 手动 retry **自算退避**（retry_backoff 对 self.retry() 不生效，"
        "不给 countdown 会退回 180 秒）",
    )
    check("fresh_session()" in tasks_src, "Celery: 任务走 fresh_session")
    check(
        "SessionLocal" not in _blank(tasks_src),
        "Celery: 任务**不出现** SessionLocal（模块级 engine 绑在创建它的事件循环上）",
    )


def t1_migration_consistency() -> None:
    print("\n== T1 迁移 0007 与模型一致 ==")

    path = ROOT / "alembic" / "versions" / "0007_create_knowledge_gap.py"
    if not path.exists():
        check(False, "迁移: 0007_create_knowledge_gap.py 存在")
        return
    src = path.read_text(encoding="utf-8")
    check(
        'down_revision: str | None = "0006"' in src,
        "迁移: down_revision = 0006（接在 be-08 之后）",
    )
    check("_CHARSET" in src and "utf8mb4" in src, "迁移: 表级 utf8mb4（字典 §1）")
    check(
        "ForeignKey" not in src,
        "迁移: **无外键**（项目一贯用应用层管一致性，加 FK 会把两边的删除顺序焊死）",
    )

    block = src.split('"knowledge_gap",', 1)
    if len(block) != 2:
        check(False, "迁移: 找到 create_table(knowledge_gap)")
    else:
        body = block[1].split("_CHARSET", 1)[0]
        migrated = set(re.findall(r'sa\.Column\(\s*"(\w+)"', body))
        modeled = {column.name for column in sa_inspect(KnowledgeGap).columns}
        check(
            migrated == modeled,
            f"迁移/模型一致: knowledge_gap（迁移多 {sorted(migrated - modeled) or '无'} / "
            f"模型多 {sorted(modeled - migrated) or '无'}）",
        )
        # 🔴 `last_seen_at` **同时是业务字段与聚合水位线**。
        #    漏了这一列则每轮重复累加（frequency 虚高 18 倍），
        #    而「虚高」只表现为「运营觉得这问题好热门」，不报任何错。
        check(
            "last_seen_at" in migrated and "nullable=False" in body.split('"last_seen_at"', 1)[1][:80],
            "迁移: last_seen_at NOT NULL（它同时是幂等水位线，不能为空）",
        )
        check(
            "user_id" not in migrated,
            "迁移: knowledge_gap **没有 user_id**（缺口是部门粒度的运营数据，"
            "落提问人会让治理页变成人员监控视图）",
        )
        check(
            "question_embedding" in migrated,
            "迁移: question_embedding 在（聚合与自动关闭都要用它）",
        )
    for index_name in ("ix_knowledge_gap_status", "ix_knowledge_gap_department_id"):
        check(index_name in src, f"迁移: 建了索引 {index_name}")

    # kb_import_task.gap_id
    check(
        '"gap_id"' in src and 'op.add_column' in src,
        "迁移: 用 add_column 给 kb_import_task 补 gap_id（不重建表，线上可能有数据）",
    )
    check(
        "ix_kb_import_task_gap_id" in src,
        "迁移: gap_id 建了索引（转建幂等键按它查）",
    )
    check(
        "drop_column" in src,
        "迁移: downgrade 会撤掉 gap_id（能回滚，不是单向迁移）",
    )
    model_cols = {c.name for c in sa_inspect(KbImportTask).columns}
    check("gap_id" in model_cols, "模型: KbImportTask 有 gap_id 列（与迁移对齐）")
    # 迁移里声明的 gap_id 与模型的类型/可空性要一致
    gap_col = KbImportTask.__table__.c.gap_id
    check(gap_col.nullable is True, "模型: gap_id 可空（历史任务都不是缺口转建来的）")
    check(
        "nullable=True" in src.split('"gap_id", sa.String(64)', 1)[-1][:40],
        "迁移: gap_id 同样可空（两处对齐）",
    )
    check(gap_col.index is True, "模型: gap_id 带 index=True（与迁移的 create_index 对齐）")


# ═══════════════════════════════════════════════════════════════
# T1.5 假会话
# ═══════════════════════════════════════════════════════════════


async def _agg(session, *, since=None, until=None):
    return await gap_engine.aggregate_gaps(
        session,
        since=since or _dt(2026, 10, 14, 9, 0, 0),
        until=until or _dt(2026, 10, 14, 12, 0, 0),
    )


async def t15_suggest_category() -> None:
    print("\n== T1.5 suggest_category 的降级与超时 ==")

    original = gap_engine.llm_client.chat
    captured: dict = {}

    async def _raise(*args, **kwargs):
        captured.update(kwargs)
        raise DependencyError("LLM 不可达")

    async def _reply(text: str, *args, **kwargs):
        captured.update(kwargs)
        return _Obj(content=text)

    try:
        gap_engine.llm_client.chat = _raise
        check(
            await gap_engine.suggest_category(f"{MARK} 报销流程") is None,
            "suggest: LLM 不可用时返回 None 而**不抛**（缺口照常入池）",
        )
        check(
            captured.get("timeout_seconds") == settings.gap_llm_suggest_timeout_seconds,
            f"suggest: 独立超时被传下去（实测 timeout_seconds={captured.get('timeout_seconds')}）",
        )

        gap_engine.llm_client.chat = lambda *a, **k: _reply("  人力资源。  ", *a, **k)
        check(
            await gap_engine.suggest_category(f"{MARK} 年假几天") == "人力资源",
            "suggest: 剥掉空白与中文标点后返回纯分类名",
        )

        gap_engine.llm_client.chat = lambda *a, **k: _reply("", *a, **k)
        check(
            await gap_engine.suggest_category(f"{MARK} 空回复") is None,
            "suggest: 空回复 → None",
        )

        # 🔴 超长**判失败而不是截断**：截断会得到一个看起来正常的半个词
        #    （如「人力资源与薪」），它会被当成有效分类一路带到导入中心，
        #    而成因（模型没听话）永远查不出来。
        long_text = "这个问题属于：人力资源 / 薪酬福利，因为涉及到员工的年假计算规则"
        gap_engine.llm_client.chat = lambda *a, **k: _reply(long_text, *a, **k)
        check(
            await gap_engine.suggest_category(f"{MARK} 长回复") is None,
            "suggest: 回了一整句话 → None（截断会得到一个看似正常的半个词）",
        )
    finally:
        gap_engine.llm_client.chat = original


async def t15_aggregate() -> None:
    print("\n== T1.5 聚合：频次、幂等、顺序、双键 ==")

    original_suggest = gap_engine.suggest_category
    calls: list[str] = []

    async def _no_suggest(question: str) -> str | None:
        calls.append(question)
        return None

    gap_engine.suggest_category = _no_suggest
    try:
        window = [_dt(2026, 10, 14, 10, 0, 0), _dt(2026, 10, 14, 12, 0, 0)]
        base = _vec(101)

        # --- 30 条同一问题（同一部门、无既有缺口）→ 1 条缺口，frequency = 30 ---
        rows = [
            _row(
                f"{MARK}_a{i}",
                vector=base,
                created_at=_dt(2026, 10, 14, 10, i, 0),
            )
            for i in range(30)
        ]
        session = _ScriptedSession(scalars_seq=[rows, [], []])
        result = await _agg(session)
        check(
            result.created == 1,
            f"A8: 30 条同一问题 → **1 条**缺口（实测 created={result.created}）",
        )
        check(
            result.updated == 29,
            f"A8: 其余 29 条累加到同一条（实测 updated={result.updated}）",
        )
        check(len(session.added) == 1, "A8: 只 add 了一个缺口对象")
        created = session.added[0] if session.added else None
        if created is not None:
            check(
                created.frequency == 30,
                f"A8: frequency = 30（实测 {created.frequency}）",
            )
            check(
                created.last_seen_at == _dt(2026, 10, 14, 10, 29, 0),
                f"A8: last_seen_at = 最后一条提问的时间（实测 {created.last_seen_at}）",
            )
            check(
                created.status == STATUS_OPEN,
                "A8: 新缺口状态为 open",
            )
        check(
            len(calls) == 1,
            f"LLM: 分类推荐只调**一次**（同一问题问 30 次不该推 30 次，实测 {len(calls)}）",
        )
        check(result.scanned == 30, f"A8: scanned = 30（实测 {result.scanned}）")

        # --- 幂等：第二轮（窗口重叠）不得再累加 ---
        # 🔴 这是本模块最要命的一条。beat 每 10 分钟一轮、回溯窗口 3 小时
        #    ⇒ 没有水位线的话同一条 `qa_log` 会被累加 **18 次**，
        #    `frequency` 虚高一个数量级，而 A8 断言的是「≈ 30」。
        session2 = _ScriptedSession(scalars_seq=[rows, [created], []])
        result2 = await _agg(session2)
        check(
            result2.updated == 0,
            f"幂等: 第二轮**零累加**（实测 updated={result2.updated}）—— "
            "水位线就是缺口自己的 last_seen_at",
        )
        check(result2.created == 0, f"幂等: 第二轮零新建（实测 created={result2.created}）")
        check(not session2.added, "幂等: 第二轮没有 add 任何对象")
        if created is not None:
            check(
                created.frequency == 30,
                f"幂等: frequency 仍是 30，没有被 18 倍虚高（实测 {created.frequency}）",
            )

        # --- 新建的缺口必须**立刻**进匹配池 ---
        # 🔴 少了这一步，同一窗口内第 2 条同问题的提问匹配不到刚建这条，
        #    会再建一条 ⇒ 同一问题两条缺口、各 frequency=1。
        #    这与 be-08 的 O(N²) 是同一个「在集合上迭代时集合会变」的坑。
        fresh = [
            _row(f"{MARK}_b{i}", vector=_vec(202), created_at=_dt(2026, 10, 14, 10, i, 0))
            for i in range(2)
        ]
        session3 = _ScriptedSession(scalars_seq=[fresh, [], []])
        result3 = await _agg(session3)
        check(
            result3.created == 1 and result3.updated == 1,
            f"匹配池: 窗口内新建的缺口**立刻**能匹配（created={result3.created} / "
            f"updated={result3.updated}，期望 1/1）",
        )
        check(
            len(session3.added) == 1,
            f"匹配池: 只建了 1 条而不是 2 条（实测 {len(session3.added)}）",
        )

        # --- 升序处理 ---
        # 🔴 顺序反了会让「较早的那条」碰到「较晚那条刚抬高的水位线」，
        #    被误判成「已计过」而**静默少计** —— 频次偏低不触发任何告警。
        asc = [
            _row(f"{MARK}_c{i}", vector=_vec(303), created_at=_dt(2026, 10, 14, 10, i, 0))
            for i in range(3)
        ]
        session4 = _ScriptedSession(scalars_seq=[asc, [], []])
        result4 = await _agg(session4)
        if session4.added:
            check(
                session4.added[0].frequency == 3,
                f"顺序: 3 条升序数据聚成 frequency=3（实测 {session4.added[0].frequency}）"
                "—— 若按 DESC 处理这里会是 1",
            )
        else:
            check(False, "顺序: 应当新建一条缺口")
        check(result4.created == 1, "顺序: 仍是 1 条缺口")

        # --- 部门双键：同问题、不同部门 → 两条 ---
        twin = [
            _row(f"{MARK}_d0", vector=_vec(404), department_id="dept_a",
                 created_at=_dt(2026, 10, 14, 10, 0, 0)),
            _row(f"{MARK}_d1", vector=_vec(404), department_id="dept_b",
                 created_at=_dt(2026, 10, 14, 10, 1, 0)),
        ]
        session5 = _ScriptedSession(scalars_seq=[twin, [], []])
        result5 = await _agg(session5)
        check(
            result5.created == 2 and result5.updated == 0,
            f"双键: 同问题在两个部门 → **两条**缺口（created={result5.created} / "
            f"updated={result5.updated}，期望 2/0）",
        )
        check(
            {g.department_id for g in session5.added} == {"dept_a", "dept_b"},
            "双键: 两条缺口各自带上自己的 department_id",
        )

        # --- 反例 ②：被权限挡下的行只计数、不入池 ---
        blocked = [
            _row(f"{MARK}_e0", vector=_vec(505), recalled=["c1", "c2"], allowed=[]),
            _row(f"{MARK}_e1", vector=_vec(506), recalled=["c3"], allowed=[]),
        ]
        session6 = _ScriptedSession(scalars_seq=[blocked, [], []])
        result6 = await _agg(session6)
        check(
            result6.skipped_by_acl == 2,
            f"反例 ②: 2 条「有召回但全被挡」计入 skipped_by_acl（实测 "
            f"{result6.skipped_by_acl}）",
        )
        check(result6.created == 0, "反例 ②: 它们**不**建缺口（缺的是权限不是知识）")
        check(result6.scanned == 0, "反例 ②: 也不计入 scanned（scanned 是「真未命中」）")

        # --- 低置信度进池 ---
        low = [
            _row(
                f"{MARK}_f0",
                vector=_vec(606),
                recalled=["c1"],
                allowed=["c1"],
                top_score=settings.kb_min_score - 0.05,
            )
        ]
        session7 = _ScriptedSession(scalars_seq=[low, [], []])
        result7 = await _agg(session7)
        check(result7.created == 1, "低置信度: 有准入但分数低 ⇒ 建缺口")
        if session7.added:
            check(
                session7.added[0].max_score == settings.kb_min_score - 0.05,
                "低置信度: max_score 落库 = 该行最高分",
            )

        # --- 脏向量行跳过、不抛 ---
        dirty = [
            _row(f"{MARK}_g0", vector=_vec(707)),
            _row(f"{MARK}_g1", vector=_vec(708)),
        ]
        dirty[0].question_embedding = b"broken"
        session8 = _ScriptedSession(scalars_seq=[dirty, [], []])
        result8 = await _agg(session8)
        check(
            result8.created == 1,
            f"脏行: 长度不符的行被跳过而**不抛**（实测 created={result8.created}，期望 1）",
        )

        # --- 窗口反向：任务侧已挡，引擎侧照常处理空窗口 ---
        session9 = _ScriptedSession(scalars_seq=[[], []])
        result9 = await _agg(session9)
        check(
            result9.created == 0 and result9.updated == 0,
            "空窗口: 返回 0 而不是抛异常",
        )
        check(result9.window.get("since", "").startswith("2026-10-14"),
            "空窗口: 回带窗口便于核对「跑的是哪一段」")
    finally:
        gap_engine.suggest_category = original_suggest


async def t15_auto_close() -> None:
    print("\n== T1.5 自动关闭（tasked → closed）==")

    original_suggest = gap_engine.suggest_category

    async def _no_suggest(question: str) -> str | None:
        return None

    gap_engine.suggest_category = _no_suggest
    try:
        base = _vec(808)
        since = _dt(2026, 10, 14, 9, 0, 0)
        until = _dt(2026, 10, 14, 12, 0, 0)

        # --- 走 FAQ 短路那条路（faq_hit=True）---
        # 🔴 「补料后这条问题已被沉淀成 FAQ」是最强、最常见的闭环证据。
        #    只认 `allowed_ids` 会漏掉它，而 `faq_hit` 恰好是 `is_miss`
        #    反例 ① **排除**的那一类 —— 两处口径必须成对地想。
        tasked = _gap(
            "g_tasked",
            vector=base,
            status=STATUS_TASKED,
            last_seen_at=_dt(2026, 10, 14, 9, 30, 0),
        )
        answer = _row(
            f"{MARK}_hit0",
            vector=base,
            faq_hit=True,
            recalled=[],
            allowed=[],
            created_at=_dt(2026, 10, 14, 10, 0, 0),
        )
        session = _ScriptedSession(scalars_seq=[[], [tasked], [tasked], [answer]])
        result = await _agg(session, since=since, until=until)
        check(
            result.closed == 1,
            f"自动关闭: faq_hit=True 的命中能关上缺口（实测 closed={result.closed}）",
        )
        check(
            tasked.status == STATUS_CLOSED,
            f"自动关闭: 缺口状态变为 closed（实测 {tasked.status}）",
        )

        # --- 走检索那条路（准入非空且分数够）---
        tasked2 = _gap(
            "g_tasked2",
            vector=_vec(809),
            status=STATUS_TASKED,
            last_seen_at=_dt(2026, 10, 14, 9, 30, 0),
        )
        good = _row(
            f"{MARK}_hit1",
            vector=_vec(809),
            recalled=["c1"],
            allowed=["c1"],
            top_score=settings.kb_min_score + 0.1,
            created_at=_dt(2026, 10, 14, 10, 0, 0),
        )
        session2 = _ScriptedSession(scalars_seq=[[], [tasked2], [tasked2], [good]])
        result2 = await _agg(session2)
        check(result2.closed == 1, "自动关闭: 检索路径（准入 + 分数够）同样能关")
        check(tasked2.status == STATUS_CLOSED, "自动关闭: 检索路径状态也变了")

        # --- 分数不够的不关 ---
        tasked3 = _gap(
            "g_tasked3",
            vector=_vec(810),
            status=STATUS_TASKED,
            last_seen_at=_dt(2026, 10, 14, 9, 30, 0),
        )
        lowhit = _row(
            f"{MARK}_hit2",
            vector=_vec(810),
            recalled=["c1"],
            allowed=["c1"],
            top_score=settings.kb_min_score - 0.2,
            created_at=_dt(2026, 10, 14, 10, 0, 0),
        )
        session3 = _ScriptedSession(scalars_seq=[[], [tasked3], [tasked3], [lowhit]])
        result3 = await _agg(session3)
        check(
            result3.closed == 0,
            f"自动关闭: 分数仍低于阈值**不关**（实测 closed={result3.closed}）",
        )
        check(tasked3.status == STATUS_TASKED, "自动关闭: 状态保持 tasked")

        # --- 相似度不够的不关（同部门但不同问题）---
        tasked4 = _gap(
            "g_tasked4",
            vector=_vec(811),
            status=STATUS_TASKED,
            last_seen_at=_dt(2026, 10, 14, 9, 30, 0),
        )
        other = _row(
            f"{MARK}_hit3",
            vector=_vec(812),
            faq_hit=True,
            created_at=_dt(2026, 10, 14, 10, 0, 0),
        )
        session4 = _ScriptedSession(scalars_seq=[[], [tasked4], [tasked4], [other]])
        result4 = await _agg(session4)
        check(
            result4.closed == 0,
            f"自动关闭: 不相干的问题**不关**（实测 closed={result4.closed}）—— 关错了比不关更糟",
        )

        # --- 水位线之下的行不算证据 ---
        # 🔴 缺口的 `last_seen_at` 之后的命中才算「补上了」。若把更早的行也当证据，
        #    一条缺口会在被转建的**当下**就被自己转建之前的旧提问关掉。
        tasked5 = _gap(
            "g_tasked5",
            vector=_vec(813),
            status=STATUS_TASKED,
            last_seen_at=_dt(2026, 10, 14, 11, 0, 0),
        )
        earlier = _row(
            f"{MARK}_hit4",
            vector=_vec(813),
            faq_hit=True,
            created_at=_dt(2026, 10, 14, 10, 0, 0),  # 早于水位线
        )
        session5 = _ScriptedSession(scalars_seq=[[], [tasked5], [tasked5], [earlier]])
        result5 = await _agg(session5)
        check(
            result5.closed == 0,
            f"自动关闭: 水位线**之前**的命中不算证据（实测 closed={result5.closed}）",
        )

        # --- 只有 tasked 才自动关 ---
        opened = _gap(
            "g_open",
            vector=_vec(814),
            status=STATUS_OPEN,
            last_seen_at=_dt(2026, 10, 14, 9, 30, 0),
        )
        hit = _row(
            f"{MARK}_hit5",
            vector=_vec(814),
            faq_hit=True,
            created_at=_dt(2026, 10, 14, 10, 0, 0),
        )
        # `_auto_close` 自己查 tasked，所以 fake 的第三批只回 []（没有 tasked）
        session6 = _ScriptedSession(scalars_seq=[[], [opened], [], [hit]])
        result6 = await _agg(session6)
        check(result6.closed == 0, "自动关闭: 只有 tasked 会被自动关（open 不动）")
        check(
            session6.statements and "status = %s" in _compiled(session6.statements[2]),
            "自动关闭: 关闭查询**只**取 tasked（SQL 侧就限定了，不是取回再过滤）",
        )

        # --- 审计留痕 ---
        tasked7 = _gap(
            "g_tasked7",
            vector=_vec(815),
            status=STATUS_TASKED,
            last_seen_at=_dt(2026, 10, 14, 9, 30, 0),
        )
        ans7 = _row(
            f"{MARK}_hit6",
            vector=_vec(815),
            faq_hit=True,
            created_at=_dt(2026, 10, 14, 10, 0, 0),
        )
        session7 = _ScriptedSession(scalars_seq=[[], [tasked7], [tasked7], [ans7]])
        await _agg(session7)
        audit_rows = [o for o in session7.added if hasattr(o, "action")]
        check(
            len(audit_rows) == 1,
            f"审计: 自动关闭落**一条** `gap.status.update`（实测 {len(audit_rows)}）",
        )
        if audit_rows:
            log = audit_rows[0]
            check(
                log.action == audit_service.AuditAction.GAP_STATUS_UPDATE,
                f"审计: action = gap.status.update（实测 {log.action}）",
            )
            check(
                log.user_id == SYSTEM_USER_ID,
                f"审计: user_id = 'system'（系统动作，实测 {log.user_id!r}）",
            )
            check(
                log.target_type == audit_service.AuditTargetType.GAP,
                f"审计: target_type = gap（实测 {log.target_type}）",
            )
            check(
                log.target_id == "g_tasked7",
                f"审计: target_id = 缺口 id（实测 {log.target_id}）",
            )
            detail = log.detail or {}
            check(
                detail.get("from_status") == STATUS_TASKED
                and detail.get("to_status") == STATUS_CLOSED,
                f"审计: detail 记了流转方向（实测 {detail}）",
            )
        check(session7.commits >= 1, "审计: 本模块自己有 commit（record() 不 commit）")

        # --- 开关可关 ---
        original_flag = settings.gap_auto_close_enabled
        try:
            object.__setattr__(settings, "gap_auto_close_enabled", False)
            tasked8 = _gap(
                "g_tasked8",
                vector=_vec(816),
                status=STATUS_TASKED,
                last_seen_at=_dt(2026, 10, 14, 9, 30, 0),
            )
            session8 = _ScriptedSession(scalars_seq=[[], [tasked8]])
            result8 = await _agg(session8)
            check(
                result8.closed == 0 and tasked8.status == STATUS_TASKED,
                "自动关闭: GAP_AUTO_CLOSE_ENABLED=false 时完全不扫（关闭不可逆，必须能停）",
            )
            check(
                len(session8.statements) == 2,
                f"自动关闭: 关掉后连查询都不发（实测 {len(session8.statements)} 条语句）",
            )
        finally:
            object.__setattr__(settings, "gap_auto_close_enabled", original_flag)
    finally:
        gap_engine.suggest_category = original_suggest


async def t15_convert() -> None:
    print("\n== T1.5 一键转建：状态机与幂等 ==")

    ctx = ctx_for("u_admin")

    # --- 缺口不存在 → 3001 ---
    session = _ScriptedSession(get_map={})
    try:
        await gap_engine.convert_gap_to_task(
            session, gap_id=f"{MARK}_missing", category=None, operator=ctx
        )
        check(False, "转建: 缺口不存在应报 NotFoundError")
    except NotFoundError:
        check(True, "转建: 缺口不存在报 NotFoundError（3001）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"转建: 报的是 {type(exc).__name__}，应为 NotFoundError")

    # --- 状态不是 open → 3002（**不是**裸 BizError 的 5000）---
    tasked = _gap(f"{MARK}_g_tasked", vector=_vec(901), status=STATUS_TASKED)
    session2 = _ScriptedSession(get_map={f"{MARK}_g_tasked": tasked})
    try:
        await gap_engine.convert_gap_to_task(
            session2, gap_id=f"{MARK}_g_tasked", category=None, operator=ctx
        )
        check(False, "转建: 非 open 状态应报 ConflictError")
    except ConflictError:
        check(True, "转建: 非 open 状态报 ConflictError（3002/409，不是默认的 5000）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"转建: 报的是 {type(exc).__name__}，应为 ConflictError")
    check(not session2.added, "转建: 状态不对时**不**建任务、不留痕")

    # `BizError` 的 code 默认是 5000 —— 前端按业务码分支时会当成服务端故障。
    # ⚠️ `code` 是**实例属性**（在 `BizError.__init__` 里赋值），不是类属性：
    #    写成 `ConflictError.code` 会 `AttributeError`，而报错位置看着像
    #    「脚本写错了」，实际是问法不对。
    check(
        ConflictError("x").code == 3002 and NotFoundError("x").code == 3001,
        f"转建: 两个异常的业务码分别是 3002 / 3001"
        f"（实测 {ConflictError('x').code} / {NotFoundError('x').code}）—— 都不是 5000",
    )
    check(
        ConflictError("x").http_status == 409,
        f"转建: ConflictError 走 HTTP 409（实测 {ConflictError('x').http_status}）",
    )

    # --- 正常路径：open → 建任务 + 置 tasked + 留痕 ---
    opened = _gap(f"{MARK}_g_open", vector=_vec(902), status=STATUS_OPEN, frequency=7)
    # ⚠️ `_find_open_task` 走 `db.scalar(...)`（不是 `scalars`）—— 两条路在假会话里
    #    是两个独立队列，给错了会静默返回默认值（少了幂等判定这一步也不报错）。
    session3 = _ScriptedSession(scalar_seq=[None], get_map={f"{MARK}_g_open": opened})
    result = await gap_engine.convert_gap_to_task(
        session3, gap_id=f"{MARK}_g_open", category="财务制度", operator=ctx
    )
    check(result.gap_id == f"{MARK}_g_open", "转建: 回 gap_id")
    check(result.status == STATUS_TASKED, f"转建: 回 status=tasked（实测 {result.status}）")
    check(bool(result.task_id), f"转建: 回 task_id（实测 {result.task_id!r}）")
    check(opened.status == STATUS_TASKED, "转建: 缺口状态置为 tasked")
    check(
        opened.suggested_category == "财务制度",
        "转建: 人工传的 category **覆盖**模型建议（be-09 §4.3）",
    )
    task_rows = [o for o in session3.added if isinstance(o, KbImportTask)]
    check(len(task_rows) == 1, f"转建: 建了 1 条 kb_import_task（实测 {len(task_rows)}）")
    if task_rows:
        task = task_rows[0]
        check(
            task.type == KbImportTask.TYPE_SUPPLEMENT,
            f"转建: type = supplement（实测 {task.type}）",
        )
        check(
            task.status == KbImportTask.STATUS_PENDING,
            f"转建: status = pending（实测 {task.status}）",
        )
        check(
            task.document_id is None,
            "转建: document_id 为 NULL（此刻还没有文档 —— 正是 §9 #64 说的那个状态）",
        )
        # 🔴 `gap_id` 落库不只是溯源，它是**转建幂等键**。
        #    此前 `create_supplement_task` 用 `del gap_id` 静默丢弃它。
        check(
            task.gap_id == f"{MARK}_g_open",
            f"转建: **`gap_id` 真的落库**（实测 {task.gap_id!r}）—— 第 64 条待确认已销",
        )
        check(task.id == result.task_id, "转建: 回给前端的 task_id 就是落库那条的 id")
    audit_rows = [o for o in session3.added if hasattr(o, "action")]
    check(len(audit_rows) == 1, f"转建: 落一条审计（实测 {len(audit_rows)}）")
    if audit_rows:
        log = audit_rows[0]
        check(
            log.action == audit_service.AuditAction.GAP_TASK_CREATE,
            f"转建: action = gap.task.create（实测 {log.action}）",
        )
        check(
            log.user_id == "u_admin",
            f"转建: user_id 只从 JWT 的 AuthContext 来（实测 {log.user_id!r}）",
        )
        submitted = {k for k, v in (log.detail or {}).items() if v is not None}
        allowed = audit_service._DETAIL_WHITELIST[  # type: ignore[attr-defined]
            audit_service.AuditAction.GAP_TASK_CREATE
        ]
        check(
            submitted <= allowed,
            f"转建: 审计 detail 的键都在冻结键集内（多的：{sorted(submitted - allowed) or '无'}）",
        )
        check(
            (log.detail or {}).get("task_id") == result.task_id,
            "转建: 审计 detail 里的 task_id 与返回值一致（回查得起来）",
        )
    check(session3.commits >= 1, "转建: 提交事务")

    # --- 幂等：已有在跑的补充任务 → 复用，不新建 ---
    # 🔴 `create_supplement_task` 自己 commit，所以「建任务」与「置 tasked」
    #    **不在同一事务**。置 tasked 失败后管理员再点一次，必须收敛为
    #    「复用 + 补上 tasked」，而不是再建一条（一条缺口补两次料 = 两份重复文档）。
    opened2 = _gap(f"{MARK}_g_open2", vector=_vec(903), status=STATUS_OPEN)
    existing_task = KbImportTask(
        id="task_already_there",
        gap_id=f"{MARK}_g_open2",
        type=KbImportTask.TYPE_SUPPLEMENT,
        status=KbImportTask.STATUS_PENDING,
        progress=0,
        uploaded_by="u_admin",
    )
    session4 = _ScriptedSession(
        scalar_seq=[existing_task],
        get_map={f"{MARK}_g_open2": opened2},
    )
    result4 = await gap_engine.convert_gap_to_task(
        session4, gap_id=f"{MARK}_g_open2", category=None, operator=ctx
    )
    check(
        result4.task_id == "task_already_there",
        f"幂等: 复用已有任务（实测 {result4.task_id!r}）",
    )
    check(
        not [o for o in session4.added if isinstance(o, KbImportTask)],
        "幂等: **没有**再建一条 kb_import_task",
    )
    check(opened2.status == STATUS_TASKED, "幂等: 仍把缺口补置为 tasked（收敛，不是静默跳过）")

    # --- `_find_open_task` 的 SQL：按 gap_id + 活跃状态 ---
    from sqlalchemy import select as sa_select

    find_sql = _compiled(
        sa_select(KbImportTask).where(
            KbImportTask.gap_id == "g_x",
            KbImportTask.status.in_(KbImportTask.ACTIVE_STATUSES),
        )
    )
    check("gap_id = %s" in find_sql, "幂等: gap_id 是绑定参数（不是拼进 SQL 文本）")
    check("status IN" in find_sql, "幂等: 只在**活跃状态**里找（已完成的任务不该被复用）")


async def t15_list_gaps() -> None:
    print("\n== T1.5 缺口清单 ==")

    rows = [
        _gap(f"{MARK}_l{i}", vector=_vec(1000 + i), frequency=5 - i)
        for i in range(3)
    ]
    session = _ScriptedSession(scalars_seq=[rows], scalar_seq=[3])
    items, total = await gap_engine.list_gaps(
        session, min_frequency=1, page=1, page_size=20
    )
    check(total == 3, f"清单: total 来自**独立的 count 查询**（实测 {total}）")
    check(len(items) == 3, f"清单: 返回 3 条（实测 {len(items)}）")
    # ⚠️ `list_gaps` **先** `db.scalar(count)` **再** `db.scalars(select)`，
    #    所以 `statements[0]` 是 count、`[1]` 才是数据查询。
    #    在 `[0]` 上断言 ORDER BY / LIMIT 会永远红着（count 语句里没有这些）。
    check(len(session.statements) == 2, f"清单: 共发 2 条语句（count + select，实测 {len(session.statements)}）")
    count_sql = _compiled(session.statements[0])
    sql = _compiled(session.statements[1])
    check("count(*)" in count_sql, "清单: 第 1 条是 count（分页的 total 与列表同源同条件）")
    check("frequency >= %s" in count_sql, "清单: count 也带同一个 min_frequency 条件（否则 total 与列表对不上）")
    check("frequency >= %s" in sql, "清单: min_frequency 是绑定参数（不是拼进 SQL 文本）")
    check(
        "ORDER BY knowledge_gap.frequency DESC" in sql,
        "清单: 按频次倒序（运营先看最热门的缺口）",
    )
    check(
        "knowledge_gap.last_seen_at DESC" in sql,
        "清单: 同频次再按最近出现倒序（否则分页在并列频次上不稳定）",
    )
    # ⚠️ MySQL 方言把 `.offset().limit()` 编译成 `LIMIT <offset>, <count>`
    #    —— **不是** `LIMIT %s OFFSET %s`（那是 SQLite/Postgres 的形状）。
    check(
        "LIMIT %s, %s" in sql,
        "清单: 分页编译为 MySQL 的 `LIMIT offset, count`，且两个都是绑定参数",
    )
    # 🔴 「不按部门过滤」必须**只看 WHERE 段**：`department_id` 是 SELECT 的列之一，
    #    对整个 SQL 做 `"department" not in sql` 会永远红着 ——
    #    而实际上这里的 WHERE 只有一条频次条件，代码是对的。
    where_seg = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    check(
        "department" not in where_seg.lower(),
        "清单: **不按调用者部门过滤**（有意口径，be-09 §5.8）—— WHERE 段里没有部门条件",
    )
    check(
        where_seg.count("knowledge_gap.") == 1,
        f"清单: WHERE 段只有一条条件 `frequency >= ?`（实测 {where_seg.strip()!r}）",
    )
    check(
        "user_id" not in where_seg.lower(),
        "清单: WHERE 段也没有按提问人过滤（缺口是部门粒度，不存在「看自己的」这种视图）",
    )


async def t15_fake_session() -> None:
    await t15_suggest_category()
    await t15_aggregate()
    await t15_auto_close()
    await t15_convert()
    await t15_list_gaps()


# ═══════════════════════════════════════════════════════════════
# T2 接口
# ═══════════════════════════════════════════════════════════════


async def t2_http() -> bool:
    print("\n== T2 接口层（需 MySQL + Redis）==")

    from backend.service import auth_service

    transport = ASGITransport(app=main.app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # --- 登录拿 token ---
            resp = await client.post(
                "/api/auth/login", json={"username": "admin", "password": PASSWORD}
            )
            if resp.status_code != 200 or resp.json().get("code") != 0:
                skip(
                    "T2 全部接口用例",
                    f"登录失败（HTTP {resp.status_code}）—— 依赖 MySQL + Redis",
                )
                return False
            token = resp.json()["data"]["access_token"]

            # --- 无 token → 401 ---
            bare = await client.get("/api/mining/gaps")
            check(bare.status_code == 401, f"T2-1 无 token 访问清单 → 401（实测 {bare.status_code}）")

            # --- 清单 ---
            listed = await client.get("/api/mining/gaps", headers=auth(token))
            check(listed.status_code == 200, f"T2-2 清单 HTTP 200（实测 {listed.status_code}）")
            body = listed.json()
            data = body.get("data") or {}
            check(
                set(data) >= {"list", "total", "page", "page_size"},
                f"T2-3 分页结构 {{list,total,page,page_size}}（实测 {sorted(data)}）",
            )
            # page_size 上限：恰好等于上限放行（T2-5），超出上限**被拒**（T2-4）。
            # 🔴 断言落在**业务码**而不是 FastAPI 的默认状态码上：
            #    `core/exceptions.py` 的 `RequestValidationError` 处理器刻意把校验失败
            #    统一成 `400 + PARAM_ERROR(1001)`（统一信封的一部分），
            #    所以 422 永远不会出现。只断言 422 的用例，红的是**用例**。
            #    要区分的是「被当成参数错误拒掉」与「静默走默认值」，`code` 才是那个信号。
            capped = await client.get(
                "/api/mining/gaps?page_size=100000", headers=auth(token)
            )
            check(
                capped.status_code in (400, 422) and capped.json().get("code") == 1001,
                f"T2-4 page_size 超上限被拒且业务码为 1001"
                f"（实测 {capped.status_code} / code={capped.json().get('code')}）",
            )
            big = await client.get(
                "/api/mining/gaps?page_size=200", headers=auth(token)
            )
            check(big.status_code == 200, f"T2-5 page_size=200 放行（实测 {big.status_code}）")

            # --- 转建：不存在的缺口 → 3001 ---
            missing = await client.post(
                f"/api/mining/gaps/{MARK}_nope/task", headers=auth(token)
            )
            check(
                missing.json().get("code") == 3001,
                f"T2-6 转建不存在的缺口 → 业务码 3001（实测 {missing.json().get('code')}）",
            )

            # --- 权限码：staff 应当 403 ---
            staff = await client.post(
                "/api/auth/login", json={"username": "cost01", "password": PASSWORD}
            )
            if staff.status_code == 200 and staff.json().get("code") == 0:
                staff_token = staff.json()["data"]["access_token"]
                denied = await client.get("/api/mining/gaps", headers=auth(staff_token))
                check(
                    denied.status_code == 403,
                    f"T2-7 staff 访问清单 → 403（红线 4/7，实测 {denied.status_code}）",
                )
                denied_post = await client.post(
                    f"/api/mining/gaps/{MARK}_nope/task", headers=auth(staff_token)
                )
                check(
                    denied_post.status_code == 403,
                    f"T2-8 staff 转建 → 403（实测 {denied_post.status_code}）",
                )
            else:
                skip("T2-7/8 staff 403", "staff 账号登录失败")

            # --- 横切：他人 session_id（be-05 的守门用例，顺带回归）----
            skip(
                "T2-9 缺口闭环端到端（未命中 → 缺口 → 转建 → closed）",
                "需先有 kb_document_permission 数据 + kb_min_score 可调",
            )
    except Exception as exc:  # noqa: BLE001
        skip("T2 全部接口用例", f"环境不可达（{type(exc).__name__}: {exc}）")
        return False

    return True


def t3_chain() -> None:
    print("\n== T3 全链路（需 Milvus + BGE + DeepSeek + worker）==")
    for label, reason in (
        ("A8-1 未命中提问落 qa_log → beat 聚合出缺口（frequency 累加）", "需 MySQL + worker + beat"),
        ("A8-2 一键转建 → kb_import_task(supplement, gap_id) 且缺口 tasked", "需 MySQL"),
        ("A8-3 补料上传并配权限 → 同一问题再问命中", "需 Milvus + BGE + DeepSeek"),
        ("A8-4 下一轮聚合 → 缺口自动 closed（审计记 gap.status.update / system）", "需 MySQL + worker"),
        ("A9-2 治理页展示缺口清单与建议分类", "需 MySQL + Redis + DeepSeek（分类推荐）"),
        ("反例 ② 回归：权限配少时提问**不**进缺口池而进 skipped_by_acl", "需 Milvus + BGE + MySQL"),
    ):
        skip(label, reason)


# ═══════════════════════════════════════════════════════════════
# 清理
# ═══════════════════════════════════════════════════════════════


async def _sweep() -> None:
    """只删自己造的数据。前缀不含下划线（`_` 在 LIKE 里是通配符）。"""
    from sqlalchemy import delete, or_

    from backend.core.db import SessionLocal

    try:
        async with SessionLocal() as db:
            removed_gaps = await db.execute(
                delete(KnowledgeGap).where(
                    or_(
                        KnowledgeGap.id.like(f"{MARK}%"),
                        KnowledgeGap.question.like(f"{MARK}%"),
                    )
                )
            )
            removed_tasks = await db.execute(
                delete(KbImportTask).where(KbImportTask.gap_id.like(f"{MARK}%"))
            )
            await db.commit()
            print(
                f"  [note] 清理自造缺口 {removed_gaps.rowcount or 0} 行 / "
                f"关联补充任务 {removed_tasks.rowcount or 0} 行"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"  [note] 清理未执行（{type(exc).__name__}）")


# ═══════════════════════════════════════════════════════════════


async def run() -> tuple[bool, bool]:
    """返回 `(全部通过, T2 是否真的跑过)`。

    ⚠️ 四层在**同一个事件循环**里跑 —— 这正是 `core/db.py` 的模块级 engine
       能复用的前提。各自塞进一个 `asyncio.run()` 会让第二层复用到上个循环
       遗留的连接，报 `'NoneType' object has no attribute 'send'`（CLAUDE.md 已知坑）。
    """
    t1_is_miss()
    t1_unit_vector_and_matcher()
    t1_contract_alignment()
    t1_routes()
    await t1_redlines()
    t1_config_and_beat()
    t1_migration_consistency()
    await t15_fake_session()
    ran = await t2_http()
    t3_chain()
    if ran:
        await _sweep()
    await engine.dispose()
    return all(_results), ran


def main_() -> None:
    ok_, ran = asyncio.run(run())
    passed = sum(_results)
    total = len(_results)
    print(f"\n{'=' * 56}")
    # 🔴 标签要说实话：`_results` 里**含 T2 的检查**（T2 真跑过时）。
    #    写死「T1/T1.5」会让人以为接口层一条都没验，把已经跑过的当成没跑
    #    —— 与「把 SKIP 当成通过」是同一类误读，只是方向相反。
    _scope = "T1/T1.5 + T2（含接口层）" if ran else "T1/T1.5（离线+假会话）"
    print(f"{_scope}：{passed}/{total} 通过")
    # 🔴 不要写死「剩下的都是 T2/T3 的依赖问题」——T2 现在真的跑起来了。
    #    写死归类会让人以为接口层一条都没验过，把已经能跑的东西当成没跑。
    print(
        f"SKIP：{len(_skipped)} 项（**不是通过**）—— 逐条原因见上；"
        "缺口通常在 T2（需 MySQL+Redis）或 T3（需 Milvus/BGE/DeepSeek+worker/beat）"
    )
    if not ran:
        print("⚠️ T2 未执行（MySQL 或 Redis 不可用）—— **本次结果不构成完整验收**。")
        print("   依赖恢复后重跑本脚本。")
    if passed != total:
        print("存在失败项，见上方 FAIL 行。")
    sys.exit(0 if (ok_ and ran) else 1)


if __name__ == "__main__":
    main_()
