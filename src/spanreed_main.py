import spanreed
import asyncio
from spanreed.apis.todoist import Todoist
import redis.asyncio as redis
import os
import logging
from typing import List

from spanreed.plugin import Plugin

from spanreed.plugins.telegram_bot import TelegramBotPlugin
from spanreed.plugins.therapy import TherapyPlugin
from spanreed.plugins.todoist_nooverdue import TodoistNoOverduePlugin
from spanreed.redis_logger import RedisPubSubHandler
from spanreed.ui.main import run_server


def setup_redis() -> redis.Redis:
    return redis.Redis(host=os.environ["REDIS_HOST"],
                            port=int(os.environ["REDIS_PORT"]),
                            db=int(os.environ["REDIS_DB_ID"]),
                            username=os.environ["REDIS_USERNAME"],
                            password=os.environ["REDIS_PASSWORD"],
                            ssl=True,
                            ssl_cert_reqs="none")


def setup_logger(redis_api: redis.Redis):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    handler = RedisPubSubHandler(channel='logs', redis_api=redis_api)
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)


def load_plugins(redis_api: redis.Redis) -> List[Plugin]:
    return [
        TelegramBotPlugin(redis_api=redis_api),
        TherapyPlugin(redis_api=redis_api),
        TodoistNoOverduePlugin(redis_api=redis_api),
    ]


async def run_all_tasks(redis_api: redis.Redis):
    plugins = load_plugins(redis_api=redis_api)
    logging.info(f"Running {len(plugins)} plugins: {[plugin.canonical_name for plugin in plugins]}")
    await asyncio.gather(*[plugin.run() for plugin in plugins] + [run_server(redis_api=redis_api)])


def main():
    redis_api = setup_redis()
    setup_logger(redis_api=redis_api)
    asyncio.run(run_all_tasks(redis_api=redis_api))


if __name__ == "__main__":
    main()
