import os
import redis.asyncio as redis


def make_redis() -> redis.Redis:
    return redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"]),
        db=int(os.environ["REDIS_DB_ID"]),
        username=os.environ["REDIS_USERNAME"],
        password=os.environ["REDIS_PASSWORD"],
        ssl=True,
        ssl_cert_reqs="none",
    )


redis_api = make_redis()
