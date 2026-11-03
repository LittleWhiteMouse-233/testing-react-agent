from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import Settings, load_settings, resolve_config_path
from app.api.schemas import ApiError, api_validation_error
from app.container import Container
from app.persistence.db import init_database


logger = logging.getLogger(__name__)


def create_app(settings_override: Settings | None = None, *, config_path: Path | None = None) -> FastAPI:
    selected_config_path = resolve_config_path(config_path)
    settings = load_settings(selected_config_path) if settings_override is None else settings_override
    if settings_override is None:
        settings.ensure_directories()

    @asynccontextmanager
    async def lifespan(instance: FastAPI):
        container = Container(settings.model_copy(deep=True), selected_config_path)
        instance.state.container = container
        await init_database(container.engine)
        await container.run_service.reconcile_orphaned_runs()
        yield
        logger.info("Application shutdown started; waiting for run and resource cleanup")
        await container.run_service.shutdown()
        await container.settings_service.shutdown()
        await container.engine.dispose()
        logger.info("Application shutdown completed")

    instance = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        lifespan=lifespan,
    )
    instance.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    instance.include_router(router)

    @instance.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail and "message" in detail:
            payload = ApiError.model_validate(detail).model_dump(mode="json", exclude_none=True)
        else:
            payload = {"code": "http_error", "message": str(detail)}
        return JSONResponse(status_code=exc.status_code, content=payload)

    @instance.exception_handler(RequestValidationError)
    async def validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=api_validation_error(exc.errors(), settings_request=_.url.path == "/api/settings").model_dump(mode="json", exclude_none=True),
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
