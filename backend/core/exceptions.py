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


class ForbiddenError(BizError):
    def __init__(self, message: str = "无权执行该操作") -> None:
        super().__init__(message, ErrorCode.FORBIDDEN, status.HTTP_403_FORBIDDEN)


class NotFoundError(BizError):
    def __init__(self, message: str = "资源不存在") -> None:
        super().__init__(message, ErrorCode.NOT_FOUND, status.HTTP_404_NOT_FOUND)


class DependencyError(BizError):
    """外部依赖（MinerU / DeepSeek / BGE / Milvus）异常。"""

    def __init__(self, message: str, http_status: int = status.HTTP_502_BAD_GATEWAY) -> None:
        super().__init__(message, ErrorCode.DEPENDENCY_ERROR, http_status)


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
