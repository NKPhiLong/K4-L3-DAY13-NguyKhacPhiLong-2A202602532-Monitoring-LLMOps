from __future__ import annotations

import re
import time
import uuid

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from structlog.contextvars import bind_contextvars, clear_contextvars

REQUEST_ID_HEADER = "x-request-id"
RESPONSE_TIME_HEADER = "x-response-time-ms"
# Chỉ nhận ID ngắn, ký tự an toàn để header không thể chèn dữ liệu tùy ý vào log.
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def new_correlation_id() -> str:
    return f"req-{uuid.uuid4().hex[:8]}"


def resolve_correlation_id(incoming: str | None) -> str:
    if incoming and SAFE_REQUEST_ID.fullmatch(incoming.strip()):
        return incoming.strip()
    return new_correlation_id()


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Xóa context của request trước để không rò metadata giữa các request.
        clear_contextvars()

        correlation_id = resolve_correlation_id(request.headers.get(REQUEST_ID_HEADER))
        bind_contextvars(correlation_id=correlation_id)
        request.state.correlation_id = correlation_id

        start = time.perf_counter()
        response = await call_next(request)

        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers[REQUEST_ID_HEADER] = correlation_id
        response.headers[RESPONSE_TIME_HEADER] = f"{elapsed_ms:.1f}"

        return response
