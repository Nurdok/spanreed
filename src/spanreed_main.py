import spanreed
import asyncio
from spanreed.apis.todoist import Todoist
import redis.asyncio as redis
import os
import logging
from typing import List

from spanreed.plugin import Plugin
from spanreed.user import User

from spanreed.apis.telegram_bot import TelegramBotPlugin

from spanreed.plugins.habit_tracker import HabitTrackerPlugin
from spanreed.plugins.therapy import TherapyPlugin
from spanreed.plugins.todoist_nooverdue import TodoistNoOverduePlugin
from spanreed.plugins.litnotes import LitNotesPlugin
from spanreed.plugins.admin import AdminPlugin


def load_plugins(redis_api: redis.Redis) -> List[Plugin]:
    core_plugins = [
        TelegramBotPlugin(redis_api=redis_api),
    ]

    # TODO: Load optional plugins dynamically.
    optional_plugins = [
        HabitTrackerPlugin(redis_api=redis_api),
        TherapyPlugin(redis_api=redis_api),
        TodoistNoOverduePlugin(redis_api=redis_api),
        LitNotesPlugin(redis_api=redis_api),
        AdminPlugin(redis_api=redis_api),
    ]

    return core_plugins + optional_plugins


async def run_all_tasks():
    redis_api = redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"]),
        db=int(os.environ["REDIS_DB_ID"]),
        username=os.environ["REDIS_USERNAME"],
        password=os.environ["REDIS_PASSWORD"],
        ssl=True,
        ssl_cert_reqs="none",
    )

    User.redis_api = redis_api
    plugins = load_plugins(redis_api=redis_api)

    logging.info(
        f"Running {len(plugins)} plugins: {[plugin.canonical_name for plugin in plugins]}"
    )
    await asyncio.gather(*[plugin.run() for plugin in plugins])


def main():
    asyncio.run(run_all_tasks())


if __name__ == "__main__":
    main()
