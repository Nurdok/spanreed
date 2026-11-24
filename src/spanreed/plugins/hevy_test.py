import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.apis.hevy import Exercise, Workout, WorkoutSet
from spanreed.apis.obsidian import ObsidianApiTimeoutError, ObsidianPlugin
from spanreed.plugin import Plugin
from spanreed.plugins.hevy import (
    SINCE_KEY,
    SYNCED_IDS_KEY,
    HevyPlugin,
    UserConfig,
)
from spanreed.test_utils import (
    EndPluginRun,
    mock_user_find_by_id,
    patch_obsidian,
    patch_user_interaction,
)


def _plugin_with_store() -> tuple[HevyPlugin, dict]:
    """Return a plugin whose config and per-user data are kept in memory."""
    Plugin.reset_registry()
    plugin = HevyPlugin()
    store: dict = {}

    async def _get(user: Any, key: str) -> Any:
        return store.get(key)

    async def _set(user: Any, key: str, data: str) -> None:
        store[key] = data

    async def _get_config(user: Any) -> UserConfig:
        return UserConfig(api_key="key", vault_dir="Fitness/Sets")

    plugin.get_user_data = _get  # type: ignore[method-assign]
    plugin.set_user_data = _set  # type: ignore[method-assign]
    plugin.get_config = _get_config  # type: ignore[method-assign]
    return plugin, store


def _workout() -> Workout:
    """Return a workout of one exercise with two sets."""
    sets = [
        WorkoutSet(
            index=index,
            type="normal",
            weight_kg=60.0,
            reps=reps,
            distance_meters=None,
            duration_seconds=None,
            rpe=None,
        )
        for index, reps in enumerate([8, 6])
    ]
    return Workout(
        id="w-1",
        title="Push day",
        description="",
        start_time="2026-05-09T08:00:00Z",
        end_time="2026-05-09T09:00:00Z",
        updated_at="2026-05-09T09:00:00Z",
        created_at="2026-05-09T09:00:00Z",
        exercises=[
            Exercise(
                index=0,
                title="Bench Press",
                notes="",
                exercise_template_id="t-1",
                supersets_id=None,
                sets=sets,
            )
        ],
    )


def _sync(plugin: HevyPlugin, mock_bot: AsyncMock, workouts: list[Workout]) -> int:
    """Run one sync against a Hevy API that returns ``workouts``."""
    with patch("spanreed.plugins.hevy.HevyApi", autospec=True) as mock_hevy:
        mock_hevy.for_user = AsyncMock(return_value=mock_hevy)
        mock_hevy.get_updated_workouts_since = AsyncMock(return_value=workouts)
        return asyncio.run(plugin._sync(mock_user_find_by_id(4), mock_bot))


def test_requires_the_obsidian_plugin() -> None:
    assert HevyPlugin.get_prerequisites() == [ObsidianPlugin]


@patch_user_interaction("spanreed.plugins.hevy")
@patch_obsidian("spanreed.plugins.hevy")
def test_sync_writes_one_note_per_set(
    mock_obsidian: AsyncMock, mock_bot: AsyncMock
) -> None:
    plugin, store = _plugin_with_store()

    assert _sync(plugin, mock_bot, [_workout()]) == 1

    calls = mock_obsidian.write_binary_file.await_args_list
    assert [c.args[0] for c in calls] == [
        "Fitness/Sets/2026-05-09/Bench Press set 1.md",
        "Fitness/Sets/2026-05-09/Bench Press set 2.md",
    ]
    for c in calls:
        # A note is written whole and replaces one already at its path, so
        # writing a workout again leaves one copy of every set.
        assert c.kwargs == {"overwrite": True}
        assert c.args[1].startswith(b"---\n")
        assert b"workout_id: w-1" in c.args[1]
    assert "w-1" in store[SYNCED_IDS_KEY]
    assert SINCE_KEY in store
    mock_bot.notify.assert_awaited_once()


@patch_user_interaction("spanreed.plugins.hevy")
@patch_obsidian("spanreed.plugins.hevy")
def test_failed_write_leaves_the_workout_for_the_next_sync(
    mock_obsidian: AsyncMock, mock_bot: AsyncMock
) -> None:
    plugin, store = _plugin_with_store()

    # Obsidian stops answering after the first note of the workout.
    mock_obsidian.write_binary_file.side_effect = [None, ObsidianApiTimeoutError()]
    with pytest.raises(ObsidianApiTimeoutError):
        _sync(plugin, mock_bot, [_workout()])

    assert SYNCED_IDS_KEY not in store
    assert SINCE_KEY not in store
    mock_bot.notify.assert_not_awaited()

    # The next sync writes every note of the workout, the first one again.
    mock_obsidian.write_binary_file.reset_mock(side_effect=True)
    assert _sync(plugin, mock_bot, [_workout()]) == 1
    assert mock_obsidian.write_binary_file.await_count == 2
    assert "w-1" in store[SYNCED_IDS_KEY]


@patch_user_interaction("spanreed.plugins.hevy")
def test_run_for_user_survives_a_failed_sync(mock_bot: AsyncMock) -> None:
    plugin, _ = _plugin_with_store()
    plugin._sync = AsyncMock(  # type: ignore[method-assign]
        side_effect=ObsidianApiTimeoutError()
    )

    # Reaching the sleep means the failure did not end the loop.
    with patch("asyncio.sleep", new=AsyncMock(side_effect=EndPluginRun)):
        with pytest.raises(EndPluginRun):
            asyncio.run(plugin.run_for_user(mock_user_find_by_id(4)))

    plugin._sync.assert_awaited_once()
