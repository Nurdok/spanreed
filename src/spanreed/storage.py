from typing import TypedDict
import os
import redis.asyncio as redis
from redis.exceptions import BusyLoadingError, ConnectionError, TimeoutError
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff


def make_redis() -> redis.Redis:
    class CommonParams(TypedDict):
        ssl_cert_reqs: str
        retry_on_error: list[type[Exception]]
        retry: Retry
        retry_on_timeout: bool

    common_params = CommonParams(
        ssl_cert_reqs="none",
        retry_on_error=[
            ConnectionError,
            TimeoutError,
            BusyLoadingError,
        ],
        retry=Retry(ExponentialBackoff(), 3),
        retry_on_timeout=True,
    )

    if os.environ.get("REDIS_URL", ""):
        return redis.from_url(
            os.environ.get("REDIS_URL", ""),
            **common_params,
        )
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", ""),
        port=int(os.environ.get("REDIS_PORT", 0)),
        db=int(os.environ.get("REDIS_DB_ID", 0)),
        username=os.environ.get("REDIS_USERNAME", ""),
        password=os.environ.get("REDIS_PASSWORD", ""),
        ssl=True,
        **common_params,
    )


redis_api = make_redis()
