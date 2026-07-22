from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.api.agent_views import router as agent_views_router
from backend.api.benchmarks import router as benchmarks_router
from backend.api.config import router as config_router
from backend.api.events import router as events_router
from backend.api.health import router as health_router
from backend.api.investigations import router as investigations_router
from backend.api.runtime_runs import router as runtime_runs_router
from backend.services.container import get_container


@asynccontextmanager
async def lifespan(_app: FastAPI):
    container = get_container()
    await container.startup()
    try:
        yield
    finally:
        await container.shutdown()


app = FastAPI(title="DiagOps", version="0.1.0", lifespan=lifespan)
app.include_router(health_router)
app.include_router(config_router)
app.include_router(events_router)
app.include_router(investigations_router)
app.include_router(agent_views_router)
app.include_router(benchmarks_router)
app.include_router(runtime_runs_router)
