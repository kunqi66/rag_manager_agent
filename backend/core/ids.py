"""主键生成。**全库唯一一处**。

形态 `前缀_12位随机hex`（如 `doc_a1b2c3d4e5f6`）。

为什么抽成公共模块：这个形态是**跨模块的隐式契约** ——
`sys_department` / `sys_user` / `kb_document` / `kb_chunk` 的 id 会长得一样，
而「长得一样」正是人肉排查、日志抓取、前缀匹配清理脚本的前提。
每个模块各写一份 `_new_id` 时，形态漂移不会有任何报错，
只会在某天发现「知识库的 id 是 UUID、组织的不是」。

🔴 **刻意不用完整 36 位 UUID**：`sys_department.path` 是 `VARCHAR(255)`，
   而路径由 id 段拼成 —— 36 位 UUID 会把部门树深度压到约 6 层（字典 §9 #33），
   真实组织很容易超过。12 位 hex 让每级约 17 字符，深度上限约 13 层。
   ⚠️ 这条限制**只对部门树成立**；kb 侧没有路径字段，沿用同一形态纯粹为了全库一致。

⚠️ 这是过渡方案：完整 ID 策略仍是字典 §9 #1 的待确认项。
"""

from uuid import uuid4

__all__ = ["new_id"]


def new_id(prefix: str) -> str:
    """`前缀_12位随机hex`。"""
    return f"{prefix}_{uuid4().hex[:12]}"
