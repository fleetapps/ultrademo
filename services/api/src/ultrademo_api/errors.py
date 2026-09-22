"""Error shape `{error, detail, request_id}` (docs/04 §3); 422 keeps pydantic's `detail[]`."""

import uuid

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(self, status: int, error: str, detail: str) -> None:
        self.status = status
        self.error = error
        self.detail = detail


def request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if rid is None:
        rid = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex}"
        request.state.request_id = rid
    return rid


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def _request_id(request: Request, call_next):
        rid = request_id(request)
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            {"error": exc.error, "detail": exc.detail, "request_id": request_id(request)},
            status_code=exc.status,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            {"error": "http_error", "detail": str(exc.detail), "request_id": request_id(request)},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "error": "validation_error",
                "detail": jsonable_encoder(exc.errors()),
                "request_id": request_id(request),
            },
            status_code=422,
        )
