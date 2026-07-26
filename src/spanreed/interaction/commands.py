"""Surface-neutral plugin command registry (design doc §2.6).

Commands have stable ids so surfaces can cache/reference them across display
-text renames: the id defaults to ``{plugin}.{slug(text)}`` at registration,
and a plugin that renames a command's display text passes the old ``id``
explicitly to keep it stable.

Both the Telegram ``/do`` menu and (in phase 3+) the Obsidian command-list
publisher read from here. Change listeners exist for the publisher: they
fire debounced after registration bursts (plugin startup) and immediately on
per-user plugin (un)registration.
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


def register_command(plugin_canonical_name: str, command: PluginCommand) -> None:
    command_id = command.id or (f"{plugin_canonical_name}.{_slugify(command.text)}")
    if command_id in _commands:
        raise ValueError(f"Command id {command_id!r} already registered")
    _commands[command_id] = RegisteredCommand(
        id=command_id,
        plugin_canonical_name=plugin_canonical_name,
        text=command.text,
        callback=command.callback,
    )
    _logger.info(
        f"Registered command {command_id!r}" f" ({len(_commands)} commands total)"
    )
    _notify_changed_debounced()


def get_command(command_id: str) -> RegisteredCommand | None:
    return _commands.get(command_id)


def commands_for_user(user: User) -> list[RegisteredCommand]:
    """The commands the user's /do menu (and palette) should show."""
    return [
        command
        for command in _commands.values()
        if command.plugin_canonical_name in user.plugins
    ]


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
