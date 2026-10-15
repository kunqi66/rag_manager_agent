"""be-05 鉴权检索问答的验收脚本（spec `P1-11-be-search-qa` §8 的可执行版本）。

跑法（项目根目录）：

    uv run python -m backend.scripts.verify_search

脚本分四层，**能跑的就跑，跑不了的明确报 SKIP 并说明原因** ——
绝不把「环境不具备」伪装成「通过」（那是验收脚本最坏的失败模式）。

| 层 | 需要什么 | 覆盖 |
|---|---|---|
| **T1 离线** | 什么都不需要 | ACL 表达式与注入剔除、探测缓存键、Prompt 组装、SSE 帧格式、L2 归一化、红线 4 静态断言 |
| **T1.5 假会话** | 什么都不需要 | 用**假 AsyncSession** 驱动 `session.py`：归属校验（横向越权）、`load_history` 的取窗与正序、`DELETE` 只动两张表 |
| **T2 接口** | MySQL + **Redis** | 7 端点注册、登录门禁、分页结构、`post_verify` 四维 OR |
| **T3 链路** | ＋ Milvus（云端直连）＋ BGE（`dev_bge_server.py`）＋ DeepSeek ＋ 真 HTTP | A4 引用卡片与分数口径、A9 SSE 真流与 done 字段、A5-3 受限提示 |

🔴 **T3 现在真的会跑，依赖自己探。** 本脚本原先把 T3 写成 11 条**写死的 skip**，
理由统一是「本机无 Docker」—— 而 Milvus 是**直连云端的**（`60.205.138.167:19530`），
BGE 用 `backend/scripts/dev_bge_server.py` 在本地补上，**理由本身就是错的**。
写死的 skip 理由不会随环境改善而更新，只会一直骗人（与 CLAUDE.md 里
「『此刻不该有』的断言自带保质期」同源）。
所以现在：`/health` 探 API、`embedding_base_url/health` 探 BGE，
探到才跑，探不到时 skip 里**写清楚是谁不可达**。
T3 必须走**真 socket** —— T2 的 `ASGITransport` 不反映分帧，
A9-1「delta 逐帧到达」在它下面**恒真**，等于没测。

⚠️ **T3 会往库里写会话**（真问答）。脚本把 `start` 事件里的 `session_id` 记下来，
收尾按 id 精确删（见 `_sweep`）。

⚠️ **T1.5 为什么要用假会话**：`session.py` 里最容易错的两处 ——
   归属校验与「取最近 N 条再翻正序」—— 都不需要真实数据库就能验证。
   等到 T2 才测的话，本机（云端 MySQL 不可达时）这两条就永远测不到，
   而它们恰好一个管横向越权、一个管「模型看不到刚说的话」这种静默失效。

⚠️ Windows 控制台是 GBK —— 输出一律 **ASCII**，断言**布尔值与具体值**，
不靠肉眼读中文。这与 `verify_org.py` / `verify_import.py` 同一约定。

⚠️ 会**真的**连 `.env` 里的 MySQL（开发期直连 ECS），只增删自己造的数据
（提问文本前缀 `ZZVFYSEARCH`）。**绝不动种子数据。**
"""

from __future__ import annotations

import asyncio
import ast
import json
import re
import sys
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import bindparam, func, select, text
from sqlalchemy.dialects import mysql

import main
from backend.core.db import engine
from backend.core.exceptions import ErrorCode, NotFoundError
from backend.core.settings import settings
from backend.engines.search_engine import acl as acl_mod
from backend.engines.search_engine import qa as qa_mod
from backend.engines.search_engine import retrieval, session as session_engine
from backend.engines.search_engine.retrieval import _probe_cache_key
from backend.models import QaLog, QaMessage, QaSession
from backend.schemas.auth import AuthContext
from backend.schemas.query import AskRequest, SearchRequest, SseCitations, SseDelta, SseEvent

PASSWORD = "Rag@12345"
#: 自造数据的标记前缀。清理一律靠它，不靠「记住 id」。
#: 🔴 **不含下划线** —— `LIKE 'zzvfy_%'` 里的 `_` 是通配符，会连带命中兄弟数据
#:    （CLAUDE.md 已知坑第一条）。清理 SQL 用 `LIKE 'ZZVFYSEARCH%'`。
MARK = "ZZVFYSEARCH"

# 🔴 Windows 控制台默认是 GBK，本脚本的标签里有 🔴 —— 不重配的话
#    `print` 直接抛 UnicodeEncodeError，**脚本在第 7 个断言处就崩了**，
#    而前面的 OK 行已经打出来，看起来像「跑到一半莫名退出」。
#    `errors="replace"` 兜底：即使终端不支持 UTF-8，也只是显示成问号，不会崩。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 —— 重配失败不该让脚本起不来
        pass

_results: list[bool] = []
_skipped: list[str] = []
#: T3 是否**真的跑过**（依赖探活通过）。收尾的标签要用它说实话 ——
#: 「T3 没跑」与「T3 全过」在只看 `passed/total` 时长得一样。
_t3_ran = False


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
    roles: list[str] | None = None,
) -> AuthContext:
    return AuthContext(
        user_id=user_id,
        username=user_id,
        department_id=(depts or ["dept_self"])[-1],
        depts=depts if depts is not None else ["dept_root", "dept_self"],
        roles=roles if roles is not None else ["role_staff"],
        perms=[],
    )


# ═══════════════════════════════════════════════════════════════
# 假会话：不需要数据库就能驱动 session.py
# ═══════════════════════════════════════════════════════════════


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows

    def __iter__(self):
        return iter(self._rows)

    def one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """只实现 `session.py` 用到的那几个方法，并**记下最后一条语句**。

    ⚠️ 不做 `add` / `commit` 的语义模拟 —— 那会变成一个半吊子 ORM，
       测的是假实现而不是真代码。这里只覆盖「构造了什么 SQL」与「返回顺序」。
    """

    def __init__(self, rows: list | None = None) -> None:
        self._rows = rows or []
        self.last_stmt = None
        self.added: list = []
        self.deleted: list = []
        self.committed = 0
        self.flushed = 0

    async def scalars(self, stmt):
        self.last_stmt = stmt
        return _FakeResult(self._rows)

    async def execute(self, stmt):
        self.last_stmt = stmt
        return _FakeResult(self._rows)

    async def scalar(self, stmt):
        self.last_stmt = stmt
        return len(self._rows)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def delete(self, obj) -> None:
        self.deleted.append(obj)

    async def commit(self) -> None:
        self.committed += 1

    async def flush(self) -> None:
        self.flushed += 1


def _sql(stmt) -> str:
    """把语句编译成 MySQL 方言的字符串（不连接数据库）。"""
    return str(stmt.compile(dialect=mysql.dialect()))


def _strip_docstrings(src: str) -> str:
    """去掉全部 docstring，只留代码本体。

    🔴 为什么必须这么做：本模块的 docstring **刻意写着**「原先有 `user_context`，
       已删除」这类说明 —— 直接对源码做子串检查，会把这段解释本身
       当成「没删干净」，于是安全用例永远红着。这类假阳性最终会让人
       把这个用例注释掉，那才是真损失。

    ⚠️ 用 `ast` 找 docstring 而不是正则剥三引号字符串：后者会把
       「代码里的多行字符串常量」也一起剥掉，留下半截语法。

    ⚠️ 也**不能**用 `src.replace(docstring_value, "")`：源文件里写的是转义后的
       `\\n\\n`，而 docstring 的**值**是真换行 —— 两者不相等，替换静默失效，
       于是这个安全用例永远红着（本文件首跑就是这么错的）。
       正确做法是按 `ast` 给的**行列位置**挖空。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:  # pragma: no cover —— 源码本身语法错该在别处暴露
        return src

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
            value = first.value
            spans.append(
                (value.lineno, value.col_offset, value.end_lineno, value.end_col_offset)
            )

    lines = src.splitlines(keepends=True)
    # 从后往前挖，避免前面的改动让后面的行号失效
    for line1, col1, line2, col2 in sorted(spans, reverse=True):
        if line1 == line2:
            line = lines[line1 - 1]
            lines[line1 - 1] = line[:col1] + line[col2:]
        else:
            lines[line1 - 1] = lines[line1 - 1][:col1] + "\n"
            for index in range(line1, line2 - 1):
                lines[index] = ""
            lines[line2 - 1] = lines[line2 - 1][col2:]
    return "".join(lines)


class _Obj:
    """通用假 ORM 对象。"""

    def __init__(self, **kw) -> None:
        for key, value in kw.items():
            setattr(self, key, value)


# ═══════════════════════════════════════════════════════════════
# T1 离线
# ═══════════════════════════════════════════════════════════════


def tier1_offline() -> None:
    print("\n== T1 离线：ACL / 缓存键 / Prompt / SSE / 红线 4 ==")

    # ---------- ACL 表达式 ----------
    visible = {"doc_aaa", "doc_bbb"}
    ctx = ctx_for(user_id="u_1", depts=["dept_root", "dept_child"], roles=["role_r1"])
    acl = acl_mod.build_acl_expr(ctx, visible)

    # T1-1 空可见集合必须**抛错**，不能构造出「匹配空集」的表达式
    raised = False
    try:
        acl_mod.build_acl_expr(ctx, set())
    except ValueError:
        raised = True
    check(raised, "T1-1 可见文档为空 -> ValueError（fail-closed，不退化成无过滤表达式）")

    check("acl_global == true" in acl.expr, "T1-2 表达式含 acl_global 维（漏掉会让公开文档全消失）")
    check("acl_departments" in acl.expr, "T1-3a 表达式含部门维")
    check("acl_roles" in acl.expr, "T1-3b 表达式含角色维")
    check("acl_users" in acl.expr, "T1-3c 表达式含用户维")
    check("document_id in" in acl.expr, "T1-4 表达式含可见文档过滤（Milvus schema 无 status 字段）")
    check(
        acl.expr.count("acl_global") == 1 and " or " in acl.expr and " and " in acl.expr,
        "T1-5 四维是 or 关系、与可见性过滤是 and 关系（写反会放行全部文档）",
    )

    # T1-6 🔴 部门继承：有效部门集合（含祖先）必须**原样**进表达式。
    #      只放自身部门的话，授权给父部门的文档全部静默不可见。
    check(
        acl.dept_count == 2 and "dept_root" in acl.expr and "dept_child" in acl.expr,
        "T1-6 🔴 ctx.depts（有效集合，含祖先）原样进表达式，不是只用 department_id",
    )
    check(acl.user_count == 1 and "u_1" in acl.expr, "T1-7 自身 user_id 进 acl_users 维")
    check(acl.doc_count == 2, "T1-8 doc_count 等于可见文档数")

    # T1-9 🔴 注入串必须被剔除且**不抛异常**（畸形的部门 id 不该让整次问答 500）。
    #      这些 id 全部由后端生成或来自 JWT，正常路径不会出现 ——
    #      它挡的是「将来某个调用方把用户输入当 id 传进来」。
    evil = ctx_for(
        user_id="u_2",
        depts=['dept_a"]) or (true', "dept_ok", "", "dept_b c"],
        roles=["role'x"],
    )
    acl_evil = acl_mod.build_acl_expr(evil, {"doc_aaa"})
    check(
        acl_evil.dept_count == 1 and "dept_ok" in acl_evil.expr,
        "T1-9a 非法 id 被剔除、合法的留下（dept_count=1，不是抛异常）",
    )
    check(
        "or (true" not in acl_evil.expr and "role'x" not in acl_evil.expr,
        "T1-9b 🔴 含引号/括号的 id 未进入表达式（Milvus filter 不支持绑定参数，白名单是唯一防线）",
    )

    # T1-10 表达式是合法 JSON 字面量（Milvus 的数组语法）
    literal = acl.expr[acl.expr.index("acl_departments, ") + len("acl_departments, "):]
    literal = literal[: literal.index(")")]
    try:
        parsed = json.loads(literal)
        check(parsed == ["dept_root", "dept_child"], "T1-10 数组字面量是合法 JSON 且内容正确")
    except Exception:  # noqa: BLE001
        check(False, "T1-10 数组字面量是合法 JSON 且内容正确")

    # T1-11 无部门/无角色时不产生空的 array_contains_any（`in []` 行为不可依赖）
    bare = acl_mod.build_acl_expr(ctx_for(depts=[], roles=[]), {"doc_aaa"})
    check(
        "acl_departments" not in bare.expr and "acl_roles" not in bare.expr,
        "T1-11 该维度为空时不产生 array_contains_any(..., []) 空数组子句",
    )
    check("acl_global == true" in bare.expr, "T1-12 空维度的用户仍能查到公开文档")

    # T1-13 desynced 就是 blocked（前置放行但后置拒绝 = 同步窗口的证据）
    pv = acl_mod.PostVerifyResult(allowed={"d1"}, blocked={"d2"})
    check(pv.desynced == {"d2"}, "T1-13 PostVerifyResult.desynced == blocked")

    # ---------- 探测缓存键 ----------
    k1 = _probe_cache_key("u_1", "差旅报销标准")
    k2 = _probe_cache_key("u_1", "差旅报销标准")
    k3 = _probe_cache_key("u_2", "差旅报销标准")
    k4 = _probe_cache_key("u_1", "年假天数")
    check(k1 == k2, "T1-14 同用户同问题 -> 键稳定（不稳定则缓存永远 miss，白多一次 Milvus 调用）")
    check(k1 != k3, "T1-15a 🔴 不同用户键不同（共享会串权限，把别人的受限集当自己的）")
    check(k1 != k4, "T1-15b 不同问题键不同")
    check(
        re.fullmatch(r"rag:query:probe:u_1:[0-9a-f]{16}", k1) is not None,
        "T1-16 键形态 rag:query:probe:{user_id}:{16位hex}（字典 §4）",
    )

    # ---------- L2 归一化 ----------
    unit = qa_mod._l2_normalize([3.0, 4.0])
    check(
        abs((unit[0] ** 2 + unit[1] ** 2) ** 0.5 - 1.0) < 1e-9,
        "T1-17 _l2_normalize 结果范数为 1（3,4 -> 0.6,0.8）",
    )
    check(qa_mod._l2_normalize([0.0, 0.0]) == [0.0, 0.0], "T1-18 零向量不除零、不抛异常")

    # ---------- Prompt 组装 ----------
    msgs = qa_mod._build_messages("住宿费多少", [], [])
    check(len(msgs) == 2 and msgs[0]["role"] == "system", "T1-19 messages[0] 是 system")
    check(
        "没有检索到任何相关资料" in msgs[1]["content"],
        "T1-20 🔴 无资料时显式声明（否则空上下文会被模型当成「自由发挥」，开始编造）",
    )
    check("住宿费多少" in msgs[1]["content"], "T1-21 当前问题进了 Prompt")

    chunks = [
        {
            "chunk_id": "c1",
            "document_id": "d1",
            "title": "差旅制度",
            "heading_path": "报销标准 > 住宿费",
            "content": "一线城市 600 元每晚。",
        },
        {
            "chunk_id": "c2",
            "document_id": "d2",
            "title": "财务手册",
            "heading_path": "",
            "content": "需附发票。",
        },
    ]
    msgs2 = qa_mod._build_messages(
        "住宿费多少",
        chunks,
        [_Obj(role="user", content="你好"), _Obj(role="assistant", content="你好，请问有什么可以帮你")],
    )
    body = msgs2[1]["content"]
    check("[1]" in body and "[2]" in body, "T1-22 参考资料带序号（模型据此定位）")
    check("差旅制度 > 报销标准 > 住宿费" in body, "T1-23 标题与标题路径都进 Prompt")
    check("财务手册" in body and "财务手册 >" not in body, "T1-24 heading_path 为空时不产生悬空的分隔符")
    check(body.index("你好") < body.index("住宿费多少"), "T1-25 历史在问题之前（顺序反了模型会答上一轮）")
    check("用户：" in body and "助手：" in body, "T1-26 历史带说话人标注")

    check(
        "不要编造" in qa_mod.SYSTEM_PROMPT and "不要输出 HTML" in qa_mod.SYSTEM_PROMPT,
        "T1-27 system prompt 含「不编造」与「不输出 HTML」两条硬约束",
    )

    # ---------- SSE 帧 ----------
    frame = main_query_frame(SseEvent.make("delta", SseDelta(content="你好，世界")))
    check(frame.endswith("\n\n"), "T1-28 🔴 帧以空行结尾（少一个换行，事件不会被派发）")
    check(frame.startswith("event: delta\n"), "T1-29 帧首行是 event: <name>")
    check("你好，世界" in frame, "T1-30 中文不被转义成 \\uXXXX（ensure_ascii=False，否则抓包与日志没法看）")
    check('data: {"content"' in frame, "T1-31 data 行是单行 JSON")
    check(
        frame.count("\n\n") == 1,
        "T1-32 帧内只有一处空行（多一处会把一帧拆成两帧）",
    )

    # T1-33 🔴 事件名与载荷必须配套：名字与载荷错配会在前端静默解析出空字段
    mismatched = False
    try:
        SseEvent.make("citations", SseDelta(content="x"))
    except TypeError:
        mismatched = True
    check(mismatched, "T1-33 SseEvent.make 拒绝事件名与载荷错配（抛 TypeError）")

    ok_cit = SseEvent.make("citations", SseCitations(items=[]))
    check(ok_cit.event == "citations" and ok_cit.data == {"items": []}, "T1-34 配合的载荷正常序列化")

    # ---------- 红线 4 的静态断言 ----------
    check("title" not in retrieval._OUTPUT_FIELDS, "T1-35 🔴 Milvus 回传字段不含 title（不改动：标题必须回权威表取）")
    check(
        settings.acl_reveal_blocked_titles is False,
        "T1-36 🔴 ACL_REVEAL_BLOCKED_TITLES 默认 false（红线 4）",
    )
    check(
        retrieval.RetrievalResult().blocked_titles is None,
        "T1-37 受限标题默认为 None，不是空列表（空列表会被前端当成「有值但为空」）",
    )

    # ---------- INTERVAL 字面量 ----------
    sql = _sql(select(func.date_sub(func.now(), session_engine._interval_days(7))))
    check(
        "INTERVAL 7 DAY" in sql and "%s" not in sql,
        "T1-38 🔴 INTERVAL 渲染成字面量而非绑定参数（MySQL 的 INTERVAL 语法位不接受 %s）",
    )

    # ---------- 迁移与模型一致性 ----------
    tier1_migration_consistency()

    # ---------- 驱动的 BLOB 落库能力（见 t1_blob_writable 的 docstring） ----------
    t1_blob_writable()

    # ---------- 权限码：本模块不引入任何新码 ----------
    src = (Path(__file__).resolve().parents[1] / "api" / "query_router.py").read_text("utf-8")
    check(
        "require_permission" not in src,
        "T1-39 7 个端点均不挂功能权限码（PRD §8.3：权限由数据维度决定，红线 7 管的是写接口）",
    )
    # T1-40 🔴 红线 1 收尾（PRD §11.3 R3）。
    # ⚠️ 断言的是**字段与取值行为**，不是「源码里没有这个词」——
    #    `query_router.py` 的模块 docstring 里刻意写了「原先有 `user_context`，
    #    已删除」这句说明，朴素子串检查会把它误判成没删干净。
    check(
        "user_context" not in SearchRequest.model_fields
        and "user_context" not in AskRequest.model_fields,
        "T1-40a 🔴 请求 DTO 里已无 user_context 字段（越权口子）",
    )
    code_only = _strip_docstrings(src)
    check(
        "class UserContext" not in code_only
        and re.search(r"\.\s*user_context\b", code_only) is None,
        "T1-40b 🔴 路由层不再定义、也不读取任何 user_context（身份只来自 JWT）",
    )


def main_query_frame(event: SseEvent) -> str:
    """复用路由层的帧拼装（导入而不是抄一份，避免两处漂移）。"""
    from backend.api.query_router import _sse_frame

    return _sse_frame(event)


#: 字典 §2.3 的列清单。**硬编码**而不是从模型反射 ——
#: 从模型反射的话，「模型少了一列」这件事永远测不出来（自己跟自己比恒等）。
_EXPECTED_COLUMNS: dict[str, set[str]] = {
    "qa_session": {"id", "user_id", "title", "message_count", "created_at", "updated_at"},
    "qa_message": {
        "id", "session_id", "role", "content", "citations", "blocked_count", "created_at",
    },
    "qa_log": {
        "id", "session_id", "user_id", "department_id", "question", "question_embedding",
        "recalled_ids", "allowed_ids", "blocked_ids", "top_score",
        "token_in", "token_out", "latency_ms", "faq_hit", "created_at",
    },
}


def tier1_migration_consistency() -> None:
    """模型 ↔ 字典 ↔ 迁移 三处一致。

    🔴 这个用例防的是：「给模型加了一列，忘了写迁移」——
       后果是**本地一切正常**（模型定义齐全），
       而 ECS 上的表缺列，接口在写入时 500（或更糟：MySQL 非严格模式静默截断）。
    """
    migration = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0003_create_qa_tables.py"
    ).read_text("utf-8")

    for table, expected in _EXPECTED_COLUMNS.items():
        model = {
            "qa_session": QaSession,
            "qa_message": QaMessage,
            "qa_log": QaLog,
        }[table]
        actual = {c.name for c in model.__table__.columns}
        check(actual == expected, f"T1-41 {table} 模型列 == 字典 §2.3（差集 {sorted(actual ^ expected)}）")

        # 迁移里每列都得出现（列名在迁移文本中以 `sa.Column("<name>"` 形态出现）
        missing = [c for c in expected if f'"{c}"' not in migration]
        check(not missing, f"T1-42 {table} 迁移 0003 覆盖全部列（缺 {missing}）")

    check(
        'op.create_table(\n        "qa_log"' in migration or '"qa_log"' in migration,
        "T1-43 迁移 0003 建了 qa_log",
    )
    # 🔴 0003 不得带外键到 sys_user —— 用户被删时要保留历史问答
    check(
        "sys_user.id" not in migration,
        "T1-44 迁移 0003 不含到 sys_user 的外键（用户被删需保留其历史问答）",
    )


def t1_blob_writable() -> None:
    """T1 静态：确认驱动**真的能把 BLOB 写进 MySQL**。

    🔴 **本用例来自一个真的踩到的坑（2026-10-15）**。
    `pymysql` 从 1.2.0 起把 `escape_bytes_prefixed` 换成了**毒丸**：

        escape_dict = escape_bytes_prefixed = "DO NOT IMPORT THIS!!!"

    而 `aiomysql 0.3.2`（当前最新版）仍然 `import` 它并在 `escape()` 里调用。
    于是**任何 `bytes` 参数**在落库时都炸：
    `TypeError: 'str' object is not callable`。

    **为什么这条对整个 be-05 是致命的**：`qa_log.question_embedding` 是 BLOB，
    而 `session.py:210` 每答一次问就写它一次 —— 也就是说
    **问答接口会在「检索完了、LLM 也答完了」的最后一步写日志时失败**。
    用户看到的是「回答失败」，而真正的原因在数据库驱动里，与业务逻辑无关。

    它还解释了另一件事：库里 `kb_chunk` 有 2387 行而
    `qa_log` / `faq` / `knowledge_gap` **各 0 行** ——
    凡是要写 embedding 的路径，一条都没成功过。

    **断言落在哪**：不看版本号，看**那个名字是不是可调用对象**。
    版本号会被人「顺手升级」，而毒丸的形态就是「import 得到、调用即炸」，
    只有断言 `callable(...)` 才能把它抓住。
    `pyproject.toml` 里的 `pymysql<1.2` 是**约束**，本用例是**守门的**。
    """
    import pymysql  # noqa: F401  —— 仅为让「本用例依赖驱动本身」显式可见
    from importlib.metadata import version as _dist_version

    from pymysql import converters

    # ⚠️ 版本号取**发行版元数据**，不要用 `pymysql.__version__` ——
    #    后者在 1.1.3 上会打印出 `2.2.8`（上游 `VERSION=(1,1,3,'final')`
    #    与 `__version__` 不一致）。失败信息里的版本号一旦是错的，
    #    会把排查的人引去查一个不存在的版本。
    _ver = _dist_version("pymysql")
    poison = converters.escape_bytes_prefixed
    check(
        callable(poison),
        f"T1-45 🔴 pymysql({_ver}) 的 escape_bytes_prefixed 可调用"
        f"（当前 {poison!r}）—— 不是可调用对象时 BLOB 落库必炸 TypeError",
    )
    check(
        callable(getattr(converters, "escape_dict", None)),
        "T1-46 escape_dict 同样不是毒丸（同一处赋值的两个名字）",
    )

    # T1-47 走**真正会炸的那条路**：aiomysql 自己重写了 `escape`，bytes 分支
    # 直接调它从 pymysql 导入的那个名字（`.venv/.../aiomysql/connection.py:418`）。
    #
    # ⚠️ 这里**不能**用 `converters.escape_item(b"...")` —— 它走的是另一个函数，
    #    有毒丸时照样返回 `_binary X'000102'`（实测）。用它当守门用例，
    #    变异测试下依然全绿，等于没测。
    # 也**不能**用 pymysql 的 `Connection.escape`：1.2.3 把它改成 `f"X'{obj.hex()}'"`，
    #    根本不碰毒丸，同样测不出来。必须用 aiomysql 的那个实现。
    from aiomysql import connection as _aiomysql_conn

    # 47a 只断言「两处是同一个对象」——它抓的是**结构漂移**
    # （aiomysql 哪天不引 pymysql 的名字了，47b 的前提就变了）。
    # ⚠️ 它**抓不到毒丸**：名字被改成字符串后两边仍指向同一个字符串，
    #    `is` 照样成立。抓毒丸的是 47b，别把这条当成它的替代品。
    check(
        _aiomysql_conn.escape_bytes_prefixed is converters.escape_bytes_prefixed,
        "T1-47a aiomysql 引用的就是 pymysql 那个名字（结构未漂移）",
    )

    # 不连网、不要真连接：bytes 分支不使用任何 self 属性，`__new__` 的空壳就够。
    # 子类只为压掉 aiomysql 的 `__del__`（它会在 GC 时试着关连接，
    # 在日志里甩一段 `AttributeError: ... no attribute '_writer'` 的假崩溃）。
    class _QuietAiomysqlConn(_aiomysql_conn.Connection):
        def __del__(self) -> None:
            pass

    fake_conn = _QuietAiomysqlConn.__new__(_QuietAiomysqlConn)
    try:
        escaped = _aiomysql_conn.Connection.escape(fake_conn, b"\x00\x01\x02\x03")
        ok47 = isinstance(escaped, str) and len(escaped) > 0
        detail = repr(escaped)[:40]
    except Exception as exc:  # noqa: BLE001
        ok47, detail = False, f"{type(exc).__name__}: {exc}"
    check(ok47, f"T1-47b aiomysql 能把 bytes 参数转义（得到 {detail}）")


# ═══════════════════════════════════════════════════════════════
# T1.5 假会话：不用数据库驱动 session.py
# ═══════════════════════════════════════════════════════════════


async def tier15_fake_session() -> None:
    print("\n== T1.5 假会话：归属校验 / 历史取窗 / 删除语义 ==")

    ctx = ctx_for(user_id="u_owner")

    # ---- T1.5-1 🔴 横向越权：拿到别人的 session_id 也读不到 ----
    foreign = _Obj(id="sess_other", user_id="u_attacker", title="别人的会话", message_count=2)
    fake = _FakeSession([foreign])
    err_foreign: NotFoundError | None = None
    try:
        await session_engine.get_owned_session(fake, "sess_other", ctx)
    except NotFoundError as exc:
        err_foreign = exc
    check(err_foreign is not None, "T1.5-1 🔴 读他人会话 -> NotFoundError（横向越权被挡）")
    check(
        err_foreign is not None and err_foreign.code == ErrorCode.NOT_FOUND,
        "T1.5-2 业务码是 3001",
    )

    # ---- T1.5-3 「不存在」与「不属于你」必须**不可区分** ----
    fake_missing = _FakeSession([])
    err_missing: NotFoundError | None = None
    try:
        await session_engine.get_owned_session(fake_missing, "sess_ghost", ctx)
    except NotFoundError as exc:
        err_missing = exc
    check(
        err_missing is not None
        and err_foreign is not None
        and err_missing.message == err_foreign.message
        and err_missing.code == err_foreign.code,
        "T1.5-3 🔴 「不存在」与「不属于你」响应完全一致（用 403 区分 = 可枚举的探针）",
    )

    # ---- T1.5-4 自己的会话正常返回 ----
    own = _Obj(id="sess_mine", user_id="u_owner", title="我的", message_count=0)
    got = await session_engine.get_owned_session(_FakeSession([own]), "sess_mine", ctx)
    check(got is own, "T1.5-4 自己的会话正常取回")

    # ---- T1.5-5 delete 走同一条归属校验 ----
    fake_del = _FakeSession([foreign])
    blocked = False
    try:
        await session_engine.delete_session(fake_del, "sess_other", ctx)
    except NotFoundError:
        blocked = True
    check(blocked, "T1.5-5a 🔴 删他人会话被挡")
    check(
        not fake_del.deleted and fake_del.committed == 0,
        "T1.5-5b 被挡时**一条 delete 都没发**（不是「先删了再报错」）",
    )

    # ---- T1.5-6 🔴 load_history：取**最近** N 条再翻正序 ----
    # 造 6 条（3 轮），DB 侧按 created_at DESC 返回「最近的在前」
    rows = [
        _Obj(id="m6", role="assistant", content="A3"),
        _Obj(id="m5", role="user", content="Q3"),
        _Obj(id="m4", role="assistant", content="A2"),
        _Obj(id="m3", role="user", content="Q2"),
        _Obj(id="m2", role="assistant", content="A1"),
        _Obj(id="m1", role="user", content="Q1"),
    ]
    fake_hist = _FakeSession(rows)
    history = await session_engine.load_history(fake_hist, "sess_x", rounds=3)
    check(
        [m.id for m in history] == ["m1", "m2", "m3", "m4", "m5", "m6"],
        "T1.5-6a 🔴 最近 3 轮、按时间**正序**返回（顺序反了模型会答上一轮，且不报错）",
    )
    sql = _sql(fake_hist.last_stmt)
    check(
        "ORDER BY qa_message.created_at DESC" in sql and "qa_message.id DESC" in sql,
        "T1.5-6b SQL 是倒序取窗（正序取前 N 会拿到**最早**的 N 条，等于看不到刚说的话）",
    )
    check("LIMIT %s" in sql, "T1.5-6c 有 LIMIT（rows=3 -> 6 条）")

    # rounds=0 时不查库
    empty_fake = _FakeSession(rows)
    check(
        await session_engine.load_history(empty_fake, "sess_x", rounds=0) == []
        and empty_fake.last_stmt is None,
        "T1.5-7 rounds=0 直接返回空且**不发 SQL**",
    )

    # ---- T1.5-8 delete_session 只动两张表，且提交一次 ----
    mine = _Obj(id="sess_mine", user_id="u_owner", title="我的", message_count=2)
    msgs = [_Obj(id="m1"), _Obj(id="m2")]
    fake_ok = _FakeSession([mine])
    # scalars 会被调用两次：一次取会话、一次取消息。这里让第二次返回消息列表。
    calls = {"n": 0}

    class _TwoPhaseSession(_FakeSession):
        async def scalars(self, stmt):
            self.last_stmt = stmt
            calls["n"] += 1
            return _FakeResult(msgs if calls["n"] > 1 else [mine])

    fake_ok = _TwoPhaseSession()
    await session_engine.delete_session(fake_ok, "sess_mine", ctx)
    check(
        len(fake_ok.deleted) == 3 and fake_ok.committed == 1,
        "T1.5-8a 删 2 条消息 + 1 个会话，恰好提交一次",
    )
    check(
        not any(isinstance(o, QaLog) for o in fake_ok.deleted),
        "T1.5-8b 🔴 删除集合里**没有 QaLog**（qa_log 不随会话删，它是看板与挖掘的数据源）",
    )

    # ---- T1.5-9 新建会话：标题截断 + 首问为空白时的兜底 ----
    fake_new = _FakeSession()
    s = await session_engine.create_session(fake_new, ctx, "  怎样报销住宿费  \n第二行")
    check(s.title == "怎样报销住宿费 第二行"[: settings.qa_session_title_chars], "T1.5-9a 标题取首问前 N 字且换行被压平")
    check(fake_new.committed == 1, "T1.5-9b 新建后立即提交")
    blank = await session_engine.create_session(_FakeSession(), ctx, "   ")
    check(blank.title == "新会话", "T1.5-9c 首问全空白时兜底标题（避免侧栏出现无名条目）")

    # ---- T1.5-10 🔴 persist_turn 是三表一个事务 ----
    fake_persist = _FakeSession()
    sess = _Obj(id="sess_p", user_id="u_owner", title="t", message_count=0)
    await session_engine.persist_turn(
        fake_persist,
        session=sess,
        ctx=ctx,
        question="Q",
        message_id="msg_assistant",
        answer="A",
        citations=[],
        blocked_count=0,
        log=session_engine.TurnLogData(
            question="Q",
            question_embedding=[0.1] * 768,
            recalled_ids=["c1"],
            allowed_ids=["c1"],
            blocked_ids=["d9"],
            top_score=0.81,
            token_in=10,
            token_out=20,
            latency_ms=1234,
        ),
    )
    kinds = [type(o) for o in fake_persist.added]
    check(
        kinds == [QaMessage, QaMessage, QaLog],
        "T1.5-10a 一轮落 2 条消息 + 1 条日志",
    )
    check(fake_persist.committed == 1, "T1.5-10b 一次事务提交（be-05 §10-106：同步落库，不留尾巴）")
    check(sess.message_count == 2, "T1.5-10c message_count 每轮 +2（与 qa_message 行数同事务维护）")

    log_row = fake_persist.added[2]
    check(
        isinstance(log_row.question_embedding, bytes) and len(log_row.question_embedding) == 768 * 4,
        "T1.5-11 🔴 question_embedding 落成 3072 字节 BLOB（不写它这条日志永远无法参与 P3 聚类）",
    )
    check(
        isinstance(log_row.department_id, str) and log_row.department_id == ctx.department_id,
        "T1.5-12 department_id 存**自身**部门（归属口径），不是含祖先的 depts（权限口径）",
    )
    check(
        log_row.token_in == 10 and log_row.token_out == 20 and log_row.top_score == 0.81,
        "T1.5-13 token 与 top_score 全列落库（漏一列不报错，只在看板上表现为「一直是 0」）",
    )
    assistant_row = fake_persist.added[1]
    check(
        assistant_row.id == "msg_assistant" and assistant_row.role == QaMessage.ROLE_ASSISTANT,
        "T1.5-14 assistant 行的 id 由调用方传入（与 SSE start 事件的 message_id 一致）",
    )

    # ---- T1.5-15 联想只查自己 ----
    fake_sug = _FakeSession([("差旅报销标准", 5), ("年假多少天", 3)])
    out = await session_engine.suggestions(fake_sug, ctx, 6)
    check(out == ["差旅报销标准", "年假多少天"], "T1.5-15a 联想返回问题原文列表")
    sug_sql = _sql(fake_sug.last_stmt)
    check(
        "qa_log.user_id = %s" in sug_sql and "GROUP BY qa_log.question" in sug_sql,
        "T1.5-15b 🔴 联想恒带 user_id 过滤（做全局热词会把他人的提问文本展示给所有人）",
    )
    check(
        "qa_log.session_id" not in sug_sql,
        "T1.5-16 🔴 不 JOIN qa_session（会话可能已被用户删除，JOIN 会丢掉这些日志）",
    )


# ═══════════════════════════════════════════════════════════════
# T2 接口层
# ═══════════════════════════════════════════════════════════════

_QUERY_PATHS = [
    "/api/query/search",
    "/api/query/ask",
    "/api/query/ask/stream",
    "/api/query/sessions",
    "/api/query/sessions/{session_id}",
    "/api/query/suggestions",
]


async def tier2_http() -> bool:
    """返回「T2 是否真的跑过」（False 表示依赖不具备，结果不构成完整验收）。"""
    print("\n== T2 接口层：需要 MySQL + Redis ==")

    # ---- T2-0 端点注册（不需要 DB，先跑）----
    openapi = main.app.openapi()["paths"]
    # ⚠️ Starlette 1.7 的 include_router 不把子路由摊平进 app.routes，
    #    要断言注册成功必须看 openapi()["paths"]（CLAUDE.md 已知坑）
    for path in _QUERY_PATHS:
        check(path in openapi, f"T2-0 路径已注册：{path}")
    check(
        sorted(openapi.get("/api/query/sessions/{session_id}", {}).keys()) == ["delete", "get"],
        "T2-1 会话详情是 GET，删除是 DELETE（同名两条）",
    )

    # ---- 起 ASGI 客户端 ----
    try:
        transport = ASGITransport(app=main.app)
        client = AsyncClient(transport=transport, base_url="http://test")
    except Exception as exc:  # noqa: BLE001
        skip("T2 全部接口用例", f"ASGI 客户端构造失败：{type(exc).__name__}")
        return False

    async with client:
        # ---- T2-2 登录门禁 ----
        for path, method in (
            ("/api/query/sessions", "get"),
            ("/api/query/suggestions", "get"),
            ("/api/query/search", "post"),
            ("/api/query/ask", "post"),
            ("/api/query/ask/stream", "post"),
        ):
            # 🔴 **只有 POST 能带 `json=`**：httpx 的 `AsyncClient.get()` 签名里
            #    没有这个参数，传了会抛
            #    `TypeError: get() got an unexpected keyword argument 'json'`。
            #    原来这两条 GET 一律带着 `json=`，于是它们**每次都走 except**、
            #    静默变成 SKIP —— 而 SKIP 和通过一样是绿的。
            #    代价是「未登录访问会话列表 / 联想接口 → 401」这两条鉴权用例
            #    从落地起一次都没真跑过（`verify_search` 的 T2 里唯一的两条 GET）。
            kwargs: dict[str, object] = {"json": {"query": "x"}} if method == "post" else {}
            try:
                resp = await getattr(client, method)(path, **kwargs)
                check(
                    resp.status_code == 401,
                    f"T2-2 {method.upper()} {path} 未登录 -> 401（实际 {resp.status_code}）",
                )
            except Exception as exc:  # noqa: BLE001
                skip(f"T2-2 {method.upper()} {path}", f"{type(exc).__name__}（依赖不可用）")
        try:
            resp = await client.delete("/api/query/sessions/sess_x")
            check(resp.status_code == 401, f"T2-2 DELETE /sessions/x 未登录 -> 401（实际 {resp.status_code}）")
        except Exception as exc:  # noqa: BLE001
            skip("T2-2 DELETE /sessions/x", f"{type(exc).__name__}")

        # ---- 登录拿 token ----
        token = ""
        try:
            r = await client.post(
                "/api/auth/login", json={"username": "cost01", "password": PASSWORD}
            )
            body = r.json()
            if body.get("code") == 0:
                token = body["data"]["access_token"]
        except Exception as exc:  # noqa: BLE001
            skip("T2 剩余接口用例", f"登录失败：{type(exc).__name__}（MySQL/Redis 不可达）")
            return False

        if not token:
            skip("T2 剩余接口用例", "登录未返回 access_token（MySQL/Redis 不可达）")
            return False

        check(True, "T2-3 登录成功（cost01）")

        # ---- T2-4 🔴 越权：用别人的 session_id 提问 ----
        try:
            r = await client.post(
                "/api/query/ask",
                headers=auth(token),
                json={"query": f"{MARK} 越权探测", "session_id": "sess_not_mine"},
            )
            body = r.json()
            check(
                body.get("code") == ErrorCode.NOT_FOUND,
                f"T2-4 🔴 用他人/不存在的 session_id 提问 -> 3001（实际 {body.get('code')}）",
            )
        except Exception as exc:  # noqa: BLE001
            skip("T2-4 越权用例", f"{type(exc).__name__}")

        # ---- T2-5 会话列表分页结构 ----
        try:
            r = await client.get("/api/query/sessions?page=1&page_size=5", headers=auth(token))
            body = r.json()
            data = body.get("data") or {}
            check(
                body.get("code") == 0
                and set(data.keys()) >= {"list", "total", "page", "page_size"},
                "T2-5 会话列表返回统一分页结构 {list,total,page,page_size}",
            )
            check(
                isinstance(data.get("list"), list) and data.get("page") == 1,
                "T2-5b page 原样回显",
            )
        except Exception as exc:  # noqa: BLE001
            skip("T2-5 会话列表", f"{type(exc).__name__}")

        # ---- T2-6 联想结构（P1 只可能是自己的问题）----
        try:
            r = await client.get("/api/query/suggestions?limit=6", headers=auth(token))
            body = r.json()
            check(
                body.get("code") == 0 and isinstance(body.get("data"), list),
                "T2-6 联想返回 list[str]",
            )
        except Exception as exc:  # noqa: BLE001
            skip("T2-6 联想", f"{type(exc).__name__}")

        # ---- T2-7 DELETE 真删除 + qa_log 保留 ----
        try:
            doc_ok = await _seed_one_turn(client, token)
            if not doc_ok:
                skip("T2-7 删除语义", "造数失败（检索链路不可用）")
            else:
                sid, log_count_before = doc_ok
                r = await client.delete(f"/api/query/sessions/{sid}", headers=auth(token))
                check(r.json().get("code") == 0, "T2-7a DELETE 返回 code 0")
                r2 = await client.get(f"/api/query/sessions/{sid}", headers=auth(token))
                check(
                    r2.json().get("code") == ErrorCode.NOT_FOUND,
                    "T2-7b 🔴 删除是真删除（再查 -> 3001），不是「清空上下文」",
                )
                async with engine.connect() as conn:
                    left = await conn.scalar(
                        text("SELECT COUNT(*) FROM qa_message WHERE session_id = :s"), {"s": sid}
                    )
                    logs = await conn.scalar(
                        text("SELECT COUNT(*) FROM qa_log WHERE session_id = :s"), {"s": sid}
                    )
                check(left == 0, "T2-7c 消息随会话删除")
                check(
                    logs == log_count_before and logs > 0,
                    "T2-7d 🔴 qa_log **保留**（跟着删会让看板指标凭空下跌）",
                )
        except Exception as exc:  # noqa: BLE001
            skip("T2-7 删除语义", f"{type(exc).__name__}")

        # ---- T2-8 post_verify 的四维 OR ----
        await _t2_post_verify()

    await _sweep()
    return True


async def _seed_one_turn(client: AsyncClient, token: str) -> tuple[str, int] | None:
    """走一次非流式问答，造出一条会话与一条 qa_log。"""
    try:
        r = await client.post(
            "/api/query/ask",
            headers=auth(token),
            json={"query": f"{MARK} 报销标准是什么", "top_k": 3},
        )
        body = r.json()
        if body.get("code") != 0:
            return None
        sid = (body.get("data") or {}).get("session_id")
        if not sid:
            return None
        async with engine.connect() as conn:
            logs = await conn.scalar(
                text("SELECT COUNT(*) FROM qa_log WHERE session_id = :s"), {"s": sid}
            )
        return sid, int(logs or 0)
    except Exception:  # noqa: BLE001
        return None


async def _t2_post_verify() -> None:
    """后置兜底的四维 OR（需要 MySQL 权威表）。"""
    ctx = ctx_for(user_id="u_probe")
    try:
        async with engine.connect() as conn:
            real = await conn.scalar(
                text("SELECT id FROM kb_document WHERE deleted_at IS NULL LIMIT 1")
            )
    except Exception as exc:  # noqa: BLE001
        skip("T2-8 post_verify 四维判定", f"MySQL 不可达：{type(exc).__name__}")
        return

    if not real:
        skip("T2-8 post_verify 四维判定", "kb_document 里没有可用文档")
        return

    class _Db:
        """把 `post_verify` 用到的 `execute` 直接转给真实连接。"""

        def __init__(self, conn) -> None:
            self._conn = conn

        async def execute(self, stmt):
            return await self._conn.execute(stmt)

    try:
        async with engine.connect() as conn:
            # 「一篇都不可见」-> 全部计入 blocked，且**不取标题**
            res = await acl_mod.post_verify(_Db(conn), {real}, ctx, set())
            check(
                res.allowed == set() and res.blocked == {real} and res.titles == {},
                "T2-8a 不在可见集合里的文档 -> blocked，且**不取标题**（红线 4）",
            )
            check(
                res.desynced == {real},
                "T2-8b desynced 非空 -> 会打 acl_desync 告警（同步窗口的唯一可见证据）",
            )
            # 空候选不查库、直接返回空
            empty = await acl_mod.post_verify(_Db(conn), set(), ctx, {real})
            check(
                empty.allowed == set() and empty.blocked == set(),
                "T2-8c 空候选直接返回空结果（不查库）",
            )
    except Exception as exc:  # noqa: BLE001
        skip("T2-8 post_verify 四维判定", f"{type(exc).__name__}")


# ═══════════════════════════════════════════════════════════════
# T3 链路
# ═══════════════════════════════════════════════════════════════


#: T3 造的会话 id，收尾按 id 精确删（不能用 MARK 前缀 ——
#: 问答会话的标题由后端生成，我们控制不了它的形状）。
_T3_SESSIONS: list[str] = []
#: T3 用真 HTTP 打哪个地址。默认就是 `uv run main.py` 起的那个。
T3_BASE = "http://127.0.0.1:8000"


def _http_up(url: str, timeout: float = 3.0) -> bool:
    """同步探活。用 `httpx.get` 而不是 async —— 只为在 async 层外快速判依赖。

    ⚠️ **判 `== 200` 而不是 `< 500`**。写成 `< 500` 时，**404 也算「在」** ——
       我第一版就是这么写的：`embedding_base_url` 是 `.../v1`，
       而健康检查在根路径 `/health`，于是探的是 `.../v1/health` → 404 → 判「BGE 在」。
       它**碰巧**是对的（BGE 确实起着），但换个场景就是假的：
       端口上蹲着任何别的 HTTP 服务（或 nginx 默认页）都会让探活通过，
       而真正的失败会推迟到几条检查之后、以「向量服务调用失败」的面目出现。
       **探活必须是「我要的那个东西答的」，不是「有人答应」** ——
       与 CLAUDE.md 里「断言要落在能区分对错的具体值上」是同一条。
    """
    import httpx

    try:
        return httpx.get(url, timeout=timeout).status_code == 200
    except Exception:  # noqa: BLE001 —— 探活失败就是「不可达」，不是错误
        return False


#: BGE 健康检查在**根路径**，而 `embedding_base_url` 带 `/v1` 后缀。
def _bge_health_url() -> str:
    base = settings.embedding_base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return f"{base}/health"


async def tier3_chain() -> None:
    """T3 链路：BGE + Milvus + DeepSeek 全在，且**必须走真 HTTP**。

    🔴 为什么必须真 HTTP：T2 用的是 `ASGITransport`（进程内直连 ASGI），
       它**不反映分帧** —— 一次 `resp.read()` 就把整段拿回来了，
       于是 A9-1「delta 逐帧到达而非一次性」在 T2 里**永远为真**，
       哪怕服务端其实是攒完再一次性发。真流只有真 socket 才看得见。

    🔴 依赖**自己探**，不照抄一句「本机无 Docker」然后永远 skip。
       本脚本原先就是那样：11 条 T3 一条不跑，理由写死「本机无 Docker」，
       而 Milvus 是直连云端的（`60.205.138.167:19530`）——**理由本身就是错的**。
       一句话头写死的 skip 理由**不会随环境改善而更新**，它只会一直骗人。
       所以这里逐项探活，不满足时 skip 里**写清楚是谁不可达**，
       而不是笼统地说「环境不具备」。
    """
    print("\n== T3 链路：需要 Milvus + BGE + DeepSeek，且走真 HTTP ==")

    api_up = _http_up(f"{T3_BASE}/health")
    bge_up = _http_up(_bge_health_url())
    if not api_up:
        for label in (
            "A4-1 检索命中已授权文档并给出引用",
            "A4-2 引用卡片含 document_id/title/heading_path/score",
            "A4-3 top_score 是余弦而非 RRF 分（与 kb_min_score 可比）",
            "A5-3 受限提示只报数量、不含标题",
            "A9-1 SSE 真流：delta 逐帧到达而非一次性",
            "A9-2 SSE 事件顺序 start→delta*→citations→blocked?→done",
            "A9-3 done 带 latency_ms/token_in/token_out/faq_hit/llm_called",
            "A4-4 多轮对话带上历史（qa_history_rounds 轮）",
        ):
            skip(label, f"API 未起：{T3_BASE}/health 不可达（先 `uv run main.py`）")
    elif not bge_up:
        for label in (
            "A4-1 检索命中已授权文档并给出引用",
            "A4-2 引用卡片含 document_id/title/heading_path/score",
            "A5-3 受限提示只报数量、不含标题",
            "A9-2 SSE 事件顺序 start→delta*→citations→blocked?→done",
        ):
            skip(label, f"BGE 不可达：{settings.embedding_base_url}（起 dev_bge_server.py）")
    else:
        global _t3_ran
        _t3_ran = True
        await _t3_real_http()

    # 这两条**确实**需要造脏数据 / 断连，与上面那些不是一类，继续如实 skip。
    skip(
        "A5-2 🔴 后置兜底：Milvus ACL 陈旧时仍剔除",
        "需要往 Milvus 注入「与该文档 MySQL 权限不一致」的脏行；"
        "本脚本只读不改线上向量，故留待专门的注入用例",
    )
    skip(
        "A9-4 客户端中途断开 -> 半截回答仍落库",
        "需要可控地在流中途 abort 并确认半截内容落库；"
        "`_sweep` 会把它删掉，观测窗口太短，故留待专门的用例",
    )


async def _t3_real_http() -> None:
    """真的打一遍 `127.0.0.1:8000`，把 A4 / A9 的链路证据收下来。"""
    import httpx

    try:
        async with httpx.AsyncClient(base_url=T3_BASE, timeout=180) as client:
            resp = await client.post(
                "/api/auth/login", json={"username": "admin", "password": PASSWORD}
            )
            if resp.status_code != 200:
                skip("T3 全部链路用例", f"登录失败 HTTP {resp.status_code}")
                return
            token = resp.json()["data"]["access_token"]
            headers = {"Authorization": f"Bearer {token}"}

            # ---- A4：非流式问答，拿引用 ----
            ask = await client.post(
                "/api/query/ask",
                json={"query": "权限感知检索是怎么做的？"},
                headers=headers,
            )
            if ask.status_code != 200:
                check(False, f"A4-1 问答接口 HTTP {ask.status_code}")
                return
            data = ask.json().get("data") or {}
            citations = data.get("citations") or []
            if data.get("session_id"):
                _T3_SESSIONS.append(data["session_id"])

            # 语料就是本项目自己的设计文档，所以「问项目自己的问题」必然能召回到。
            check(
                len(citations) > 0,
                f"A4-1 检索命中已授权文档并给出引用（实测 {len(citations)} 条）",
            )
            check(
                bool(data.get("content")),
                f"A4-2 回答正文非空（{len(str(data.get('content')))} 字）",
            )
            if citations:
                first = citations[0]
                check(
                    all(k in first for k in ("document_id", "title", "heading_path", "score")),
                    f"A4-2 引用卡片字段齐全（实测 {sorted(first.keys())}）",
                )
                check(
                    bool(first.get("title")) and bool(first.get("heading_path")),
                    "A4-2 引用带**标题与面包屑**（红线 4：能拿到标题说明过了后置复核）",
                )
                scores = [float(c.get("score", 0)) for c in citations]
                # 余弦恒在 [-1,1]；RRF 融合分是 1/(k+rank) 量级（~0.016），
                # 与 `kb_min_score`(0.4) 不同量纲。用「有没有超过 kb_min_score」区分：
                # RRF 分**不可能**超过 0.4，所以这一条能真的分开两种口径。
                check(
                    all(0.0 <= s <= 1.0 for s in scores)
                    and max(scores) >= settings.kb_min_score,
                    f"A4-3 top_score 与 kb_min_score 同量纲（max={max(scores):.4f} "
                    f"≥ {settings.kb_min_score}）—— RRF 分量级只有 ~0.016，过不了这条",
                )

            # ---- A9：SSE 真流 ----
            events: list[str] = []
            deltas: list[str] = []
            payloads: dict[str, str] = {}
            async with client.stream(
                "POST",
                "/api/query/ask/stream",
                json={"query": "运营看板的聚合为什么必须幂等？"},
                headers={**headers, "Accept": "text/event-stream"},
            ) as stream:
                if stream.status_code != 200:
                    check(False, f"A9-2 SSE HTTP {stream.status_code}")
                    return
                name = None
                async for line in stream.aiter_lines():
                    if line.startswith("event:"):
                        name = line[6:].strip()
                        events.append(name)
                    elif line.startswith("data:"):
                        payload = line[5:].strip()
                        if name and name not in payloads:
                            payloads[name] = payload
                        if name == "delta":
                            try:
                                deltas.append(json.loads(payload).get("content", ""))
                            except Exception:  # noqa: BLE001
                                pass
                # 会话 id 走 `start` 事件的载荷（不是响应头 —— 别猜一个不存在的头）
                if "start" in payloads:
                    sid = json.loads(payloads["start"]).get("session_id")
                    if sid:
                        _T3_SESSIONS.append(sid)

            check(
                len(deltas) > 1,
                f"A9-1 delta **逐帧**到达而非一次性（实测 {len(deltas)} 帧）"
                "—— ASGITransport 下这条恒真，真 socket 才有意义",
            )
            check(
                bool(events) and events[0] == "start" and events[-1] == "done",
                f"A9-2 事件顺序 start→…→done（首={events[:1]} 尾={events[-1:]}）",
            )
            check(
                events.count("citations") == 1,
                f"A9-2 `citations` 恰好一次（实测 {events.count('citations')}）",
            )
            # `blocked` 是可选的（无受限文档时不发）——断言「最多一次」而不是「必须一次」
            check(
                events.count("blocked") <= 1,
                f"A9-2 `blocked` 最多一次（实测 {events.count('blocked')}）",
            )
            if "done" in payloads:
                done = json.loads(payloads["done"])
                need = ("latency_ms", "token_in", "token_out", "faq_hit", "llm_called")
                check(
                    all(k in done for k in need),
                    f"A9-3 done 带 {need}（实测 {sorted(done.keys())}）",
                )
                check(
                    done.get("latency_ms", 0) > 0,
                    f"A9-3 latency_ms > 0（实测 {done.get('latency_ms')}）",
                )
                # FAQ 命中与 LLM 调用必须**互斥** —— 同时为真说明降级桩又回来了
                check(
                    not (done.get("faq_hit") and done.get("llm_called")),
                    f"A9-3 faq_hit({done.get('faq_hit')}) 与 llm_called"
                    f"({done.get('llm_called')}) 不共存",
                )
                if done.get("faq_hit"):
                    check(
                        done.get("token_in") == 0 and done.get("token_out") == 0,
                        f"A9-3 FAQ 命中时 token 恒 0（实测 {done.get('token_in')}/"
                        f"{done.get('token_out')}）",
                    )
            else:
                check(False, "A9-3 SSE 里没有 done 事件")

            check(
                "blocked" not in payloads
                or json.loads(payloads["blocked"]).get("titles") is None,
                "A5-3 受限提示只报数量、titles 为 null（红线 4，"
                "`ACL_REVEAL_BLOCKED_TITLES` 默认 false）",
            )

            # ---- A4-4 多轮：同一会话再问一句，历史应被带上 ----
            if data.get("session_id"):
                second = await client.post(
                    "/api/query/ask",
                    json={"query": "那 be-07 呢？", "session_id": data["session_id"]},
                    headers=headers,
                )
                check(
                    second.status_code == 200
                    and (second.json().get("data") or {}).get("session_id")
                    == data["session_id"],
                    f"A4-4 同会话追问复用 session_id（HTTP {second.status_code}）",
                )
    except Exception as exc:  # noqa: BLE001 —— 链路用例失败要报 FAIL，不要炸脚本
        check(False, f"T3 链路异常：{type(exc).__name__}: {exc}")


# ═══════════════════════════════════════════════════════════════
# 清理
# ═══════════════════════════════════════════════════════════════


async def _sweep() -> None:
    """清掉本脚本造的数据。

    🔴 前缀 `ZZVFYSEARCH` **不含下划线** —— `LIKE 'zzvfy_%'` 里的 `_` 是通配符，
       会连带命中 `zzvfyX...` 这类兄弟数据（CLAUDE.md 已知坑第一条）。
       `qa_log` 不随会话删，必须单独按前缀清。

    ⚠️ **T3 造的会话不吃前缀清理**：它是打真 HTTP 问出来的，标题由后端按问题
       生成，形状我们控制不了。所以 T3 自己把 `session_id` 记进 `_T3_SESSIONS`，
       这里**按 id 精确删**（`IN (...)`，不是 `LIKE`）。
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(
                text("DELETE FROM qa_message WHERE session_id IN "
                     "(SELECT id FROM qa_session WHERE title LIKE :p)"),
                {"p": f"{MARK}%"},
            )
            await conn.execute(
                text("DELETE FROM qa_session WHERE title LIKE :p"), {"p": f"{MARK}%"}
            )
            await conn.execute(
                text("DELETE FROM qa_log WHERE question LIKE :p"), {"p": f"{MARK}%"}
            )
            if _T3_SESSIONS:
                ids = list(dict.fromkeys(_T3_SESSIONS))  # 去重且保序
                await conn.execute(
                    text("DELETE FROM qa_message WHERE session_id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
                    {"ids": ids},
                )
                await conn.execute(
                    text("DELETE FROM qa_session WHERE id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
                    {"ids": ids},
                )
                await conn.execute(
                    text("DELETE FROM qa_log WHERE session_id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
                    {"ids": ids},
                )
                print(f"  [note] 清理 T3 自造会话：{len(ids)} 个")
            await conn.commit()
    except Exception as exc:  # noqa: BLE001 —— 清理失败不该让验收结果变成 FAIL
        print(f"  [note] 清理未执行（{type(exc).__name__}）")


# ═══════════════════════════════════════════════════════════════


async def run() -> tuple[bool, bool]:
    """返回 `(全部通过, T2 是否真的跑过)`。

    ⚠️ T1 / T1.5 / T2 在**同一个事件循环**里跑 —— 这正是 `core/db.py` 的
       模块级 engine 能跨层复用的前提。若把它们各自塞进一个 `asyncio.run()`，
       第二层会复用到上个循环遗留的连接，报
       `'NoneType' object has no attribute 'send'`（CLAUDE.md 已知坑），
       而且症状是「单跑某一层都过、合起来必挂」。
    """
    tier1_offline()
    await tier15_fake_session()
    ran = await tier2_http()
    await tier3_chain()
    # 🔴 **T3 之后必须再扫一次**：T2 里那次 `_sweep()` 发生在 T3 之前，
    #    而 T3 是打真 HTTP 问出来的会话（标题由后端生成、不吃 MARK 前缀），
    #    不补这一次，它们就**留在库里**了 —— 验收脚本往业务库里留脏数据，
    #    而且是「跑一次留几个、跑 N 次留 N 批」，越跑越多。
    await _sweep()
    await engine.dispose()
    return all(_results), ran


def main_() -> None:
    ok_, ran = asyncio.run(run())
    passed = sum(_results)
    total = len(_results)
    print(f"\n{'=' * 56}")
    # 🔴 标签要说实话：`_results` 里**含 T2 / T3 的检查**（真跑过时）。
    #    写死「T1/T1.5」会让人以为接口层一条都没验，把已经跑过的当成没跑
    #    —— 与「把 SKIP 当成通过」是同一类误读，只是方向相反。
    layers = ["T1/T1.5（离线+假会话）"]
    if ran:
        layers.append("T2（接口层）")
    if _t3_ran:
        layers.append("T3（链路·真 HTTP）")
    print(f"{' + '.join(layers)}：{passed}/{total} 通过")
    # 不写死「都是 T2/T3 的依赖问题」——T2/T3 现在都真的跑起来了。
    print(f"SKIP：{len(_skipped)} 项（**不是通过**）—— 逐条原因见上")
    if not ran:
        print("⚠️ T2 未执行（MySQL 或 Redis 不可用）—— **本次结果不构成完整验收**。")
        print("   依赖恢复后重跑本脚本。")
    if not _t3_ran:
        print("⚠️ T3 未执行（API / BGE 不可达）—— A4/A9 的链路证据本次**没有**收到。")
    if passed != total:
        print("存在失败项，见上方 FAIL 行。")
    sys.exit(0 if (ok_ and ran) else 1)


if __name__ == "__main__":
    main_()
