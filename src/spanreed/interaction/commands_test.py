import asyncio

import pytest

from spanreed.interaction import commands
from spanreed.interaction.commands import PluginCommand
from spanreed.test_utils import mock_user_find_by_id


def setup_function() -> None:
    commands.reset_registry_for_tests()


async def _noop(user: object) -> None:
    pass


def test_default_id_is_plugin_dot_slug() -> None:
    commands.register_command(
        "habit-tracker", PluginCommand(text="Track Habits!", callback=_noop)
    )
    command = commands.get_command("habit-tracker.track-habits")
    assert command is not None
    assert command.text == "Track Habits!"


def test_explicit_id_survives_display_text_rename() -> None:
    # A renamed command keeps its old id by passing it explicitly.
    commands.register_command(
        "habit-tracker",
        PluginCommand(
            text="Track My Habits (new name)",
            callback=_noop,
            id="habit-tracker.track-habits",
        ),
    )
    command = commands.get_command("habit-tracker.track-habits")
    assert command is not None
    assert command.text == "Track My Habits (new name)"


def test_duplicate_id_rejected() -> None:
    commands.register_command("p", PluginCommand(text="Do Thing", callback=_noop))
    with pytest.raises(ValueError):
        commands.register_command("p", PluginCommand(text="Do Thing", callback=_noop))


def test_commands_for_user_filters_by_registered_plugins() -> None:
    commands.register_command(
        "habit-tracker", PluginCommand(text="Track Habits", callback=_noop)
    )
    commands.register_command(
        "litnotes", PluginCommand(text="Add Book Note", callback=_noop)
    )

    user = mock_user_find_by_id(4)
    user.plugins = ["habit-tracker"]

    shown = commands.commands_for_user(user)
    assert [c.id for c in shown] == ["habit-tracker.track-habits"]


def test_commands_for_user_preserves_registration_order() -> None:
    for text in ["B Command", "A Command", "C Command"]:
        commands.register_command("p", PluginCommand(text=text, callback=_noop))
    user = mock_user_find_by_id(4)
    user.plugins = ["p"]

    shown = commands.commands_for_user(user)
    assert [c.text for c in shown] == ["B Command", "A Command", "C Command"]


def test_change_listeners_fire_debounced_once_per_burst() -> None:
    fired: list[int] = []

    async def run() -> None:
        commands.add_change_listener(lambda: fired.append(1))
        commands._CHANGE_DEBOUNCE_S = 0.01
        try:
            for i in range(5):
                commands.register_command(
                    "p", PluginCommand(text=f"Command {i}", callback=_noop)
                )
            assert fired == []  # not yet ג€” debounced
            await asyncio.sleep(0.05)
            assert fired == [1]  # one burst, one notification
        finally:
            commands._CHANGE_DEBOUNCE_S = 2.0

    asyncio.run(run())


def test_user_plugin_change_fires_listeners_immediately() -> None:
    fired: list[int] = []
    commands.add_change_listener(lambda: fired.append(1))
    commands.notify_user_plugins_changed(mock_user_find_by_id(4))
    assert fired == [1]


def test_a_command_of_one_user_is_only_theirs() -> None:
    commands.register_command(
        "workflows",
        PluginCommand(text="Run it", callback=_noop, id="workflow.wf_1"),
        user_id=4,
    )
    owner, other = mock_user_find_by_id(4), mock_user_find_by_id(7)
    owner.plugins = other.plugins = ["workflows"]

    assert [c.id for c in commands.commands_for_user(owner)] == ["workflow.wf_1"]
    assert commands.commands_for_user(other) == []
    assert commands.get_command_for_user(owner, "workflow.wf_1") is not None
    assert commands.get_command_for_user(other, "workflow.wf_1") is None


def test_get_command_for_user_checks_the_plugins_of_the_user() -> None:
    commands.register_command(
        "habit-tracker", PluginCommand(text="Track Habits", callback=_noop)
    )
    user = mock_user_find_by_id(4)

    assert commands.get_command_for_user(user, "habit-tracker.track-habits") is None
    user.plugins = ["habit-tracker"]
    assert commands.get_command_for_user(user, "habit-tracker.track-habits") is not None
    assert commands.get_command_for_user(user, "habit-tracker.nothing") is None


def test_a_command_can_be_unregistered_and_registered_again() -> None:
    command = PluginCommand(text="Run it", callback=_noop, id="workflow.wf_1")
    commands.register_command("workflows", command, user_id=4)

    assert commands.unregister_command("workflow.wf_1")
    assert commands.get_command("workflow.wf_1") is None
    assert not commands.unregister_command("workflow.wf_1")
    commands.register_command("workflows", command, user_id=4)
    assert commands.get_command("workflow.wf_1") is not None
