"""Tests of what every plugin has."""

import asyncio
from unittest.mock import patch

from spanreed.plugin import Plugin
from spanreed.test_utils import FakeRedis


class _Quiet(Plugin):
    @classmethod
    def name(cls) -> str:
        return "Quiet One"


def test_a_plugin_runs_nothing_by_itself() -> None:
    Plugin.reset_registry()
    plugin = _Quiet()

    # It returns at once: there is no loop for each user any more.
    asyncio.run(asyncio.wait_for(plugin.run(), timeout=1))


def test_a_plugin_knows_the_ids_of_its_users() -> None:
    Plugin.reset_registry()
    plugin = _Quiet()
    redis = FakeRedis()

    # As Redis gives them, and as the stand-in for it in tests does.
    redis.sets["config:plugin:name=quiet-one:users"] = {b"4", "9"}

    with patch("spanreed.plugin.redis_api", new=redis):
        assert asyncio.run(plugin.user_ids()) == {4, 9}
