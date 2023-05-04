import asyncio
import redis.asyncio as redis
import logging
from typing import List

from spanreed.plugin import Plugin

from spanreed.apis.telegram_bot import TelegramBotPlugin
from spanreed.apis.todoist import TodoistPlugin
from spanreed.apis.obsidian_webhook import ObsidianWebhookPlugin

from spanreed.plugins.habit_tracker import HabitTrackerPlugin
from spanreed.plugins.recurring_payments import RecurringPaymentsPlugin
from spanreed.plugins.todoist_nooverdue import TodoistNoOverduePlugin
from spanreed.plugins.litnotes import LitNotesPlugin
from spanreed.plugins.plugin_manager import PluginManagerPlugin


def load_plugins() -> List[Plugin]:
    core_plugins: list[Plugin] = [
        TelegramBotPlugin(),
        PluginManagerPlugin(),
    ]

    # TODO: Load optional plugins dynamically.
    optional_plugins: list[Plugin] = [
        TodoistPlugin(),
        ObsidianWebhookPlugin(),
        HabitTrackerPlugin(),
        RecurringPaymentsPlugin(),
        TodoistNoOverduePlugin(),
        LitNotesPlugin(),
    ]

    return core_plugins + optional_plugins


async def run_all_tasks() -> None:
    plugins = load_plugins()

    logging.info(
        f"Running {len(plugins)} plugins: "
        f"{[plugin.canonical_name for plugin in plugins]}"
    )
    await asyncio.gather(*[plugin.run() for plugin in plugins])


def main() -> None:
    asyncio.run(run_all_tasks())


if __name__ == "__main__":
    main()
