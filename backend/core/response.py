"""统一响应构造。

所有接口返回 {"code": 0, "message": "ok", "data": ...}。
路由层用这里的 helper 组装，不要手写字典字面量。
"""

from typing import Any


def ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    """成功响应。"""
    return {"code": 0, "message": message, "data": data}


def page_result(
    items: list[Any],
    total: int,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """分页响应体，放在 ok() 的 data 里。"""
    return {"list": items, "total": total, "page": page, "page_size": page_size}
