"""Surface-agnostic user interaction (design doc: obsidian-interaction-surface-design.md).

Plugins import everything interaction-related from this package:

    from spanreed.interaction import (
        UserInteractionApi,
        UserInteractionPriority,
        UserInteractionPreempted,
        SurfaceBusyError,
        PluginCommand,
    )
"""

from spanreed.interaction.api import UserInteractionApi
from spanreed.interaction.engine import UserInteractionPriority
from spanreed.interaction.errors import (
    PromptDeliveryError,
    SurfaceBusyError,
    UserInteractionPreempted,
)
from spanreed.interaction.surface import (
    Content,
    MessageHandle,
    PromptSpec,
    SurfaceScope,
    UserInteractionSurface,
)

from spanreed.interaction.commands import PluginCommand

__all__ = [
    "Content",
    "MessageHandle",
    "PluginCommand",
    "PromptDeliveryError",
    "PromptSpec",
    "SurfaceBusyError",
    "SurfaceScope",
    "UserInteractionApi",
    "UserInteractionPreempted",
    "UserInteractionPriority",
    "UserInteractionSurface",
]
