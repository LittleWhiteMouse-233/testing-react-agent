from __future__ import annotations

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import get_settings
from app.container import Container
from app.persistence.db import init_database


logger = logging.getLogger(__name__)


def create_app(settings_override=None) -> FastAPI:
    settings = settings_override or get_settings()

    @asynccontextmanager
    async def lifespan(instance: FastAPI):
        container = Container(settings)
        instance.state.container = container
        await init_database(container.engine)
        await container.run_service.reconcile_orphaned_runs()
        yield
        await container.active_run_registry.shutdown()
        await container.engine.dispose()

    instance = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        lifespan=lifespan,
    )
    instance.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    instance.include_router(router)

    @instance.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail and "message" in detail:
            payload = {
                "code": str(detail.get("code")),
                "message": str(detail.get("message")),
            }
        else:
            payload = {"code": "http_error", "message": str(detail)}
        return JSONResponse(status_code=exc.status_code, content=payload)

    @instance.exception_handler(RequestValidationError)
    async def validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"code": "validation_error", "message": str(exc)},
        )

    @instance.exception_handler(Exception)
    async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "Unhandled request error",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "message": "Internal server error"},
        )

    @instance.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": settings.app_version}

    return instance


app = create_app()
