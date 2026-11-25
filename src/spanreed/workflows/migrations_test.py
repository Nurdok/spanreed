import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.test_utils import FakeRedis, mock_user_find_by_id
from spanreed.workflows import migrations
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.migrations import Conversion, keep_daily_note_properties
from spanreed.workflows.model import Step, TriggerSpec, Workflow
from spanreed.workflows.testing import fake_storage, registry_of

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


# -- putting right the notes that got ".md" twice ----------------------------


def _appends(path: str, **fields: Any) -> Workflow:
    """Return a workflow, turned off, that appends to the note at a path."""
    values: dict[str, Any] = {"id": "wf_1", "user_id": 4, "name": "Therapy"}
    values.update(fields)
    return Workflow(
        trigger=TriggerSpec(block="core.schedule", params={"every": 4}),
        steps=[
            Step(
                id="each",
                block="core.for-each",
                inputs={"items": []},
                steps=[
                    Step(
                        id="log",
                        block="obsidian.append-to-note",
                        inputs={"path": path, "text": "- a line\n"},
                    )
                ],
            )
        ],
        enabled=False,
        **values,
    )


def _mend(*workflows: Workflow, users: tuple[int, ...] = (4,)) -> tuple[Any, AsyncMock]:
    """Store the workflows and do what a start does. Return the engine and the bot."""
    engine = WorkflowEngine(registry_of())

    async def run() -> None:
        for workflow in workflows:
            await engine.store.save(workflow)
        await migrations.mend_note_paths(engine, users)

    with patch(
        "spanreed.workflows.migrations.UserInteractionApi", autospec=True
    ) as bot:
        bot.for_user = AsyncMock(return_value=bot)
        bot.notify = AsyncMock()
        asyncio.run(run())
    return engine, bot


def _path(engine: Any, workflow_id: str) -> str:
    stored = asyncio.run(engine.store.get(workflow_id))
    return str(stored.steps[0].steps[0].inputs["path"])


def test_a_note_that_got_md_twice_when_its_plugin_was_moved_gets_one(
    redis: FakeRedis,
) -> None:
    payment = _appends("Health/Therapy.md.md", recipe="recurring-payment-1")
    books = _appends(
        "{% set book = book.item %}Books/{{ book.title }}.md.md",
        id="wf_2",
        name="Add book note",
        recipe="lit-notes-add-book-note",
    )

    with fake_storage():
        engine, bot = _mend(payment, books)

        assert _path(engine, "wf_1") == "Health/Therapy.md"
        assert _path(engine, "wf_2") == (
            "{% set book = book.item %}Books/{{ book.title }}.md"
        )
        # The user is told once, about both.
        bot.notify.assert_awaited_once()
        text = bot.notify.await_args.args[0]
        assert text.startswith("“Add book note” and “Therapy” wrote to notes whose")
        assert "under the name with “.md.md”" in text

        # It is done once: a later start looks at nothing.
        asyncio.run(engine.store.save(payment))
        with patch.object(engine.store, "list_for_user", AsyncMock()) as listed:
            asyncio.run(migrations.mend_note_paths(engine, [4]))
        listed.assert_not_awaited()


def test_a_path_that_is_right_or_that_the_user_wrote_is_left_alone(
    redis: FakeRedis,
) -> None:
    right = _appends("Health/Dr. Cohen.md", recipe="recurring-payment-1")
    # Not from a plugin that was moved: its path is the user's own.
    own = _appends("Health/Therapy.md.md", id="wf_2", name="Mine")
    from_a_recipe = _appends(
        "Notes.md.md", id="wf_3", name="Chores", recipe="todoist-no-overdue"
    )

    with fake_storage():
        engine, bot = _mend(right, own, from_a_recipe)

        assert _path(engine, "wf_1") == "Health/Dr. Cohen.md"
        assert _path(engine, "wf_2") == "Health/Therapy.md.md"
        assert _path(engine, "wf_3") == "Notes.md.md"
        bot.notify.assert_not_awaited()
    assert asyncio.run(redis.get("workflows:mended:note-paths")) == "1"


def test_a_user_whose_workflows_cannot_be_read_is_tried_at_the_next_start(
    redis: FakeRedis,
) -> None:
    payment = _appends("Health/Therapy.md.md", recipe="recurring-payment-1")

    with fake_storage():
        engine = WorkflowEngine(registry_of())
        listed = engine.store.list_for_user

        async def list_for_user(user_id: int) -> list[Workflow]:
            if user_id == 9:
                raise ConnectionError("Redis is away")
            return await listed(user_id)

        async def run() -> None:
            await engine.store.save(payment)
            with patch.object(engine.store, "list_for_user", list_for_user):
                await migrations.mend_note_paths(engine, [9, 4])

        with patch(
            "spanreed.workflows.migrations.UserInteractionApi", autospec=True
        ) as bot:
            bot.for_user = AsyncMock(return_value=bot)
            bot.notify = AsyncMock()
            asyncio.run(run())

        # The other user was not held up.
        assert _path(engine, "wf_1") == "Health/Therapy.md"
    # Not marked as done, so the next start tries again.
    assert asyncio.run(redis.get("workflows:mended:note-paths")) is None
