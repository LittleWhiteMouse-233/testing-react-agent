from __future__ import annotations

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import Settings, get_settings
from app.api.schemas import ApiError, api_validation_error
from app.container import Container
from app.persistence.db import ensure_sqlite_database_directory, init_database
from app.resources.mcp import MCPConnectionError


logger = logging.getLogger(__name__)


def create_app(settings_override: Settings | None = None) -> FastAPI:
    settings = get_settings() if settings_override is None else settings_override

    @asynccontextmanager
    async def lifespan(instance: FastAPI):
        startup_settings = settings.model_copy(deep=True)
        startup_settings.ensure_directories()
        ensure_sqlite_database_directory(startup_settings.db_url)
        container = Container(startup_settings)
        instance.state.container = container
        await init_database(container.engine)
        await container.run_service.reconcile_orphaned_runs()
        try:
            await container.capabilities.discover(startup_settings.execution.tool_call_timeout_seconds)
        except MCPConnectionError as exc:
            logger.warning("Capability preview discovery failed: %s", exc)
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
