from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import engine

settings = get_settings()
setup_logging(settings.log_level)
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    log.info("api.startup", environment=settings.environment)
    yield
    await engine.dispose()
    log.info("api.shutdown")


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="CryptoParser — аналіз P2P-ринку криптовалют з ШІ",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
