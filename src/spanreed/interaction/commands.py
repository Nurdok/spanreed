"""Surface-neutral plugin command registry (design doc §2.6).

Commands have stable ids so surfaces can cache/reference them across display
-text renames: the id defaults to ``{plugin}.{slug(text)}`` at registration,
and a plugin that renames a command's display text passes the old ``id``
explicitly to keep it stable.

Both the Telegram ``/do`` menu and (in phase 3+) the Obsidian command-list
publisher read from here. Change listeners exist for the publisher: they
fire debounced after registration bursts (plugin startup) and immediately on
per-user plugin (un)registration.

A command is for every user of its plugin, unless it is registered for one
user. Workflows do that: each workflow that can be started by hand is a
command of the user it belongs to, and it comes and goes as the workflow is
turned on and off.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional

from spanreed.user import User

_logger = logging.getLogger(__name__)

# Registration happens in bursts during plugin startup; listeners are
# notified once per burst.
_CHANGE_DEBOUNCE_S = 2.0


class PluginCommand(NamedTuple):
    text: str
    callback: Callable
    # Stable identity for caching surfaces. Defaults to a slug of the
    # (plugin, text) pair at registration; pass the previous id explicitly
    # when renaming display text.
    id: Optional[str] = None


@dataclass(frozen=True)
class RegisteredCommand:
    id: str
    plugin_canonical_name: str
    text: str
    callback: Callable
    # The only user who has this command. None: every user of the plugin.
    user_id: int | None = None


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "command"


# Insertion-ordered (dicts preserve order): menu display order is
# registration order, as before.
_commands: dict[str, RegisteredCommand] = {}
_change_listeners: list[Callable[[], None]] = []
_debounce_task: asyncio.Task | None = None


def reset_registry_for_tests() -> None:
    global _debounce_task
    _commands.clear()
    _change_listeners.clear()
    _debounce_task = None


def register_command(
    plugin_canonical_name: str,
    command: PluginCommand,
    user_id: int | None = None,
) -> None:
    """Register a command of a plugin.

    If ``user_id`` is given, only that user has the command. Raise
    ``ValueError`` if a command with the same id is registered already.
    """
    command_id = command.id or (f"{plugin_canonical_name}.{_slugify(command.text)}")
    if command_id in _commands:
        raise ValueError(f"Command id {command_id!r} already registered")
    _commands[command_id] = RegisteredCommand(
        id=command_id,
        plugin_canonical_name=plugin_canonical_name,
        text=command.text,
        callback=command.callback,
        user_id=user_id,
    )
    _logger.info(
        f"Registered command {command_id!r}" f" ({len(_commands)} commands total)"
    )
    _notify_changed_debounced()


def unregister_command(command_id: str) -> bool:
    """Remove a command, and return whether it was registered."""
    if _commands.pop(command_id, None) is None:
        return False
    _logger.info(f"Unregistered command {command_id!r}")
    _notify_changed_debounced()
    return True


def get_command(command_id: str) -> RegisteredCommand | None:
    return _commands.get(command_id)


def _is_for_user(command: RegisteredCommand, user: User) -> bool:
    return command.plugin_canonical_name in user.plugins and (
        command.user_id is None or command.user_id == user.id
    )


def get_command_for_user(user: User, command_id: str) -> RegisteredCommand | None:
    """Return a command if the user has it, and ``None`` otherwise.

    A surface checks with this before it runs a command, because the list
    of commands it showed the user may be out of date.
    """
    command = _commands.get(command_id)
    if command is None or not _is_for_user(command, user):
        return None
    return command


def commands_for_user(user: User) -> list[RegisteredCommand]:
    """The commands the user's /do menu (and palette) should show."""
    return [command for command in _commands.values() if _is_for_user(command, user)]


def add_change_listener(listener: Callable[[], None]) -> None:
    """Register a callback fired when the effective command set may have
    changed (new registrations, or a user's plugin set changed)."""
    _change_listeners.append(listener)


def notify_user_plugins_changed(user: User) -> None:
    """Called on plugin (un)registration for a user — the *registry* is
    unchanged but the user's effective command set isn't."""
    _fire_change_listeners()


def _notify_changed_debounced() -> None:
    global _debounce_task
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Registration outside an event loop (tests); listeners fire on the
        # next in-loop change.
        return
    if _debounce_task is not None and not _debounce_task.done():
        _debounce_task.cancel()
    _debounce_task = asyncio.create_task(_debounced_fire())


async def _debounced_fire() -> None:
    try:
        await asyncio.sleep(_CHANGE_DEBOUNCE_S)
    except asyncio.CancelledError:
        return
    _fire_change_listeners()


def _fire_change_listeners() -> None:
    for listener in _change_listeners:
        try:
            listener()
        except Exception:
            _logger.exception("Command-registry change listener failed")
