"""
Redis-backed task queue with RPM rate limiting.

Tasks are enqueued by the API and dequeued by the background dispatcher
in api/main.py, which enforces MAX_RPM before spawning Docker workers.
"""
import time
import redis.asyncio as aioredis
from api.config import settings

_redis: aioredis.Redis | None = None

QUEUE_KEY = "bankstatement:queue"   # List — LPUSH to enqueue, BRPOP to dequeue
RPM_KEY   = "bankstatement:rpm"     # Sorted set — score = dispatch timestamp (ms)
MAX_RPM   = 85                       # Maximum tasks dispatched per 60-second window


def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None


async def enqueue_task(task_id: str) -> None:
    """Push task_id onto the left of the queue (FIFO: LPUSH + BRPOP)."""
    await get_redis().lpush(QUEUE_KEY, task_id)


async def queue_length() -> int:
    """Return the number of tasks currently waiting in the queue."""
    return await get_redis().llen(QUEUE_KEY)


async def current_rpm() -> int:
    """
    Return the number of tasks dispatched in the last 60 seconds.
    Uses a Redis sorted set with millisecond timestamps as scores.
    Atomically prunes stale entries before counting.
    """
    r = get_redis()
    now_ms = int(time.time() * 1000)
    pipe = r.pipeline()
    pipe.zremrangebyscore(RPM_KEY, "-inf", now_ms - 60_000)
    pipe.zcard(RPM_KEY)
    _, count = await pipe.execute()
    return count


async def record_dispatch(task_id: str) -> None:
    """Record that a task was dispatched now (for RPM accounting)."""
    now_ms = int(time.time() * 1000)
    r = get_redis()
    await r.zadd(RPM_KEY, {task_id: now_ms})
    await r.expire(RPM_KEY, 120)   # auto-expire key 2 min after last write
