"""业务服务层。

`api/` 只做校验 + 注入 + 转发，全部业务编排落在这一层。
跨层传数据一律用 `backend/schemas/` 的 Pydantic 模型，**不传裸 dict**。
"""
