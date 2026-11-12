from fastapi import APIRouter
from app.api.v1.endpoints import health, auth, lead_finder

api_router = APIRouter()
api_router.include_router(health.router, tags=["Health"])
api_router.include_router(auth.router, prefix="/auth", tags=["Auth"])
api_router.include_router(lead_finder.router, prefix="/lead-finder", tags=["Lead Finder"])
