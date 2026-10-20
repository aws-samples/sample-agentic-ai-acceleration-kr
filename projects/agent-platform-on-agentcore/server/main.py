"""
FastAPI server for the agent platform: threads/chat streaming, registry, harness,
knowledge, artifacts and Insights.
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import anyio.to_thread

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

# Import routers
from routes import health, threads, mcp_apps, mcp, auth, registry, harness, artifacts, knowledge, insights, config, settings, org
from core.config import BLOCKING_THREADS, COLLECTOR_ENABLED, COLLECTOR_INTERVAL_SECONDS
from core.dependencies import collector_service, streaming_service, verify_tables


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the insights collector with the server, stop it with the server.

    An asyncio task rather than a separate scheduler: the stack runs one server
    task, and the collector's own DDB lease keeps a second replica from doubling
    the work. Its passes run in a worker thread (`asyncio.to_thread`), so the
    blocking boto3 calls never stall request handling.

    Also drains in-flight chat runs on shutdown; see services/run_broker.py.

    Verifies the DynamoDB tables first. Raising here fails uvicorn's startup, so
    a misconfigured task exits before it serves — the check that used to happen
    as a side effect of importing the repositories.

    Sizes the two thread pools blocking work runs on. Both defaults scale with
    the CPU count, and a 1-vCPU task got 5-6 `asyncio.to_thread` workers (every
    chat turn's approval, binding and thread setup) and 40 for sync handlers: at
    300 concurrent new chats the setup queued past the ALB's 60 s and 208 turns
    got 504 before they started (bst load test, 2026-10-18). The work is boto3
    I/O, so the pools follow the DynamoDB client's connection pool, not the CPUs.
    """
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=BLOCKING_THREADS, thread_name_prefix="blocking")
    )
    anyio.to_thread.current_default_thread_limiter().total_tokens = BLOCKING_THREADS
    await asyncio.to_thread(verify_tables)
    task = None
    if COLLECTOR_ENABLED and collector_service is not None:
        task = asyncio.create_task(collector_service.run_forever(COLLECTOR_INTERVAL_SECONDS))
        logger.info("Insights collector started (every %ss)", COLLECTOR_INTERVAL_SECONDS)
    elif COLLECTOR_ENABLED:
        logger.warning("COLLECTOR_ENABLED but USAGE_TABLE is unset; collector not started")
    heartbeat = None
    if streaming_service.run_broker.leases is not None:
        heartbeat = asyncio.create_task(streaming_service.run_broker.heartbeat_forever())
    try:
        yield
    finally:
        # ECS stops a task with SIGTERM and uvicorn runs this before exiting.
        # Each run's generator writes `interrupted` on cancellation, so a
        # redeploy leaves no thread stuck on `busy` with nothing behind it.
        await streaming_service.run_broker.shutdown()
        if heartbeat is not None:
            heartbeat.cancel()
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
app.include_router(org.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
