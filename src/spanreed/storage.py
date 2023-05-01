import os
import redis.asyncio as redis


def make_redis() -> redis.Redis:
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", ""),
        port=int(os.environ.get("REDIS_PORT", 0)),
        db=int(os.environ.get("REDIS_DB_ID", 0)),
        username=os.environ.get("REDIS_USERNAME", ""),
        password=os.environ.get("REDIS_PASSWORD", ""),
        ssl=True,
        ssl_cert_reqs="none",
    )


redis_api = make_redis()
