"""统一异常与错误码。

约定：
- 业务错误一律抛 BizError，由全局处理器转成统一响应结构。
- 未捕获异常由兜底处理器记录堆栈后返回 5000，绝不把堆栈暴露给客户端。
"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorCode:
    """业务错误码。区间划分见 PRD §5.3。"""

    OK = 0
    PARAM_ERROR = 1001
    UNAUTHORIZED = 2001
    FORBIDDEN = 2002
    NOT_FOUND = 3001
    CONFLICT = 3002
    DEPENDENCY_ERROR = 4001
    INTERNAL_ERROR = 5000


class BizError(Exception):
    """业务异常基类。"""

    def __init__(
        self,
        message: str,
        code: int = ErrorCode.INTERNAL_ERROR,
        http_status: int = status.HTTP_400_BAD_REQUEST,
    ) -> None:
        self.message = message
        self.code = code
        self.http_status = http_status
        super().__init__(message)


class UnauthorizedError(BizError):
    def __init__(self, message: str = "未登录或登录已失效") -> None:
        super().__init__(message, ErrorCode.UNAUTHORIZED, status.HTTP_401_UNAUTHORIZED)


class ParamError(BizError):
    """请求内容不合法（1001 / 400）。

    ⚠️ **不要**用裸 `BizError(msg)` 表达参数错误：它的 code 默认是
    `INTERNAL_ERROR`(5000)，于是响应变成「HTTP 400 + 业务码 5000」——
    前端按业务码分支时会当成服务端故障，日志里也看不出是用户输入的问题。
    本类存在的唯一目的就是让「参数错」有一个不会写错的入口。
    """

    def __init__(self, message: str = "参数错误") -> None:
        super().__init__(message, ErrorCode.PARAM_ERROR, status.HTTP_400_BAD_REQUEST)


class ForbiddenError(BizError):
    def __init__(self, message: str = "无权执行该操作") -> None:
        super().__init__(message, ErrorCode.FORBIDDEN, status.HTTP_403_FORBIDDEN)


class NotFoundError(BizError):
    def __init__(self, message: str = "资源不存在") -> None:
        super().__init__(message, ErrorCode.NOT_FOUND, status.HTTP_404_NOT_FOUND)


class ConflictError(BizError):
    """资源冲突（3002 / 409）。

    用于「请求本身合法，但与既有数据冲突」—— 唯一约束重复、删除仍有引用、
    内置资源不可删、并发锁竞争等。与 `ParamError`(1001) 的分界：
    1001 是**请求内容**不合法（如把部门移到自己子孙下），3002 是**服务端状态**不允许。
    """

    def __init__(self, message: str = "操作冲突") -> None:
        super().__init__(message, ErrorCode.CONFLICT, status.HTTP_409_CONFLICT)


class DependencyError(BizError):
    """外部依赖（MinerU / DeepSeek / BGE / Milvus）异常。"""

    def __init__(self, message: str, http_status: int = status.HTTP_502_BAD_GATEWAY) -> None:
        super().__init__(message, ErrorCode.DEPENDENCY_ERROR, http_status)


class UnsafePayloadError(BizError):
    """第三方返回的**内容不可用**：不安全（zip 路径穿越 / 解压体积超限）
    或压根没法用（MinerU 判定解析失败、解析结果为空）。

    共同点是**重试没有意义** —— 同一份输入必然得到同样的结果。

    为什么单独一个类，而不是复用 `DependencyError`：
    重试逻辑要靠**异常类型**区分「网络抖一下」与「这个压缩包本身是恶意的」。
    前者重试有用，后者重试只会重复命中同一个恶意压缩包、白烧三次配额。
    ⇒ 本异常一律**不可重试**（be-04 §5.3a）。

    ⚠️ 刻意**不**继承 `DependencyError`：那样 `except DependencyError` 会把它
    一起吞掉，而「先 except 子类」这种顺序依赖是很容易在后续编辑中被破坏的隐性契约。
    平行子类则强制调用方显式处理它。

    错误码沿用 `DEPENDENCY_ERROR`(4001) —— 它确实源自外部依赖，
    新增码位会让前端多一个无意义的分支。
    """

    def __init__(self, message: str = "第三方返回内容不安全") -> None:
        super().__init__(message, ErrorCode.DEPENDENCY_ERROR, status.HTTP_502_BAD_GATEWAY)


def _payload(code: int, message: str) -> dict:
    return {"code": code, "message": message, "data": None}


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器。"""

    @app.exception_handler(BizError)
    async def _biz_error(_: Request, exc: BizError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=_payload(exc.code, exc.message),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 只回第一条错误，避免把内部字段结构整包吐出去
        errors = exc.errors()
        detail = errors[0].get("msg", "参数校验失败") if errors else "参数校验失败"
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_payload(ErrorCode.PARAM_ERROR, f"参数错误：{detail}"),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.INTERNAL_ERROR
        return JSONResponse(
            status_code=exc.status_code,
            content=_payload(code, str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常：%s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_payload(ErrorCode.INTERNAL_ERROR, "服务内部错误"),
        )
