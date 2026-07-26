# Design: Obsidian as a User Interaction Surface

**Status:** Draft v2 — post red-team review
**Requirements:** [obsidian-interaction-surface.md](obsidian-interaction-surface.md)
**Repos:** `spanreed` (server), `spanreed-obsidian` (plugin)
**Date:** 2026-07-26

## 1. Architecture overview

```
                         ┌──────────────────────── spanreed server ───────────────────────┐
                         │                                                                │
  plugins ──────────────►│  UserInteractionApi (per-user facade)                          │
  (habit_tracker, ...)   │    │        │                                                  │
                         │    │        ├── CommandRegistry (stable ids, surface-neutral)  │
                         │    │        │                                                  │
                         │  InteractionEngine (queues / priority / preemption /           │
                         │    │           pending answers — keyed by spanreed user id)    │
                         │    │                                                           │
                         │    ├── TelegramSurface (wraps existing bot + dispatcher)       │
                         │    └── ObsidianSurface (fan-out to instances over Redis)       │
                         └────────────────────────────┬───────────────────────────────────┘
                                                      │ Redis (existing managed instance)
                     ┌────────────────────────────────┴─────────────────────────┐
                     │ per-instance inbound queues · events queue · presence    │
                     │ keys · command-list key · notification inbox             │
                     └────────────────────────────────┬─────────────────────────┘
                                                      │
                      Obsidian instance A (laptop)    │    Obsidian instance B (desktop)
                      sidebar view · palette · inbox ─┴─── (same, own instance id)
```

Three server-side layers replace today's monolithic `TelegramBotApi`:

1. **`InteractionEngine`** — the extracted serialization core (queues,
   priorities, preemption, pending-answer futures), keyed by spanreed user id.
2. **`UserInteractionSurface`** implementations — Telegram and Obsidian, each
   owning delivery/rendering/collection for its channel.
3. **`UserInteractionApi`** — the per-user facade plugins use. Owns scope
   resolution (broadcast / origin-local / exclusions), first-answer-wins,
   message handles, error translation, and command registration.

## 2. Server-side design (`spanreed` repo)

New package: `src/spanreed/interaction/`.

### 2.1 `InteractionEngine` (`interaction/engine.py`)

Everything that today lives in `bot_data` keyed by Telegram chat id moves here,
keyed by `user.id`, as module-level per-user state (the process is a single
asyncio loop; no cross-process state needed):

- `USER_INTERACTION_LOCKS`, `USER_INTERACTION_QUEUES` (priority → list),
  `CURRENT_USER_INTERACTION` — moved as-is from `telegram_bot.py`, including
  `UserInteractionPriority`, `UserInteraction`, `UserInteractionPreempted`,
  preemption and the 1-hour preemption-eligibility timeout. Behavior is
  unchanged; only the keying and module change.
- **Pending prompts**: `PENDING_PROMPTS: dict[interaction_id, PendingPrompt]`
  — keyed by **interaction id, not user id**, plus a per-user
  `current_prompt_id` pointer. (A per-user single slot would race under
  preemption: a cancelled LOW task's cleanup, delivered at its next
  scheduling point, could delete the HIGH prompt that replaced it. Keying by
  id means cleanup only ever deletes its own entry.)
  `PendingPrompt = (interaction_id: str, user_id, generation: int,
  future: asyncio.Future, kind: choice|input, per_surface_state: dict)`.
  This replaces `CALLBACK_EVENTS`/`CALLBACK_EVENT_RESULTS`/
  `USER_MESSAGE_CALLBACK_ID`.
  - `interaction_id` is a UUID minted per prompt; `generation` increments on
    each re-prompt cycle (§2.4). **Generation is used only for client-side
    render dedup and prompt-staleness — never for answer validation.** An
    answer resolves on `interaction_id` alone while the future is unresolved
    (an instance that slept through three re-prompt cycles still holds a
    perfectly valid prompt card; its answer must count).
  - First answer wins = first `future.set_result(value)`; the engine then
    calls `dismiss_prompt(...)` on all other in-scope surfaces (best-effort).
  - **Resolved entries become tombstones** (kept ~24h in a bounded ring
    buffer) so a late answer is answered `already-answered`, distinguishable
    from an unknown id (server restarted) which gets `interaction-expired`.
- The engine has **no Telegram imports** and no dependency on the Telegram
  application lifecycle — it works with zero registered surfaces (prompts then
  block until a surface appears or the caller is preempted), satisfying the
  "staging without Telegram" requirement.

### 2.2 `UserInteractionSurface` ABC (`interaction/surface.py`)

```python
class UserInteractionSurface(abc.ABC):
    name: str  # "telegram", "obsidian"

    @abc.abstractmethod
    async def is_reachable(self, user: User) -> bool: ...
    @abc.abstractmethod
    async def send_message(self, user, content: Content) -> SurfaceMessageId | None: ...
    @abc.abstractmethod
    async def notify(self, user, content: Content) -> None: ...  # durable
    @abc.abstractmethod
    async def render_prompt(self, user, prompt: PromptSpec) -> None: ...
    @abc.abstractmethod
    async def dismiss_prompt(self, user, interaction_id: str) -> None: ...
    # optional-capability primitives, default = graceful degradation:
    async def send_document(self, user, file_name, data) -> None: ...
    async def edit_message(self, user, msg_id: SurfaceMessageId, content) -> None: ...
    async def delete_message(self, user, msg_id: SurfaceMessageId) -> None: ...
```

- `Content = (text: str, parse_html: bool, parse_markdown: bool)` — the
  canonical content is **exactly what plugins pass today** (no new markup
  language; mechanical migration). Each surface owns rendering (§4.4).
- `PromptSpec = (interaction_id, generation, kind, prompt: Content,
  choices: list[list[str]] | None, columns: int, flow: FlowInfo)`. Choice
  nesting/`columns` are advisory layout hints. `flow` carries the plugin name
  and origin for UI labeling.
- Surfaces **register with the engine** at startup
  (`InteractionEngine.register_surface(surface)`). Whether a given *user* has
  a given surface is resolved per-call: Telegram = has `TelegramBotPlugin`
  config; Obsidian = has ≥1 registered instance with the toggle on (§3.1).

**`TelegramSurface`** (`interaction/telegram_surface.py`): a thin adapter over
the existing machinery. `render_prompt` sends the inline keyboard (re-using
current keyboard-building code and the `OutboundDispatcher`); the callback
handler resolves the engine's pending future instead of the old callback-event
dicts; `handle_message` routes free text the same way. `dismiss_prompt`
deletes/edits the keyboard message via the dispatcher (fails soft during flood
bans — the stale-press path answers "already answered"). Flood control,
dispatcher lanes, and the chat-id `UserConfig` are untouched.

**`ObsidianSurface`** (`interaction/obsidian_surface.py`): implements the
protocol in §3. `is_reachable` = any live instance presence key.
`send_document` degrades to a `message` naming the file (see §4.3 — for the
scan flow the PDF is already in the vault, so a link message is strictly
better than a silent no-op). `edit/delete_message` = sends `update-message` /
`retract-message` events to instances (the sidebar mutates its transcript).

### 2.3 Error contract and broadcast failure semantics

The abstraction defines a neutral exception taxonomy in
`interaction/errors.py`; **no `telegram.error` type crosses the facade**:

- `SurfaceBusyError(retry_after: float)` — raised by `TelegramSurface` by
  translating `telegram.error.RetryAfter` (the dispatcher's flood-ban
  fast-fail). The base-plugin supervisor's `RetryAfter` handling
  (`plugin.py`) and the three plugins that catch `RetryAfter` directly
  (`habit_tracker`, `book_progress`, `googlefit`) migrate to
  `SurfaceBusyError`. **This is a small semantic migration, not a mechanical
  rename** — it is called out explicitly in phase 1 (§5) and verified by the
  existing flood-control tests.
- `PromptDeliveryError` — raised by prompt primitives only when **no**
  in-scope surface could render (see below).

**Broadcast partial failure:** a prompt or `send_message` succeeds if **at
least one** in-scope surface delivered; per-surface failures within a
broadcast are logged, not raised. Only if *all* in-scope surfaces fail does
the call raise (for a Telegram-only user this preserves today's fast-fail
behavior exactly — `SurfaceBusyError` propagates when Telegram is the only
surface and it is flood-banned). Fan-out is **concurrent**
(`asyncio.gather` over per-surface render calls): Telegram's dispatcher
pacing (1.1s/chat) or an init-await must never delay the Obsidian `RPUSH`
past its sub-second latency target, and vice versa.

### 2.4 `UserInteractionApi` (`interaction/api.py`) — what plugins see

```python
ui = await UserInteractionApi.for_user(user)
async with ui.user_interaction(priority=UserInteractionPriority.LOW):
    choice = await ui.request_user_choice("Did you exercise?", ["Yes", "No"])
```

- Same public method set as `TelegramBotApi` today (R1 list), so migration is
  `TelegramBotApi.for_user` → `UserInteractionApi.for_user` plus import/type
  renames (and the error-type migration of §2.3). `TelegramBotApi.for_user`
  remains temporarily as a deprecated alias during the migration phase, then
  is removed.
- **Scope resolution.** `ui.user_interaction(...)` accepts
  `origin: SurfaceScope | None`:
  - `None` (default — every proactive plugin loop): broadcast to all
    reachable surfaces.
  - A specific surface/instance (set by the `/do` handler and the palette
    invocation path): the whole flow (prompts + `send_message`) is delivered
    only there. The scope is stored on the `UserInteraction` object; nested
    calls resolve the ambient scope **from the engine's current-interaction
    record for the user, not task-local state** — so sibling tasks (e.g. the
    vault-RPC progress-bar task, which is spawned alongside the flow's task)
    see the same scope.
  - `ui.excluding("obsidian")` returns a scoped facade whose sends resolve as
    **broadcast minus the excluded surface, overriding any ambient flow
    scope**. This is the R2 rule for Obsidian-health traffic: even inside an
    Obsidian-origin sticky flow, the vault-RPC progress bar and failure
    notifications must reach the *other* surfaces (an Obsidian-origin scope
    intersected with "except Obsidian" would otherwise be the empty set).
    `obsidian.py` migrates with one line.
  - `notify()` ignores flow scope (always all surfaces, minus exclusions).
- **Message handles.** `send_message` returns `MessageHandle` holding
  `{surface_name → SurfaceMessageId}` for each surface that delivered.
  `edit_message_text(handle, ...)`/`delete_message(handle)` fan out to those
  surfaces; surfaces without the capability were never in the map.
- **`has_active_user_interaction()`** delegates to the engine (per-user,
  surface-independent).

### 2.5 Prompt lifecycle & re-prompting

Owned by the engine, surface-neutral (replaces `wait_for_user_interaction`):

1. Mint `interaction_id`, `generation=0`; create future; call
   `render_prompt` concurrently on all in-scope, reachable surfaces.
2. Await the future with a 60-min timer. On timer: `generation += 1`,
   re-render on all in-scope surfaces (Telegram: delete + re-send, plus its
   surface-local "N pending interactions" notice, both implemented inside
   `TelegramSurface`; Obsidian: `re-prompt` message that refreshes the
   existing card — a no-op if already displayed). Loop forever, as today.
3. On answer: if the future for `interaction_id` is unresolved, resolve it
   (regardless of the answer's generation, §2.1); dismiss elsewhere.
   Tombstoned (resolved) id → `already-answered` to the sender; unknown id →
   `interaction-expired`.
4. On preemption/cancellation: dismiss on all surfaces
   (`dismiss_prompt`), then propagate `UserInteractionPreempted` as today.
5. **Origin-death cancellation:** when an Obsidian-origin flow's instance
   disables the surface (`instance-update(disabled)`) or its presence
   expires, `ObsidianSurface` tells the engine to cancel that flow via the
   preemption cancel path (so `UserInteractionPreempted` semantics hold and
   the user's single interaction slot is released — otherwise the flow would
   re-prompt hourly into a surface that no longer exists).
6. Server restart: all state is in-memory, so pending prompts die with the
   process. Any later answer carries an unknown `interaction_id` → the events
   consumer replies `interaction-expired` (rendered in the sidebar as
   "expired — re-run the command"; Telegram's existing stale-keyboard answer
   covers its side).

### 2.6 `CommandRegistry` (`interaction/commands.py`)

- `PluginCommand(text: str, callback: Callable, id: str | None = None)` —
  `id` defaults to `f"{plugin.canonical_name}.{slugify(text)}"` at
  registration. A plugin that renames display text passes the old `id`
  explicitly to keep it stable. Registry: `dict[command_id → RegisteredCommand
  (plugin_canonical_name, text, callback)]`, module-level, populated at
  startup via `UserInteractionApi.register_command` (registration order is
  preserved for menu display).
- `commands_for_user(user) -> list[(id, plugin_name, text)]` filters by
  `user.plugins` — used by both the `/do` menu and the Obsidian command-list
  publisher.
- **Publish triggers** for the per-user command list (§3.4): (a) a debounced
  publish ~2s after the last `register_command` call (plugin `run()`
  coroutines never return, so there is no "startup complete" barrier to hook);
  (b) **`Plugin.register_user` / `unregister_user`** — `user.plugins` is the
  only input that changes at runtime, and without this trigger a plugin
  registered via the plugin-manager flow would never reach the palette until
  the next server restart. Each publish also pushes a `commands-updated`
  nudge to live instances.
- `/do` (`TelegramBotPlugin.show_command_menu`) is rewritten to read from the
  registry and to open its flow with `origin=telegram`. **User-initiated
  flows share a single per-user slot with cross-surface cancel-and-replace**
  (a `current_user_flow_task` in the engine replaces `_do_tasks`): invoking a
  palette command cancels an in-progress `/do` flow and vice versa, newest
  intent wins. This deliberately broadens today's `/do`-cancels-`/do`
  behavior across surfaces — without it, a HIGH palette command would queue
  forever behind an abandoned open `/do` menu, since HIGH does not preempt
  HIGH. (Decided with the user; the cross-surface case is in the test plan.)

## 3. Redis protocol (server ⇄ Obsidian instances)

All keys namespaced `obsidian-surface:{user_id}:…`, JSON payloads, consistent
with the existing RPC convention. The existing vault-RPC queues
(`obsidian-plugin-tasks:*`) and the monitor queue are **untouched**.

**Ordering rule for every queue in this protocol: server/producer `RPUSH`,
consumer `BLPOP` — strict FIFO.** (The existing vault RPC gets away with
`LPUSH`+`BLPOP` — LIFO — because it is one-request-at-a-time; the interaction
channel is not, and prompt/dismiss or dismiss/new-prompt pairs must never
invert.) Caps are therefore `LTRIM key -N -1` (keep newest at the tail, drop
oldest first).

### 3.1 Instance registration & presence

- **Instance id:** UUID minted per device per vault, stored via Obsidian's
  `app.saveLocalStorage` (device-local — deliberately *not* in `data.json`,
  which may sync between devices and would collide instance ids).
- `…:instances` — Redis hash: `instance_id → {vault, device, enabled,
  registered_at, last_seen}`. Written by the instance on load and on toggle
  change (this doubles as the server-visible toggle state, R6; propagation is
  one Redis write, well under the 5s bound). `last_seen` is refreshed ~hourly
  alongside a presence write — it exists so GC is computable (the presence
  key's 90s TTL leaves no trace).
- `…:{instance_id}:presence` — `SET EX 90`, refreshed **from the poll loop**
  (after each `BLPOP` returns), not from an independent timer — a wedged poll
  loop must look unreachable, otherwise the server fans out into a queue
  nobody drains. Also refreshed on every event sent. Reachable instance =
  hash entry with `enabled` **and** live presence key.
- **GC:** server sweeps the hash lazily (on fan-out) and drops entries whose
  `last_seen` is > 30 days old, deleting their queues.

### 3.2 Server → instance (fan-out)

- `…:{instance_id}:inbound` — FIFO list (see ordering rule); server `RPUSH`es
  to every reachable instance; instance `BLPOP`s on a **dedicated**
  duplicated Redis connection. Capped (`LTRIM -100 -1`) so a wedged instance
  can't grow unboundedly.
- Every envelope carries a per-instance monotonic sequence number `seq`:
  `{seq, kind, payload}`. Kinds:
  - `prompt` — full `PromptSpec` (interaction_id, generation, kind, content +
    parse flags, choices, columns, flow info, `issued_at`)
  - `re-prompt` — same payload; refresh-or-render
  - `dismiss-prompt` — `{interaction_id, reason: answered|preempted|cancelled|toggle}`
  - `message` / `update-message` / `retract-message` — conversational
    transcript ops (`{message_id, content}`)
  - `notification` — live `notify` delivery (toast + inbox entry already
    written by server, carries inbox `entry_id`)
  - `already-answered`, `interaction-expired`, `flow-error` — feedback
  - `commands-updated` — nudge to re-fetch the command list
  - `sync-reply` — see below
- **Staleness guard (R5 "live-only"):** `prompt` carries `issued_at`; the
  client discards prompts older than 120s on pop (they were queued while it
  was closed) and any whose interaction_id it has already seen dismissed.
  The same rule applies to `re-prompt`. Dismissals ride the same FIFO queue;
  an instance that misses a dismissal entirely (offline past the cap)
  discards the zombie prompt via the staleness rule or the sync below.
- **Reconnect sync:** on connect, the instance sends `sync`; the server
  replies with `sync-reply` carrying the current pending prompt for that user
  (if any, and if its scope includes this instance), the current command-list
  version, and the `seq` high-water mark at reply time. The client applies
  the reply as authoritative state and then processes **only envelopes with
  `seq` greater than the reply's high-water mark** — pure queue order is not
  trusted for the sync race (a fresh prompt pushed between the sync request
  and its reply must not be clobbered by the reply, nor double-rendered).

### 3.3 Instance → server

- `…:events` — single FIFO list (`RPUSH` by instances, server `BLPOP`s;
  server is the only consumer — safe). Envelope
  `{kind, instance_id, payload}`:
  - `answer` — `{interaction_id, generation, value: int|str}` (generation is
    diagnostic only, §2.1)
  - `invoke-command` — `{command_id}` → server validates against
    `commands_for_user`, then cancel-and-replace flow at HIGH priority
    (§2.6); invalid → `flow-error` back to that instance
  - `sync` — see above
  - `instance-update` — toggle/registration changes (also writes the hash
    directly; the event makes the server react immediately: dismissing
    displayed prompts and **cancelling Obsidian-origin flows** on toggle-off,
    §2.5)
- The events consumer is a task inside `ObsidianSurface`, started with the
  server, resilient to malformed JSON (log + skip).

### 3.4 Command list & notification inbox (read-model keys, not queues)

- `…:command-list` — `SET` JSON `{version, commands: [{id, plugin, text}]}`
  written by the server per §2.6's publish triggers. Client `GET`s on load,
  every 5 min, and on `commands-updated`. Version is a hash of the content;
  unchanged version = no palette re-registration.
- `…:inbox` — list of `{entry_id, ts, content, parse flags}`; server
  `RPUSH + LTRIM -200 -1` from `ObsidianSurface.notify`. Clients read
  non-destructively (`LRANGE`) so all instances see the full inbox.
  Read state is client-local (`saveLocalStorage`: last-read `entry_id` +
  cleared-up-to marker) — per-device unread badges, per R4. "Clear inbox"
  only moves the local marker; entries age out via the cap.

## 4. Obsidian plugin design (`spanreed-obsidian` repo)

### 4.1 Restructure

`main.ts` (636 lines) splits into `src/` (esbuild entry point updated):

- `src/main.ts` — plugin class, wiring only
- `src/settings.ts` — settings tab + types (adds the surface toggle per
  environment)
- `src/redis.ts` — connection manager: the existing RPC client + a
  `duplicate()`d connection **dedicated to the inbound `BLPOP`** + a third
  for presence writes **and all event/answer `RPUSH`es** (node-redis
  serializes commands per connection — an answer sharing the BLPOP
  connection would wait out the blocking pop, destroying the ~ms latency
  claim). **`onunload` must close/abort all clients, including the blocked
  BLPOP** (via `client.disconnect()`): a leaked blocked consumer from a
  previous plugin load would steal inbound messages from the new one.
- `src/vault-rpc.ts` — the existing method handlers, moved verbatim
- `src/surface/client.ts` — instance id, registration, presence heartbeat,
  inbound poll loop, event sending, reconnect `sync`. **All surface state
  (current prompt, transcript, seq high-water mark, unread counts) lives
  here** — the view is a pure projection of it, because the leaf can be
  closed and recreated at any time, including mid-prompt.
- `src/surface/commands.ts` — dynamic palette registration
- `src/surface/view.ts` — the `SpanreedView` sidebar `ItemView`
- `src/surface/inbox.ts` — inbox fetch/read-state
- `src/surface/render.ts` — content rendering (§4.4)

### 4.2 Dynamic palette commands

- On command-list fetch: diff against currently registered; add via
  `this.addCommand({id: cmd.id, name: text, callback})` — **no "Spanreed: "
  prefix**; Obsidian prepends the plugin display name to palette entries
  already, and a manual prefix would render "Spanreed: Spanreed: …".
  Remove stale via `(app as any).commands.removeCommand(fullId)` — the same
  undocumented-API tier the plugin already uses for `executeCommandById`;
  acceptable for a personal plugin, noted as a fragility.
- Command callback = push `invoke-command` event (on the write connection) +
  reveal the sidebar view. Latency budget: one `RPUSH` (~ms); the flow's
  first prompt arrives via the inbound queue.

### 4.3 `SpanreedView` (sidebar)

- Registered `ItemView` (`registerView(VIEW_TYPE_SPANREED, ...)`), right
  sidebar by default, ribbon icon replaces the sample dice icon. Renders
  purely from `surface/client.ts` state (§4.1); closing/reopening the leaf
  mid-prompt loses nothing.
- Two stacked sections: **Conversation** (top) and **Inbox** (bottom,
  collapsible).
- Conversation renders the current flow: transcript of `message`s, then the
  active prompt card — buttons for `choice` (vertical list; `columns` hint
  used only when it fits), text field + submit for `input`. Answering
  disables the card optimistically; `already-answered`/`interaction-expired`
  replace it with a status line. `dismiss-prompt(preempted)` swaps in the
  preempting flow's card. A degraded `send_document` renders as a message
  naming the file (with a vault link when the path is inside the vault —
  e.g. the timekiller scan flow, whose PDF is already written to the vault).
  Transcript is per-flow and cleared when a new flow starts (history
  retention beyond the active flow is out of scope, v1).
- Prompt arrival: `revealLeaf` + `new Notice` if the view is closed;
  unread-style badge on the ribbon icon as fallback.
- Toggle off: view shows a "surface disabled" state; client sends
  `instance-update`, stops the poll loop, unregisters palette commands.

### 4.4 Content rendering

- `parse_markdown` (Telegram MarkdownV2): unescape **only
  backslash-before-punctuation, and only outside code spans/fences** (the
  monitor sends tracebacks in code fences with `parse_markdown=True`;
  literal `\n`s, Windows paths, and regexes inside them must survive), then
  render with Obsidian's `MarkdownRenderer.render()`.
- `parse_html` (Telegram HTML subset: `b/i/u/s/a/code/pre/blockquote`):
  convert tags → Markdown equivalents (tiny mapper, whitelist-based; unknown
  tags stripped), then `MarkdownRenderer.render()`. No raw HTML injection.
- Plain text: rendered as text. All three paths share `render.ts` and are
  used by conversation, prompt cards, toasts (toast gets a plain-text
  downgrade), and inbox.

## 5. Migration & delivery phases

Each phase lands independently deployable; Telegram behavior is verified
unchanged at every step (with the two deliberate exceptions noted below).

1. **Extract the engine + facade (spanreed only).** Move
   queues/priorities/preemption/pending-answers to `interaction/`;
   `TelegramSurface` adapter; `UserInteractionApi` with Telegram as the only
   surface. Mechanically migrate all plugins and the shared test helper
   (`patch_telegram_bot` → `patch_user_interaction`). Two non-mechanical
   pieces, called out: (a) the §2.3 error migration (`RetryAfter` →
   `SurfaceBusyError` in `plugin.py` supervisor + 3 plugins); (b)
   `UserInteractionApi.register_command` ships as a **thin shim over the
   existing `bot_data` mechanism** (retaining its await-on-Telegram-init),
   because the real registry and the `/do` rewrite land together in phase 2 —
   otherwise phase 1 would strand registrations where `/do` can't see them.
   Ship; soak in prod.
2. **Command registry.** Stable ids, registry replaces the `bot_data` shim,
   `/do` rewritten to read from it, shared cross-surface flow slot (§2.6),
   publish triggers wired to `register_user`/`unregister_user`. **Deliberate
   behavior changes to verify:** (a) plugin startup no longer serializes
   behind Telegram polling init (registration no longer awaits the Telegram
   app — `run()` bodies proceed earlier); (b) `/do` re-invocation semantics
   now live in the shared slot. Ship.
3. **ObsidianSurface + protocol (server side).** Instance registry, fan-out,
   events consumer, command-list publisher, inbox writer. Feature-flagged by
   the absence of registered instances — inert in prod until the plugin ships.
4. **Obsidian plugin.** Restructure, surface client, palette, sidebar view,
   inbox, toggle. Test against staging end-to-end, then production.
5. **Hardening.** Multi-instance testing, restart/reconnect scenarios,
   `ObsidianApi` progress-bar migration to `ui.excluding("obsidian")`, the
   acceptance-criteria checklist from the requirements doc run end-to-end.

## 6. Testing strategy

- **Engine unit tests** (new, most valuable): fake surfaces; cover priority
  ordering, preemption + propagation, first-answer-wins + late answer
  (tombstone vs expired), dismissal fan-out, re-prompt generations +
  stale-generation answers still resolving, broadcast partial failure
  (one surface raising `SurfaceBusyError`), scope algebra (origin-sticky
  flow + `excluding("obsidian")` from a sibling task), cross-surface flow
  cancel-and-replace, origin-death cancellation, zero-surface operation.
- **Protocol tests** with `fakeredis`: FIFO ordering of prompt/dismiss
  pairs, fan-out to N instances, presence expiry, staleness discard,
  sync-reply seq high-water-mark race, events-consumer resilience to garbage
  payloads, inbox cap + non-destructive reads.
- **Existing suite:** the ~15 plugin test modules migrate mechanically with
  the renamed patch helper. **Carve-out:** `telegram_bot_test.py`'s
  interaction-machinery tests (locks, queues, cancellation) do not migrate —
  they are rewritten as engine unit tests against `interaction/engine.py`,
  with only Telegram-adapter-specific tests remaining.
- **Obsidian plugin:** no test infra exists today; keep it that way for v1
  (personal project) but keep `surface/` logic (diffing, staleness rules,
  seq handling, render mapping) in pure functions to allow tests later.
  End-to-end verification = the acceptance checklist against staging,
  explicitly including: palette-origin flow with slow vault RPC, toggle-off
  mid-flow, two instances answering the same prompt, and Obsidian answering
  a prompt issued before its laptop slept.

## 7. Explicitly out of scope (v1)

- Conversation history beyond the active flow in the sidebar.
- `send_document` content delivery on Obsidian (degrades to a filename
  message / vault link; no byte transfer).
- Command-palette entries for *starting* plugin registration flows
  (plugin-manager flows work, but via the generic command path only).
- Any HTTP transport, auth hardening, or mobile support (future work per the
  requirements doc).
