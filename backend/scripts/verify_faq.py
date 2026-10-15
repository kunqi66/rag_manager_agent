"""be-08 FAQ 挖掘的验收脚本（spec `P3-14-be-faq-mining` §8 的可执行版本）。

跑法（项目根目录）：

    uv run python -m backend.scripts.verify_faq

脚本分四层，**能跑的就跑，跑不了的明确报 SKIP 并说明原因** ——
绝不把「环境不具备」伪装成「通过」（那是验收脚本最坏的失败模式）。

| 层 | 需要什么 | 覆盖 |
|---|---|---|
| **T1 离线** | 什么都不需要 | 相似度/聚类/代表元、BLOB 契约（字节序、长度、下标重映射）、置信度公式、`FaqCache` 增删改、DTO ↔ 前端类型逐字段对齐、迁移与模型一致、红线 1/4/7 静态断言、配置与 beat 排班 |
| **T1.5 假会话** | 什么都不需要 | 用**脚本化假 `AsyncSession`** 驱动 `faq_mining`：一轮挖掘产出候选、**幂等**（同批数据跑两次不新增）、频次不足被滤、窗口反向报 1001、审核状态机（重复审核 → 3002）、chunk→document 反查 |
| **T2 接口** | MySQL + **Redis** | 8 端点注册、登录门禁、权限码 403、分页结构、`/faq/mine` 真跑 |
| **T3 链路** | ＋ Milvus ＋ BGE ＋ DeepSeek ＋ worker | A7 全链路（提问 → 挖掘 → 审核发布 → 再提问 **token=0 命中**） |

**本机跑不了 T2/T3**：云端 MySQL 不可达（本次实测 `Can't connect to MySQL
server on 60.205.138.167`）、本机没有 Docker（Milvus 单机版依赖它）。
每一项都标注了它依赖什么，供在 ECS 上跑时对照。

⚠️ **T1.5 为什么要用假会话**：`faq_mining` 里最容易错的三处 ——
   幂等（去重池）、簇内下标到 `qa_log` 行的映射、chunk→document 反查 ——
   都不需要真实数据库就能验证。等到 T2 才测的话，本机（MySQL 不可达时）
   这三条就永远测不到，而它们恰好都是**不报错的静默失效**。

⚠️ Windows 控制台是 GBK —— 输出一律 **ASCII**，断言**布尔值与具体值**，
不靠肉眼读中文。这与 `verify_org.py` / `verify_import.py` / `verify_search.py`
同一约定。

⚠️ T2 会真的连 `.env` 里的 MySQL（开发期直连 ECS），只增删自己造的数据
（提问文本前缀 `ZZVFYFAQ`）。**绝不动种子数据**，也**不碰 `rag_kb` 以外的库**。
"""

from __future__ import annotations

import asyncio
import ast
import re
import struct
import sys
from pathlib import Path

import numpy as np
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect as sa_inspect

import main
from backend.core.db import engine
from backend.core.exceptions import ConflictError, NotFoundError, ParamError
from backend.core.settings import settings
from backend.engines.data_mining_engine import faq_mining
from backend.engines.data_mining_engine import similarity as sim
from backend.faq.faq_cache import FaqCache
from backend.models import Faq, FaqCandidate
from backend.models.qa_log import pack_embedding, unpack_embedding
from backend.schemas import mining as mining_schemas
from backend.schemas.auth import AuthContext

PASSWORD = "Rag@12345"
#: 自造数据的标记前缀。清理一律靠它，不靠「记住 id」。
#: 🔴 **不含下划线** —— `LIKE 'zzvfy_%'` 里的 `_` 是通配符（CLAUDE.md 已知坑第一条）。
MARK = "ZZVFYFAQ"

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


def _blank(source: str) -> str:
    """去掉 docstring 与注释，只留代码本体。

    🔴 本仓库的 docstring **刻意写着**「原先有 `user_context`，已删除」这类说明。
       直接对源码做子串检查，会把这段解释本身当成「没删干净」，
       于是安全用例永远红着 —— 而永远红着的用例最终会被人注释掉，那才是真损失。
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
    # 行注释也去掉：`# 不是 user_context` 这类说明同样会造成假阳性
    return "\n".join(re.sub(r"#.*$", "", line) for line in text.splitlines())


# ═══════════════════════════════════════════════════════════════
# 假会话：不需要数据库就能驱动 faq_mining
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
       测的是假实现而不是真代码。这里只覆盖
       「构造了什么 SQL」+「按什么顺序返回什么」+「add 进去的是谁」。

    为什么需要「按顺序」而不是「返回同一批」：`faq_mining` 一轮里会查
    4 类不同的东西（窗口日志 / 候选去重池 / FAQ 去重池 / chunk→document），
    返回同一批会让「反查到底查了哪张表」这件事无法验证。
    """

    def __init__(
        self,
        *,
        scalars_seq: list[list] | None = None,
        execute_seq: list[list] | None = None,
        get_map: dict | None = None,
    ) -> None:
        self._scalars = list(scalars_seq or [])
        self._execute = list(execute_seq or [])
        self._get_map = get_map or {}
        self.statements: list = []
        self.added: list = []
        self.commits = 0

    async def scalars(self, stmt):
        self.statements.append(stmt)
        return _FakeResult(self._scalars.pop(0) if self._scalars else [])

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _FakeResult(self._execute.pop(0) if self._execute else [])

    async def scalar(self, stmt):
        self.statements.append(stmt)
        return len(self._scalars[0]) if self._scalars else 0

    async def get(self, model, key):
        self.statements.append(("get", model, key))
        return self._get_map.get(key)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj) -> None:
        return None


class _Obj:
    """通用假 ORM 对象。"""

    def __init__(self, **kw) -> None:
        for key, value in kw.items():
            setattr(self, key, value)


def _vec(seed: int, dim: int = sim.EMBED_DIM) -> list[float]:
    """确定性伪随机单位向量。用 `default_rng` 而不是 `np.random.rand`：
    后者受全局状态影响，同一份脚本两次运行会得到不同数据。"""
    rng = np.random.default_rng(seed)
    raw = rng.random(dim).astype(np.float32)
    return (raw / np.linalg.norm(raw)).tolist()


def _log(log_id: str, question: str, vector: list[float], allowed=None) -> _Obj:
    return _Obj(
        id=log_id,
        question=question,
        question_embedding=pack_embedding(vector),
        allowed_ids=allowed or [],
    )


# ═══════════════════════════════════════════════════════════════
# T1 离线
# ═══════════════════════════════════════════════════════════════


def t1_similarity() -> None:
    print("\n== T1 相似度与聚类 ==")

    # --- L2 归一化 ---
    matrix = np.array([[3.0, 4.0], [0.0, 0.0]], dtype=np.float32)
    normalized = sim.l2_normalize(matrix)
    check(abs(float(np.linalg.norm(normalized[0])) - 1.0) < 1e-6, "l2_normalize: 非零行范数为 1")
    check(
        not np.isnan(normalized[1]).any() and float(np.linalg.norm(normalized[1])) == 0.0,
        "l2_normalize: 零向量保持为 0 而不是 nan",
    )

    # --- 聚类：合成向量，可控 ---
    base = _vec(1)
    near = _vec(2)
    # 造两条「和 base 很像」的向量：base*0.99 + near*0.01 再归一化
    v = np.array(base, dtype=np.float32)
    n = np.array(near, dtype=np.float32)
    twin1 = sim.l2_normalize((v * 0.995 + n * 0.005).reshape(1, -1))[0]
    twin2 = sim.l2_normalize((v * 0.99 + n * 0.01).reshape(1, -1))[0]
    far = _vec(99)
    vectors = sim.l2_normalize(np.vstack([v, twin1, twin2, np.array(far, dtype=np.float32)]))

    clusters = sim.build_clusters(vectors, 0.9)
    sizes = sorted(len(c) for c in clusters)
    check(sizes == [1, 3], f"build_clusters: 3 条相近 + 1 条远离 → 簇大小 {sizes}")

    # --- 顺序不敏感（并查集的核心卖点）---
    order = [3, 0, 2, 1]
    shuffled = vectors[order]
    shuffled_clusters = sim.build_clusters(shuffled, 0.9)
    # 把下标映射回原顺序再比较集合
    remapped = sorted(
        sorted(order[i] for i in members) for members in shuffled_clusters
    )
    check(
        remapped == sorted(sorted(c) for c in clusters),
        "build_clusters: 打乱输入顺序得到同一组簇（顺序不敏感）",
    )

    # --- 阈值边界 ---
    check(len(sim.build_clusters(vectors, 1.1)) == 4, "build_clusters: 阈值 > 1 时每个点自成一簇")
    check(sim.build_clusters(np.empty((0, sim.EMBED_DIM), np.float32), 0.5) == [], "build_clusters: 空输入返回 []")

    # --- 代表元：距质心最近，且可复现 ---
    # ⚠️ 用一组**不对称**的向量。原先我拿上面那个「base + 两个极近扰动」的簇来测，
    #    三个成员与质心的夹角几乎相同，argmax 由浮点噪声决定 ——
    #    那样的用例会时红时绿，比没有用例更糟（红久了就会被人注释掉）。
    trio = sim.l2_normalize(
        np.array([[1.0, 0.0, 0.0], [0.9, 0.2, 0.0], [0.9, -0.2, 0.0]], dtype=np.float32)
    )
    first = sim.representative_index(trio, [0, 1, 2])
    check(
        first == sim.representative_index(trio, [0, 1, 2]),
        "representative_index: 同一输入两次调用结果一致",
    )
    check(first == 0, f"representative_index: 取距质心最近那条（实测下标 {first}）")
    check(
        sim.representative_index(trio, [1, 2]) in (1, 2),
        "representative_index: 返回值必是簇内成员",
    )

    # --- nearest_existing ---
    empty_pool = np.empty((0, sim.EMBED_DIM), np.float32)
    probe = np.array(_vec(1234), np.float32)
    check(
        sim.nearest_existing(probe, empty_pool, 0.9) is None,
        "nearest_existing: 空池返回 None 而不是抛异常",
    )
    # 🔴 查询向量**不能取自池内**。我第一版直接拿池里的 `far` 当查询，
    #    它当然和自己相似度 1.0 —— 于是「低于阈值应返回 None」这条永远红。
    #    这类用例错的不是代码，是**用例本身构造得没有区分度**。
    sims = vectors @ probe
    expected_best = int(np.argmax(sims))
    hit = sim.nearest_existing(probe, vectors, 0.0)
    check(
        hit is not None and hit[0] == expected_best,
        f"nearest_existing: 返回池内最相似那一行的下标（实测 {hit[0] if hit else None} / 期望 {expected_best}）",
    )
    check(
        hit is not None and abs(hit[1] - float(sims[expected_best])) < 1e-6,
        "nearest_existing: 一并返回该行的相似度（调用方要拿它做「弱命中」判断）",
    )
    check(
        sim.nearest_existing(probe, vectors, 0.9999) is None,
        "nearest_existing: 最相似者低于阈值时返回 None",
    )
    check(
        sim.nearest_existing(np.array(base, np.float32), vectors, 0.99) is not None,
        "nearest_existing: 池内自命中（相似度 1.0）不低于任何合法阈值",
    )


def t1_blob_contract() -> None:
    print("\n== T1 BLOB 二进制契约（字典 §2.4.1）==")

    vector = _vec(7)
    blob = pack_embedding(vector)
    check(blob is not None and len(blob) == 3072, "pack_embedding: 768 维 → 恰好 3072 字节")
    check(
        len(struct.pack("<768f", *vector)) == len(blob),
        "pack_embedding: 与 struct '<f' 小端口径一致",
    )
    restored = unpack_embedding(blob)
    check(
        len(restored) == 768 and max(abs(a - b) for a, b in zip(restored, vector)) < 1e-6,
        "pack/unpack: 往返无损",
    )

    # 🔴 字节序写反**不会报错**，只会得到噪声向量（相似度全部失真）。
    #    这条断言把「为什么必须统一口径」变成可执行的证据。
    big_endian = struct.unpack(">768f", blob)
    check(
        max(abs(a - b) for a, b in zip(big_endian, vector)) > 1.0,
        "字节序反了会得到完全不同的数值（证明契约必须只有一份实现）",
    )

    # --- from_blob / build_matrix 的长度校验与下标重映射 ---
    check(sim.from_blob(None) is None, "from_blob: None → None")
    check(sim.from_blob(b"\x00" * 3071) is None, "from_blob: 长度差 1 字节 → None")
    check(sim.from_blob(blob) is not None, "from_blob: 合规长度 → ndarray")

    good_a = pack_embedding(_vec(11))
    good_b = pack_embedding(_vec(12))
    matrix, kept = sim.build_matrix([None, good_a, b"\x00" * 100, good_b])
    check(kept == [1, 3], f"build_matrix: 脏行被跳过且返回有效行下标 {kept}")
    check(matrix.shape == (2, sim.EMBED_DIM), "build_matrix: 矩阵行数 = 有效行数")
    check(
        sim.build_matrix([])[0].shape == (0, sim.EMBED_DIM),
        "build_matrix: 空输入返回 (0, 768) 空矩阵（调用方无需特判）",
    )


def t1_confidence_and_key() -> None:
    print("\n== T1 置信度与簇标识 ==")

    check(
        sim.EMBED_DIM == 768,
        "EMBED_DIM 与 bge-base-zh-v1.5 的 768 维一致（BLOB 长度校验的依据）",
    )
    # freq 达到参考点后封顶
    ref = settings.faq_confidence_freq_ref
    low = faq_mining._confidence(0.9, 1)
    high = faq_mining._confidence(0.9, ref)
    over = faq_mining._confidence(0.9, ref * 100)
    check(abs(low - 0.9 / ref) < 1e-9, f"confidence: 频次 1 → avg_sim/{ref}")
    check(abs(high - 0.9) < 1e-9, "confidence: 频次到参考点 → 等于 avg_sim")
    check(abs(over - 0.9) < 1e-9, "confidence: 频次超过参考点后**封顶**（不会被高频寒暄句盖过）")
    check(0.0 <= faq_mining._confidence(1.0, 10**6) <= 1.0, "confidence: 恒在 [0,1]")

    logs = [
        _Obj(id="log_b", question="q", question_embedding=None, allowed_ids=[]),
        _Obj(id="log_a", question="q", question_embedding=None, allowed_ids=[]),
    ]
    key1 = faq_mining._cluster_key(logs, [0, 1])
    key2 = faq_mining._cluster_key(logs, [1, 0])
    check(key1 == key2, "cluster_key: 与簇内成员顺序无关")
    check(len(key1) == 16, "cluster_key: 长度 16（md5 前 16 位）")
    check(
        faq_mining._cluster_key(logs, [0]) != key1,
        "cluster_key: 成员不同则键不同",
    )


def t1_faq_cache() -> None:
    print("\n== T1 进程内索引（FaqCache）==")

    cache = FaqCache()
    check(cache.match(np.array(_vec(1), np.float32)) is None, "match: 空索引返回 None 而不是抛异常")

    v1, v2, v3 = _vec(21), _vec(22), _vec(23)
    cache.load_all(
        [
            ("faq_1", "问题一", "答案一", pack_embedding(v1)),
            ("faq_2", "问题二", "答案二", pack_embedding(v2)),
            ("faq_bad", "坏行", "坏答案", b"\x00" * 10),
        ]
    )
    check(cache.size() == 2, "load_all: 长度不符的行被跳过（2/3）")

    hit = cache.match(np.array(v1, np.float32))
    check(hit is not None and hit.faq_id == "faq_1", "match: 命中同向量那条")
    check(hit is not None and hit.answer == "答案一", "match: 答案取自同行（矩阵行与 id 表不错位）")

    # 🔴 match 自己归一化：传**未归一化**的向量也必须命中同样的条目。
    #    漏了这一步会让相似度变成未归一化的点积（数值偏大）⇒ 大量误命中。
    scaled = (np.array(v1, np.float32) * 37.0).tolist()
    hit_scaled = cache.match(np.array(scaled, np.float32))
    check(
        hit_scaled is not None and hit_scaled.faq_id == "faq_1",
        "match: 入参未归一化时**自己归一化**（否则会静默误命中）",
    )

    # 低于阈值必须不命中
    check(
        cache.match(np.array(_vec(199), np.float32)) is None,
        "match: 相似度低于 FAQ_MATCH_THRESHOLD 时返回 None",
    )

    # --- upsert 原地覆盖 ---
    cache.upsert("faq_1", "问题一改", "答案一改", pack_embedding(v1))
    check(cache.size() == 2, "upsert: 已存在的 id 原地覆盖，不新增行")
    hit2 = cache.match(np.array(v1, np.float32))
    check(hit2 is not None and hit2.answer == "答案一改", "upsert: 覆盖后命中的是新答案")

    cache.upsert("faq_3", "问题三", "答案三", pack_embedding(v3))
    check(cache.size() == 3, "upsert: 新 id 追加一行")

    # 🔴 remove 必须**重建行号表**：只 pop 掉键的话其余键仍指着旧行号，
    #    删掉中间一行后，下一次 upsert 会写到**别人**那一行或长出重复行
    #    （症状：改了 A 的缓存开关，B 的答案变了 —— 两边都不报错）。
    #
    #    ⚠️ 用例要**有区分度**：删的必须是中间行、随后 upsert 的必须是**行号在它之后**的那条。
    #       删最后一行、或者接着改被删的同一个 id，两种写法在有 bug 的实现上也照样绿。
    cache.remove("faq_2")
    check(cache.size() == 2, "remove: 条数减一")
    # 白盒不变式：行号表的值必须**恰好是** 0..n-1 的一个排列。
    # 「只 pop 键、不重建表」会让 values 出现空洞或重复，而读侧仍然全部正常 ——
    # 这是本条 trap 唯一能在**写之前**就发现的信号。
    check(
        sorted(cache._index.values()) == list(range(cache.size())),
        f"remove 后不变式: _index 的值恰是 0..{cache.size() - 1} 的排列（实测 {sorted(cache._index.values())}）",
    )
    # ⚠️ 这一段包 try/except：行号表一旦失效，`upsert` 会直接抛 IndexError。
    #    不裹的话，脚本会在**这一条**炸掉、后面所有用例一条都不跑 ——
    #    回归发生时看到的是 traceback 而不是「哪几条红了」。
    try:
        cache.upsert("faq_3", "问题三改", "答案三改", pack_embedding(v3))
        size_after = cache.size()
        hit3 = cache.match(np.array(v3, np.float32))
        hit1 = cache.match(np.array(v1, np.float32))
    except Exception as exc:  # noqa: BLE001 —— 见上：要报成 FAIL 而不是崩掉
        check(False, f"remove 后 upsert 抛 {type(exc).__name__} —— 行号表已失效（{exc}）")
    else:
        check(size_after == 2, f"remove 后 upsert: 没长出重复行（实测 {size_after}，应为 2）")
        check(hit3 is not None and hit3.answer == "答案三改", "remove 后 upsert: 改的是自己那一行")
        check(hit1 is not None and hit1.answer == "答案一改", "remove 后其它行未被串改")
    cache.remove("faq_nope")
    check(cache.size() == 2, "remove: 不存在的 id 是安全的空操作")


def _ts_interfaces(source: str) -> dict[str, list[str]]:
    """从 `frontend/src/types/governance.ts` 抽出 `interface X { ... }` 的字段名。

    🔴 **前端类型文件是真正的契约**（be-07 的教训）：字段名不一致不会编译报错，
       只会让前端 `data.xxx` 取到 `undefined`，指标卡显示 `NaN`，
       而**接口照回 200**。所以这条对账必须是自动化的，不能靠人眼比对。
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


def t1_contract_alignment() -> None:
    print("\n== T1 DTO ↔ 前端类型逐字段对齐（前端是真正的契约）==")

    ts_path = ROOT / "frontend" / "src" / "types" / "governance.ts"
    if not ts_path.exists():
        skip("DTO ↔ 前端类型对齐", "找不到 frontend/src/types/governance.ts")
        return
    ts = _ts_interfaces(ts_path.read_text(encoding="utf-8"))

    # (后端 DTO, 前端 interface)。**名字可以不同，字段必须逐字相同。**
    pairs = [
        (mining_schemas.FaqCandidateItemDTO, "FaqCandidateItem"),
        (mining_schemas.FaqItemDTO, "FaqItem"),
        (mining_schemas.RelatedDocBrief, "RelatedDocBrief"),
        (mining_schemas.FaqReviewPayload, "FaqReviewPayload"),
        (mining_schemas.CandidateQuery, "CandidateQuery"),
        (mining_schemas.FaqQuery, "FaqQuery"),
        (mining_schemas.CandidateReviewDTO, "CandidateReviewResult"),
        (mining_schemas.FaqCacheDTO, "FaqCacheResult"),
        (mining_schemas.MiningOverviewDTO, "MiningOverviewVo"),
    ]
    for dto, ts_name in pairs:
        if ts_name not in ts:
            check(False, f"{ts_name}: 前端类型里找不到该 interface")
            continue
        backend_fields = set(dto.model_fields)
        frontend_fields = set(ts[ts_name])
        missing = frontend_fields - backend_fields
        # 后端多出来的字段是允许的（前端可以不用），但前端要的**必须**都有 ——
        # 少一个就是 `undefined`，而接口照回 200。
        check(
            not missing,
            f"{dto.__name__} ⊇ {ts_name}（前端要而后端缺的：{sorted(missing) or '无'}）",
        )

    # 前端**刻意不定义** question_embedding —— 后端也不该把它放进 DTO
    check(
        "question_embedding" not in mining_schemas.FaqCandidateItemDTO.model_fields,
        "FaqCandidateItemDTO: 不含 question_embedding（与前端契约一致）",
    )
    check(
        "question_embedding" not in mining_schemas.FaqItemDTO.model_fields,
        "FaqItemDTO: 不含 question_embedding",
    )


def t1_redlines() -> None:
    print("\n== T1 安全红线静态断言 ==")

    router_src = (ROOT / "backend" / "api" / "data_mining_router.py").read_text(encoding="utf-8")
    router_code = _blank(router_src)

    # --- 红线 1：身份只从 JWT ---
    check("user_context" not in router_code, "红线 1: 路由里不存在 user_context（PRD §11.3 R3）")
    # 🔴 断言要落在**入参**类型上，不能对 schemas 文件做整篇子串搜索：
    #    `published_by` 是 `FaqItemDTO` 的**输出**字段（告诉前端是谁发布的），
    #    它是正当的。整篇搜会把这条对的写法判成违规 —— 而「永远红的用例」
    #    最终会被人注释掉，那才是真损失。
    identity_names = {
        "reviewer_id", "user_id", "published_by", "operator_id", "user_context", "ctx",
    }
    for payload in (
        mining_schemas.FaqReviewPayload,
        mining_schemas.CandidateQuery,
        mining_schemas.FaqQuery,
    ):
        overlap = identity_names & set(payload.model_fields)
        check(
            not overlap,
            f"红线 1: {payload.__name__} 无身份入参字段（实测多出 {sorted(overlap) or '无'}）",
        )
    check(
        "published_by" in mining_schemas.FaqItemDTO.model_fields,
        "红线 1: published_by 只出现在**输出** DTO（FaqItemDTO）上",
    )
    check(
        mining_schemas.FaqReviewPayload.model_config.get("extra") == "forbid",
        "红线 1: FaqReviewPayload 是 extra=forbid（硬塞身份字段会 422 而不是被静默忽略）",
    )

    # --- 红线 7：每个端点独立挂权限码 ---
    # 🔴 **按端点切块**再找码，不做全文件的正则汇总。
    #    本 router（`data_mining_router.py`）由**两个模块共用**：be-08 的 6 个
    #    `faq/*` 端点在先，be-09 的 2 个 `gaps` 端点在后，各自挂各自域的码。
    #    全文件汇总会把两个域混成一个集合，于是
    #      ① 「N 个码 == N 个 faq 端点」对不上（8 个码 / 6 个端点）；
    #      ② 「码全在 faq: 域内」被 `gap:` 码判红。
    #    两条都红得理直气壮，**但错的是用例**：它把「多模块共用文件」当成了
    #    「单模块文件」。修法不是删断言，是让断言的**作用域**与它想守的东西一致。
    blocks = [
        b for b in re.split(r"\n(?=@router\.)", router_code) if b.startswith("@router.")
    ]
    per_endpoint: list[tuple[str, str, str | None]] = []
    for block in blocks:
        head = re.match(r'@router\.(get|post|put|delete)\(\s*"([^"]+)"', block)
        if head is None:
            continue
        code_match = re.search(r'require_permission\("([^"]+)"\)', block)
        per_endpoint.append(
            (head.group(1), head.group(2), code_match.group(1) if code_match else None)
        )
    # ⚠️ 这条「扫描真的扫到了东西」的断言不能省：否则切块的正则一旦失效，
    #    下面所有 `all(...)` 都在**空集**上通过，「没有违规」与「扫描器坏了」
    #    会是同一个绿灯。
    check(
        len(per_endpoint) == 8,
        f"红线 7: 扫描到 8 个端点（be-08 六个 faq + be-09 两个 gaps，实测 {len(per_endpoint)}）",
    )
    faq_eps = [(m, p, c) for m, p, c in per_endpoint if "/gaps" not in p]
    gap_eps = [(m, p, c) for m, p, c in per_endpoint if "/gaps" in p]
    check(
        len(faq_eps) == 6 and len(gap_eps) == 2,
        f"红线 7: faq 域 6 个 / gap 域 2 个（实测 {len(faq_eps)} / {len(gap_eps)}）",
    )
    uncoded = [p for _, p, c in faq_eps + gap_eps if c is None]
    check(
        not uncoded,
        f"红线 7: 每个端点各挂一个权限码（实测裸奔的：{uncoded}）",
    )
    # 域内一致：每个端点的码必须落在**它自己那个域**里 ——
    # 混域会让「谁能用治理中心」这件事分裂成两处授权决定。
    misplaced = [
        (p, c) for _, p, c in faq_eps if c and not c.startswith("faq:")
    ] + [(p, c) for _, p, c in gap_eps if c and not c.startswith("gap:")]
    check(
        not misplaced,
        f"权限码: faq 端点用 faq: 码、gap 端点用 gap: 码（实测错位：{misplaced}）",
    )
    # --- 「页面门不禁摘要」---
    # 🔴 这条不照实现抄，而是从**另一个来源**推要求：
    #    ① `faq:candidate:list` 是治理中心的**菜单码**（`seed_org.py` 的注释写明
    #       `path=/kb/governance`），而侧栏**不按权限过滤菜单** ⇒ 能打开这个页面的人
    #       就会发这个请求；
    #    ② 所以「有这个菜单码的人」必须也有**该页面会发的那些端点**的码，
    #       否则页面能打开、摘要块 403 —— 用户看到的是半坏不坏的页面。
    #    ⚠️ 只取 faq 域的码：缺口清单是另一个页面（另一个 Tab），
    #       它的码不属于「打开治理中心就会发出去」的集合。
    #    ⚠️ 今天两个角色（sys_admin / kb_admin）恰好都有，所以**现状看不出差别**；
    #       这条断言的价值是**在将来拆分角色时拦住它**，而不是描述现状。
    id_to_code, roles = _seeded_roles()
    menu_code = "faq:candidate:list"
    if not id_to_code or not roles:
        skip("权限码: 菜单码 ⊇ 本模块端点码", "解析不出 seed_org.py 的权限/角色结构")
    else:
        router_codes_all = {c for _, _, c in faq_eps if c}
        drifted = []
        for role_id, perm_ids in roles.items():
            granted = {id_to_code.get(pid, pid) for pid in perm_ids}
            if menu_code in granted and not router_codes_all <= granted:
                drifted.append((role_id, sorted(router_codes_all - granted)))
        check(
            not drifted,
            f"权限码: 有治理中心菜单码的角色，也在本页面端点码上（缺 {drifted or '无'}）",
        )
        check(
            "faq:candidate:list" in _seeded_permission_codes(),
            "权限码: faq:candidate:list 已进 sys_permission 种子（前端菜单直接依赖它）",
        )

    # 写端点必须**各自**挂码，不能共用读端点的码。
    # ⚠️ 按 `@router.` 切块再整块找码。原先我写的是
    #    `@router\.(post|put)\((.*?)async def` —— 它只截到**装饰器文本**为止，
    #    而 `require_permission` 在**函数签名**里（`async def` 之后），
    #    于是三条全判失败：用例的切法错了，代码是对的。
    write_blocks = [
        block
        for block in re.split(r"\n(?=@router\.)", router_code)
        if re.match(r"@router\.(?:post|put)\(", block) and "/gaps" not in block
    ]
    check(len(write_blocks) == 3, f"红线 7: be-08 有 3 个写端点（实测 {len(write_blocks)}）")
    write_codes = [
        match.group(1)
        for block in write_blocks
        if (match := re.search(r'require_permission\("([^"]+)"\)', block))
    ]
    check(
        len(write_codes) == len(write_blocks),
        f"红线 7: 每个写端点都显式挂了权限码（{len(write_codes)}/{len(write_blocks)}）",
    )
    check(
        len(set(write_codes)) == len(write_codes),
        f"红线 7: 三个写端点用的是**各自**的权限码，没有共用（实测 {write_codes}）",
    )
    check(
        {"faq:candidate:mine", "faq:candidate:publish", "faq:cache:update"} <= set(write_codes),
        f"红线 7: 挖掘/审核/缓存三个动作各有专属码（实测 {write_codes}）",
    )

    # --- 红线 4：关联文档标题的权限回填 ---
    mining_src = (ROOT / "backend" / "engines" / "data_mining_engine" / "faq_mining.py").read_text(
        encoding="utf-8"
    )
    check(
        "post_verify" in (ROOT / "backend" / "service" / "faq_service.py").read_text(encoding="utf-8"),
        "红线 4: 标题回填复用 search_engine.acl.post_verify（同一语义只有一份实现）",
    )
    service_src = (ROOT / "backend" / "service" / "faq_service.py").read_text(encoding="utf-8")
    check(
        "attach_related_docs" in service_src and "result.allowed" in service_src,
        "红线 4: 无权看的关联文档**整条剔除**而不是置空标题",
    )
    check(
        "title_by_id" not in mining_src,
        "红线 4: 挖掘引擎自己不查标题（标题一律过权限复核）",
    )

    # --- 红线 8：不拼 SQL ---
    for path, label in (
        (ROOT / "backend" / "engines" / "data_mining_engine" / "faq_mining.py", "faq_mining"),
        (ROOT / "backend" / "service" / "faq_service.py", "faq_service"),
    ):
        code = _blank(path.read_text(encoding="utf-8"))
        check(
            "execute(text(" not in code and "f\"SELECT" not in code,
            f"红线 8: {label} 没有裸 SQL 拼接",
        )
    # 🔴 「参数化」不能靠**形状**判断。`Faq.question.like(f"%{kw}%")` 看起来像
    #    f-string 拼 SQL，其实 f-string 拼的是**绑定参数的值**，语句文本仍是 `LIKE %s`。
    #    我第一版把这行判成违规，是错判 —— 所以这里改成**真的编一次语句**：
    #    塞一个注入串进去，看它有没有落进 SQL 文本。这条断言的价值在于，
    #    后来的人看到源码里那个 f-string 时，不必再犹豫一次。
    from sqlalchemy import select as sa_select
    from sqlalchemy.dialects import mysql as mysql_dialect

    from backend.models import Faq

    probe = "ZZVFYFAQ' OR 1=1 -- "
    compiled = str(
        sa_select(Faq.id).where(Faq.question.like(f"%{probe}%")).compile(
            dialect=mysql_dialect.dialect()
        )
    )
    check(probe not in compiled, "红线 8: 关键词（含注入串）不进 SQL 文本")
    check(
        "LIKE %s" in compiled,
        f"红线 8: LIKE 右侧是绑定参数 %s（实测 {compiled.split('WHERE', 1)[-1].strip()}）",
    )

    # --- qa.py 的短路（be-05 回填）---
    # ⚠️ 先在**原始源码**上按注释里的分届标记切出命中分支，再对这一小段去注释。
    #    直接在 `_blank()` 之后的代码上切是错的：`_blank` 会把
    #    `# ---- ③④⑤⑥` 这个标记本身删掉，切不出来，`branch` 一路吃到文件末尾 ——
    #    于是「命中分支里不检索」永远红（文件后半段当然有 `retrieval.retrieve`）。
    qa_src = (ROOT / "backend" / "engines" / "search_engine" / "qa.py").read_text(encoding="utf-8")
    qa_code = _blank(qa_src)
    check("# ---- ③④⑤⑥" in qa_src, "be-05 回填: ② / ③ 之间的分届标记仍在（用例依赖它定位）")
    if "if hit is not None:" not in qa_src or "# ---- ③④⑤⑥" not in qa_src:
        check(False, "be-05 回填: qa.py 里定位不到 FAQ 短路分支")
    else:
        raw_branch = qa_src.split("if hit is not None:", 1)[1].split("# ---- ③④⑤⑥", 1)[0]
        branch = "\n".join(re.sub(r"#.*$", "", line) for line in raw_branch.splitlines())
        check(
            "retrieval.retrieve" not in branch,
            "be-05 回填: 命中分支里**不检索**（这才是加速的意义）",
        )
        check(
            "stream_chat" not in branch,
            "be-05 回填: 命中分支里**不调 LLM**（token 恒为 0 的前提）",
        )
        check(
            "SseCitations(items=[])" in branch,
            "be-05 回填: 命中时发**空数组**的 citations（不是不发这个事件）",
        )
        check(
            '"blocked"' not in branch,
            "be-05 回填: 命中时不发 blocked 事件（FAQ 没检索，谈不上受限）",
        )
        check(
            "llm_called=False" in branch,
            "be-05 回填: done 事件如实回 llm_called=False",
        )
        check(
            "persist_turn" in branch,
            "be-05 回填: 命中**也要落 qa_log**（否则已发布 FAQ 的问法会永远留在挖掘窗口）",
        )
        check("return" in branch, "be-05 回填: 命中分支提前 return")
    check(
        "log.faq_hit = True" in qa_code and "log.token_in = 0" in qa_code,
        "be-05 回填: 命中时 faq_hit=True 且 token 记 0（PRD §9.2 场景二的口径）",
    )
    # 旧的降级桩必须已被真实现替换
    check(
        "except AttributeError" not in qa_code,
        "be-05 回填: 旧的 FAQ 降级桩（except AttributeError）已移除",
    )
    check(
        "from backend.faq.faq_cache import faq_cache" in qa_code,
        "be-05 回填: 短路口指向 be-08 交付的真实索引",
    )

    # --- main.py 的 lifespan ---
    main_code = _blank((ROOT / "main.py").read_text(encoding="utf-8"))
    check(
        "faq_service.startup()" in main_code and "faq_service.stop_background_tasks()" in main_code,
        "lifespan: 启动装载 FAQ 索引、关闭时停止后台任务",
    )
    check(
        main_code.index("faq_service.startup()") < main_code.index("yield"),
        "lifespan: 装载在 yield **之前**（先有索引再接请求）",
    )


def t1_config_and_beat() -> None:
    print("\n== T1 配置与 beat 排班 ==")

    http_ts = ROOT / "frontend" / "src" / "api" / "http.ts"
    if http_ts.exists():
        match = re.search(r"timeout:\s*([\d_]+)", http_ts.read_text(encoding="utf-8"))
        if match:
            frontend_timeout = int(match.group(1).replace("_", "")) / 1000
            check(
                settings.faq_mine_sync_max_seconds < frontend_timeout,
                f"配置: FAQ_MINE_SYNC_MAX_SECONDS({settings.faq_mine_sync_max_seconds}s) "
                f"< 前端全局超时({frontend_timeout:g}s)",
            )
        else:
            skip("配置: 挖掘自限耗时 < 前端超时", "http.ts 里没找到 timeout")
    else:
        skip("配置: 挖掘自限耗时 < 前端超时", "找不到 frontend/src/api/http.ts")

    check(
        settings.faq_confidence_freq_ref >= 1,
        "配置: FAQ_CONFIDENCE_FREQ_REF ≥ 1（否则频次项恒定封顶，公式退化为纯相似度）",
    )
    check(
        settings.faq_cache_reload_interval_seconds >= 60,
        "配置: 兜底重载周期 ≥ 60s（太短会让「定时重载」变成实际上的主路径）",
    )
    check(settings.faq_page_size_max >= 20, "配置: 分页上限 ≥ 默认页大小")
    check(
        0 < settings.faq_match_threshold <= 1 and 0 < settings.faq_cluster_similarity <= 1,
        "配置: 匹配阈值与聚类阈值都在 (0,1]",
    )

    celery_src = (ROOT / "backend" / "tasks" / "celery_app.py").read_text(encoding="utf-8")
    tasks_src = (ROOT / "backend" / "tasks" / "mining_tasks.py").read_text(encoding="utf-8")

    check(
        "mining.mine_faq_clusters" in tasks_src and "mining.mine_faq_clusters" in celery_src,
        "beat: 任务名在 celery_app 的 beat 计划与任务定义处**逐字一致**",
    )
    mine_block = celery_src.split('"mine-faq-clusters-daily"', 1)
    check(len(mine_block) == 2, "beat: 存在 mine-faq-clusters-daily 计划")
    if len(mine_block) == 2:
        block = mine_block[1].split("},", 1)[0]
        check("minute=37" in block, "beat: 挖掘排在 02:37（避开 */10 预聚合的整点，字典 §9 #116）")
        check("hour=2," in block.replace(" ", ""), "beat: 挖掘在凌晨 2 点档")
    # 与 `*/10` 的间隔型计划撞车检查：37 不是 10 的倍数
    check(37 % 10 != 0, "beat: 02:37 不在 */10 的刻度上（不会与 stats.aggregate_hourly 撞车）")
    check(
        "include" in celery_src and "backend.tasks.mining_tasks" in celery_src,
        "beat: mining_tasks 在 celery include 列表里（漏写 = Received unregistered task）",
    )
    check(
        "countdown = min(" in tasks_src,
        "Celery: 手动 retry **自算退避**（retry_backoff 对 self.retry() 不生效）",
    )
    check(
        "fresh_session()" in tasks_src,
        "Celery: 任务走 fresh_session（异步 engine 不能跨 asyncio.run 复用）",
    )
    check(
        "SessionLocal" not in _blank(tasks_src),
        "Celery: 任务**不出现** SessionLocal（模块级 engine 绑在创建它的事件循环上，会报 "
        "'NoneType' object has no attribute 'send'）",
    )

    # --- numpy 必须在 pyproject 里显式声明 ---
    # 本机之所以「能跑」，是因为 numpy 是某个依赖的传递依赖。
    # 换台机器（或上游改依赖树）就会 ImportError，而那时搜代码只会看到
    # 「到处都在 import numpy」却找不到声明 —— 属于「本机能跑、别处炸」的典型。
    # ⚠️ 用 `tomllib` 解析而不是切字符串：依赖项里带 extras 的写法
    #    （`pwdlib[argon2]>=0.2`）自身就含 `[` `]`，按第一个 `]` 切会把列表截断 ——
    #    我第一版就是这么写的，只读出了 3 个依赖，而断言看起来「正常工作」。
    import tomllib

    with (ROOT / "pyproject.toml").open("rb") as handle:
        declared = {
            re.split(r"[<>=!\[; ]", spec.strip())[0].lower()
            for spec in tomllib.load(handle)["project"]["dependencies"]
        }
    check(
        "numpy" in declared,
        f"依赖: pyproject 显式声明 numpy（实测 {len(declared)} 个依赖：{sorted(declared)}）",
    )


def t1_cache_match_accepts_list() -> None:
    """FAQ 索引的 `match` 必须吃下 `qa.py` 实际传的那种入参（**list**）。

    🔴 这是一个「接口一直 200、功能永远不生效」的静默失效，2026-10-15 实测抓到：
       `FaqCache.match` 注解写 `np.ndarray`，内部却在 `np.asarray(...)` **之前**
       就读 `query_vector.size`；而 `search_engine/qa.py` 传的是它自己
       `_l2_normalize` 算出来的 **list** ⇒ `'list' object has no attribute 'size'`。
       `_try_faq_match` 的 `except Exception` 是**刻意**的（缓存故障不该把问答打成
       500），于是异常被吞、按未命中处理 ⇒ **A7 的「缓存加速应答」永远不会发生**，
       而 `faq_hit` 恒为 False、接口恒 200。

    所以这条用例**不能**只测 `faq_cache.match(np.asarray(v))` ——
    那正是旧实现能通过的形状（自证式断言）。
    它必须走 `qa.py` 真实那条路：`_try_faq_match(_l2_normalize(list))`。

    ⚠️ 变异测试：把 `match` 里的 `np.asarray` 挪回判空之后，本条变红。
    """
    print("\n== T1.5 FAQ 索引入参口径（list，不是 ndarray） ==")

    from backend.engines.search_engine.qa import _l2_normalize, _try_faq_match
    from backend.faq.faq_cache import FaqCache

    cache = FaqCache()
    vector = np.asarray(_vec(4242), dtype=np.float32)
    blob = sim.pack_embedding(vector) if hasattr(sim, "pack_embedding") else vector.tobytes()
    cache.load_all([("faq_probe", "探针问题", "探针答案", blob)])
    check(cache.size() == 1, f"索引: 装载 1 条（实测 {cache.size()}）")

    # ① 真实调用形状：qa.py 自己算的 list
    as_list = _l2_normalize([float(x) for x in vector])
    check(isinstance(as_list, list), f"入参形状: `_l2_normalize` 返回 list（实测 {type(as_list).__name__}）")

    import backend.faq.faq_cache as _fc

    real_cache = _fc.faq_cache
    _fc.faq_cache = cache  # `_try_faq_match` 内部 import 的是模块属性
    try:
        hit = _try_faq_match(as_list)
    finally:
        _fc.faq_cache = real_cache

    # 归一化把模长抹平 ⇒ 同向量余弦 1.0 ⇒ 必然过阈值。None 只可能来自那个被吞掉的异常。
    check(
        hit is not None,
        "match: `_try_faq_match(_l2_normalize(list))` 命中"
        "（返回 None 说明异常被吞成了「未命中」——旧实现就是这个症状）",
    )
    if hit is not None:
        check(hit.faq_id == "faq_probe", f"match: 命中正确的 faq_id（实测 {hit.faq_id}）")
        check(hit.score > 0.99, f"match: 同向量余弦 ≈ 1（实测 {hit.score:.4f}）")

    # ② 直接调也不该炸（ndarray 与 list 两种都要收）
    try:
        cache.match(as_list)
        check(True, "match: 直接传 list 不抛异常")
    except Exception as exc:  # noqa: BLE001
        check(False, f"match: 直接传 list 抛了 {type(exc).__name__}: {exc}")
    try:
        cache.match(vector)
        check(True, "match: 传 ndarray 照常工作")
    except Exception as exc:  # noqa: BLE001
        check(False, f"match: 传 ndarray 抛了 {type(exc).__name__}: {exc}")

    # ③ 空向量仍返回 None 而不是抛（零向量的余弦无意义）
    # ⚠️ 包 try：变异（把 asarray 挪回判空之后）时这里是抛 AttributeError 的，
    #    不包的话脚本会**中断**而不是报 FAIL —— 中断看起来像「脚本坏了」，
    #    而 FAIL 才指向实现。回归时要让它红，不要让它把验收跑停。
    try:
        check(cache.match([]) is None, "match: 空入参返回 None（不是抛异常）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"match: 空入参抛了 {type(exc).__name__}（应返回 None）")


def t1_publish_embedding_side() -> None:
    """发布 FAQ 时算的那条向量，必须与**查询侧**同口径。

    🔴 这里是一个「两侧都不报错」的静默失效，2026-10-15 实测抓到：
       `faq.question_embedding` 的唯一用途就是**被用户提问去比**
       （`faq_cache.search` 拿 `query_dense` 比它；判重拿 `qa_log.question_embedding` 比它），
       而这两个向量都出自 `retrieval.embed_query`，是**带 BGE 查询前缀**的。
       发布路径若用裸 `embed_texts`（passage 口径，不带前缀），两侧就错位了。

       实测本机 BGE：同一条问题「带前缀 vs 不带前缀」余弦只有
       **0.827 / 0.942 / 0.837**，而 `faq_match_threshold = 0.92`
       ⇒ 用户一字不差问同一条已发布 FAQ 也会匹配失败。
       慢、费 token，但界面与「缓存没启用」完全一样。

    ⚠️ 断言必须落在 `_blank()` 之后的代码上：本仓库的注释**刻意**解释了
       「不能用裸 embed_texts」，直接对原文做子串检查会匹配到那段解释。
    """
    print("\n== T1 发布侧向量口径 ==")

    src = (ROOT / "backend" / "engines" / "data_mining_engine" / "faq_mining.py").read_text(
        encoding="utf-8"
    )
    code = _blank(src)

    # 正向：发布路径走查询侧封装
    check(
        "retrieval.embed_query(" in code,
        "发布: 算 FAQ 向量走 `retrieval.embed_query`（带查询前缀，与提问侧同口径）",
    )
    # 反向：不许再出现 passage 口径的裸调用（那是错位的那一版）
    check(
        "embed_texts([question])" not in code,
        "发布: 代码里没有 `embed_texts([question])`（passage 口径，会让缓存恒不命中）",
    )
    # 顺带钉住阈值口径本身没被人改小到「0.83 也算过」来自圆其说
    check(
        settings.faq_match_threshold >= 0.9,
        f"配置: faq_match_threshold={settings.faq_match_threshold} ≥ 0.9"
        "（若有人把阈值降到 0.83 来「修好」上面那个错位，等于放进不相关的问题）",
    )


def t1_migration_consistency() -> None:
    print("\n== T1 迁移 0006 与模型一致 ==")

    path = ROOT / "alembic" / "versions" / "0006_create_faq_tables.py"
    if not path.exists():
        check(False, "迁移: 0006_create_faq_tables.py 存在")
        return
    src = path.read_text(encoding="utf-8")
    check('down_revision: str | None = "0005"' in src, "迁移: down_revision = 0005（接在 be-07 之后）")
    check("_CHARSET" in src and "utf8mb4" in src, "迁移: 表级 utf8mb4（字典 §1）")

    # 迁移里声明的列 vs 模型声明的列 —— 逐表比对
    for table, model, index_name in (
        ("faq_candidate", FaqCandidate, "ix_faq_candidate_status"),
        ("faq", Faq, "ix_faq_cache_enabled"),
    ):
        block = src.split(f'"{table}",', 1)
        if len(block) != 2:
            check(False, f"迁移: 找到 create_table({table})")
            continue
        body = block[1].split("_CHARSET", 1)[0]
        migrated = set(re.findall(r'sa\.Column\(\s*"(\w+)"', body))
        modeled = {column.name for column in sa_inspect(model).columns}
        check(
            migrated == modeled,
            f"迁移/模型一致: {table}（迁移多 {sorted(migrated - modeled) or '无'} / "
            f"模型多 {sorted(modeled - migrated) or '无'}）",
        )
        check(index_name in src, f"迁移: {table} 上建了 {index_name}（字典 §2.4 要求）")
    check(
        "ForeignKey" not in src,
        "迁移: faq 与 faq_candidate **无外键**（候选被清理不该拖住已发布的 FAQ）",
    )


# ═══════════════════════════════════════════════════════════════
# T1.5 假会话
# ═══════════════════════════════════════════════════════════════


async def t15_fake_session() -> None:
    print("\n== T1.5 假会话驱动挖掘与审核 ==")

    # --- 窗口参数反向 → 1001（ParamError，不是 5000）---
    session = _ScriptedSession()
    try:
        await faq_mining.mine_faq_clusters(
            session, start=_dt(2026, 10, 10), end=_dt(2026, 10, 1)
        )
        check(False, "mine: 窗口反向应报 ParamError")
    except ParamError:
        check(True, "mine: 窗口反向报 ParamError（1001，不是默认的 5000）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"mine: 窗口反向报的是 {type(exc).__name__}，应为 ParamError")

    # --- 空窗口 → 0 条，不抛 ---
    empty = _ScriptedSession(scalars_seq=[[]])
    result = await faq_mining.mine_faq_clusters(empty)
    check(result["candidates_created"] == 0, "mine: 空窗口返回 0 条而不是抛异常")
    check(result["clusters_scanned"] == 0, "mine: 空窗口 clusters_scanned = 0")

    # --- 一轮挖掘：5 条同一问题 → 1 条候选 ---
    base = _vec(101)
    # 5 条同向量 + 1 条远离的（不足 min_frequency=5，应被滤掉）
    logs = [
        _log(f"{MARK}_log_{i}", f"{MARK} 报销流程是什么 {i}", base, allowed=[f"chunk_{i}"])
        for i in range(5)
    ]
    logs.append(_log(f"{MARK}_log_far", f"{MARK} 完全无关的问题", _vec(202)))
    # 调用顺序：① 窗口日志 ② 候选去重池的 question 列表 ③ FAQ 去重池
    # ④（每条候选）chunk→document 反查
    session = _ScriptedSession(
        scalars_seq=[[], ["doc_a", "doc_b"]],
        execute_seq=[[], []],
    )
    session._scalars.insert(0, logs)  # ①

    # 🔴 **必须显式把 LLM 打成不可用，不能指望「环境里没配 key」。**
    #    这条用例原先就是这么写的 —— 它靠 `llm_client.chat()` 真的连不上而通过。
    #    2026-10-15 把 DeepSeek key 配好之后，chat 真的返回了答案，
    #    降级分支不再被走到，于是它**变红，而代码是对的**。
    #    断言依赖环境 = 断言没有守任何东西：环境一变，红绿就跟着变，
    #    而下一个看到红色的人会把对的代码改坏。
    #    （同 CLAUDE.md「验收脚本自身」那一节：假红比没测更糟。）
    real_chat = faq_mining.llm_client.chat

    async def _llm_down(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("verify: 强制 LLM 不可用")

    faq_mining.llm_client.chat = _llm_down  # type: ignore[assignment]
    try:
        result = await faq_mining.mine_faq_clusters(session, min_frequency=5)
    finally:
        faq_mining.llm_client.chat = real_chat  # type: ignore[assignment]
    check(result["candidates_created"] == 1, f"mine: 5 条同问题 → 1 条候选（实测 {result['candidates_created']}）")
    check(len(session.added) == 1, "mine: 只 add 了一个候选对象")
    if session.added:
        candidate = session.added[0]
        check(
            candidate.frequency == 5,
            f"mine: 候选频次 = 簇内成员数 5（实测 {candidate.frequency}）",
        )
        check(
            candidate.related_doc_ids == ["doc_a", "doc_b"],
            "mine: related_doc_ids 是**反查出来的 document_id**，不是 chunk_id",
        )
        check(candidate.status == "pending", "mine: 新候选状态为 pending")
        check(
            0.0 < candidate.confidence <= 1.0,
            f"mine: confidence 落在 (0,1]（实测 {candidate.confidence:.4f}）",
        )
        check(
            candidate.question.startswith(MARK),
            "mine: 代表性问题取自 qa_log 原文（LLM 不可用时降级）",
        )
        check(
            "原始提问原文" in candidate.answer,
            "mine: LLM 不可用时 answer 降级为原文并**标注未经归纳**",
        )
    check(session.commits >= 1, "mine: 有新增时提交事务")

    # --- 另一半：LLM **可用**时走归纳分支，不落降级前缀 ---
    # 上面那条只证明了「LLM 挂了会降级」，没证明「LLM 好了会用它的结果」。
    # 只测降级的话，一个「永远降级」的实现也能全绿 —— 那正是 A7 要避免的。
    class _Reply:
        text = "Q: 报销流程是什么？\nA: 提交单据 → 主管审批 → 财务打款。"

    async def _llm_up(*_args: object, **_kwargs: object) -> _Reply:
        return _Reply()

    up_session = _ScriptedSession(
        scalars_seq=[[], ["doc_a", "doc_b"]],
        execute_seq=[[], []],
    )
    up_session._scalars.insert(0, logs)  # ①
    faq_mining.llm_client.chat = _llm_up  # type: ignore[assignment]
    try:
        await faq_mining.mine_faq_clusters(up_session, min_frequency=5)
    finally:
        faq_mining.llm_client.chat = real_chat  # type: ignore[assignment]
    if up_session.added:
        up = up_session.added[0]
        check(
            up.answer.startswith("提交单据"),
            f"mine: LLM 可用时用归纳结果（实测 {up.answer[:24]!r}）",
        )
        check(
            "原始提问原文" not in up.answer,
            "mine: LLM 可用时 answer **不带**降级前缀（否则会把 LLM 的结果也标成未经归纳）",
        )
        check(
            up.question == "报销流程是什么？",
            f"mine: 代表性问题取 LLM 归纳的问法（实测 {up.question!r}）",
        )
    else:
        check(False, "mine: LLM 可用时也应产出一条候选")

    # --- 幂等：同一批数据 + 已存在的候选 → 0 条新增 ---
    session2 = _ScriptedSession(
        scalars_seq=[logs, [f"{MARK} 报销流程是什么 0"], ["chunk_0"]],
        execute_seq=[[(f"{MARK} 报销流程是什么 0", pack_embedding(base))], []],
    )
    result2 = await faq_mining.mine_faq_clusters(session2, min_frequency=5)
    check(
        result2["candidates_created"] == 0,
        f"mine: **幂等** —— 同批数据第二次跑不新增候选（实测 {result2['candidates_created']}）",
    )
    check(not session2.added, "mine: 幂等时没有 add 任何对象")
    check(result2["clusters_scanned"] >= 1, "mine: 幂等时仍然报告扫过的簇数（不是静默跳过）")

    # --- 频次不足被滤 ---
    session3 = _ScriptedSession(
        scalars_seq=[logs[:2], [], []],
        execute_seq=[[], []],
    )
    result3 = await faq_mining.mine_faq_clusters(session3, min_frequency=5)
    check(result3["candidates_created"] == 0, "mine: 频次不足 min_frequency 的簇不产出候选")
    check(result3["clusters_scanned"] >= 1, "mine: 频次不足的簇仍计入 clusters_scanned（便于判断阈值是否过高）")

    # --- 脏向量行：下标重映射不能错位 ---
    dirty = [
        _log(f"{MARK}_d0", f"{MARK} 脏行 A", base),
        _log(f"{MARK}_d1", f"{MARK} 脏行 B", base),
    ]
    dirty[0].question_embedding = b"\x00" * 9  # 长度不符
    session4 = _ScriptedSession(scalars_seq=[dirty, [], []], execute_seq=[[], []])
    result4 = await faq_mining.mine_faq_clusters(session4, min_frequency=2)
    # 只剩 1 条有效 → 不足 2，应无候选；关键是**不能**因为下标错位而报错或产出
    check(
        result4["candidates_created"] == 0,
        "mine: 脏行被滤掉后**按有效行重新计数**（不会把 1 条当成 2 条）",
    )

    # --- 窗口上的 SQL 条件（离线可断言）---
    from sqlalchemy.dialects import mysql

    session5 = _ScriptedSession(scalars_seq=[[]])
    await faq_mining.mine_faq_clusters(session5)
    if session5.statements:
        sql = str(session5.statements[0].compile(dialect=mysql.dialect()))
        check(
            re.search(r"faq_hit\s+(IS|=)\s+false", sql, re.I) is not None,
            f"mine SQL: 只扫 faq_hit = false（SQLAlchemy 实际渲染为 `IS false`，实测 {sql})",
        )
        check("qa_log.question_embedding IS NOT NULL" in sql, "mine SQL: 滤掉向量为空的行")
        check("ORDER BY" in sql and "LIMIT" in sql, "mine SQL: 有 ORDER BY（可复现）与 LIMIT（防超时）")

    # --- 审核状态机 ---
    pending = _Obj(
        id="faq_c_test",
        cluster_key="k",
        question="原始问题",
        answer="原始答案",
        frequency=5,
        confidence=0.9,
        related_doc_ids=[],
        status="pending",
        reviewer_id=None,
        reviewed_at=None,
    )
    session6 = _ScriptedSession(get_map={"faq_c_test": pending})
    status, faq_id = await faq_mining.review_candidate(
        session6,
        candidate_id="faq_c_test",
        action="reject",
        question="改后问题",
        answer="改后答案",
        ctx=ctx_for(user_id="user_admin"),
    )
    check(status == "rejected" and faq_id is None, "review: 驳回 → rejected 且无 faq_id")
    # 🔴 审核人取自 `ctx`（JWT），**不是**独立入参：这样 `reviewer_id` 与
    #    审计行的 `user_id` 同源，不存在「两处各写一份身份」的可能。
    check(pending.reviewer_id == "user_admin", "review: 审核人取自 ctx.user_id（红线 1）")
    check(pending.reviewed_at is not None, "review: 驳回也记审核时间")
    # 驳回必须留痕（be-06 §5.4 的 faq.reject）——没接上的话接口照回 200、
    # 只是审计表少一行，运行时完全无声，所以在这里守住。
    reject_rows = [
        obj
        for obj in session6.added
        if getattr(obj, "action", None) == "faq.reject"
    ]
    check(
        len(reject_rows) == 1 and reject_rows[0].user_id == "user_admin",
        f"review: 驳回留痕 faq.reject 且 user_id 同源（实测 {len(reject_rows)} 行）",
    )

    # 重复审核 → 3002
    session7 = _ScriptedSession(get_map={"faq_c_test": pending})
    try:
        await faq_mining.review_candidate(
            session7,
            candidate_id="faq_c_test",
            action="reject",
            question="q",
            answer="a",
            ctx=ctx_for(user_id="user_admin"),
        )
        check(False, "review: 已审过的候选再审应报 ConflictError")
    except ConflictError:
        check(True, "review: 已审过的候选再审报 ConflictError（3002）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"review: 重复审核报的是 {type(exc).__name__}，应为 ConflictError")

    # 不存在的候选 → 3001
    session8 = _ScriptedSession(get_map={})
    try:
        await faq_mining.review_candidate(
            session8, candidate_id="nope", action="publish", question="q", answer="a",
            ctx=ctx_for(user_id="u"),
        )
        check(False, "review: 不存在的候选应报 NotFoundError")
    except NotFoundError:
        check(True, "review: 不存在的候选报 NotFoundError（3001）")
    except Exception as exc:  # noqa: BLE001
        check(False, f"review: 不存在的候选报的是 {type(exc).__name__}，应为 NotFoundError")

    # 发布 → 写 faq 行 + 候选置 published
    fresh = _Obj(
        id="faq_c_pub", cluster_key="k", question="q0", answer="a0", frequency=3,
        confidence=0.8, related_doc_ids=[], status="pending",
        reviewer_id=None, reviewed_at=None,
    )
    session9 = _ScriptedSession(get_map={"faq_c_pub": fresh})
    status9, faq_id9 = await faq_mining.review_candidate(
        session9, candidate_id="faq_c_pub", action="publish",
        question="审定后的问题", answer="审定后的答案", ctx=ctx_for(user_id="user_admin"),
    )
    check(status9 == "published" and faq_id9 is not None, "review: 发布 → published 且生成 faq_id")
    check(fresh.status == "published", "review: 候选置为 published")
    # 发布留痕：`target_id` 是**新 FAQ id**（不是 candidate_id），
    # 这样它与后续 faq.cache.toggle 落在同一个 (target_type, target_id) 上。
    publish_rows = [
        obj for obj in session9.added if getattr(obj, "action", None) == "faq.publish"
    ]
    check(
        len(publish_rows) == 1
        and publish_rows[0].target_id == faq_id9
        and (publish_rows[0].detail or {}).get("candidate_id") == "faq_c_pub",
        f"review: 发布留痕 faq.publish，target_id=新 faq_id（实测 {len(publish_rows)} 行）",
    )
    check(
        fresh.question == "审定后的问题" and fresh.answer == "审定后的答案",
        "review: 候选同步成**审定后**的内容（列表显示与 FAQ 一致）",
    )
    faq_rows = [obj for obj in session9.added if isinstance(obj, Faq)]
    check(len(faq_rows) == 1, "review: 恰好写一行 faq")
    if faq_rows:
        row = faq_rows[0]
        check(row.source_candidate_id == "faq_c_pub", "review: faq 回指来源候选")
        check(row.published_by == "user_admin", "review: published_by 来自参数（JWT）")
        check(row.cache_enabled is True, "review: 新发布的 FAQ 默认启用缓存")
        # 本机无 BGE 时 embedding 调用必失败 → 应降级为 None 且**发布照常成功**
        check(
            row.question_embedding is None or len(row.question_embedding) == 3072,
            "review: 向量算不出来时发布照常成功（缓存少一条，功能不中断）",
        )

    # --- chunk → document 反查（不 JOIN，用 IN）---
    session10 = _ScriptedSession(scalars_seq=[["doc_x", "doc_x", "doc_y"]])
    docs = await faq_mining._resolve_document_ids(
        session10,
        [_Obj(allowed_ids=["c1", "c2"]), _Obj(allowed_ids=["c3", "c1"])],
        [0, 1],
    )
    check(docs == ["doc_x", "doc_y"], f"resolve_document_ids: 去重且保序（实测 {docs}）")
    if session10.statements:
        sql = str(session10.statements[0].compile(dialect=mysql.dialect()))
        check("IN (" in sql, "resolve_document_ids: 一条 IN 查询而不是 N+1")

    session11 = _ScriptedSession()
    check(
        await faq_mining._resolve_document_ids(session11, [_Obj(allowed_ids=[])], [0]) == [],
        "resolve_document_ids: 簇内没有任何引用时**不查库**直接返回 []",
    )


def _dt(year: int, month: int, day: int):
    from datetime import datetime

    return datetime(year, month, day)


# ═══════════════════════════════════════════════════════════════
# T2 接口层
# ═══════════════════════════════════════════════════════════════


async def t2_http() -> bool:
    print("\n== T2 接口层（需 MySQL + Redis）==")

    paths = main.app.openapi()["paths"]
    # 🔴 用 `app.openapi()["paths"]` 而不是数 `app.routes`：
    #    Starlette 1.7 的 include_router 把子路由包成 `_IncludedRouter`，
    #    数 app.routes 会漏报（CLAUDE.md 已知坑）。
    expected = {
        ("/api/mining/faq/mine", "post"),
        ("/api/mining/faq/candidates", "get"),
        ("/api/mining/faq/candidates/{candidate_id}/review", "post"),
        ("/api/mining/faq", "get"),
        ("/api/mining/faq/{faq_id}/cache", "put"),
        ("/api/mining/stats/overview", "get"),
        ("/api/mining/gaps", "get"),
        ("/api/mining/gaps/{gap_id}/task", "post"),
    }
    registered = {(path, method) for path, ops in paths.items() for method in ops}
    missing = expected - registered
    check(not missing, f"路由: 8 个端点全部注册（缺 {sorted(missing) or '无'}）")

    spec = main.app.openapi()
    # --- 每个已实现端点都要求鉴权（HTTPBearer 出现在 security，不在 parameters）---
    for path, method in sorted(expected):
        if "/gaps" in path:
            continue
        operation = spec["paths"][path][method]
        check(
            bool(operation.get("security")),
            f"路由: {method.upper()} {path} 声明了 security（HTTPBearer）",
        )

    # --- 权限码与字典 §5.6 一致 ---
    router_src = (ROOT / "backend" / "api" / "data_mining_router.py").read_text(encoding="utf-8")
    codes = sorted(set(re.findall(r'require_permission\("([^"]+)"\)', router_src)))
    seeded = _seeded_permission_codes()
    if seeded:
        unseeded = [code for code in codes if code not in seeded]
        check(
            not unseeded,
            f"权限码: 路由用的码都在 seed_org.py 里（缺 {unseeded or '无'}）",
        )
    else:
        skip("权限码: 与种子数据比对", "读不到 seed_org.py 的权限码清单")

    # --- 真实连通性 ---
    try:
        async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://t") as client:
            resp = await client.post(
                "/api/auth/login", json={"username": "admin", "password": PASSWORD}
            )
            if resp.status_code != 200 or resp.json().get("code") != 0:
                skip("T2 全部接口用例", f"登录失败（HTTP {resp.status_code}）——MySQL/Redis 不可达")
                return False
            token = resp.json()["data"]["access_token"]

            # 无 token → 401
            anon = await client.get("/api/mining/faq/candidates")
            check(
                anon.status_code in (401, 403),
                f"门禁: 无 token 访问候选列表被拒（HTTP {anon.status_code}）",
            )

            # 有 token → 分页结构
            listed = await client.get("/api/mining/faq/candidates", headers=auth(token))
            check(listed.status_code == 200, f"候选列表: HTTP 200（实测 {listed.status_code}）")
            if listed.status_code == 200:
                body = listed.json()
                check(body.get("code") == 0, "候选列表: 业务码 0")
                data = body.get("data") or {}
                check(
                    set(data) >= {"list", "total", "page", "page_size"},
                    f"候选列表: 分页结构完整（实测 {sorted(data)}）",
                )
                check(data.get("page") == 1, "候选列表: 回带 page（前端翻页依赖它）")

            # 分页参数真的生效（不是「静默忽略」）
            paged = await client.get(
                "/api/mining/faq/candidates?page=2&page_size=5", headers=auth(token)
            )
            if paged.status_code == 200:
                data = paged.json().get("data") or {}
                check(
                    data.get("page") == 2 and data.get("page_size") == 5,
                    "候选列表: page/page_size 真的生效（FastAPI 会静默忽略未声明的参数）",
                )

            # 未发布 FAQ 列表（faq:list 权限）
            faq_list = await client.get("/api/mining/faq", headers=auth(token))
            check(faq_list.status_code == 200, f"FAQ 列表: HTTP 200（实测 {faq_list.status_code}）")

            # 运营摘要：5 个字段齐全
            overview = await client.get("/api/mining/stats/overview", headers=auth(token))
            check(overview.status_code == 200, f"运营摘要: HTTP 200（实测 {overview.status_code}）")
            if overview.status_code == 200:
                data = overview.json().get("data") or {}
                check(
                    set(data) >= {
                        "question_count",
                        "unique_user_count",
                        "faq_hit_rate",
                        "kb_coverage",
                        "avg_latency_ms",
                    },
                    f"运营摘要: 5 个字段齐全（实测 {sorted(data)}）",
                )

            # 自定义窗口必须报 1001（不能静默忽略）
            bad = await client.get(
                "/api/mining/faq/candidates?start_time=2026-01-01", headers=auth(token)
            )
            check(bad.status_code == 200, "候选列表: 不接受 start_time（无该参数，静默忽略）")

            # 审核一个不存在的候选 → 3001
            missing_review = await client.post(
                "/api/mining/faq/candidates/nope_xxx/review",
                json={"action": "publish", "question": "q", "answer": "a"},
                headers=auth(token),
            )
            check(
                missing_review.status_code in (200, 404)
                and missing_review.json().get("code") == 3001,
                f"审核: 不存在的候选返回业务码 3001（实测 {missing_review.json().get('code')}）",
            )

            # 身份字段必须被拒（extra=forbid）
            # 🔴 断言**业务码 1001**而不是 FastAPI 的默认 422：`core/exceptions.py`
            #    刻意把一切校验失败统一成 `400 + PARAM_ERROR`（统一信封的一部分）。
            #    只断言 422 是在断言框架的默认行为，不是本项目的行为。
            forged = await client.post(
                "/api/mining/faq/candidates/nope_xxx/review",
                json={
                    "action": "publish",
                    "question": "q",
                    "answer": "a",
                    "reviewer_id": "user_hacker",
                },
                headers=auth(token),
            )
            check(
                forged.status_code in (400, 422) and forged.json().get("code") == 1001,
                f"红线 1: 请求体里硬塞 reviewer_id 被拒且业务码 1001"
                f"（HTTP {forged.status_code} / code={forged.json().get('code')}）",
            )

            # 手动挖掘真跑
            mined = await client.post("/api/mining/faq/mine", headers=auth(token))
            check(mined.status_code == 200, f"手动挖掘: HTTP 200（实测 {mined.status_code}）")
            if mined.status_code == 200:
                data = mined.json().get("data") or {}
                check(
                    "candidates_created" in data and "clusters_scanned" in data,
                    f"手动挖掘: 返回 frontend `MineResult` 要的字段（实测 {sorted(data)}）",
                )
                check(
                    data.get("partial") in (True, False),
                    "手动挖掘: 回带 partial 标志（前端据此决定文案）",
                )

            # 自定义窗口 → 1001
            rejected = await client.post(
                "/api/mining/faq/mine?start_time=2026-01-01", headers=auth(token)
            )
            check(
                rejected.json().get("code") == 1001,
                f"手动挖掘: 传自定义窗口报 1001 而不是静默忽略（实测 {rejected.json().get('code')}）",
            )

            # 缓存开关：不存在的 faq → 3001
            toggle = await client.put(
                "/api/mining/faq/nope_xxx/cache?enabled=false", headers=auth(token)
            )
            check(
                toggle.json().get("code") == 3001,
                f"缓存开关: 不存在的 FAQ 返回 3001（实测 {toggle.json().get('code')}）",
            )

            # 缺 enabled 参数必须**被拒**（不能有默认值）—— 业务码 1001，理由同上。
            # 这条要区分的是「漏传被拒」与「漏传静默打开缓存」，`code` 才是那个信号。
            no_flag = await client.put("/api/mining/faq/nope_xxx/cache", headers=auth(token))
            check(
                no_flag.status_code in (400, 422) and no_flag.json().get("code") == 1001,
                f"缓存开关: 缺 enabled 参数被拒且业务码 1001（不能静默打开缓存，"
                f"实测 {no_flag.status_code} / code={no_flag.json().get('code')}）",
            )
    except Exception as exc:  # noqa: BLE001
        skip("T2 全部接口用例", f"环境不可达（{type(exc).__name__}）")
        return False

    return True


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
    —— 于是断言在错误的输入上「正常工作」（本脚本已因同类错误返工过 4 次）。
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
        #    断言于是变成一条 SKIP，看起来像「环境不具备」而不是「我解析错了」。
        if isinstance(node, ast.Assign):
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(value, ast.List | ast.Dict):
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


def t3_chain() -> None:
    print("\n== T3 全链路（需 Milvus + BGE + DeepSeek + worker）==")
    for label, reason in (
        ("A7-1 提问 → qa_log 落库（faq_hit=false）", "需 Milvus + BGE + DeepSeek"),
        ("A7-2 挖掘产出候选 → 人工审核发布 → faq 表有向量", "需 BGE（算 FAQ 向量）"),
        ("A7-3 再问同一问题 → FAQ 命中，**token_in = token_out = 0**", "需 Redis（索引广播）+ BGE"),
        ("A7-4 命中时 citations 为空数组、无 blocked 事件、llm_called=false", "需 Redis + BGE"),
        ("A7-5 命中后 qa_log.faq_hit=true 且该问法不再进挖掘窗口", "需 Milvus + BGE + MySQL"),
        # ⚠️ 这里的理由曾经是「be-09 未实现」—— 已过期（be-09 早已落地）。
        #    真正跑不动的原因是**链路依赖**：闭环要走完 Milvus → BGE →
        #    worker（`aggregate_knowledge_gaps`）→ 再导入，缺一不可。
        #    留着旧理由会把下一个读者引去"重新实现一个已经跑通的模块"。
        ("be-09 缺口闭环（未命中 → 缺口 → 转建 → 再导入 → closed）", "需 Milvus + BGE + worker"),
    ):
        skip(label, reason)


# ═══════════════════════════════════════════════════════════════
# 清理
# ═══════════════════════════════════════════════════════════════


async def _sweep() -> None:
    """只删自己造的数据。前缀不含下划线（`_` 在 LIKE 里是通配符）。"""
    from sqlalchemy import delete, or_

    from backend.core.db import SessionLocal
    from backend.models.qa_log import QaLog

    try:
        async with SessionLocal() as db:
            removed = await db.execute(
                delete(QaLog).where(
                    or_(
                        QaLog.question.like(f"{MARK}%"),
                        QaLog.id.like(f"{MARK}%"),
                    )
                )
            )
            await db.commit()
            print(f"  [note] 清理自造 qa_log：{removed.rowcount or 0} 行")
    except Exception as exc:  # noqa: BLE001
        print(f"  [note] 清理未执行（{type(exc).__name__}）")


# ═══════════════════════════════════════════════════════════════


async def run() -> tuple[bool, bool]:
    """返回 `(全部通过, T2 是否真的跑过)`。

    ⚠️ 四层在**同一个事件循环**里跑 —— 这正是 `core/db.py` 的模块级 engine
       能复用的前提。各自塞进一个 `asyncio.run()` 会让第二层复用到上个循环
       遗留的连接，报 `'NoneType' object has no attribute 'send'`（CLAUDE.md 已知坑）。
    """
    t1_similarity()
    t1_blob_contract()
    t1_confidence_and_key()
    t1_faq_cache()
    t1_contract_alignment()
    t1_redlines()
    t1_config_and_beat()
    t1_cache_match_accepts_list()
    t1_publish_embedding_side()
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
    # 🔴 这句**不是**「剩下的都是 T2/T3 的依赖问题」——T2 现在真的跑起来了。
    #    写死「T2 的 MySQL/Redis 依赖」会让人以为接口层一条都没验过，
    #    于是把已经能跑的东西当成没跑。逐条原因就在上方，这里只做归类提示。
    print(
        f"SKIP：{len(_skipped)} 项（**不是通过**）—— 逐条原因见上；"
        "缺口通常在 T2（需 MySQL+Redis）或 T3（需 Milvus+BGE+DeepSeek+worker）"
    )
    if not ran:
        print("⚠️ T2 未执行（MySQL 或 Redis 不可用）—— **本次结果不构成完整验收**。")
        print("   依赖恢复后重跑本脚本。")
    if passed != total:
        print("存在失败项，见上方 FAIL 行。")
    sys.exit(0 if (ok_ and ran) else 1)


if __name__ == "__main__":
    main_()
