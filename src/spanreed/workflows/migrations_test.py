import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.test_utils import FakeRedis, mock_user_find_by_id
from spanreed.workflows import migrations
from spanreed.workflows.engine import WorkflowEngine, digest
from spanreed.workflows.migrations import (
    Conversion,
    RenamedPlugin,
    keep_daily_note_properties,
)
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


# -- a plugin that another plugin took the place of ---------------------------

OLD_USERS = "config:plugin:name=lit-notes:users"
NEW_USERS = "config:plugin:name=google-books:users"
OLD_ID, NEW_ID = "lit-notes.search-books", "google-books.search-books"


def _searches(**fields: Any) -> Workflow:
    """Return a workflow that searches for books wherever a step can be."""
    values: dict[str, Any] = {"id": "wf_1", "user_id": 4, "name": "Add book note"}
    values.update(fields)
    return Workflow(
        trigger=TriggerSpec(block="core.command", params={}),
        steps=[
            Step(id="first", block=OLD_ID, inputs={"query": "dune"}),
            Step(
                id="each",
                block="core.for-each",
                inputs={"items": []},
                steps=[Step(id="inside", block=OLD_ID, inputs={"query": "emma"})],
            ),
            Step(
                id="whether",
                block="core.if",
                inputs={"condition": "{{ first.count }}"},
                steps=[Step(id="say", block="core.send-message", inputs={})],
                otherwise=[Step(id="again", block=OLD_ID, inputs={"query": "x"})],
            ),
        ],
        enabled=False,
        **values,
    )


def _with(data: Any, old: str, new: str) -> Any:
    """Return JSON data with one text in the place of another, wherever it is."""
    return json.loads(json.dumps(data).replace(f'"{old}"', f'"{new}"'))


def _blocks(steps: list[Step]) -> list[str]:
    """Return the block of each step and of the steps inside it, in order."""
    found = []
    for step in steps:
        found += [step.block, *_blocks(step.steps), *_blocks(step.otherwise)]
    return found


def _rename(
    *workflows: Workflow, users: tuple[int, ...] = (4,), engine: Any = None
) -> tuple[Any, AsyncMock]:
    """Store the workflows and do what a start does. Return the engine and the bot."""
    engine = engine or WorkflowEngine(registry_of())

    async def run() -> None:
        for workflow in workflows:
            await engine.store.save(workflow)
        moved = await migrations.rename_plugins(engine, users)
        # The engine would start here.
        await migrations.tell_about_renames(moved)

    with patch(
        "spanreed.workflows.migrations.UserInteractionApi", autospec=True
    ) as bot:
        bot.for_user = AsyncMock(return_value=bot)
        bot.notify = AsyncMock()
        asyncio.run(run())
    return engine, bot


def test_a_block_is_known_by_the_id_it_has_now() -> None:
    assert migrations.current_block(OLD_ID) == NEW_ID
    assert migrations.current_block(NEW_ID) == NEW_ID
    assert migrations.current_block("core.if") == "core.if"
    assert migrations.current_block("gone.block") == "gone.block"


def test_the_users_of_a_renamed_plugin_are_registered_to_the_new_one(
    redis: FakeRedis,
) -> None:
    asyncio.run(redis.sadd(OLD_USERS, 4, 7))
    asyncio.run(redis.sadd("user:4:plugins", "lit-notes", "obsidian"))
    asyncio.run(redis.sadd("user:7:plugins", "lit-notes"))

    with fake_storage():
        _, bot = _rename()

        assert {int(user) for user in asyncio.run(redis.smembers(NEW_USERS))} == {4, 7}
        assert asyncio.run(redis.smembers(OLD_USERS)) == set()
        assert asyncio.run(redis.smembers("user:4:plugins")) == {
            "google-books",
            "obsidian",
        }
        assert asyncio.run(redis.smembers("user:7:plugins")) == {"google-books"}
        # Each is told, once.
        assert bot.notify.await_count == 2
        assert bot.notify.await_args.args[0] == (
            "The plugin “Lit Notes” is called “Google Books” now. Searching "
            "Google Books is all it did since “Add book note” became a "
            "workflow. Your workflows go on as they were, and “Manage "
            "plugins” has it under the new name."
        )

        # A later start has nobody to move, and tells nobody.
        _, bot = _rename()
        bot.notify.assert_not_awaited()
        assert {int(user) for user in asyncio.run(redis.smembers(NEW_USERS))} == {4, 7}


def test_the_users_are_moved_before_the_engine_starts_and_told_after(
    redis: FakeRedis,
) -> None:
    asyncio.run(redis.sadd(OLD_USERS, 4, 7))
    engine = WorkflowEngine(registry_of())

    with fake_storage(), patch(
        "spanreed.workflows.migrations.UserInteractionApi", autospec=True
    ) as bot:
        bot.for_user = AsyncMock(return_value=bot)
        bot.notify = AsyncMock()
        moved = asyncio.run(migrations.rename_plugins(engine, [4, 7]))

        # Moving does not wait for a surface that can carry the telling:
        # the workflows start first.
        bot.for_user.assert_not_awaited()
        assert {int(user) for user in asyncio.run(redis.smembers(NEW_USERS))} == {4, 7}
        assert sorted(user_id for _, user_id in moved) == [4, 7]
        assert {plugin.canonical_name for plugin, _ in moved} == {"lit-notes"}

        asyncio.run(migrations.tell_about_renames(moved))
        assert bot.notify.await_count == 2


def test_a_user_who_cannot_be_told_does_not_stop_the_others(redis: FakeRedis) -> None:
    asyncio.run(redis.sadd(OLD_USERS, 4, 7))
    engine = WorkflowEngine(registry_of())
    told: list[int] = []

    async def for_user(user: Any) -> Any:
        if not told:
            told.append(-1)
            raise RuntimeError("no surface")
        told.append(user.id)
        return bot

    with fake_storage(), patch(
        "spanreed.workflows.migrations.UserInteractionApi", autospec=True
    ) as bot:
        bot.for_user = AsyncMock(side_effect=for_user)
        bot.notify = AsyncMock()
        moved = asyncio.run(migrations.rename_plugins(engine, [4, 7]))
        # Nothing is raised.
        asyncio.run(migrations.tell_about_renames(moved))

    assert len(told) == 2 and told[1] in (4, 7)
    bot.notify.assert_awaited_once()
    assert {int(user) for user in asyncio.run(redis.smembers(NEW_USERS))} == {4, 7}
    assert asyncio.run(redis.smembers(OLD_USERS)) == set()


def test_the_conversion_of_the_old_plugin_still_finds_its_users(
    redis: FakeRedis,
) -> None:
    # A user of Lit Notes from before it became a workflow, who was never
    # converted: the conversion reads its users from the registrations.
    asyncio.run(redis.sadd(OLD_USERS, 4))

    with fake_storage():
        _rename()

    (conversion,) = [
        conversion
        for conversion in migrations._conversions()
        if conversion.canonical_name == "lit-notes"
    ]
    assert asyncio.run(redis.smembers(OLD_USERS)) == set()
    assert asyncio.run(migrations._users_to_convert(conversion)) == [4]


def test_the_workflows_name_the_blocks_of_a_renamed_plugin_by_their_new_ids(
    redis: FakeRedis,
) -> None:
    other = _appends("Notes.md", id="wf_2", name="Other")

    with fake_storage():
        engine, bot = _rename(_searches(), other)

        stored = asyncio.run(engine.store.get("wf_1"))
        assert _blocks(stored.steps) == [
            NEW_ID,
            "core.for-each",
            NEW_ID,
            "core.if",
            "core.send-message",
            NEW_ID,
        ]
        # Nothing but the ids changed.
        was = _searches().to_data()
        assert stored.to_data() == _with(was, OLD_ID, NEW_ID)
        assert asyncio.run(engine.store.get("wf_2")).to_data() == other.to_data()
        # The workflows do the same, so there is nothing to tell.
        bot.notify.assert_not_awaited()

        # It is done once: a later start looks at nothing.
        with patch.object(engine.store, "list_for_user", AsyncMock()) as listed:
            asyncio.run(migrations.rename_plugins(engine, [4]))
        listed.assert_not_awaited()
    assert asyncio.run(redis.get("workflows:renamed:lit-notes")) == "1"


def test_a_trigger_of_a_renamed_plugin_is_renamed_too(redis: FakeRedis) -> None:
    renamed = RenamedPlugin(
        canonical_name="scale",
        name="Scale",
        new_canonical_name="fake",
        new_name="Fake",
        blocks={"scale.weighed": "fake.events"},
    )
    workflow = _searches()
    workflow.trigger = TriggerSpec(block="scale.weighed", params={"events": []})

    with fake_storage(), patch.object(migrations, "RENAMED_PLUGINS", [renamed]):
        engine, _ = _rename(workflow)
        stored = asyncio.run(engine.store.get("wf_1"))

    assert stored.trigger.block == "fake.events"
    assert stored.trigger.params == {"events": []}
    # The blocks of another plugin are left as they are.
    assert _blocks(stored.steps).count(OLD_ID) == 3
    assert asyncio.run(redis.get("workflows:renamed:scale")) == "1"


def test_a_run_that_was_cut_short_goes_on_in_the_renamed_workflow(
    redis: FakeRedis,
) -> None:
    workflow = _searches()
    engine = WorkflowEngine(registry_of())

    async def run() -> tuple[dict[str, Any], Workflow | None]:
        await engine.store.save(workflow)
        # A run of the workflow as it is, and one of how it was before.
        kept = {"workflow": digest(workflow), "manual": True, "started_at": "t"}
        await engine.store.keep_place("wf_1", "run_2", kept)
        await engine.store.keep_place("wf_1", "run_1", {**kept, "workflow": "older"})
        await migrations.rename_plugins(engine, [4])
        return await engine.store.places("wf_1"), await engine.store.get("wf_1")

    with fake_storage():
        places, stored = asyncio.run(run())

    assert stored is not None
    assert digest(stored) != digest(_searches())
    # The engine goes on with a run only in the workflow it started in.
    assert places["run_2"] == {
        "workflow": digest(stored),
        "manual": True,
        "started_at": "t",
    }
    assert places["run_1"]["workflow"] == "older"


def test_a_user_whose_workflows_cannot_be_renamed_is_tried_at_the_next_start(
    redis: FakeRedis,
) -> None:
    with fake_storage():
        engine = WorkflowEngine(registry_of())
        listed = engine.store.list_for_user

        async def list_for_user(user_id: int) -> list[Workflow]:
            if user_id == 9:
                raise ConnectionError("Redis is away")
            return await listed(user_id)

        with patch.object(engine.store, "list_for_user", list_for_user):
            _rename(_searches(), users=(9, 4), engine=engine)

        # The other user was not held up.
        stored = asyncio.run(engine.store.get("wf_1"))
        assert stored is not None
        assert _blocks(stored.steps).count(NEW_ID) == 3
        # Not marked as done, so the next start tries again.
        assert asyncio.run(redis.get("workflows:renamed:lit-notes")) is None

        _rename(users=(9, 4), engine=engine)
    assert asyncio.run(redis.get("workflows:renamed:lit-notes")) == "1"


def test_the_engine_starts_also_if_a_plugin_cannot_be_renamed(
    redis: FakeRedis,
) -> None:
    asyncio.run(redis.sadd(OLD_USERS, 4))

    with fake_storage(), patch.object(
        redis, "smembers", AsyncMock(side_effect=ConnectionError("Redis is away"))
    ):
        # Nothing is raised: the start goes on.
        engine, _ = _rename(_searches())
        stored = asyncio.run(engine.store.get("wf_1"))

    # No user is moved by halves: the next start moves them.
    assert {int(user) for user in asyncio.run(redis.smembers(OLD_USERS))} == {4}
    assert asyncio.run(redis.smembers(NEW_USERS)) == set()
    # The workflows did not wait for the users.
    assert _blocks(stored.steps).count(NEW_ID) == 3
