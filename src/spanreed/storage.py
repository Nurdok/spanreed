import os
import redis.asyncio as redis
from redis.exceptions import BusyLoadingError, ConnectionError, TimeoutError
from redis.retry import ExponentialBackoff, Retry


def make_redis() -> redis.Redis:
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", ""),
        port=int(os.environ.get("REDIS_PORT", 0)),
        db=int(os.environ.get("REDIS_DB_ID", 0)),
        username=os.environ.get("REDIS_USERNAME", ""),
        password=os.environ.get("REDIS_PASSWORD", ""),
        ssl=True,
        ssl_cert_reqs="none",
        retry_on_error=[ConnectionError, TimeoutError, BusyLoadingError],
        retry=Retry(ExponentialBackoff(), 3),
        retry_on_timeout=True,
    )


redis_api = make_redis()
