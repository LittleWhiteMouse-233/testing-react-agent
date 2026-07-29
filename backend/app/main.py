from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config import get_settings
from app.container import Container
from app.persistence.db import init_database


def create_app(settings_override=None) -> FastAPI:
    settings = settings_override or get_settings()

    @asynccontextmanager
    async def lifespan(instance: FastAPI):
        container = Container(settings)
        instance.state.container = container
        await init_database(container.engine)
        await container.run_service.reconcile_orphaned_runs()
        yield
        await container.registry.shutdown()
        await container.engine.dispose()

    instance = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
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

    @instance.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": settings.app_version}

    return instance


app = create_app()
