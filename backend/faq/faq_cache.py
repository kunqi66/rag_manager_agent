"""FAQ 高速应答缓存 —— **进程内**的 numpy 矩阵索引（PRD §6.3.2）。

回答路径上每次问答都要问一句「这个问题是不是已经沉淀过了」，而这条判断
必须在**毫秒级**完成：它是检索之前的一步，慢了就等于让每次问答都白等。
所以索引放在进程内存里，匹配是一次 `矩阵 @ 向量` + 一次 `argmax`。

⚠️ **为什么不是 Redis / Milvus**：
- Redis 走一次网络往返（~1ms）+ 反序列化，「毫秒级」就不成立了；
- Milvus 里没有 FAQ —— 它是「知识单元」的向量库，FAQ 不是知识单元（be-08 §3.4）。
  往 Milvus 里塞第二类实体，ACL 过滤表达式就得跟着改，而 FAQ 的可见性规则
  与文档完全不同（FAQ 是全局的、不分部门）。

🔴 **多进程一致性**：API 是 `uvicorn` 多 worker、worker 是 Celery 多进程，
   每个进程各有一份索引。发布一条 FAQ 后若只刷新了当前进程，
   其余进程**直到重启都命中不了新 FAQ**，且不报错。
   所以刷新走 **Redis pub/sub** 广播（`faq:changed`），
   外加一个兜底的定时全量重载（订阅可能掉线）。
   见 be-08 §5.4。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from backend.core.settings import settings
from backend.engines.data_mining_engine.similarity import EMBED_DIM, from_blob, l2_normalize

logger = logging.getLogger(__name__)

__all__ = ["FaqCache", "FaqHit", "faq_cache"]

#: 广播通道。**不新增 Redis 键命名空间**（be-08 §3.3）：只用一个频道名。
CHANGED_CHANNEL = "faq:changed"


@dataclass(frozen=True, slots=True)
class FaqHit:
    """一次命中。`score` 是余弦相似度（`FAQ_MATCH_THRESHOLD` 之上）。"""

    faq_id: str
    question: str
    answer: str
    score: float


class FaqCache:
    """进程内 FAQ 索引。**读多写少，读侧不阻塞**。

    并发模型：`match` 在请求协程里跑；`load_all` / `upsert` / `remove`
    由刷新的后台 asyncio 任务调用。同一个事件循环里它们不会真正并行，
    但**读侧不持锁**是刻意的 —— `match` 在问答热路径上，每次问答都去抢一把
    全局锁，就为了防一个每天发生几次的刷新，不划算。

    不持锁的前提是**写入侧整体替换而非就地修改**：`load_all` 先在本地临时结构上
    建好矩阵，最后一次性换掉 `self._matrix` 等三个引用。
    引用赋值是原子的，读侧要么看到完整的旧版、要么看到完整的新版。
    `upsert` / `remove` 也用 `np.vstack` / `np.delete` 造新数组而不是就地写。

    ⚠️ **`match` 里的 `self._matrix` / `self._faq_ids` / `self._meta` 必须先取到
       局部变量再拼装**（见该方法的注释）：分三次读属性的话，中间被换掉就会
       拿到「新矩阵配旧 id 表」，命中时返回**别人的**答案。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 三个字段必须**同生共死**（一起替换），否则「矩阵第 i 行」与
        # `_faq_ids[i]` 会错位 —— 命中时返回**别人的**答案，最坏的一种错。
        self._matrix: np.ndarray = np.empty((0, EMBED_DIM), dtype=np.float32)
        self._faq_ids: list[str] = []
        self._meta: list[tuple[str, str]] = []  # (question, answer)
        self._index: dict[str, int] = {}  # faq_id → 行号，增量刷新用

    # ---------- 写入侧 ----------

    def load_all(self, rows: Sequence[tuple[str, str, str, bytes | None]]) -> int:
        """全量重建。`rows` 是 `(faq_id, question, answer, embedding_blob)`。

        返回装进索引的行数。**长度不符的行跳过**（`from_blob` 返回 `None`）：
        换模型前后的历史行、或被截断的行都会走到这里，为它们让整次加载失败
        等于把整个缓存打空 —— 那比少几条 FAQ 严重得多。

        `cache_enabled=false` 的行**不传进来**（SQL 侧已过滤），
        所以本函数不需要知道开关的存在。
        """
        ids: list[str] = []
        meta: list[tuple[str, str]] = []
        vectors: list[np.ndarray] = []
        for faq_id, question, answer, blob in rows:
            vector = from_blob(blob)
            if vector is None:
                # 见模块头部：静默跳过会让「发布了却命中不了」无从解释，必须留痕。
                logger.warning("FAQ %s 的 question_embedding 缺失或长度不符，已跳过", faq_id)
                continue
            ids.append(faq_id)
            meta.append((question, answer))
            vectors.append(vector)

        matrix = (
            l2_normalize(np.vstack(vectors)) if vectors else np.empty((0, EMBED_DIM), np.float32)
        )
        with self._lock:
            # 三个字段一次性替换，见类 docstring
            self._matrix, self._faq_ids, self._meta = matrix, ids, meta
            self._index = {faq_id: position for position, faq_id in enumerate(ids)}
        logger.info("FAQ 索引已加载 %d 条（送出 %d 条）", len(ids), len(rows))
        return len(ids)

    def upsert(self, faq_id: str, question: str, answer: str, blob: bytes | None) -> bool:
        """增量更新一条。已在索引里则**原地覆盖**，否则**追加一行**。

        「原地覆盖」而不是「删掉再追加」：追加会让矩阵无限增长 ——
        一条 FAQ 改 N 次就有 N 行，`argmax` 会命中旧行（旧答案），
        而 `_index` 只指向最新那行，两者不一致且不报错。

        返回是否成功装载（`False` 表示向量不合规、索引未动）。
        """
        vector = from_blob(blob)
        if vector is None:
            logger.warning("FAQ %s 的 question_embedding 不合规，索引未更新", faq_id)
            return False
        normalized = l2_normalize(np.asarray(vector, dtype=np.float32))
        with self._lock:
            position = self._index.get(faq_id)
            if position is None:
                self._matrix = np.vstack([self._matrix, normalized])
                self._faq_ids.append(faq_id)
                self._meta.append((question, answer))
                self._index[faq_id] = len(self._faq_ids) - 1
            else:
                # `np.vstack` 造新数组而非就地写：读侧可能正在用旧矩阵
                self._matrix = np.vstack(
                    [self._matrix[:position], normalized, self._matrix[position + 1 :]]
                )
                self._meta[position] = (question, answer)
        return True

    def remove(self, faq_id: str) -> None:
        """移除一条（停用缓存 / 删除 FAQ）。

        ⚠️ **必须同时重建 `_index`**：删掉中间一行后后面所有行号都变了，
        只 `pop` 掉被删的那个键，剩下的键仍指着旧行号 ——
        下一次 `upsert` 会覆盖**别人**那一行，症状是「改了 A 的缓存开关，B 的答案变了」。
        """
        with self._lock:
            position = self._index.get(faq_id)
            if position is None:
                return
            self._matrix = np.delete(self._matrix, position, axis=0)
            self._faq_ids.pop(position)
            self._meta.pop(position)
            self._index = {key: idx for idx, key in enumerate(self._faq_ids)}

    # ---------- 读取侧 ----------

    def match(self, query_vector: np.ndarray | Sequence[float]) -> FaqHit | None:
        """余弦相似度 ≥ `FAQ_MATCH_THRESHOLD` 的最高一条；否则 `None`。

        ⚠️ **入参收 `list` 也收 `ndarray`**（内部统一 `np.asarray`）。
        调用方 `search_engine/qa.py` 传的是 list —— 注解只写 `np.ndarray`
        曾让两侧对不上而**静默恒不命中**，见下方注释。

        ⚠️ **本函数自己归一化入参**，不要求调用方先归一化。
        这是刻意的：调用方（`search_engine/qa.py`）拿到的 query 向量可能已被
        归一化、也可能没有，让「归一化」这件事取决于调用方的记性 ——
        漏了的话相似度会变成未归一化的点积，**数值偏大 ⇒ 大量误命中**，
        用户看到「问什么答什么都答到同一条 FAQ 上」。
        多算一次 `norm` 是 O(768)，在这个路径上可以忽略。

        `size == 0`（未加载 / 空库）返回 `None`：未加载时的正确行为是
        「当作没有缓存」走正常检索，**不是抛异常** —— 缓存不命中从来不是错误。
        """
        # 🔴 **先转成 ndarray 再判空。** 原先这里写的是 `query_vector.size == 0`，
        #    而 `query_vector` 的注解虽然写了 `np.ndarray`，实际调用方
        #    `search_engine/qa.py` 传的是一个**list**（它自己的 `_l2_normalize`
        #    逐元素除，返回 list）⇒ `'list' object has no attribute 'size'`
        #    ⇒ `AttributeError`。
        #    而 `qa.py::_try_faq_match` 的 `except Exception` 是**刻意**的
        #    （缓存故障不能把问答打成 500），于是它被吞掉、记一条 exception 日志、
        #    **按未命中处理** —— 结果是 A7「启用缓存后加速应答」**永远不会发生**，
        #    在任何数据量下都不会，而接口一直回 200、`faq_hit` 恒为 False。
        #    上一行本来就写了 `np.asarray(...)`，只是**写在了判空之后**，
        #    所以只在「list 恰好有 .size」时才不出错 —— 即永远出错。
        #    实测抓到于 2026-10-15；守门用例 `verify_faq.py::t1_cache_match_accepts_list`。
        query_vector = np.asarray(query_vector, dtype=np.float32)
        # 先取引用再判空：`load_all` 可能在两次读之间换掉矩阵，
        # 分开读 `self._matrix` 与 `self._faq_ids` 有机会拿到不同代的组合。
        matrix, faq_ids, meta = self._matrix, self._faq_ids, self._meta
        if matrix.size == 0 or not faq_ids or query_vector.size == 0:
            return None
        normalized = l2_normalize(query_vector)
        scores = matrix @ normalized
        best = int(np.argmax(scores))
        score = float(scores[best])
        if score < settings.faq_match_threshold:
            return None
        question, answer = meta[best]
        return FaqHit(faq_id=faq_ids[best], question=question, answer=answer, score=score)

    def size(self) -> int:
        """当前索引条数。健康检查与验收断言用。"""
        return len(self._faq_ids)


#: 进程内单例。**由 `faq_service` 负责装载与刷新**，本模块不自建后台线程 ——
#: 索引的生命周期与 `lifespan` 对齐，散在这里会让「谁来关、什么时候关」没有答案。
faq_cache = FaqCache()
