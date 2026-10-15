"""数据挖掘引擎。

- `similarity`：余弦相似度 + 并查集聚类（be-08 / be-09 **共用**，见该模块 docstring）
- `faq_mining`：FAQ 沉淀挖掘与审核流转（be-08）
- `gap`：缺口聚合与一键转建（be-09）

⚠️ **本包只 `import` `similarity`，不在这里 import `faq_mining` / `gap`**：
   后两者会拉起 `clients.llm_client` → `httpx`，而 `similarity` 是纯计算。
   把重依赖挂到包级 import 上，会让「只想用相似度」的调用方（如验收脚本）
   被迫依赖整个 LLM 客户端栈。
"""
