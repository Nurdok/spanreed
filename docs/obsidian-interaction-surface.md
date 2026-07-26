# Requirements: Obsidian as a User Interaction Surface

**Status:** Draft v2 — requirements agreed (post red-team review), design pending
**Repos involved:** `spanreed` (server), `spanreed-obsidian` (Obsidian plugin)
**Date:** 2026-07-26

## Context

Today, all user interaction in Spanreed happens through the Telegram bot: plugins
send messages, ask multiple-choice questions, and request free-text input via
`TelegramBotApi`, and users trigger plugin commands through the `/do` menu. The
Obsidian companion plugin (`spanreed-obsidian`) is a passive RPC agent — it
executes vault operations pushed by the server over Redis queues, but exposes
nothing interactive to the user inside Obsidian.

This feature makes Obsidian a **second, first-class interaction surface**: every
interaction that can happen in Telegram should also be possible from within
Obsidian. The user works in Obsidian for large parts of the day; prompts,
notifications, and commands should meet them there instead of requiring a
context switch to Telegram.

## Goals

1. **Command palette integration.** Every plugin command available via `/do` in
   Telegram is also available in Obsidian's command palette.
2. **Push notifications in Obsidian.** Durable notifications plugins send to
   the user appear in Obsidian.
3. **Interactive flows in Obsidian.** Prompts that require user input
   (multiple choice, free text) — whether proactive (plugin-initiated) or
   user-initiated (via a palette command) — can be answered from Obsidian.
4. **Surface-agnostic plugins.** Plugins interact with users through a generic
   abstraction and are unaware of which surface(s) the user is on.

## Non-goals

- **Obsidian mobile support.** The Obsidian surface is desktop-only. The plugin
  talks to Redis directly via the node `redis` client, which does not work on
  mobile. Telegram remains the mobile / always-available surface. (A future
  move to HTTP transport could revisit this; nothing in this feature should
  make it *harder*, but it does not need to enable it.)
- **Per-plugin surface routing.** Choosing which plugin talks to which surface
  is future work; delivery scope in this version is fixed by the rules in R2.
- **Replacing Telegram.** Telegram remains fully functional and is the
  durable/authoritative fallback surface.
- **New auth model.** The existing implicit-auth model (Redis URL + user id in
  plugin settings) is retained for this feature. (See Security notes below.)

## Requirements

### R1 — Generic interaction abstraction (`UserInteractionSurface`)

- Replace direct plugin use of `TelegramBotApi` with a generic interaction API
  (working name: `UserInteractionSurface` / a per-user facade over all of the
  user's surfaces). Plugins must not import or reference Telegram-specific
  types for user interaction **or for command registration**.
- The abstraction must cover the full interaction contract plugins use today,
  with the **actual current signatures** (the migration promise in R8 depends
  on this):
  - `send_message(text, *, parse_html=True, parse_markdown=False) -> MessageHandle`
  - `notify(text, *, parse_html=True, parse_markdown=False)` — durable
    fire-and-forget notification
  - `send_multiple_messages(*text, delay=1, parse_html=True)`
  - `send_document(file_name, data)`
  - `edit_message_text(handle, text)` / `delete_message(handle)`
  - `request_user_choice(prompt, choices: list[str] | list[str | list[str]], *, columns=1) -> int`
    — nested lists and `columns` are **advisory layout hints**; each surface
    may render its own layout (e.g., Obsidian may show a vertical list)
  - `request_user_input(prompt) -> str`
  - `user_interaction(priority=..., propagate_preemption=...)` — the
    serialization/priority context manager for conversation exclusivity
  - `has_active_user_interaction()`
  - `register_command(plugin, command)` — command registration is part of the
    contract; see R3
- **Message handles:** `send_message` returns a surface-neutral handle
  representing one logical message with N surface-local message ids, so that
  `edit_message_text`/`delete_message` work on broadcast messages. Surfaces
  may degrade gracefully per primitive (e.g., Obsidian may skip
  `send_document` or render it as a vault file) — exact per-primitive behavior
  is a design decision.
- **Content format:** the abstraction defines a canonical message content
  format and carries the existing parse flags. Each surface owns converting/
  rendering it: Telegram keeps its HTML/MarkdownV2 behavior; Obsidian must
  render formatted content properly (its own Markdown or sanitized HTML) —
  raw `<i>` tags or MarkdownV2 escape backslashes must never be shown to the
  user.
- **The interaction engine moves out of Telegram.** The per-user
  serialization/priority/preemption machinery (interaction queues, current-
  interaction tracking, pending-answer bookkeeping) currently lives inside
  Telegram's application state, keyed by Telegram chat id, and is unavailable
  until the Telegram app initializes. It must move to the surface-agnostic
  layer, keyed by Spanreed user id, and must function even when the Telegram
  surface is unavailable (e.g., a staging environment without a Telegram bot).
  Telegram-specific machinery (flood control, outbound dispatcher, chat-id
  mapping) stays inside the Telegram surface implementation.
- Existing plugins must require only mechanical changes (obtain the generic
  API instead of `TelegramBotApi`); no plugin logic rewrites. This includes
  the test suite: the shared test helpers that patch `TelegramBotApi`
  per-module must be migrated to patch the new abstraction, mechanically.

### R2 — Delivery scope and answer semantics

Delivery scope depends on how an interaction starts:

- **Proactive (plugin-initiated) interactions** — background loops acquiring
  `user_interaction` and prompting — are **broadcast** to all reachable
  surfaces. The **first answer from any surface wins** and resolves the
  interaction; the prompt is then dismissed / marked answered on the other
  surfaces (best-effort cleanup; a second surface answering a just-resolved
  prompt must receive a graceful "already answered" response, not crash —
  same spirit as the existing stale-keyboard handling).
- **User-initiated flows** (a command picked from `/do` in Telegram or from
  the palette in Obsidian) **stick to the originating surface**: all prompts
  and conversational messages of that flow render only there. Surface
  navigation itself (the `/do` menu, the palette picker) is inherently
  surface-local and is never broadcast.
- **Durable notifications (`notify`) always go to all surfaces**, regardless
  of what flow they were sent from (subject to exclusions below).
- **Surface exclusion:** the abstraction supports addressing a message or flow
  to "all surfaces except X". Obsidian-health traffic — the vault-RPC
  progress bar ("⏳ Waiting for Obsidian"), timeout edits, and failure
  notifications from `ObsidianApi` — must target *all surfaces except
  Obsidian*, since Obsidian is by definition the impaired party. `ObsidianApi`
  is itself a `TelegramBotApi` consumer today and migrates to the abstraction
  like any plugin.
- **Serialization remains per-user, across surfaces**, regardless of scope:
  one interaction queue per user with the existing priority levels
  (HIGH/NORMAL/LOW) and preemption semantics. An origin-local flow still
  occupies the user's single interaction slot and blocks new conversations on
  all surfaces, exactly as a second Telegram conversation is blocked today.

### R3 — Dynamic command palette integration

- All plugin commands the user's `/do` menu would show (i.e., commands from
  plugins in `user.plugins`) appear in Obsidian's command palette, e.g. as
  `Spanreed: <command text>`.
- **Surface-neutral registry with stable ids.** The command registry moves out
  of the Telegram application state into the surface-agnostic layer; both
  `/do` and the palette read from it. Each command has a **stable id**
  (independent of its display text) used in the protocol and for cache
  correlation — display text may be renamed freely without breaking cached
  palette entries.
- **Dynamic:** adding/removing/renaming a command on the Spanreed side must
  require **no code change or release** of `spanreed-obsidian`. The plugin
  fetches the available command list from the server at runtime.
- **Async & low-latency:** fetching the command list must never block normal
  Obsidian operation. The plugin registers commands from a cached/fetched
  list; staleness is acceptable within a bounded window (target: commands
  refresh on plugin load and at most a few minutes behind server changes).
- **Invocation semantics:** invoking a palette command sends the command id to
  the server, which (a) **re-validates** that the command still exists and the
  user is still registered to its plugin, and (b) runs the callback **wrapped
  in a HIGH-priority `user_interaction`**, mirroring `/do` (command callbacks
  rely on the caller's interaction context). The resulting flow renders in the
  sidebar (origin-local per R2).
- **Re-invocation: cancel and replace.** Invoking a palette command while a
  previous palette invocation's flow is still queued or active cancels the
  previous one and starts the new one — mirroring `/do`'s existing behavior.
- Invoking a command that no longer exists (or whose plugin the user
  unregistered) must fail gracefully with a user-visible error in Obsidian,
  not crash either side.

### R4 — Notifications in Obsidian

- **Form:** dual presentation —
  - a transient **Notice (toast)** when a notification arrives while Obsidian
    is open, and
  - a persistent **notification inbox** in the Spanreed sidebar tab (R5),
    where every notification is listed with an unread-count badge on the tab.
- **Offline queueing:** durable notifications (`notify()`) sent while Obsidian
  is closed are queued (Redis) and appear in the inbox when Obsidian next
  opens. Queued notifications must not spam a pile of toasts on startup — a
  single summary toast (e.g., "N notifications while you were away") plus the
  inbox is acceptable.
- **Queue cap:** the per-user Obsidian notification queue is capped (same
  policy as Telegram's durable queue: 200 entries, oldest dropped). An
  environment left unattended (e.g., production queue accumulating while
  working against staging) must not grow unboundedly.
- **Inbox lifecycle:** read/unread state is tracked by the Obsidian surface,
  persists across Obsidian restarts, and is independent of whether the same
  notification was seen on Telegram. Notifications are marked read when
  viewed in the inbox; the user can clear the inbox.
- Non-durable conversational messages (`send_message`) are shown live if
  Obsidian is open (and the flow's scope includes Obsidian per R2); they are
  not queued for later.

### R5 — Interactive flows via a sidebar tab

- The plugin registers a **sidebar view** (an `ItemView` leaf, like
  Backlinks/Calendar) — the "Spanreed" tab. All interactive flows whose scope
  includes Obsidian render there: broadcast proactive prompts **and** flows
  started from the command palette.
- The view presents a conversation-style UI: message history for the current
  interaction, choice buttons for `request_user_choice`, a text field for
  `request_user_input`.
- When a prompt arrives (or a palette command starts a flow), the tab should
  reveal/focus itself (or at minimum badge itself) so the user notices.
- **Live prompts only:** interactive prompts are shown on Obsidian only if it
  is connected when the prompt is issued. Prompts issued while Obsidian is
  closed go to the other surfaces only (they are not queued for Obsidian).
  Because delivery is via Redis lists, "live-only" must be enforced by the
  protocol, not assumed: prompt messages carry an interaction id and an
  expiry/generation marker, and the client **discards expired or stale
  prompts on receipt** instead of rendering them.
- **Re-prompt semantics (matching actual Telegram behavior):** an unanswered
  prompt does not time out — the server re-prompts indefinitely (Telegram:
  delete + re-send hourly, plus a "you have N pending interactions" notice).
  On a surface with a persistent prompt UI, a re-prompt is a refresh/no-op,
  not a new message. Surface-internal glue messages (the pending-interactions
  notice, delete-and-resend mechanics) are **surface-local implementation
  details** and are not broadcast content. After 1 hour an interaction becomes
  eligible for preemption by interactions of any priority (existing
  behavior, unchanged).
- **Preemption UI:** when an interaction is preempted (e.g., HIGH preempts a
  LOW background prompt), its prompt is withdrawn from the sidebar and
  replaced by the preempting flow. The preempted plugin retries later
  (existing `UserInteractionPreempted` semantics, unchanged).
- **Reconnect/restart validation:** on plugin startup or reconnect, any
  displayed prompt state must be re-derived from or validated against the
  server — never assumed still-live. Answers submitted in the sidebar resolve
  the server-side wait exactly like a Telegram button press / reply does
  (index for choices, text for input); answers referencing an unknown or
  expired interaction get an explicit "expired — re-run the command" response
  rendered in the sidebar (see R8).

### R6 — On/off switch

- The `spanreed-obsidian` settings tab gets a single toggle for the entire
  interaction-surface feature (palette commands, notifications, sidebar tab,
  prompts).
- The toggle only gates the interaction surface. The existing vault-RPC
  functionality (server reading/writing notes via `obsidian-plugin-tasks`) is
  independent and keeps working when the toggle is off.
- **Server-visible state:** the toggle is not client-only — the plugin writes
  the surface registration state to the server (Redis) on change, and that
  state *is* the per-user (per-instance, see R7) surface registration the
  `UserInteractionSurface` layer consults. **Propagation bound:** the server
  must observe a toggle change before starting any subsequent interaction,
  within a few seconds (target ≤ 5s).
- **When off:** no commands are registered in the palette, the sidebar view is
  unavailable (or shows a "disabled" state), no notifications are shown, and
  the server treats the user as not having an Obsidian surface. **In-flight
  behavior at toggle-off:** a currently displayed prompt is withdrawn from
  Obsidian; if the flow is broadcast, it continues on the other surfaces; if
  it is Obsidian-origin, it is cancelled (preemption-style). Notifications
  queued for the inbox are retained (within the R4 cap) and delivered on
  re-enable. Toggling must not require an Obsidian restart.

### R7 — Transport

- Reuse and extend the **existing Redis bridge** model (per-user queues,
  blocking pops for push latency, per-request reply lists). No new
  HTTP/WebSocket infrastructure in this version.
- **The transport's real properties must be confronted, not assumed.** Redis
  lists are single-consumer, at-most-once, unordered delivery: each pushed
  item is received by exactly one popper. The requirements below exist
  because broadcast, liveness, and dismissal do not come for free on this
  substrate:
  - **Per-instance queues with server-side fan-out.** Multiple Obsidian
    instances (laptop + desktop, multiple vaults) may run concurrently with
    the same user id. Each instance generates a persistent **instance id**
    and registers itself with the server; each instance gets its own
    delivery queue(s), and the server fans out prompts, notifications, and
    dismissals to every live instance. Answers are correlated by interaction
    id regardless of which instance sent them (first answer wins across
    instances just as across surfaces). Dead instances' registrations and
    queues are expired/garbage-collected (e.g., TTL-based); the mechanism is
    a design decision.
  - **Reachability via presence keys, not the monitor queue.** The existing
    watchdog queue (`obsidian-plugin-monitor:{user_id}`) is exclusively
    consumed by the spanreed-monitor plugin and cannot serve a second
    consumer. Instance reachability is signaled by a per-instance presence
    key with a TTL (`SET ... EX`, refreshed on each poll cycle), which the
    server checks when deciding whether to deliver live prompts (R5).
  - **Prompt expiry and durable dismissal.** Prompt messages carry expiry/
    generation info so a stale prompt popped after reconnect is discarded
    client-side (R5); dismissals must be durable or prompts must self-expire
    on the client, so a dismissal sent during a brief disconnect cannot leave
    a zombie prompt.
  - **Dedicated Redis connection for the interaction channel.** The plugin's
    vault-RPC loop holds a blocking `blPop` (up to 60s) on its connection;
    interaction traffic (prompt delivery, answers, command-list fetches) must
    use a separate connection (e.g., `client.duplicate()`) so it never
    inherits head-of-line blocking from vault RPC. Target prompt-delivery
    latency while Obsidian is open: sub-second.
- New message kinds (command-list requests, prompt delivery, answers,
  notifications, dismissals, instance registration/presence) follow a
  protocol shape consistent with the existing `{request_id, method, params}` /
  `{success, result}` RPC convention. Exact queue topology and message schema
  are design decisions.

### R8 — Reliability & compatibility

- Telegram behavior must be unchanged for users without the Obsidian surface
  enabled, and degraded-gracefully (identical to today) when Obsidian is
  offline.
- An unreachable/slow Obsidian surface must never block or delay a Telegram
  interaction (delivery to surfaces is independent; the interaction resolves
  on first answer regardless of other surfaces' delivery state).
- **Restart resilience, both directions:**
  - Server restarts while a sidebar prompt is displayed: the interaction
    state is gone; a subsequent answer from Obsidian must be met with an
    explicit "expired — re-run the command" response rendered in the sidebar
    (not silently dropped, not a crash).
  - Obsidian plugin reload/restart while the server awaits an answer: the
    client re-validates displayed state on startup (R5); the server-side wait
    continues to be answerable from other surfaces and via the re-prompt
    cycle.
- Malformed or stale messages on either side must be logged and skipped, not
  crash the plugin loop or the Obsidian poller.
- The existing test suite must pass after the migration, with its
  Telegram-patching test helpers mechanically migrated to the new abstraction
  (see R1).

## Security notes (related, tracked separately)

- `spanreed-obsidian` currently has `data.json` committed with live Redis
  credentials; those must be rotated and the file removed from version
  control. Not a blocker for this feature but should happen first.
- The Redis-transport model means the Obsidian plugin holds full Redis
  credentials and per-user isolation is by convention only. Accepted for this
  single-user deployment; a per-user auth boundary is future work (and the
  main argument for an eventual HTTP transport).

## Deferred to the design doc

- Exact queue topology, message schema, and instance registration/GC
  mechanism (R7).
- Canonical message content format and per-surface rendering rules (R1).
- Per-primitive degradation behavior on Obsidian (`send_document`, message
  edits, multi-message sequences) (R1).
- Sidebar UI details (history retention, layout, inbox storage format).
- Command-list refresh mechanism (poll interval vs. server push).

## Acceptance criteria

1. **Dynamic commands:** a plugin command registered on the server appears in
   Obsidian's command palette without any change to `spanreed-obsidian`, and
   invoking it runs the same flow as picking it from `/do` in Telegram,
   rendered in the sidebar.
2. **Broadcast + first-answer-wins:** a proactive prompt (e.g., habit
   tracker) appears in Telegram and the Obsidian sidebar; answering in
   Obsidian resolves it, and the Telegram keyboard is dismissed within a few
   seconds — or, if dismissal fails (e.g., during a flood-control ban), a
   late press on it gets a graceful "already answered" response. And vice
   versa.
3. **Origin stickiness:** a flow started from `/do` in Telegram renders no
   prompts in Obsidian, and a palette-initiated flow renders no prompts in
   Telegram; a `notify()` from either flow reaches both surfaces.
4. **Notifications:** a `notify()` sent while Obsidian is closed appears in
   the sidebar inbox (unread) on next launch; one sent while open also shows
   a toast. A Telegram-HTML-formatted message and a MarkdownV2-escaped
   notification both render formatted in Obsidian, never as raw markup.
5. **Toggle:** with the settings toggle off, Obsidian shows no Spanreed
   commands, notifications, or prompts — including for a prompt issued ~5s
   after toggling — while vault RPC still works and Telegram is unaffected.
   Re-enabling delivers retained inbox notifications.
6. **Stale command:** invoking a palette command that was removed (or whose
   plugin was unregistered) server-side shows a clear error in Obsidian; no
   crash on either side.
7. **Isolation:** with Obsidian unreachable (or its poller artificially
   stalled), Telegram prompt latency is unchanged.
8. **Restart resilience:** answering a sidebar prompt after a server restart
   yields the "expired" response in the sidebar; reloading Obsidian
   mid-prompt and reconnecting does not leave a zombie prompt.
9. **Multi-instance:** with two Obsidian instances open under the same user
   id, a proactive prompt appears in both; answering in one dismisses it in
   the other.
10. **Preemption:** a HIGH-priority flow started while a LOW background
    prompt is displayed withdraws the LOW prompt from the sidebar and shows
    the HIGH flow; the LOW plugin re-prompts later.
11. **Migration:** all existing plugins run with unchanged behavior after the
    mechanical interaction-API migration, verified by the migrated test suite
    plus new tests for the surface abstraction and delivery-scope rules.
