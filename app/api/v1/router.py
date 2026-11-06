from fastapi import APIRouter

from app.api.v1 import collector, health, market, offers, opportunities

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(market.router)
api_router.include_router(offers.router)
api_router.include_router(opportunities.router)
api_router.include_router(collector.router)
