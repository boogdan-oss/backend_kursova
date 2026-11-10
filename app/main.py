import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import structlog
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api import ws
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.db.session import engine
from app.pubsub import close_redis
from app.services.market_stream import relay_events

settings = get_settings()
setup_logging(settings.log_level)
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    log.info("api.startup", environment=settings.environment)
    relay = asyncio.create_task(relay_events(ws.hub))
    yield
    relay.cancel()
    with suppress(asyncio.CancelledError):
        await relay
    await close_redis()
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


@app.exception_handler(SQLAlchemyError)
@app.exception_handler(ConnectionError)
async def database_unavailable(_: Request, exc: Exception) -> JSONResponse:
    log.error("api.database_error", error=repr(exc))
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "База даних тимчасово недоступна"},
    )


app.include_router(api_router)
app.include_router(ws.router)
