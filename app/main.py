import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.utils.logger import setup_logging
from app.routes import health, ifood_webhooks, orders

setup_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler - startup and shutdown events."""
    logger.info("=" * 60)
    logger.info("iFood-Odoo Middleware starting up")
    logger.info("Environment: %s", settings.app_env)
    logger.info("Host: %s:%s", settings.app_host, settings.app_port)
    logger.info("iFood API Base URL: %s", settings.ifood_api_base_url)
    logger.info("Odoo URL: %s", settings.odoo_url)
    logger.info("=" * 60)
    yield
    logger.info("iFood-Odoo Middleware shutting down")


app = FastAPI(
    title="iFood-Odoo Middleware",
    description="Middleware that integrates iFood orders with Odoo ERP",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
if settings.is_production:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # In production, replace with specific origins
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Include routers
app.include_router(health.router, tags=["Health"])
app.include_router(ifood_webhooks.router, tags=["Webhooks"])
app.include_router(orders.router, prefix="/orders", tags=["Orders"])


@app.get("/", tags=["Root"])
async def root():
    """Root endpoint."""
    return {
        "service": "iFood-Odoo Middleware",
        "version": "1.0.0",
        "status": "running",
    }
