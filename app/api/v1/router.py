from fastapi import APIRouter

from app.api.v1 import (
    admin,
    ai,
    alerts,
    assistant,
    auth,
    collector,
    community,
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
api_router.include_router(assistant.router)
api_router.include_router(community.router)
api_router.include_router(admin.router)
