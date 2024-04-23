import asyncio
import traceback
import logging
from typing import List

from spanreed.plugin import Plugin

from spanreed.apis.telegram_bot import TelegramBotPlugin
from spanreed.plugins.todoist import TodoistPlugin
from spanreed.apis.obsidian_webhook import ObsidianWebhookPlugin
from spanreed.apis.obsidian import ObsidianPlugin

from spanreed.plugins.habit_tracker import HabitTrackerPlugin
from spanreed.plugins.recurring_payments import RecurringPaymentsPlugin
from spanreed.plugins.todoist_nooverdue import TodoistNoOverduePlugin
from spanreed.plugins.litnotes import LitNotesPlugin
from spanreed.plugins.plugin_manager import PluginManagerPlugin
from spanreed.plugins.web_ui import WebUiPlugin
from spanreed.plugins.spanreed_monitor import SpanreedMonitorPlugin
from spanreed.plugins.timekiller import TimekillerPlugin


def setup_logger() -> None:
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logging.getLogger().setLevel(logging.DEBUG)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)


def load_plugins() -> List[Plugin]:
    core_plugins: list[Plugin] = [
        TelegramBotPlugin(),
        PluginManagerPlugin(),
    ]

    # TODO: Load optional plugins dynamically.
    optional_plugins: list[Plugin] = [
        ObsidianPlugin(),
        TodoistPlugin(),
        ObsidianWebhookPlugin(),
        HabitTrackerPlugin(),
        RecurringPaymentsPlugin(),
        TodoistNoOverduePlugin(),
        LitNotesPlugin(),
        WebUiPlugin(),
        SpanreedMonitorPlugin(),
        TimekillerPlugin(),
    ]

    return core_plugins + optional_plugins


async def run_all_tasks() -> None:
    plugins = load_plugins()

    logging.info(
        f"Running {len(plugins)} plugins: "
        f"{[plugin.canonical_name() for plugin in plugins]}"
    )

    async with asyncio.TaskGroup() as tg:
        for plugin in plugins:
            tg.create_task(plugin.run())

    logging.error("All plugins have stopped. Exiting.")


def main() -> None:
    setup_logger()
    try:
        asyncio.run(run_all_tasks())
    except BaseException as e:
        logging.info(f"Exception caught, storing in Redis: {e}")
        from spanreed.storage import make_redis

        asyncio.run(
            make_redis().lpush(
                SpanreedMonitorPlugin.EXCEPTION_QUEUE_NAME,
                "".join(traceback.format_exception(type(e), e, None)),
            )
        )
        raise


if __name__ == "__main__":
    main()
