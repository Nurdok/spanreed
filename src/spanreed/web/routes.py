"""HTTP routes of the web UI, mounted on the Web UI plugin's Quart app.

Everything lives under ``/app`` so it rides on the existing nginx proxy to
the Quart app (same as the OAuth callbacks) and needs no nginx change. The
browser talks plain HTTP: JSON calls plus a long-poll for the event feed, so
no WebSocket upgrade is needed in the proxy either.

Commands come from the shared registry (``spanreed.interaction.commands``)
and run exactly like a Telegram ``/do`` pick or an Obsidian palette
invocation: as a HIGH-priority user-initiated flow in the shared slot
(newest wins), scoped to the web surface.
"""

from __future__ import annotations

import asyncio
import functools
import html
import logging
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar, cast

from quart import Blueprint, Response, g, jsonify, redirect, request

from spanreed.interaction import commands, engine
from spanreed.interaction.api import UserInteractionApi
from spanreed.interaction.engine import PromptKind, UserInteractionPriority
from spanreed.interaction.surface import SurfaceScope
from spanreed.interaction.web_surface import SURFACE_NAME, WebSurface, read_inbox
from spanreed.user import User
from spanreed.web import auth

_logger = logging.getLogger(__name__)

LONG_POLL_TIMEOUT_S = 25.0
# State-changing requests must carry this header. Browsers can't add a
# custom header to a cross-site request without a CORS preflight (which we
# never grant), so together with the SameSite cookie this blocks CSRF.
CSRF_HEADER = "X-Spanreed-Request"

_STATIC_DIR = Path(__file__).parent / "static"

blueprint = Blueprint("web_ui_app", __name__, url_prefix="/app")

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def get_web_surface() -> WebSurface:
    UserInteractionApi._ensure_default_surfaces()
    for surface in UserInteractionApi._surfaces:
        if isinstance(surface, WebSurface) and surface.name == SURFACE_NAME:
            return surface
    raise RuntimeError("WebSurface not registered")


def _page(name: str) -> str:
    return (_STATIC_DIR / name).read_text(encoding="utf-8")


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


async def _current_user() -> User | None:
    user_id = await auth.user_id_for_session(request.cookies.get(auth.SESSION_COOKIE))
    if user_id is None:
        return None
    try:
        return await User.find_by_id(user_id)
    except Exception:
        _logger.exception(f"Session points at unknown user {user_id}")
        return None


def api_auth(f: F) -> F:
    """Require a session (401 otherwise) and, for writes, the CSRF header."""

    @functools.wraps(f)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if request.method != "GET" and request.headers.get(CSRF_HEADER) != "1":
            return jsonify({"error": "missing request header"}), 403
        user = await _current_user()
        if user is None:
            return jsonify({"error": "not logged in"}), 401
        g.user = user
        return await f(*args, **kwargs)

    return cast(F, wrapper)


# ---------------------------------------------------------------------------
# Pages and login
# ---------------------------------------------------------------------------


@blueprint.get("/")
async def index() -> Any:
    if await _current_user() is None:
        return _no_store(Response(_page("login.html"), mimetype="text/html"))
    return _no_store(Response(_page("index.html"), mimetype="text/html"))


def _invalid_link_page() -> Response:
    page = _page("login.html").replace(
        "<!--error-->",
        '<p class="error">That login link is invalid, expired or was'
        " already used. Get a new one.</p>",
    )
    return _no_store(Response(page, status=403, mimetype="text/html"))


@blueprint.get("/login")
async def login_page() -> Any:
    """Show a "Log in" button for a login link; never consume the token.

    Chat apps fetch links to build previews (Telegram does it as soon as
    the message arrives), so a GET that redeemed the one-time token would
    burn it before the user ever tapped it. Only the POST below redeems.
    """
    token = request.args.get("token", "")
    if not await auth.login_token_is_valid(token):
        return _invalid_link_page()
    page = _page("login_confirm.html").replace("{{token}}", html.escape(token))
    response = _no_store(Response(page, mimetype="text/html"))
    # Keep the one-time token out of Referer headers.
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@blueprint.post("/login")
async def login() -> Any:
    form = await request.form
    user_id = await auth.redeem_login_token(form.get("token", ""))
    if user_id is None:
        return _invalid_link_page()
    session_id = await auth.create_session(user_id)
    _logger.info(f"Web UI login for user {user_id}")
    response = redirect("/app/", code=303)
    response.set_cookie(
        auth.SESSION_COOKIE,
        session_id,
        max_age=auth.SESSION_TTL_S,
        path="/app",
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    return response


@blueprint.post("/api/logout")
@api_auth
async def logout() -> Any:
    session_id = request.cookies.get(auth.SESSION_COOKIE)
    if session_id:
        await auth.end_session(session_id)
    response = jsonify({"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE, path="/app")
    return response


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _command_list(user: User) -> list[dict[str, str]]:
    return [
        {"id": c.id, "plugin": c.plugin_canonical_name, "text": c.text}
        for c in commands.commands_for_user(user)
    ]


# user_id -> (flow id, task) of the user's latest web-started flow.
_web_flows: dict[int, tuple[str, asyncio.Task]] = {}


def active_web_flow_id(user_id: int) -> str | None:
    entry = _web_flows.get(user_id)
    if entry is None or entry[1].done():
        return None
    return entry[0]


@blueprint.get("/api/state")
@api_auth
async def state() -> Any:
    user: User = g.user
    surface = get_web_surface()
    surface.mark_seen(user.id)
    return _no_store(
        jsonify(
            {
                "user": {"id": user.id, "name": user.name},
                "commands": _command_list(user),
                "seq": surface.current_seq(user.id),
                "events": surface.recent_events(user.id),
                "open_prompt": surface.open_prompt(user.id),
                "active_flow_id": active_web_flow_id(user.id),
                "inbox": await read_inbox(user.id),
            }
        )
    )


@blueprint.get("/api/events")
@api_auth
async def events() -> Any:
    user: User = g.user
    try:
        after = int(request.args.get("after", "0"))
    except ValueError:
        return jsonify({"error": "bad cursor"}), 400
    seq, new_events, reset = await get_web_surface().wait_for_events(
        user.id, after, LONG_POLL_TIMEOUT_S
    )
    return _no_store(jsonify({"seq": seq, "events": new_events, "reset": reset}))


@blueprint.post("/api/commands/<command_id>/run")
@api_auth
async def run_command(command_id: str) -> Any:
    user: User = g.user
    command = commands.get_command(command_id)
    # Revalidate against the user's current plugin set: the page's list may
    # be stale.
    if command is None or command.plugin_canonical_name not in user.plugins:
        return jsonify({"error": "This command is not available."}), 404
    surface = get_web_surface()
    surface.mark_seen(user.id)
    flow_id = start_web_command_flow(user, command.callback, surface, command.text)
    return jsonify({"ok": True, "flow_id": flow_id})


def start_web_command_flow(
    user: User,
    callback: Callable[[User], Awaitable[Any]],
    surface: WebSurface,
    title: str,
) -> str:
    flow_id = uuid.uuid4().hex
    surface.flow_started(user.id, flow_id, title)

    async def command_flow() -> None:
        error: str | None = None
        try:
            ui = await UserInteractionApi.for_user(user)
            async with ui.user_interaction(
                priority=UserInteractionPriority.HIGH,
                # User-initiated: the whole flow sticks to the web UI and
                # never touches Telegram.
                scope=SurfaceScope(origin_surface=SURFACE_NAME),
            ):
                await callback(user)
        except asyncio.CancelledError:
            error = "Cancelled."
            raise
        except Exception as e:
            _logger.exception(f"Web command flow failed for user {user.id}")
            error = f"The command failed: {e}"
        finally:
            surface.flow_finished(user.id, flow_id, error)

    # Shared slot with /do and the Obsidian palette: newest flow wins. (No
    # origin_instance: that field names Obsidian instances, and the
    # Obsidian housekeeping cancels flows whose instance isn't live.)
    task = engine.start_user_flow(user.id, command_flow())
    _web_flows[user.id] = (flow_id, task)
    return flow_id


@blueprint.post("/api/answer")
@api_auth
async def answer() -> Any:
    user: User = g.user
    body = await request.get_json(silent=True) or {}
    interaction_id = body.get("interaction_id")
    value = body.get("value")
    if not isinstance(interaction_id, str):
        return jsonify({"error": "missing interaction_id"}), 400
    prompt = engine.get_prompt(interaction_id)
    if prompt is None or prompt.user_id != user.id:
        return jsonify({"error": "This question is no longer open."}), 409
    if prompt.kind == PromptKind.CHOICE:
        if not isinstance(value, int) or isinstance(value, bool):
            return jsonify({"error": "bad choice"}), 400
    elif not isinstance(value, str) or not value.strip():
        return jsonify({"error": "empty answer"}), 400
    surface = get_web_surface()
    # Echo before resolving: resolving lets the flow run on, and its
    # cleanup closes the open prompt this reads the button label from.
    open_prompt = surface.open_prompt(user.id)
    resolution = engine.resolve_prompt(interaction_id, value)
    if resolution != engine.PromptResolution.RESOLVED:
        return jsonify({"error": "This question was already answered."}), 409
    surface.record_answer(user.id, interaction_id, value, open_prompt)
    return jsonify({"ok": True})


@blueprint.post("/api/cancel")
@api_auth
async def cancel() -> Any:
    """Abort the flow that owns the open prompt (or the user's web flow).

    Mirrors the Obsidian surface's cancel: a user-initiated flow is
    cancelled outright; a proactive interaction is preempted so the
    plugin's own retry policy applies.
    """
    user: User = g.user
    body = await request.get_json(silent=True) or {}
    interaction_id = body.get("interaction_id")
    pending = engine.get_pending_prompt_for_user(user.id)
    if (
        interaction_id is not None
        and pending is not None
        and pending.interaction_id != interaction_id
    ):
        return jsonify({"ok": False, "stale": True})
    if engine.cancel_current_user_flow(user.id):
        return jsonify({"ok": True})
    if pending is not None and engine.preempt_current_interaction(user.id):
        return jsonify({"ok": True})
    return jsonify({"ok": False})


@blueprint.get("/api/documents/<document_id>")
@api_auth
async def document(document_id: str) -> Any:
    user: User = g.user
    found = get_web_surface().get_document(user.id, document_id)
    if found is None:
        return jsonify({"error": "not found"}), 404
    file_name, data = found
    safe_name = "".join(c for c in file_name if c.isalnum() or c in "._- ") or "file"
    response = Response(data, mimetype="application/octet-stream")
    response.headers["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    return response
