"""启动与 schema 就绪的验收脚本（「修 MySQL 缺表 / 保证项目可启动」改动的可执行版本）。

跑法（项目根目录）：

    uv run python -m backend.scripts.verify_startup

## 这次改动在修什么

启动报错、MySQL 里缺表。**根因不是某个模块写错，而是没有任何东西会跑迁移**：

- `backend/Dockerfile` 里没有 `alembic upgrade`；
- FastAPI 的 `lifespan` 里没有 `create_all()`（刻意的，见下）；
- `deploy/mysql/init/01-charset.sql` 只设字符集、**不建表**，且只在数据卷首次初始化时执行。

于是真实库里停在 `0002`，而代码已经到 `0007`。后果是**进程能正常起来**：
`/health` 回 200，然后第一个写操作 500（`sys_audit_log` 缺失 —— 审计与业务同事务，
这是刻意的取舍）、5 个看板端点 500、`aggregate_hourly` 每 10 分钟失败一次……

## 覆盖面

| 层 | 需要什么 | 覆盖 |
|---|---|---|
| **T1 离线** | 什么都不需要 | 目标库常量、`db.py` 的**只读性**（AST 级）、`main.py` 的调用顺序与 try/finally、`/health` 与 `/ready` 的分工、`alembic/env.py` 的目标库闸门、compose 的 migrate 编排与依赖条件、迁移链单头 + `0008` 与 ORM 一致 |
| **T1.5 假连接** | 什么都不需要 | 用假 engine/假 heads 驱动 `assert_schema_ready` 的**四个分支**（库名不对 / 连不上 / 版本落后 / 版本对但缺表）、`/ready` 的 503、FAQ 装载失败仍降级、**启动失败仍执行清理** |
| **T2 真实库** | MySQL（`.env` 指向的 `rag_kb`） | 表清单齐全、版本 == head、`kb_import_task.gap_id` 在、`kb_document_permission.created_at` 已删、`assert_schema_ready()` 真的不抛、真实 `/ready` 回 200 |

⚠️ **T1.5 是「假连接」**：它把 `engine` 与 `_script_directory` 换成受控替身，
   只验证**判定逻辑与报错文案**。真正的 SQL 能不能跑通由 T2 证明 ——
   两者不可互相替代（这一点在 be-08 / be-09 的假会话层已经写过教训：
   假会话抓出了两个只有真跑才会暴露的名字错误，但它证明不了 SQL 本身对）。

⚠️ **本脚本对数据库只读**：不建表、不写种子、不删任何行，也不需要标记前缀。
   （唯一会写的动作是 T2 之前的自检 —— 没有。）
   目标库由 `assert_schema_ready` 与 `alembic/env.py` 双重限制为 `rag_kb`。

⚠️ Windows 控制台是 GBK —— 输出一律 ASCII 骨架 + 断言**布尔值与具体值**。
"""

from __future__ import annotations

import ast
import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alembic.runtime.migration import MigrationContext
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

import main
from backend.core import db as core_db
from backend.core.db import ALLOWED_DATABASE, SchemaStateError, engine
from backend.core.settings import settings
from backend.models import Base
from backend.models.kb_document import KbDocumentPermission
from backend.service import faq_service

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
VERSIONS = ROOT / "alembic" / "versions"

#: 仓库当前的迁移 head。**故意写成常量并断言**：它一变，本脚本就红 ——
#: 逼着加迁移的人回来确认「新迁移是否也需要一条守门用例」。
#: 与之对照的是「从文件推出来的 head」（T1-20），两者相等才算自洽。
EXPECTED_HEAD = "0008"

# 🔴 Windows 控制台默认 GBK，本脚本标签里有 🔴 —— 不重配的话 print 直接抛
#    UnicodeEncodeError，而且抛在**报 SKIP 的那一行**，
#    症状就成了「环境不可用时脚本崩了」，看起来像代码坏了。
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


def _src(rel: str) -> str:
    return (BACKEND / rel).read_text(encoding="utf-8")


def _code_only(src: str) -> str:
    """去掉注释与 docstring，只留代码本体。

    用于「源码里不许出现 X」这类静态断言。**必须去注释与 docstring**：
    本项目的注释会大段引用被禁的东西（`db.py` 的 docstring 里就写着
    「不 `create_all()`」），直接子串匹配会把**解释**当成**违规**，
    用例永远红着，最后被人注释掉 —— 那才是真损失。

    ⚠️ docstring 按 `ast` 给的**行列位置**挖空，不能用 `src.replace(值, "")`：
       源文件里写的是转义后的字面量，而 docstring 的**值**是真字符 ——
       两者不相等，替换会静默失效（`verify_search.py` 首跑踩过）。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:  # pragma: no cover —— 语法错该在别处暴露
        return src

    blanked = src.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr | ast.Assign | ast.AnnAssign):
            continue
        value = getattr(node, "value", None)
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        start = getattr(value, "lineno", None)
        end = getattr(value, "end_lineno", None)
        if start is None or end is None:
            continue
        for lineno in range(start, end + 1):
            blanked[lineno - 1] = ""
    return "\n".join(blanked)


def _called_names(src: str, func_name: str) -> list[str]:
    """按**源码出现顺序**列出 `func_name` 里所有被调用者的名字。

    取 `Call.func` 的 `Name.id` 或 `Attribute.attr`，所以 `op.drop_column` → `drop_column`。
    顺序是必需的：本脚本有多处断言是「A 必须早于 B」，用集合就把这个信息丢了。

    🔴 **必须显式按 `(lineno, col_offset)` 排序** —— `ast.walk` 是**广度优先**的，
       产出顺序**不是**源码顺序。首跑就踩到了：`0008` 的 `upgrade` 里
       `if not _has_column(): return` 写在 `op.drop_column` 之前，而
       `ast.walk` 先吐出**语句层**那个 `drop_column`（它在 BFS 的第 3 层），
       后吐出嵌在 `if` 条件里的 `_has_column`（第 4 层），
       于是断言报「先 drop 再判存在」—— **红得理直气壮，但是假的**。
       这类假红比没测更糟：它会把人把用例注释掉，或者去"修"本来正确的代码。
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == func_name:
            found: list[tuple[int, int, str]] = []
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                fn = sub.func
                if isinstance(fn, ast.Name):
                    found.append((sub.lineno, sub.col_offset, fn.id))
                elif isinstance(fn, ast.Attribute):
                    found.append((sub.lineno, sub.col_offset, fn.attr))
            found.sort()
            return [name for _, _, name in found]
    return []


def _compose_services() -> dict[str, str]:
    """把 `docker-compose.yml` 按**服务名**切成块，并**去掉注释行**。

    不 import yaml（不是项目依赖，为几条断言引一个新依赖不划算），
    但也不对整个文件做子串匹配 —— `MYSQL_HOST: mysql` 这类行在每个业务服务里
    都出现一次，整文件匹配等于**断言不到具体是谁**（那种断言永远绿）。
    按缩进切开后，断言就落到了具体服务上。

    🔴 **必须去注释**，而且理由不是「洁癖」：本项目的注释会**引用被断言的关键字**
       （`depends_on` 上方那三行注释里就写着 `service_completed_successfully`），
       留着注释的话，把真正的 `condition:` 那行删掉，用例照样绿 ——
       这正是 CLAUDE.md 里「假绿比没测更糟」那一类。
    """
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    blocks: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            if current is not None:
                blocks[current] = "\n".join(buf)
            current, buf = m.group(1), []
            continue
        if current is not None and re.match(r"^[A-Za-z]", line):
            # 回到顶层键（name/services/volumes/networks），本块结束
            blocks[current] = "\n".join(buf)
            current, buf = None, []
            continue
        if current is None or line.strip().startswith("#"):
            continue
        buf.append(line)
    if current is not None:
        blocks[current] = "\n".join(buf)
    return blocks


# ═══════════════════════════════════════════════════════════════
# T1 离线
# ═══════════════════════════════════════════════════════════════


def t1_target_db_guard() -> None:
    print("\n[T1] 目标库保护")

    check(
        ALLOWED_DATABASE == "rag_kb",
        f"T1-1  ALLOWED_DATABASE 恰为 'rag_kb'（实际 {ALLOWED_DATABASE!r}）",
    )

    db_code = _code_only(_src("core/db.py"))
    check(
        "ALLOWED_DATABASE" in db_code and "settings.mysql_db" in db_code,
        "T1-2  core/db.py 用常量与 settings.mysql_db 做比较（不是硬编码字面量两处）",
    )

    # 🔴 迁移侧必须也有这道闸。只查启动侧的话，`alembic upgrade head` 仍可能
    #    在别的库上建表 —— 而那条命令不会报错，事后才发现动错了库。
    env_code = _code_only((ROOT / "alembic" / "env.py").read_text(encoding="utf-8"))
    check(
        "ALLOWED_DATABASE" in env_code and "raise" in env_code,
        "T1-3  alembic/env.py 引同一个常量并在不匹配时 raise",
    )
    check(
        "ALLOWED_DATABASE" in env_code and 'settings.mysql_db' in env_code,
        "T1-4  alembic/env.py 的闸门判的是 settings.mysql_db",
    )


def t1_read_only_schema_check() -> None:
    print("\n[T1] 启动检查必须是只读的")

    db_src = _src("core/db.py")
    calls = _called_names(db_src, "assert_schema_ready")

    # 🔴 这是本模块最重要的一条静态断言。`assert_schema_ready` 一旦变成「顺手建个表」，
    #    schema 就有了第二个写者：本地 `--reload` 每次改代码都会碰库，
    #    容器多副本时几个进程抢着改同一个 schema，而**平时看不出来** ——
    #    只有在某个副本改到一半、另一个开始读的时候才炸。
    forbidden = {"create_all", "drop_all", "stamp", "create_table", "drop_table"}
    hit = sorted(forbidden & set(calls))
    check(not hit, f"T1-5  assert_schema_ready 不调用任何 schema 写操作（命中 {hit}）")

    tree = ast.parse(db_src)
    fn = next(
        (
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "assert_schema_ready"
        ),
        None,
    )
    check(fn is not None, "T1-6  assert_schema_ready 存在且是 async")

    # 🔴 **反向确认扫描器真的扫到了东西**：没有这一条，「没有违规」与
    #    「AST 解析失败、calls 是空列表」是同一个绿灯 —— 而后者会让
    #    T1-5 从这天起永远通过，谁也不会发现它已经不再检查任何东西。
    #    （be-09 的教训：断言要落在能区分对错的具体值上，还要顺带断言「扫到了东西」。）
    check(
        {"configure", "get_current_heads", "get_table_names"} <= set(calls),
        f"T1-7  （自查）解析到关键调用，扫描器工作正常（{sorted(set(calls))}）",
    )


def t1_main_wiring() -> None:
    print("\n[T1] main.py 的接线")

    # main.py 在仓库根，不在 backend/ 下，所以不走 `_src`
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    code = _code_only(src)

    check("await assert_schema_ready()" in code, "T1-8  lifespan 里调用了 assert_schema_ready")

    # 🔴 顺序断言：schema 检查必须**早于** FAQ 装载。
    #    反过来的话，缺表时 FAQ 那一步会吞掉异常、留下一句「已降级」日志，
    #    把真正的原因（表缺了）盖过去，排查方向直接跑偏。
    i_schema = code.find("await assert_schema_ready()")
    i_faq = code.find("await faq_service.startup()")
    i_yield = code.find("\n        yield")
    i_finally = code.find("finally:")
    check(
        0 <= i_schema < i_faq,
        f"T1-9  schema 检查早于 FAQ 装载（{i_schema} < {i_faq}）",
    )
    check(i_yield > i_faq > 0, "T1-10 两者都在 yield 之前（先装载再接请求）")

    # 🔴 `try:` 必须**包住启动段**，不能只包 `yield`。
    #    否则启动中途抛异常时 `finally` 不执行，已起的后台任务与连接池成为孤儿 ——
    #    `uvicorn --reload` 每次重启攒一批，直到打满 MySQL max_connections。
    check(
        0 <= code.find("try:") < i_schema < i_yield < i_finally,
        f"T1-11 try 包住整个启动段（try={code.find('try:')} < schema={i_schema} "
        f"< yield={i_yield} < finally={i_finally}）",
    )

    check(
        "await redis_client.close_client()" in code
        and "milvus_client.close_client()" in code,
        "T1-12 finally 里同时释放 Redis 与 Milvus 客户端",
    )

    # `/health` 必须**不查库**：把依赖探活塞进存活探针，会让「MySQL 抖一下」
    # 升级成「容器被重启」——重启治不好远端数据库，只会放大成重启循环。
    health_src = ""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == "health":
            health_src = ast.get_source_segment(src, node) or ""
    check(
        bool(health_src) and "assert_schema_ready" not in health_src,
        "T1-13 /health 是纯存活探针（不调 assert_schema_ready）",
    )

    ready_src = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == "ready":
            ready_src = ast.get_source_segment(src, node) or ""
    check("assert_schema_ready" in ready_src, "T1-14 /ready 复用了同一个检查函数")
    check("503" in ready_src, "T1-15 /ready 未就绪时回 503")

    paths = main.app.openapi()["paths"]
    # ⚠️ 用 openapi() 而不是数 app.routes —— Starlette 1.7 的 include_router
    #    把子路由包成 _IncludedRouter，数 app.routes 会漏报（CLAUDE.md 已知坑）。
    check("/health" in paths, "T1-16 /health 已注册")
    check("/ready" in paths, "T1-17 /ready 已注册")


def t1_compose() -> None:
    print("\n[T1] docker-compose 编排")

    services = _compose_services()
    check("migrate" in services, f"T1-18 migrate 服务存在（现有：{sorted(services)}）")

    migrate = services.get("migrate", "")
    check(
        "alembic" in migrate and "upgrade" in migrate and "head" in migrate,
        "T1-19 migrate 跑的是 alembic upgrade head",
    )
    # 跑完即退，且**不能**自动重启：`restart: on-failure` 会让
    # `service_completed_successfully` 永不满足，业务服务随之永远起不来。
    check(
        bool(re.search(r"restart:\s*[\"']?no[\"']?", migrate)),
        "T1-20 migrate 的 restart 是 no",
    )
    # 🔴 .env 里的 MYSQL_HOST 指的是**开发机的云端实例**。漏掉这行覆盖，
    #    迁移会跑去改云端那个库，而容器里的 api 连的是 compose 的 mysql ——
    #    迁移"成功"了，但业务库依然是空的。
    check(
        "MYSQL_HOST: mysql" in migrate,
        "T1-21 migrate 用 MYSQL_HOST: mysql 覆盖 .env（否则会去改云端库）",
    )
    check(
        "condition: service_healthy" in migrate and "mysql:" in migrate,
        "T1-22 migrate 等 mysql healthy",
    )
    # 迁移只依赖 MySQL。依赖 Redis/Milvus 会把「向量库没起来」变成
    # 「迁移没跑」，而这两件事毫无关系。
    check(
        "milvus" not in migrate and "redis" not in migrate,
        "T1-23 migrate 不依赖 redis / milvus",
    )

    for name in ("api", "worker", "beat"):
        blk = services.get(name, "")
        # 🔴 `service_completed_successfully` 而非 `service_started`：
        #    要的是「迁移成功」这个**结果**。写成 started 的话，迁移还在跑
        #    （或已经失败退出）服务就起来了，等于没加这道闸。
        check(
            "migrate:" in blk and "service_completed_successfully" in blk,
            f"T1-24 {name} 依赖 migrate 成功退出",
        )

    api = services.get("api", "")
    check("healthcheck" in api, "T1-25 api 配了 healthcheck")
    check("/ready" in api, "T1-26 api 的 healthcheck 打的是 /ready（不是 /health）")
    check(
        "start_period" in api,
        "T1-27 api 的 healthcheck 有 start_period（启动期不被误判为 unhealthy）",
    )

    nginx = services.get("nginx", "")
    check(
        bool(re.search(r"api:\s*\n\s+condition:\s*service_healthy", nginx)),
        "T1-28 nginx 等 api healthy（原来是列表形式，只保证启动顺序）",
    )


def t1_migrations() -> None:
    print("\n[T1] 迁移链与 0008")

    revisions: dict[str, str | None] = {}
    for path in sorted(VERSIONS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        rev = re.search(r'^revision:\s*str\s*=\s*"([^"]+)"', text, re.M)
        down = re.search(r'^down_revision:\s*str\s*\|\s*None\s*=\s*"([^"]*)"', text, re.M)
        if rev:
            revisions[rev.group(1)] = down.group(1) if down else None
            # 文件名前缀必须等于 revision —— 不相等时 `alembic history` 与
            # 目录列表对不上，而功能完全正常，属于「迟早误导人」的那种不一致。
            check(
                path.name.startswith(rev.group(1)),
                f"T1-29 {path.name} 的文件名前缀 == revision {rev.group(1)}",
            )

    referenced = {v for v in revisions.values() if v}
    heads = sorted(set(revisions) - referenced)
    check(len(heads) == 1, f"T1-30 迁移链只有一个 head（实际 {heads}）")
    check(heads == [EXPECTED_HEAD], f"T1-31 head 是 {EXPECTED_HEAD}（实际 {heads}）")
    check(
        revisions.get(EXPECTED_HEAD) == "0007",
        f"T1-32 {EXPECTED_HEAD} 接在 0007 之后（实际 {revisions.get(EXPECTED_HEAD)!r}）",
    )

    head_src = next(
        (p.read_text(encoding="utf-8") for p in VERSIONS.glob(f"{EXPECTED_HEAD}*.py")),
        "",
    )
    up_calls = _called_names(head_src, "upgrade")
    # 🔴 必须「存在才删」。MySQL 8 **没有** `DROP COLUMN IF EXISTS`（那是 MariaDB 的扩展），
    #    所以只能先问 inspector。写成无条件 drop_column 的话，对「新 0002 建出来的库」
    #    会抛 "Can't DROP 'created_at'"，而那条错发生在 `upgrade head` 中途，
    #    直接把整条启动链卡死。
    check(
        "_has_column" in up_calls
        and up_calls.index("_has_column") < up_calls.index("drop_column"),
        f"T1-33 {EXPECTED_HEAD} 的 upgrade 先判存在再 drop（调用序 {up_calls}）",
    )

    # 迁移的意图要与 ORM 对得上：ORM 里没有 created_at，所以才删它。
    # 反向断言（ORM 有这个字段）会立刻变红 —— 防的是「有人把列加回模型、
    # 迁移却还在删」这种两边打架的状态。
    cols = {c.name for c in KbDocumentPermission.__table__.columns}
    check(
        "created_at" not in cols,
        f"T1-34 ORM 的 kb_document_permission 无 created_at（实际列 {sorted(cols)}）",
    )
    # 独立性自查：**同一个表**用两条路径各取一次列名，必须一致。
    # 不一致说明 `Base.metadata` 与 ORM 类脱钩了（比如模块没被 import 全），
    # 那时 T1-34 会对着一个「不是这张表」的列集合作判断，绿得毫无意义。
    check(
        cols == set(Base.metadata.tables["kb_document_permission"].columns.keys()),
        "T1-35 （自查）ORM 类与 Base.metadata 取到同一张表的列集合",
    )


# ═══════════════════════════════════════════════════════════════
# T1.5 假连接：assert_schema_ready 的四个分支
# ═══════════════════════════════════════════════════════════════


class _FakeConn:
    """假连接：`run_sync` 依次吐出**预设**结果，不碰数据库。

    只用来验证判定逻辑。真正的 SQL 由 T2 证明。
    """

    def __init__(self, values: list[Any]) -> None:
        self._values = list(values)

    async def run_sync(self, _fn: Any) -> Any:
        return self._values.pop(0)


class _FakeEngine:
    def __init__(self, values: list[Any] | None = None, boom: bool = False) -> None:
        self._values = values or []
        self._boom = boom

    def connect(self) -> Any:
        if self._boom:
            raise OperationalError("SELECT 1", {}, Exception("simulated"))
        conn = _FakeConn(self._values)

        class _Ctx:
            async def __aenter__(self) -> _FakeConn:
                return conn

            async def __aexit__(self, *_a: Any) -> bool:
                return False

        return _Ctx()


def _fake_script_dir(heads: list[str]) -> Any:
    """替身 `_script_directory()`：只回答「仓库的 head 是什么」。"""
    return lambda: SimpleNamespace(get_heads=lambda: list(heads))


async def t15_schema_check_branches() -> None:
    print("\n[T1.5] assert_schema_ready 的分支（假连接）")

    real_engine, real_script = core_db.engine, core_db._script_directory
    real_db_name = settings.mysql_db
    try:
        # ① 目标库不是 rag_kb
        settings.mysql_db = "some_other_db"
        try:
            await core_db.assert_schema_ready()
            check(False, "T1.5-1 非 rag_kb 被拒（竟然没抛）")
        except SchemaStateError as exc:
            check(
                "rag_kb" in str(exc),
                f"T1.5-1 非 rag_kb 被拒且报出允许的库名（{exc}）",
            )
        finally:
            settings.mysql_db = real_db_name

        core_db._script_directory = _fake_script_dir([EXPECTED_HEAD])

        # ② 连不上。报错文案**不许含凭据** —— 启动日志会被贴进工单。
        core_db.engine = _FakeEngine(boom=True)
        try:
            await core_db.assert_schema_ready()
            check(False, "T1.5-2 连不上时报错（竟然没抛）")
        except SchemaStateError as exc:
            msg = str(exc)
            pwd = settings.mysql_password or ""
            check("不可达" in msg, f"T1.5-2 连接失败的文案可辨认（{msg}）")
            check(
                not pwd or pwd not in msg,
                "T1.5-3 连接失败文案里**不含密码**",
            )
            check(
                not any(seg in msg for seg in ("aiomysql", "mysql+", "@")),
                f"T1.5-4 连接失败文案里不含连接串形态（{msg}）",
            )

        # ③ 版本落后
        core_db.engine = _FakeEngine([{"0002"}, set(Base.metadata.tables)])
        try:
            await core_db.assert_schema_ready()
            check(False, "T1.5-5 版本落后时报错（竟然没抛）")
        except SchemaStateError as exc:
            msg = str(exc)
            check(
                "0002" in msg and EXPECTED_HEAD in msg,
                f"T1.5-5 文案里同时给出当前与期望版本（{msg}）",
            )
            check("upgrade head" in msg, "T1.5-6 文案里给出修复命令")

        # ④ 版本已是 head，但表缺失。
        #    🔴 这是 Alembic **自愈不了**的那一类：版本记录不会因为手工 DROP TABLE
        #       而回退，`upgrade head` 是 no-op。所以文案必须说清要人工介入，
        #       否则运维会照着「跑 upgrade head」做一遍、发现没用、然后怀疑代码。
        missing = set(Base.metadata.tables) - {"knowledge_gap"}
        core_db.engine = _FakeEngine([{EXPECTED_HEAD}, missing])
        try:
            await core_db.assert_schema_ready()
            check(False, "T1.5-7 版本对但缺表时报错（竟然没抛）")
        except SchemaStateError as exc:
            msg = str(exc)
            check("knowledge_gap" in msg, f"T1.5-8 文案点名缺失的表（{msg}）")
            check("不存在" in msg, "T1.5-9 文案说明是「表不存在」而非「版本落后」")

        # ⑤ 全部正常 → 不抛
        core_db.engine = _FakeEngine([{EXPECTED_HEAD}, set(Base.metadata.tables)])
        try:
            await core_db.assert_schema_ready()
            check(True, "T1.5-10 版本对 + 表齐 ⇒ 通过（无异常）")
        except SchemaStateError as exc:  # pragma: no cover
            check(False, f"T1.5-10 版本对 + 表齐 ⇒ 通过（却抛了 {exc}）")

        # ⑥ 空的 alembic_version（全新库、一条迁移都没跑）
        core_db.engine = _FakeEngine([set(), set()])
        try:
            await core_db.assert_schema_ready()
            check(False, "T1.5-11 空版本被判为未升级（竟然没抛）")
        except SchemaStateError as exc:
            check("(空)" in str(exc), f"T1.5-11 空版本给出可读文案（{exc}）")
    finally:
        core_db.engine, core_db._script_directory = real_engine, real_script
        settings.mysql_db = real_db_name


# ═══════════════════════════════════════════════════════════════
# T1.5 假连接：/ready 与启动/降级/清理
# ═══════════════════════════════════════════════════════════════


async def t15_ready_endpoint() -> None:
    print("\n[T1.5] /ready 的两种返回（替身检查函数）")

    real = main.assert_schema_ready

    async def _ok() -> None:
        return None

    async def _bad() -> None:
        raise SchemaStateError("模拟：schema 未就绪")

    transport = ASGITransport(app=main.app)
    try:
        main.assert_schema_ready = _bad
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get("/ready")
            check(resp.status_code == 503, f"T1.5-12 未就绪回 503（实际 {resp.status_code}）")
            body = resp.json()
            # 状态码之外还要看信封：503 却回 `code: 0` 的话，前端按 code 分支
            # 会把它当成功，然后把 `data.status` 当字符串显示 —— 两头都不报错。
            check(body.get("code") == 4001, f"T1.5-13 未就绪的信封 code=4001（{body.get('code')}）")
            check(
                body.get("data", {}).get("status") == "not_ready",
                f"T1.5-14 data.status=not_ready（{body.get('data')}）",
            )

            health = await client.get("/health")
            check(
                health.status_code == 200,
                f"T1.5-15 schema 未就绪时 /health **仍**回 200（存活与就绪分离，"
                f"{health.status_code}）",
            )

        main.assert_schema_ready = _ok
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.get("/ready")
            check(resp.status_code == 200, f"T1.5-16 就绪回 200（实际 {resp.status_code}）")
            check(resp.json().get("code") == 0, "T1.5-17 就绪的信封 code=0")
    finally:
        main.assert_schema_ready = real


async def t15_faq_degrade_and_cleanup() -> None:
    print("\n[T1.5] FAQ 降级 + 启动失败的资源清理")

    # ① FAQ 装载失败必须**降级**而不是阻断启动。
    #    `load_cache_from_db` 是真会因缺表/MySQL 抖动而抛的那一步；
    #    把它换成一个必定抛异常的替身，验证 startup() 仍然吞掉并起后台任务。
    real_loader = faq_service.load_cache_from_db

    async def _boom(_db: Any) -> int:
        raise RuntimeError("模拟：FAQ 表不存在")

    faq_service.load_cache_from_db = _boom  # type: ignore[assignment]
    try:
        raised: Exception | None = None
        try:
            await faq_service.startup()
        except Exception as exc:  # noqa: BLE001
            raised = exc
        check(raised is None, f"T1.5-18 FAQ 装载失败不阻断启动（抛出的是 {raised!r}）")
        check(
            len(faq_service._tasks) > 0,
            f"T1.5-19 降级后后台任务仍然起了（{len(faq_service._tasks)} 个）",
        )
        await faq_service.stop_background_tasks()
        check(not faq_service._tasks, "T1.5-20 stop_background_tasks 清空了任务表")
    finally:
        faq_service.load_cache_from_db = real_loader  # type: ignore[assignment]

    # ② schema 检查失败时，清理段**仍然要跑**。
    #    这是 T1-11 那条静态断言的动态对照：静态只能证明「try 的位置对」，
    #    证明不了「finally 真的执行」。没有这条，孤儿后台任务/连接池
    #    要等到 `uvicorn --reload` 重启几百次、打满 max_connections 才暴露。
    real_check = main.assert_schema_ready
    real_stop = faq_service.stop_background_tasks
    stop_calls: list[str] = []

    async def _bad() -> None:
        raise SchemaStateError("模拟：启动时 schema 未就绪")

    async def _spy_stop() -> None:
        stop_calls.append("stop")
        await real_stop()

    main.assert_schema_ready = _bad
    faq_service.stop_background_tasks = _spy_stop  # type: ignore[assignment]
    try:
        raised = None
        try:
            async with main.lifespan(main.app):  # type: ignore[arg-type]
                check(False, "T1.5-21 schema 未就绪时 lifespan 应抛出（竟然进去了）")
        except SchemaStateError:
            raised = "SchemaStateError"
        check(raised == "SchemaStateError", "T1.5-21 schema 未就绪时启动失败（不是静默继续）")
        check(stop_calls == ["stop"], f"T1.5-22 启动失败时清理段仍执行（{stop_calls}）")
    finally:
        main.assert_schema_ready = real_check
        faq_service.stop_background_tasks = real_stop  # type: ignore[assignment]


# ═══════════════════════════════════════════════════════════════
# T2 真实库
# ═══════════════════════════════════════════════════════════════


async def t2_real_db() -> bool:
    """返回 T2 是否真的跑过。"""
    print("\n[T2] 真实数据库（只读）")

    expected_tables = set(Base.metadata.tables)

    try:
        async with engine.connect() as conn:
            heads = await conn.run_sync(
                lambda c: set(MigrationContext.configure(c).get_current_heads())
            )
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
            gap_cols = await conn.run_sync(
                lambda c: {col["name"] for col in inspect(c).get_columns("kb_import_task")}
            )
            perm_cols = await conn.run_sync(
                lambda c: {
                    col["name"] for col in inspect(c).get_columns("kb_document_permission")
                }
            )
    except Exception as exc:  # noqa: BLE001 —— 连不上就是 SKIP，不是 FAIL
        skip(
            "T2-1…T2-6 真实库校验",
            f"数据库不可达或被目标库闸门拒绝（{type(exc).__name__}）",
        )
        return False

    check(heads == {EXPECTED_HEAD}, f"T2-1  库版本 == {EXPECTED_HEAD}（实际 {sorted(heads)}）")

    missing = sorted(expected_tables - tables)
    # 🔴 这条就是本次故障的直接断言。`Base.metadata` 是「代码认为该有哪些表」，
    #    `information_schema` 是「库里真有哪张表」—— 两者相减非空，
    #    就是「启动报错、MySQL 缺表」那个状态。
    check(not missing, f"T2-2  模型注册的 {len(expected_tables)} 张表全部存在（缺 {missing}）")

    # 0007 用 add_column 补的列：漏了它，be-09 的「一键转建」写任务时会 500
    # （`kb_import_task.gap_id` 不存在），而缺口清单看起来完全正常。
    check("gap_id" in gap_cols, f"T2-3  kb_import_task.gap_id 存在（实际列 {sorted(gap_cols)}）")

    # 0008 删掉的列。它还在的话，说明库是「老 0002」建的而 0008 没生效。
    check(
        "created_at" not in perm_cols,
        f"T2-4  kb_document_permission.created_at 已删（实际列 {sorted(perm_cols)}）",
    )

    # 上面四条是自己查；这条是**跑真正的启动检查**，证明修复对启动路径有效。
    try:
        await core_db.assert_schema_ready()
        check(True, "T2-5  assert_schema_ready() 对真实库不抛（启动可通过）")
    except SchemaStateError as exc:
        check(False, f"T2-5  assert_schema_ready() 对真实库不抛（却抛了 {exc}）")

    # 真实 /ready 也打一次：这是编排系统 healthcheck 走的那条路。
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/ready")
        check(resp.status_code == 200, f"T2-6  真实 /ready 回 200（实际 {resp.status_code}）")
        resp_h = await client.get("/health")
        check(resp_h.status_code == 200, "T2-7  真实 /health 回 200")

    return True


# ═══════════════════════════════════════════════════════════════


async def run() -> tuple[bool, bool]:
    """返回 `(全部通过, T2 是否真的跑过)`。

    ⚠️ 所有层在**同一个事件循环**里跑 —— 这正是 `core/db.py` 的模块级 engine
       能被复用的前提。各自塞进一个 `asyncio.run()` 会让后一层复用到上一循环
       遗留的连接，报 `'NoneType' object has no attribute 'send'`（CLAUDE.md 已知坑）。

    ⚠️ **顺序有讲究**：T1.5-21/22 里那次 lifespan 会 `engine.dispose()`，
       所以它必须排在 T2 真实库校验**之后**，否则 T2 要多付一次重连。
       （dispose 只关池子、不废 engine，重连是懒的，出不了错 —— 但没必要。）
    """
    t1_target_db_guard()
    t1_read_only_schema_check()
    t1_main_wiring()
    t1_compose()
    t1_migrations()
    await t15_schema_check_branches()
    await t15_ready_endpoint()
    ran = await t2_real_db()
    await t15_faq_degrade_and_cleanup()
    await engine.dispose()
    return all(_results), ran


def main_() -> None:
    ok_, ran = asyncio.run(run())
    passed, total = sum(_results), len(_results)
    print(f"\n{'=' * 56}")
    print(f"T1/T1.5/T2：{passed}/{total} 通过")
    print(f"SKIP：{len(_skipped)} 项")
    if not ran:
        print("⚠️ T2 未执行（MySQL 不可达，或目标库闸门拒绝了本次连接）")
        print("   —— **本次结果不构成完整验收**。")
    if passed != total:
        print("存在失败项，见上方 FAIL 行。")
    sys.exit(0 if (ok_ and ran) else 1)


if __name__ == "__main__":
    main_()
