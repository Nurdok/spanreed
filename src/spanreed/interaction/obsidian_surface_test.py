import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

from spanreed.interaction import commands, engine
from spanreed.interaction import obsidian_protocol as protocol
from spanreed.interaction.commands import PluginCommand
from spanreed.interaction.engine import PromptKind
from spanreed.interaction.obsidian_surface import ObsidianSurface
from spanreed.interaction.surface import Content, PromptSpec, SurfaceScope
from spanreed.plugin import Plugin
from spanreed.plugins.obsidian_surface import (
    ObsidianSurfacePlugin,
    build_command_list,
    publish_command_list,
)
from spanreed.test_utils import (
    AsyncContextManager,
    FakeRedis,
    mock_user_find_by_id,
)

USER_ID = 4


def setup_function() -> None:
    engine.reset_state_for_tests()
    commands.reset_registry_for_tests()


async def _register_instance(
    fake: FakeRedis, instance_id: str, *, enabled: bool = True, live: bool = True
) -> None:
    await fake.hset(
        protocol.instances_key(USER_ID),
        instance_id,
        json.dumps({"vault": "v", "enabled": enabled, "last_seen": time.time()}),
    )
    if live:
        await fake.set(protocol.presence_key(USER_ID, instance_id), "1")
    await fake.sadd(protocol.USERS_SET_KEY, USER_ID)


def _envelopes(fake: FakeRedis, instance_id: str) -> list[dict]:
    return [
        json.loads(raw)
        for raw in fake.lists.get(protocol.inbound_key(USER_ID, instance_id), [])
    ]


def _patch_redis():  # type: ignore[no-untyped-def]
    fake = FakeRedis()
    return fake, patch("spanreed.interaction.obsidian_protocol.redis_api", new=fake)


# ---------------------------------------------------------------------------
# Protocol: FIFO fan-out, seq, caps
# ---------------------------------------------------------------------------


def test_fan_out_is_fifo_with_monotonic_seq() -> None:
    fake, patcher = _patch_redis()

    async def run() -> None:
        await _register_instance(fake, "laptop")
        await _register_instance(fake, "desktop")
        await protocol.fan_out(USER_ID, "message", {"n": 1})
        await protocol.fan_out(USER_ID, "message", {"n": 2})

        for instance in ("laptop", "desktop"):
            envelopes = _envelopes(fake, instance)
            assert [e["payload"]["n"] for e in envelopes] == [1, 2]  # FIFO
            assert [e["seq"] for e in envelopes] == [1, 2]  # monotonic

    with patcher:
        asyncio.run(run())


def test_fan_out_skips_disabled_and_dead_instances() -> None:
    fake, patcher = _patch_redis()

    async def run() -> None:
        await _register_instance(fake, "live")
        await _register_instance(fake, "disabled", enabled=False)
        await _register_instance(fake, "dead", live=False)
        delivered = await protocol.fan_out(USER_ID, "message", {})
        assert delivered == ["live"]

    with patcher:
        asyncio.run(run())


def test_inbound_queue_capped_dropping_oldest() -> None:
    fake, patcher = _patch_redis()

    async def run() -> None:
        await _register_instance(fake, "laptop")
        for i in range(protocol.INBOUND_QUEUE_MAX + 10):
            await protocol.push_envelope(USER_ID, "laptop", "message", {"n": i})
        envelopes = _envelopes(fake, "laptop")
        assert len(envelopes) == protocol.INBOUND_QUEUE_MAX
        # Newest kept at the tail; oldest dropped.
        assert envelopes[-1]["payload"]["n"] == protocol.INBOUND_QUEUE_MAX + 9
        assert envelopes[0]["payload"]["n"] == 10

    with patcher:
        asyncio.run(run())


def test_inbox_capped_and_notification_fanned_out() -> None:
    fake, patcher = _patch_redis()
    surface = ObsidianSurface()
    user = mock_user_find_by_id(USER_ID)

    async def run() -> None:
        await _register_instance(fake, "laptop")
        for i in range(protocol.INBOX_MAX + 5):
            await surface.notify(user, Content(f"m{i}"))
        inbox = fake.lists[protocol.inbox_key(USER_ID)]
        assert len(inbox) == protocol.INBOX_MAX
        assert json.loads(inbox[-1])["text"] == f"m{protocol.INBOX_MAX + 4}"
        kinds = {e["kind"] for e in _envelopes(fake, "laptop")}
        assert kinds == {"notification"}

    with patcher:
        asyncio.run(run())


# ---------------------------------------------------------------------------
# Scope: origin-sticky targeting
# ---------------------------------------------------------------------------


def test_prompt_targets_only_origin_instance_for_sticky_flow() -> None:
    fake, patcher = _patch_redis()
    surface = ObsidianSurface()
    user = mock_user_find_by_id(USER_ID)

    async def run() -> None:
        await _register_instance(fake, "laptop")
        await _register_instance(fake, "desktop")

        async with engine.user_interaction(
            USER_ID,
            scope=SurfaceScope(origin_surface="obsidian", origin_instance="laptop"),
        ):
            pending = engine.create_prompt(USER_ID, PromptKind.CHOICE)
            spec = PromptSpec(
                interaction_id=pending.interaction_id,
                generation=0,
                kind=PromptKind.CHOICE,
                prompt=Content("pick"),
                choices=["a", "b"],
            )
            await surface.render_prompt(user, pending, spec)

        assert [e["kind"] for e in _envelopes(fake, "laptop")] == ["prompt"]
        assert _envelopes(fake, "desktop") == []

    with patcher:
        asyncio.run(run())


def test_broadcast_prompt_reaches_all_live_instances() -> None:
    fake, patcher = _patch_redis()
    surface = ObsidianSurface()
    user = mock_user_find_by_id(USER_ID)

    async def run() -> None:
        await _register_instance(fake, "laptop")
        await _register_instance(fake, "desktop")
        pending = engine.create_prompt(USER_ID, PromptKind.CHOICE)
        spec = PromptSpec(
            interaction_id=pending.interaction_id,
            generation=0,
            kind=PromptKind.CHOICE,
            prompt=Content("pick"),
            choices=["a"],
        )
        await surface.render_prompt(user, pending, spec)
        for instance in ("laptop", "desktop"):
            assert [e["kind"] for e in _envelopes(fake, instance)] == ["prompt"]

    with patcher:
        asyncio.run(run())


def test_surface_scope_excludes_other_surface() -> None:
    scope = SurfaceScope(origin_surface="telegram")
    assert scope.includes("telegram")
    assert not scope.includes("obsidian")
    assert SurfaceScope().includes("obsidian")


# ---------------------------------------------------------------------------
# Events: answers, invoke-command, sync
# ---------------------------------------------------------------------------


def _make_plugin() -> ObsidianSurfacePlugin:
    Plugin.reset_registry()
    plugin = ObsidianSurfacePlugin()
    plugin._surface = ObsidianSurface()
    return plugin


def test_answer_event_resolves_prompt_and_late_answer_gets_feedback() -> None:
    fake, patcher = _patch_redis()
    plugin = _make_plugin()
    user = mock_user_find_by_id(USER_ID)

    async def run() -> None:
        await _register_instance(fake, "laptop")
        pending = engine.create_prompt(USER_ID, PromptKind.CHOICE)

        await plugin.handle_event(
            user,
            "laptop",
            "answer",
            {"interaction_id": pending.interaction_id, "value": 1},
        )
        assert await pending.future == 1

        # Second answer from another instance: already-answered feedback.
        await _register_instance(fake, "desktop")
        await plugin.handle_event(
            user,
            "desktop",
            "answer",
            {"interaction_id": pending.interaction_id, "value": 0},
        )
        kinds = [e["kind"] for e in _envelopes(fake, "desktop")]
        assert kinds == ["already-answered"]

        # Unknown id (pre-restart): interaction-expired feedback.
        await plugin.handle_event(
            user,
            "desktop",
            "answer",
            {"interaction_id": "gone", "value": 0},
        )
        kinds = [e["kind"] for e in _envelopes(fake, "desktop")]
        assert kinds == ["already-answered", "interaction-expired"]

    with patcher:
        asyncio.run(run())


def test_invoke_command_runs_flow_at_high_priority_with_origin_scope() -> None:
    fake, patcher = _patch_redis()
    plugin = _make_plugin()
    user = mock_user_find_by_id(USER_ID)
    user.plugins = ["habit-tracker"]
    ran = asyncio.Event()

    async def callback(u: object) -> None:
        scope = engine.get_current_scope(USER_ID)
        assert isinstance(scope, SurfaceScope)
        assert scope.origin_instance == "laptop"
        ran.set()

    commands.register_command(
        "habit-tracker", PluginCommand(text="Track Habits", callback=callback)
    )

    mock_ui = MagicMock()
    mock_ui.user_interaction = lambda **kwargs: engine.user_interaction(
        USER_ID, **kwargs
    )

    async def run() -> None:
        await _register_instance(fake, "laptop")
        with patch("spanreed.plugins.obsidian_surface.UserInteractionApi") as mock_api:
            mock_api.for_user = AsyncMock(return_value=mock_ui)
            await plugin.handle_event(
                user,
                "laptop",
                "invoke-command",
                {"command_id": "habit-tracker.track-habits"},
            )
            await asyncio.wait_for(ran.wait(), timeout=1)

    with patcher:
        asyncio.run(run())


def test_invoke_unknown_or_unregistered_command_sends_flow_error() -> None:
    fake, patcher = _patch_redis()
    plugin = _make_plugin()
    user = mock_user_find_by_id(USER_ID)
    user.plugins = []  # not registered to any plugin

    commands.register_command(
        "habit-tracker", PluginCommand(text="Track Habits", callback=AsyncMock())
    )

    async def run() -> None:
        await _register_instance(fake, "laptop")
        for command_id in ("does-not-exist", "habit-tracker.track-habits"):
            await plugin.handle_event(
                user, "laptop", "invoke-command", {"command_id": command_id}
            )
        kinds = [e["kind"] for e in _envelopes(fake, "laptop")]
        assert kinds == ["flow-error", "flow-error"]

    with patcher:
        asyncio.run(run())


def test_toggle_off_cancels_origin_flow() -> None:
    fake, patcher = _patch_redis()
    plugin = _make_plugin()
    user = mock_user_find_by_id(USER_ID)

    async def run() -> None:
        started = asyncio.Event()

        async def flow() -> None:
            started.set()
            await asyncio.sleep(30)

        task = engine.start_user_flow(USER_ID, flow(), origin_instance="laptop")
        await started.wait()

        await plugin.handle_event(user, "laptop", "instance-update", {"enabled": False})
        await asyncio.sleep(0.01)
        assert task.cancelled()

    with patcher:
        asyncio.run(run())


def test_sync_publishes_command_list_and_replies_with_fresh_version() -> None:
    # A brand-new instance syncs before any registry change has fired for
    # its user; the sync itself must create the palette read-model.
    fake, patcher = _patch_redis()
    plugin = _make_plugin()
    user = mock_user_find_by_id(USER_ID)
    user.plugins = ["habit-tracker"]
    commands.register_command(
        "habit-tracker", PluginCommand(text="Track Habits", callback=AsyncMock())
    )

    async def run() -> None:
        await _register_instance(fake, "laptop")
        assert protocol.command_list_key(USER_ID) not in fake.strings
        with patch("spanreed.plugins.obsidian_surface.redis_api", new=fake):
            await plugin.handle_event(user, "laptop", "sync", {})
        stored = json.loads(fake.strings[protocol.command_list_key(USER_ID)])
        envelopes = _envelopes(fake, "laptop")
        assert [e["kind"] for e in envelopes] == [
            "commands-updated",
            "sync-reply",
        ]
        reply = envelopes[-1]["payload"]
        assert reply["command_list_version"] == stored["version"]

    with patcher:
        asyncio.run(run())


# ---------------------------------------------------------------------------
# Command-list publishing
# ---------------------------------------------------------------------------


def test_command_list_version_is_content_hash() -> None:
    user = mock_user_find_by_id(USER_ID)
    user.plugins = ["habit-tracker"]
    commands.register_command(
        "habit-tracker", PluginCommand(text="Track Habits", callback=AsyncMock())
    )

    first = build_command_list(user)
    second = build_command_list(user)
    assert first["version"] == second["version"]  # stable for same content
    assert first["commands"] == [
        {
            "id": "habit-tracker.track-habits",
            "plugin": "habit-tracker",
            "text": "Track Habits",
        }
    ]

    commands.register_command(
        "habit-tracker", PluginCommand(text="Add Habit", callback=AsyncMock())
    )
    assert build_command_list(user)["version"] != first["version"]


def test_publish_command_list_sets_key_and_nudges_instances() -> None:
    fake, protocol_patcher = _patch_redis()
    user = mock_user_find_by_id(USER_ID)
    user.plugins = ["habit-tracker"]
    commands.register_command(
        "habit-tracker", PluginCommand(text="Track Habits", callback=AsyncMock())
    )

    async def run() -> None:
        await _register_instance(fake, "laptop")
        with patch("spanreed.plugins.obsidian_surface.redis_api", new=fake):
            version = await publish_command_list(user)
        stored = json.loads(fake.strings[protocol.command_list_key(USER_ID)])
        assert stored["version"] == version
        assert [e["kind"] for e in _envelopes(fake, "laptop")] == ["commands-updated"]

    with protocol_patcher:
        asyncio.run(run())


# ---------------------------------------------------------------------------
# GC
# ---------------------------------------------------------------------------


def test_gc_drops_instances_older_than_30_days() -> None:
    fake, patcher = _patch_redis()

    async def run() -> None:
        await _register_instance(fake, "fresh")
        await fake.hset(
            protocol.instances_key(USER_ID),
            "stale",
            json.dumps({"enabled": True, "last_seen": time.time() - 40 * 24 * 3600}),
        )
        dropped = await protocol.gc_dead_instances(USER_ID)
        assert dropped == ["stale"]
        remaining = await protocol.get_instances(USER_ID)
        assert set(remaining) == {"fresh"}

    with patcher:
        asyncio.run(run())
