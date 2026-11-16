"""Единый формат ошибок API — RFC 7807 (application/problem+json)."""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .context import correlation_id


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str, headers: dict | None = None, **extra):
        self.status, self.code, self.detail = status, code, detail
        self.headers, self.extra = headers, extra


def _problem(status: int, code: str, detail: str, request: Request, headers=None, **extra) -> JSONResponse:
    body = {
        "type": f"https://docs.lockerflow.example/errors/{code}",
        "title": code.replace("_", " ").capitalize(),
        "status": status,
        "code": code,
        "detail": detail,
        "instance": request.url.path,
        "correlationId": correlation_id.get(),
        **extra,
    }
    return JSONResponse(body, status_code=status, media_type="application/problem+json", headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return _problem(exc.status, exc.code, exc.detail, request, exc.headers, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = [
            {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()
        ]
        return _problem(400, "VALIDATION_ERROR", "Запрос не прошёл валидацию", request, errors=errors)
