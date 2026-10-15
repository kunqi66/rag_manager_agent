"""余弦相似度 + 并查集聚类 —— **be-08 与 be-09 的共用件**（be-08 §4.1 / §8.4）。

为什么是这个文件而不是各写一份：be-09 的缺口聚合也要「同一个问题的提问聚起来」，
两处各写一份的后果不是重复代码，而是**两套阈值语义漂移** ——
FAQ 那边 0.85 算同一个问题、缺口那边 0.83 算，于是同一个提问在两张表里
一会儿是一簇一会儿是两簇，且双方都不报错。阈值 `FAQ_CLUSTER_SIMILARITY`
在这里只被读一次。

🔴 **`engines/` 禁止 import FastAPI**（CLAUDE.md 分层铁律）。本模块是纯计算，
   连 `models/` 都只借一个 BLOB 解码函数，没有任何 IO。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

import numpy as np

from backend.models.qa_log import unpack_embedding

logger = logging.getLogger(__name__)

__all__ = [
    "EMBED_DIM",
    "build_clusters",
    "build_matrix",
    "from_blob",
    "l2_normalize",
    "nearest_existing",
    "representative_index",
]

#: `bge-base-zh-v1.5` 的输出维度。写死在这里是**刻意的**：
#: 它同时是「一行的 BLOB 必须是 3072 字节」这条校验的依据（字典 §2.4.1）。
#: 换模型必须改这里 + 重灌 Milvus + 重算全部 `question_embedding`，不是改个配置就完事。
EMBED_DIM = 768

#: 一行的字节数。`768 * 4`。
_BLOB_BYTES = EMBED_DIM * 4

#: 分块算相似度矩阵的块大小。见 `build_clusters` 的说明。
_CHUNK = 1000


def from_blob(blob: bytes | None) -> np.ndarray | None:
    """BLOB → 一维 `float32` ndarray；长度不符返回 `None`。

    🔴 **解码本身走 `models.qa_log.unpack_embedding`，本模块不重复实现。**
       be-05 那边已经按 `<f4` 小端写死了，且它的 docstring 明确写了
       「字节序必须与 be-08 的读取端完全一致，本模块读写口径放一起避免分叉」。
       在这里再写一份 `np.frombuffer(blob, dtype="<f4")` 看着更"干净"，
       但那正是分叉的开始 —— 两份实现对同一段字节给出不同解释时**不会报错**。

    **长度不符返回 `None` 而不是抛异常**：这是刻意的。库里可能躺着
    换模型前后的历史行、或某次写入被截断的行；为一行脏数据让整轮挖掘抛异常，
    代价是「当天一个候选都产不出来」，而收益只是"报了个错"。
    调用方负责计数并告警（`build_matrix` 会做）。
    """
    if not blob:
        return None
    if len(blob) != _BLOB_BYTES:
        return None
    return np.asarray(unpack_embedding(blob), dtype=np.float32)


def build_matrix(blobs: Iterable[bytes | None]) -> tuple[np.ndarray, list[int]]:
    """一批 BLOB → `(N×768 矩阵, 有效行的下标列表)`。

    第二个返回值是**必要的**：调用方要拿它把「矩阵第 i 行」映射回
    「`qa_log` 列表第几个元素」—— 跳过脏行之后两者不再相等，
    直接用 `enumerate` 会把候选的代表性问题安到别人头上，而且看起来完全正常。

    `N == 0` 时返回 `(形状 (0, 768) 的空矩阵, [])` —— 让调用方不必特判，
    空矩阵上的 `@` 与 `argmax` 都是良定义的。
    """
    rows: list[np.ndarray] = []
    kept: list[int] = []
    skipped = 0
    for index, blob in enumerate(blobs):
        vector = from_blob(blob)
        if vector is None:
            skipped += 1
            continue
        rows.append(vector)
        kept.append(index)
    if skipped:
        # 有脏行一定要留下痕迹：静默跳过会让「候选数比预期少」无从解释。
        logger.warning("挖掘跳过 %d 行长度不符的 question_embedding（非 %d 字节）", skipped, _BLOB_BYTES)
    if not rows:
        return np.empty((0, EMBED_DIM), dtype=np.float32), []
    return l2_normalize(np.vstack(rows)), kept


def l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """按行做 L2 归一化，之后余弦相似度退化为点积。

    零向量（范数为 0）保持为 0 而不是产生 `nan`：`nan` 会一路传染到
    `argmax` 与阈值比较上，使**整个簇**的判定失效，比一个"最不相似"的行危险得多。
    """
    if matrix.size == 0:
        return matrix
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    # 范数为 0 的位置换成 1，除完这一行仍是全 0（而不是 nan）。
    norms = np.where(norms == 0, 1.0, norms)
    return (matrix / norms).astype(np.float32, copy=False)


def build_clusters(vectors: np.ndarray, threshold: float) -> list[list[int]]:
    """并查集聚类：相似度 ≥ `threshold` 的两两相连，连通分量即簇。

    **为什么是并查集而不是 HDBSCAN**（be-08 §4.2 已定，此处不重开）：
    零调参（只有一个能说人话的阈值）、对输入顺序不敏感、不把低频点判成噪声
    （运营要的恰恰是把低频长尾也聚起来看频次）、不引 `scikit-learn`。

    代价是 O(N²) 相似度。缓解办法是**分块**：一次只算 `_CHUNK × N` 的
    相似度块，峰值内存从 `N²` 降到 `_CHUNK × N`。

    ⚠️ 只有 `i < j` 的这一半参与 union —— 对称矩阵算两遍是双倍耗时，
       而且会让「并查集本身是否幂等」这类验证失去意义。
    """
    count = int(vectors.shape[0])
    if count == 0:
        return []
    parent = list(range(count))

    def find(node: int) -> int:
        # 路径压缩（迭代版：递归在 N 上万时会撞 Python 的递归深度限制）
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for start in range(0, count, _CHUNK):
        stop = min(start + _CHUNK, count)
        # (stop-start) × N：整块与全部行比，只取右上三角
        sims = vectors[start:stop] @ vectors.T
        for row in range(stop - start):
            index = start + row
            # 只看 index 之后的那一半
            for other in np.nonzero(sims[row, index + 1 :] >= threshold)[0]:
                union(index, index + 1 + int(other))

    buckets: dict[int, list[int]] = {}
    # 按 index 升序插入 ⇒ 每簇内成员天然有序、簇之间的首次出现顺序也稳定。
    # 不排序的话 dict 的迭代序虽在 CPython 下是插入序，但簇的**代表元**
    # 会随 `union` 的路径压缩结果漂移，表现为「同一批数据跑两次，候选顺序不同」。
    for index in range(count):
        buckets.setdefault(find(index), []).append(index)
    return list(buckets.values())


def representative_index(vectors: np.ndarray, members: Sequence[int]) -> int:
    """簇内**距质心最近**的成员下标 —— 拿它的原文当代表性问题。

    为什么不是「取第一条」或「取最长的一条」：质心最近的那条最能代表整簇的语义，
    而其余两种都会让代表性问题随「谁先被 union 进来」漂移 ——
    同一批数据换个顺序跑，候选的标题就变了，人工审核时看着像新候选。

    向量已归一化，所以「距质心最近」等价于「与质心余弦最大」。
    """
    if not members:
        raise ValueError("representative_index 需要至少一个成员")
    stacked = vectors[list(members)]
    centroid = stacked.mean(axis=0)
    return int(members[int(np.argmax(stacked @ centroid))])


def nearest_existing(
    vector: np.ndarray, pool: np.ndarray, threshold: float
) -> tuple[int, float] | None:
    """在 `pool` 里找与 `vector` 最相似且 ≥ `threshold` 的一行。

    返回 `(下标, 相似度)`；没有够像的返回 `None`。

    **归 be-08 交付**（be-09 §3.2 也声明要用）—— 因为 be-08 自己的去重步骤
    （新簇 vs 已有 pending 候选）就是它的第一个调用方。见 be-08 §4.1。
    """
    if pool.size == 0 or vector.size == 0:
        return None
    sims = pool @ vector
    best = int(np.argmax(sims))
    score = float(sims[best])
    if score < threshold:
        return None
    return best, score
