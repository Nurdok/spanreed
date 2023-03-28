import telegram_bot
import todoist_nooverdue_main
import asyncio
from todoist import Todoist
import redis.asyncio as redis
import os


async def run_all_tasks():
    todoist_api = Todoist(os.environ['TODOIST_API_TOKEN'])

    redis_api = redis.Redis(host=os.environ["REDIS_HOST"],
                            port=int(os.environ["REDIS_PORT"]),
                            db=int(os.environ["REDIS_DB_ID"]),
                            username=os.environ["REDIS_USERNAME"],
                            password=os.environ["REDIS_PASSWORD"],
                            ssl=True,
                            ssl_cert_reqs="none")

    await asyncio.gather(
        todoist_nooverdue_main.main(todoist_api),
        telegram_bot.main(redis_api),
    )


def main():
    asyncio.run(run_all_tasks())


if __name__ == "__main__":
    main()