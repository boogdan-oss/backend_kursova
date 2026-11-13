from fastapi import APIRouter

from app.api.v1 import (
    ai,
    alerts,
    auth,
    collector,
    health,
    market,
    me,
    offers,
    opportunities,
    watchlist,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(market.router)
api_router.include_router(offers.router)
api_router.include_router(opportunities.router)
api_router.include_router(collector.router)
api_router.include_router(auth.router)
api_router.include_router(me.router)
api_router.include_router(watchlist.router)
api_router.include_router(alerts.router)
api_router.include_router(ai.router)
