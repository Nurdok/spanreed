import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.test_utils import FakeRedis, mock_user_find_by_id
from spanreed.workflows import migrations
from spanreed.workflows.migrations import Conversion, keep_daily_note_properties
from spanreed.workflows.model import Step, TriggerSpec, Workflow

USERS_KEY = "config:plugin:name=scale:users"


@pytest.fixture(autouse=True)
def redis() -> Iterator[FakeRedis]:
    fake = FakeRedis()
    with (
        patch("spanreed.workflows.migrations.redis_api", new=fake),
        patch(
            "spanreed.workflows.migrations.User.find_by_id",
            new=AsyncMock(side_effect=mock_user_find_by_id),
        ),
    ):
        yield fake


class _Scale:
    """A made-up plugin that became recipes. It keeps who was converted."""

    def __init__(self) -> None:
        self.converted: list[int] = []
        # The users whose conversion fails.
        self.broken: set[int] = set()

    async def convert(self, engine: Any, user: Any) -> list[Workflow]:
        if user.id in self.broken:
            raise RuntimeError("no settings")
        self.converted.append(user.id)
        return []

    def start(self) -> None:
        """Do what a start of Spanreed does."""
        conversion = Conversion(
            canonical_name="scale", name="Scale", convert=self.convert
        )
        with patch(
            "spanreed.workflows.migrations._conversions", return_value=[conversion]
        ):
            asyncio.run(migrations.convert_plugins(engine=None))  # type: ignore[arg-type]


def test_the_users_a_plugin_has_are_converted_once(redis: FakeRedis) -> None:
    scale = _Scale()
    asyncio.run(redis.sadd(USERS_KEY, 4, 7))

    scale.start()
    scale.start()

    assert scale.converted == [4, 7]


def test_a_user_who_joins_the_plugin_later_is_not_converted(redis: FakeRedis) -> None:
    scale = _Scale()
    asyncio.run(redis.sadd(USERS_KEY, 4))
    scale.start()

    # A new user never had the old behavior, so there is nothing to keep.
    asyncio.run(redis.sadd(USERS_KEY, 7))
    scale.start()

    assert scale.converted == [4]


def test_a_plugin_with_no_users_converts_nobody_later(redis: FakeRedis) -> None:
    scale = _Scale()
    scale.start()

    asyncio.run(redis.sadd(USERS_KEY, 7))
    scale.start()

    assert scale.converted == []


def test_a_user_who_could_not_be_converted_is_tried_at_the_next_start(
    redis: FakeRedis,
) -> None:
    scale = _Scale()
    scale.broken = {4}
    asyncio.run(redis.sadd(USERS_KEY, 4, 7))

    scale.start()
    assert scale.converted == [7]

    scale.broken = set()
    scale.start()
    scale.start()

    assert scale.converted == [7, 4]


def test_a_user_converted_by_an_earlier_version_is_not_converted_again(
    redis: FakeRedis,
) -> None:
    scale = _Scale()
    asyncio.run(redis.sadd(USERS_KEY, 4, 7))
    # What the first version of the conversions kept: who is done.
    asyncio.run(redis.sadd("workflows:converted:scale", "4"))

    scale.start()

    assert scale.converted == [7]


# -- the daily note properties a plugin wrote ----------------------------------


def _workflow() -> Workflow:
    return Workflow(
        id="wf_1",
        user_id=4,
        name="Scale → daily note",
        trigger=TriggerSpec(block="scale.weighed", params={}),
        steps=[
            Step(id="today", block="obsidian.create-daily-note", inputs={}),
            Step(
                id="write",
                block="obsidian.set-note-properties",
                inputs={"properties": {"weight": "w", "fat-mass": "f"}},
            ),
        ],
    )


def test_a_property_that_was_off_has_no_row() -> None:
    workflow = _workflow()

    keep_daily_note_properties({"weight": True, "fat_mass": False})(workflow)

    assert workflow.steps[1].inputs["properties"] == {"weight": "w"}
    assert workflow.enabled


def test_a_plugin_that_wrote_nothing_gives_a_workflow_that_is_off() -> None:
    workflow = _workflow()

    keep_daily_note_properties({"weight": False, "fat_mass": False})(workflow)

    # Every row stays, so that turning it on does what the plugin could.
    assert workflow.steps[1].inputs["properties"] == {"weight": "w", "fat-mass": "f"}
    assert not workflow.enabled


@pytest.mark.parametrize("fields", [None, {}, "nonsense", {"gone": False}])
def test_a_choice_that_was_never_saved_keeps_every_row(fields: Any) -> None:
    workflow = _workflow()

    keep_daily_note_properties(fields)(workflow)

    assert workflow.steps[1].inputs["properties"] == {"weight": "w", "fat-mass": "f"}
    assert workflow.enabled
