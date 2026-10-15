"""结构感知切片（PRD §6.4.2 / D13）。

纯函数，**不碰数据库、不碰网络** —— 输入 Markdown，输出 `ChunkDraft` 列表。
这样它可以被单测直接喂字符串，不必起 Milvus / MySQL。

PRD 步骤与本文件的对应：

| PRD 步骤 | 实现 |
|---|---|
| 1. 按标题切块，维护 heading_path 栈 | `_iter_units` |
| 2. 块内 ≤ 512 整块成片；超出按段落切、带 64 重叠 | `_pack_pieces` + `_apply_overlap` |
| 3. 表格整体保留，超长按行分组且每组重复表头 | `_split_table` |
| 4. 代码块整体保留不切 | `_split_unit` 的 code 分支 |
| 5. < 32 的碎片合并入相邻片 | `_merge_tiny` |
| 6. 每片写 content/heading_path/chunk_index/token_count | `split_markdown` 收尾 |

⚠️ **`content` 里不含标题行**。`heading_path` 是独立字段，它本身就是 BM25 的召回信号
   （PRD §6.4.2 明说「双重作用」）。把标题也塞进 `content` 会让前端引用卡片里
   标题出现两遍 —— 一遍来自面包屑，一遍来自正文。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from backend.core.settings import settings
from backend.engines.import_engine.tokenizer import count_tokens
from backend.schemas.kb_import import ChunkDraft

logger = logging.getLogger(__name__)

__all__ = ["split_markdown"]

#: ATX 标题。捕获级别与标题文本，尾部的 `#` 允许省略
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
#: 围栏代码块 ``` 或 ~~~
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
#: GFM 表格行：首尾都有 `|`。MinerU 输出的表格恒为这种全包围写法
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
#: 表格分隔行，如 `|---|:--:|`
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
#: 段落分隔（空行）
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")
#: 句子边界：中英文句末标点。用 lookbehind 保留标点本身
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])\s*")

_KIND_TEXT = "text"
_KIND_TABLE = "table"
_KIND_CODE = "code"


@dataclass
class _Unit:
    """一个与标题栈无关的连续语义块。

    `kind` 决定后续怎么切：`text` 按段落切，`table` 按行分组，`code` 不切。
    """

    heading_path: str
    kind: str
    text: str


# ═══════════════════════════════════════════════════════════════
# 第一步：把 Markdown 拆成 (heading_path, kind, text)
# ═══════════════════════════════════════════════════════════════


def _iter_units(md: str) -> list[_Unit]:
    """单遍扫描，维护标题栈。

    🔴 **围栏代码块内的内容一律不当结构与语法解析** —— 代码里出现
       `# 这是一行注释` 会被当成一级标题，把整个标题栈冲掉；
       出现 `| a | b |` 会被当成表格。这两者都只在特定文档上偶发，
       表现为「某份文档的 heading_path 全乱」，极难归因。
    """
    lines = md.splitlines()
    units: list[_Unit] = []
    stack: list[tuple[int, str]] = []  # [(level, title)]
    buf: list[str] = []
    kind = _KIND_TEXT
    fence: str | None = None  # 已匹配的围栏标记本身（``` 或 ~~~ 或更长）

    def current_path() -> str:
        return " > ".join(title for _, title in stack)

    def flush() -> None:
        nonlocal buf, kind
        text = "\n".join(buf).strip()
        buf = []
        opened_as = kind
        kind = _KIND_TEXT
        if text:
            units.append(_Unit(current_path(), opened_as, text))

    for line in lines:
        # —— 代码块内部：整段吞掉，直到遇见闭合围栏 ——
        if fence is not None:
            buf.append(line)
            if line.strip().startswith(fence):
                fence = None
                flush()
            continue

        if (m := _FENCE_RE.match(line)) is not None:
            # 开围栏前先把已攒的正文冲掉，否则上文会被算进代码块
            flush()
            fence = m.group(1)
            kind = _KIND_CODE
            buf.append(line)
            continue

        if (m := _HEADING_RE.match(line)) is not None:
            flush()
            level = len(m.group(1))
            # 同级或更高级的标题出现时，栈里比它深的全弹出
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
            continue

        if _TABLE_ROW_RE.match(line):
            if kind != _KIND_TABLE:
                flush()
                kind = _KIND_TABLE
            buf.append(line)
            continue

        # 普通正文。紧跟在表格后面 → 表格结束
        if kind == _KIND_TABLE:
            flush()
        buf.append(line)

    flush()
    return units


# ═══════════════════════════════════════════════════════════════
# 第二步：切分（辅助）
# ═══════════════════════════════════════════════════════════════


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in _PARA_SPLIT_RE.split(text) if p.strip()]


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT_RE.split(text) if s.strip()]


def _hard_split(text: str, max_tokens: int) -> list[str]:
    """按 token 上限硬切，用于「一整段没有任何标点」这种病态输入。

    二分找最长可容纳前缀 —— 按字符数等分会因为中英文 token/字符比差异
    导致某几片超限，而超限片会被模型静默截断。
    """
    out: list[str] = []
    start = 0
    end = len(text)
    while start < end:
        lo, hi, best = start + 1, end, start + 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if count_tokens(text[start:mid]) <= max_tokens:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        out.append(text[start:best])
        start = best
    return out


def _pack_pieces(pieces: list[str], max_tokens: int) -> list[str]:
    """贪心装箱：把若干段/句拼到接近 `max_tokens`，不超限。

    每次用**拼接后的真实 token 数**判断，而不是「各片相加」——
    段间连接符 `\\n\\n` 本身也占 token，累加法会系统性低估，
    在片段都很小的文档上足以让最后一片超出上限。
    """
    out: list[str] = []
    cur: list[str] = []
    for piece in pieces:
        if count_tokens(piece) > max_tokens:
            # 单段就超限（比如无标点的长文本），先冲掉手上这片，再硬切
            if cur:
                out.append("\n\n".join(cur))
                cur = []
            out.extend(_hard_split(piece, max_tokens))
            continue
        if cur and count_tokens("\n\n".join([*cur, piece])) > max_tokens:
            out.append("\n\n".join(cur))
            cur = [piece]
        else:
            cur.append(piece)
    if cur:
        out.append("\n\n".join(cur))
    return out


def _greedy_tail(pieces: list[str], budget: int) -> str:
    """从末尾往前取整片，累计不超过 `budget`。

    末尾那片**本身**就超预算时返回空串，交给更细的粒度去试 ——
    这里返回「截半句」是最坏的选择：半句话既读不通，又会把噪声喂进向量。
    """
    if not pieces or count_tokens(pieces[-1]) > budget:
        return ""
    tail: list[str] = []
    total = 0
    for piece in reversed(pieces):
        tokens = count_tokens(piece)
        if total + tokens > budget:
            break
        tail.insert(0, piece)
        total += tokens
    return "\n\n".join(tail).strip()


def _hard_tail(text: str, budget: int) -> str:
    """无标点长串的兜底：二分取 token 数不超 `budget` 的最长后缀。"""
    lo, hi, best = 1, len(text), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if count_tokens(text[-mid:]) <= budget:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return text[len(text) - best :].strip() if best else ""


def _tail_for_overlap(text: str, overlap_tokens: int) -> str:
    """取 `text` 末尾不超过 `overlap_tokens` 的一段，用作下一片的上下文。

    粒度**逐级退化**：段落 → 句子 → 硬截。

    🔴 必须有这个退化链。只看段落粒度的话，`chunk_overlap=64` 而正文段落普遍
       在 150+ token 时，「末尾那一段本身就超预算」会**恒定**成立，
       于是重叠**永远为空** —— 不报错、不留痕，只是在长文档上静默失去
       PRD 步骤 2 要求的上下文衔接，表现为「跨片的问题检索不到」。
    """
    for pieces in (_split_paragraphs(text), _split_sentences(text)):
        tail = _greedy_tail(pieces, overlap_tokens)
        if tail:
            return tail
    return _hard_tail(text, overlap_tokens)


def _apply_overlap(chunks: list[str], max_tokens: int, overlap_tokens: int) -> list[str]:
    """给相邻片之间加重叠。

    ⚠️ 重叠会把本片撑大，**撑到超过上限时宁可不加** ——
       上限是模型的硬约束，重叠只是召回率的优化项，两者冲突时保硬约束。
    """
    if overlap_tokens <= 0 or len(chunks) < 2:
        return chunks

    out = [chunks[0]]
    for prev, cur in zip(chunks, chunks[1:]):
        tail = _tail_for_overlap(prev, overlap_tokens)
        if not tail:
            out.append(cur)
            continue
        candidate = f"{tail}\n\n{cur}"
        out.append(candidate if count_tokens(candidate) <= max_tokens else cur)
    return out


def _split_table(text: str, max_tokens: int) -> list[str]:
    """超长表格按行分组，**每组重复表头**（PRD 步骤 3）。

    重复表头的理由：拆开之后每组都是独立切片、独立被检索到，
    没有表头的 `| 200 | 300 | 实报实销 |` 谁也不知道这三列是什么。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []

    has_sep = len(lines) >= 2 and _TABLE_SEP_RE.match(lines[1]) is not None
    header = lines[:2] if has_sep else lines[:1]
    body = lines[len(header):]
    header_text = "\n".join(header)

    if not body:
        return [header_text]

    out: list[str] = []
    cur: list[str] = []
    for row in body:
        if cur and count_tokens("\n".join([header_text, *cur, row])) > max_tokens:
            out.append("\n".join([header_text, *cur]))
            cur = [row]
        else:
            cur.append(row)
    if cur:
        out.append("\n".join([header_text, *cur]))

    # 单行就超限的极端表（比如某单元格里塞了一整段话）：整体保留并告警，
    # 不硬切 —— 切开的一行在语义上是残缺的，宁可让它超出（BM25 仍能命中全文）
    for chunk in out:
        if count_tokens(chunk) > max_tokens:
            logger.warning(
                "表格切片超出上限（%d > %d tokens），已整体保留；表格行过长时分列可能更好",
                count_tokens(chunk),
                max_tokens,
            )
            break
    return out


# ═══════════════════════════════════════════════════════════════
# 第三步：按 kind 分派切分策略
# ═══════════════════════════════════════════════════════════════


def _split_unit(unit: _Unit, max_tokens: int, overlap_tokens: int) -> list[str]:
    total = count_tokens(unit.text)

    if unit.kind == _KIND_CODE:
        # PRD 步骤 4：代码块整体保留，不切。
        # ⚠️ 超过上限时 BGE 只会**截断向量**，但 `content` 全量落在 MySQL、
        #    也被 Milvus 的 BM25 全文索引，所以关键词仍检索得到 ——
        #    代价可以接受，但必须告警，否则「某段代码永远搜不到」无从发现。
        if total > max_tokens:
            logger.warning(
                "代码块超出上限（%d > %d tokens），按 PRD 整体保留不切；"
                "该片稠密向量会被模型截断，仅 BM25 可全文命中",
                total,
                max_tokens,
            )
        return [unit.text]

    if unit.kind == _KIND_TABLE:
        if total <= max_tokens:
            return [unit.text]
        return _split_table(unit.text, max_tokens)

    # 正文
    if total <= max_tokens:
        return [unit.text]

    paragraphs = _split_paragraphs(unit.text)
    chunks = _pack_pieces(paragraphs, max_tokens)
    return _apply_overlap(chunks, max_tokens, overlap_tokens)


# ═══════════════════════════════════════════════════════════════
# 第四步：合并碎片
# ═══════════════════════════════════════════════════════════════


def _merge_tiny(
    items: list[tuple[str, str]], max_tokens: int, min_tokens: int
) -> list[tuple[str, str]]:
    """把 `token < CHUNK_MIN_TOKENS` 的碎片并进相邻片（PRD 步骤 5）。

    ⚠️ **只在同一 `heading_path` 内合并**。跨章节合并会让碎片挂上错误的面包屑：
       「年假天数」的正文顶着「报销标准」的标题路径，引用卡片指向错误位置 ——
       这种错误没有任何报错，只能靠人眼看出来。

    合并**优先后一片**而非前一片（先并入前一片会让碎片的归属偏向它前面那一节）。
    """
    out: list[tuple[str, str]] = []
    for content, path in items:
        if not out or count_tokens(content) >= min_tokens:
            out.append((content, path))
            continue

        prev_content, prev_path = out[-1]
        if prev_path == path:
            joined = f"{prev_content}\n\n{content}"
            if count_tokens(joined) <= max_tokens:
                out[-1] = (joined, prev_path)
                continue
        # 前一片不同章节 / 并进去会超限 → 保留为独立片。
        # 宁可留一个小片，也不要为凑长度把两个章节缝在一起。
        out.append((content, path))

    # 头部碎片没有「前一片」可并，回头并进后一片
    if (
        len(out) >= 2
        and count_tokens(out[0][0]) < min_tokens
        and out[0][1] == out[1][1]
    ):
        joined = f"{out[0][0]}\n\n{out[1][0]}"
        if count_tokens(joined) <= max_tokens:
            out[1] = (joined, out[1][1])
            out.pop(0)

    return out


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════


def split_markdown(md: str) -> list[ChunkDraft]:
    """把 Markdown 切成 `ChunkDraft` 列表。

    空文档返回**空列表**（不是一片空的）—— 上游据此把任务判为失败，
    好过写进去一片 `content=""` 的垃圾向量，那种向量会以中等相似度
    命中**所有**查询，是最坏的一种脏数据。
    """
    if not md or not md.strip():
        return []

    max_tokens = settings.chunk_max_tokens
    min_tokens = settings.chunk_min_tokens
    overlap_tokens = settings.chunk_overlap

    raw: list[tuple[str, str]] = []
    for unit in _iter_units(md):
        for content in _split_unit(unit, max_tokens, overlap_tokens):
            if content.strip():
                raw.append((content, unit.heading_path))

    merged = _merge_tiny(raw, max_tokens, min_tokens)

    drafts: list[ChunkDraft] = []
    for index, (content, path) in enumerate(merged):
        drafts.append(
            ChunkDraft(
                content=content,
                heading_path=path,
                chunk_index=index,
                # 这里再数一次而不是复用中间结果：合并会改内容，
                # 而复用陈旧计数正是「token_count 与 content 对不上」的来源
                token_count=count_tokens(content),
            )
        )

    logger.info(
        "切片完成：%d 片，平均 %d tokens",
        len(drafts),
        (sum(d.token_count for d in drafts) // len(drafts)) if drafts else 0,
    )
    return drafts
