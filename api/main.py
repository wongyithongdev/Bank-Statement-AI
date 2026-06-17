import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.db.connection import create_pool, close_pool
from api.db import queries
from api.routes.bankstatement import router as bankstatement_router
from api.services import queue as queue_svc
from api.services.docker_runner import start_worker

logger = logging.getLogger(__name__)


async def _dispatcher():
    """
    Background task: dequeue tasks from Redis and dispatch Docker workers,
    respecting the MAX_RPM sliding-window rate limit.
    """
    while True:
        try:
            r = queue_svc.get_redis()
            item = await r.brpop(queue_svc.QUEUE_KEY, timeout=2)
            if not item:
                continue
            _, task_id = item

            # If MiMo RPM limit reached, put the task back and wait
            while await queue_svc.current_rpm() >= queue_svc.max_rpm():
                await r.rpush(queue_svc.QUEUE_KEY, task_id)
                await asyncio.sleep(1)
                item = await r.brpop(queue_svc.QUEUE_KEY, timeout=2)
                if not item:
                    task_id = None
                    break
                _, task_id = item

            if not task_id:
                continue

            await queue_svc.record_dispatch(task_id)
            start_worker(task_id)  # detach=True — returns immediately

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Dispatcher error (task=%s): %s", locals().get("task_id"), exc)
            try:
                if task_id:
                    await queries.update_task_status(task_id, "failed", error=str(exc))
            except Exception:
                pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_pool()
    dispatcher_task = asyncio.create_task(_dispatcher())
    yield
    dispatcher_task.cancel()
    try:
        await dispatcher_task
    except asyncio.CancelledError:
        pass
    await close_pool()
    await queue_svc.close_redis()


app = FastAPI(
    title="Bank Statement AI API",
    version="1.0.0",
    description="Agentic PDF → Excel extraction with per-task Docker isolation",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(bankstatement_router)


@app.get("/health")
async def health():
    rpm = await queue_svc.current_rpm()
    qlen = await queue_svc.queue_length()
    return {"status": "ok", "queue_length": qlen, "rpm": rpm, "rpm_limit": queue_svc.max_rpm()}
