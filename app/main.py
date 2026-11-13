import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from postgrest.exceptions import APIError

from app.core.config import settings
from app.api.v1.api import api_router
from app.services.lead_finder.runner import agent_loop, lead_finder_loop


@asynccontextmanager
async def lifespan(app: FastAPI):
    lead_finder = asyncio.create_task(lead_finder_loop())
    agent = asyncio.create_task(agent_loop())
    yield
    lead_finder.cancel()
    agent.cancel()


app = FastAPI(
    lifespan=lifespan,
    title=settings.PROJECT_NAME,
    version="1.0.0",
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url=f"{settings.API_V1_STR}/docs",
    redoc_url=f"{settings.API_V1_STR}/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_STR)

SCHEMA_ERROR_CODES = {"PGRST205", "PGRST204", "42P01", "42703"}


@app.exception_handler(APIError)
async def supabase_error_handler(request: Request, exc: APIError):
    if exc.code in SCHEMA_ERROR_CODES:
        return JSONResponse(status_code=503, content={"detail": "Database setup incomplete: run the migration SQL in the Supabase SQL Editor."})
    return JSONResponse(status_code=502, content={"detail": f"Database error: {exc.message}"})


@app.get("/")
def root():
    return {"message": "Lead Finder API is running", "docs": f"{settings.API_V1_STR}/docs"}
