"""Plugin command model.

Phase 1: just the surface-neutral ``PluginCommand`` type; the registry
itself still lives in the Telegram application's ``bot_data`` behind the
``UserInteractionApi.register_command`` shim. Phase 2 replaces that with a
real registry here (stable command ids, /do + palette both reading from it).
"""

from typing import Callable, NamedTuple


class PluginCommand(NamedTuple):
    text: str
    callback: Callable
