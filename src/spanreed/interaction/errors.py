"""Surface-neutral interaction exceptions.

Plugins must catch these (never ``telegram.error`` types): each surface
adapter translates its transport-specific errors at the boundary, so plugin
code stays unaware of which surface it is talking to.
"""


class UserInteractionPreempted(Exception):
    """The current interaction was preempted by a higher-priority one.

    Raised out of the ``user_interaction()`` context manager (when
    ``propagate_preemption=True``) so background loops can back off and
    retry later.
    """


class SurfaceBusyError(Exception):
    """A surface refused delivery because it is rate-limited or banned.

    The Telegram surface raises this in place of ``telegram.error.RetryAfter``
    (the dispatcher's flood-control fast-fail). ``retry_after`` is the
    suggested wait in seconds before retrying.
    """

    def __init__(self, retry_after: float):
        super().__init__(f"Surface busy; retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class PromptDeliveryError(Exception):
    """No in-scope surface could deliver a prompt to the user."""
