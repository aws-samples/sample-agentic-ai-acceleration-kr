"""
FastAPI server for the agent platform: threads/chat streaming, registry, harness,
knowledge, artifacts and Insights.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

# Import routers
from routes import health, threads, mcp_apps, mcp, auth, registry, harness, artifacts, knowledge, insights, config, settings
from core.config import COLLECTOR_ENABLED, COLLECTOR_INTERVAL_SECONDS
from core.dependencies import collector_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the insights collector with the server, stop it with the server.

    An asyncio task rather than a separate scheduler: the stack runs one server
    task, and the collector's own DDB lease keeps a second replica from doubling
    the work. Its passes run in a worker thread (`asyncio.to_thread`), so the
    blocking boto3 calls never stall request handling.
    """
    task = None
    if COLLECTOR_ENABLED and collector_service is not None:
        task = asyncio.create_task(collector_service.run_forever(COLLECTOR_INTERVAL_SECONDS))
        logger.info("Insights collector started (every %ss)", COLLECTOR_INTERVAL_SECONDS)
    elif COLLECTOR_ENABLED:
        logger.warning("COLLECTOR_ENABLED but USAGE_TABLE is unset; collector not started")
    try:
        yield
    finally:
        if task is not None:
            task.cancel()


app = FastAPI(
    title="Agent Platform API",
    description="Chat streaming to AgentCore Runtime and Harness agents, Agent Registry, knowledge bases, artifacts and Insights",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health.router)
app.include_router(config.router)
app.include_router(auth.router)
app.include_router(threads.router)
app.include_router(mcp_apps.router)
app.include_router(mcp.router)
app.include_router(registry.router)
app.include_router(harness.router)
app.include_router(artifacts.router)
app.include_router(knowledge.router)
app.include_router(insights.router)
app.include_router(settings.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
