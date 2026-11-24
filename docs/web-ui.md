# Web UI for commands

`https://spanreed.ink/app/` runs the same commands as the Telegram `/do` menu
and the Obsidian palette, in a browser. It exists for when Telegram flood
control blocks the bot: a command started here never touches Telegram.

![Web UI on desktop, mid-flow in Sync steps](images/web-ui-desktop.png)

On a phone the messages fill the screen. The command list is a drawer: tap
☰ or swipe right to pull it out, and it closes when you pick a command. In
Settings the same drawer holds the list of plugins. On a wider screen the
list stays beside the messages.

![Web UI on a phone](images/web-ui-phone.png)
![The command drawer on a phone](images/web-ui-phone-drawer.png)

## How it fits

- **A third interaction surface.** `spanreed.interaction.web_surface.WebSurface`
  implements `UserInteractionSurface` next to Telegram and Obsidian. Plugins
  don't change: `send_message`, `request_user_choice`, `request_user_input`,
  `notify`, `send_document`, edits and deletes all render in the page.
- **Same registry, same flow semantics.** The command list is
  `commands.commands_for_user(user)`. Running one starts a HIGH-priority
  user-initiated flow through `engine.start_user_flow` (the slot shared with
  `/do` and the palette, newest wins), scoped with
  `SurfaceScope(origin_surface="web")` so all of the flow's traffic goes to
  the page only.
- **Proactive prompts too.** While a page is open (it polls at least every
  25s; presence lasts 60s), or while a device has notifications turned on,
  broadcast traffic from plugins also reaches the web UI.
- **Transport.** Plain HTTP: JSON calls plus a long-poll on
  `/app/api/events`. No WebSocket, so nothing to change in nginx. The feed
  is in-process memory, like the engine's pending prompts, so it resets on
  restart; the page reloads its state when that happens. `notify()` is also
  kept in a small Redis inbox (`web-surface:{user_id}:inbox`).

The routes are a Quart blueprint (`spanreed.web.routes`) registered on the
existing Web UI plugin app on `127.0.0.1:5000`, which nginx already proxies
for the OAuth callbacks.

## An app on your phone, with notifications

The web UI can be installed as an app and can send push notifications, so
it can stand in for Telegram on a phone.

**Set it up (Android, Chrome):**

1. Open `https://spanreed.ink/app/` and log in.
2. Chrome menu → **Add to Home screen** → **Install**.
3. Open the installed app and tap **🔕 Off** in the header. Allow
   notifications when asked. The button turns to **🔔 On** and a test
   notification arrives.

Notifications are per device: turn them on in each browser or phone that
should get them. If they arrive late, exempt Chrome from battery
optimisation in Android's settings.

**What gets a notification:** a prompt, a message or a notification that a
plugin sends on its own. Tapping it opens the app, where the prompt is
waiting. A prompt that is asked again replaces its earlier notification.
Nothing is pushed for replies inside a command you started from the web UI,
or while the app is on screen.

**How it works:**

- `static/manifest.webmanifest` and the icons make the page installable.
  The icons are the feather logo, centred on white; `badge-96.png` is its
  silhouette, which Android shows in the status bar for a notification.
  `static/sw.js` is the service worker: it shows the notifications and opens
  the app when one is tapped. It caches nothing.
- `spanreed.web.push` keeps the server's key pair (created on first use,
  Redis key `web-ui:vapid`) and each user's subscriptions (Redis hash
  `web-ui:user:{id}:push`), and sends the pushes with `pywebpush`. The
  payload is encrypted for the browser. A subscription that the push service
  reports as gone is deleted. Only the push services of Chrome, Firefox,
  Safari and Edge are accepted as endpoints.
- `WebSurface` pushes when it publishes a prompt, message or notification.
  The push is sent in the background and never fails the plugin's call: it
  is only a hint to open the app.

**Routing, and Telegram as the fallback:**

- With notifications on, the web surface is reachable even when no page is
  open, and it counts as able to alert the user unattended.
- Telegram is a fallback surface. While another surface can alert the user
  unattended, unprompted traffic (prompts, messages and notifications from
  plugins) does not go to Telegram at all. With notifications off on every
  device, it goes to Telegram as before.
- A command started from Telegram's `/do` still talks on Telegram.
- A surface that fails does not stop the others. During a Telegram ban,
  every kind of send, edit and delete still reaches the other surfaces; the
  ban is only reported to the plugin when no other surface delivered.

## Workflows

**Workflows** in the header shows the workflows you own (see
[workflows.md](workflows.md)). It reads, controls and edits them, and makes
new ones.

![A workflow's steps on desktop](images/web-ui-workflow-steps-desktop.png)

- **The list** has one row for each workflow: its name, a mark for each
  block it uses, and how its last run went. On a phone the list is the
  drawer, as the commands are.
- **Steps** draws the workflow as cards: the trigger, marked T, then the
  steps, numbered as the engine numbers them in its messages. A card shows
  the block, the plugin it comes from, its inputs as they are stored and the
  names of its outputs. A value that comes from an earlier card is drawn as
  a chip with that card's mark. The cards go left to right, and top to
  bottom on a phone.
- **A loop**, such as "For each", holds the cards of the steps inside it, top
  to bottom, numbered 1.1, 1.2.
- **A branch**, "If", holds two lists of cards: the steps for when its
  condition holds, and the steps for when it does not.
- **History** lists the last 30 runs. Opening a run shows what the trigger
  gave and what each step got and gave, and where the run stopped or failed.
  A loop shows each time it went through its steps, as "Item 3 of 5". The one
  that failed is open. A branch shows the side that ran.
- **The switch** turns the workflow on and off at once.
- **Run now** is there if the trigger can be started by hand. It runs the
  workflow as its command does and goes to the conversation, where what the
  run asks and says shows up.
- **Show it / Hide it** puts the workflow's command in the command menu or
  takes it out.
- **Remembers…** is there if the workflow keeps something between runs.
  **Show** lists the names, and can make it forget one or all of them.

![A workflow's history on desktop](images/web-ui-workflow-history-desktop.png)

![The conversation after Run now](images/web-ui-workflow-run-desktop.png)

![A workflow on a phone](images/web-ui-workflow-steps-phone.png)
![A run on a phone](images/web-ui-workflow-history-phone.png)

A workflow with a loop, and a run of it in which the third item failed:

![A loop on desktop](images/web-ui-workflow-loop-desktop.png)
![A run of a loop on desktop](images/web-ui-workflow-loop-history-desktop.png)

![A loop on a phone](images/web-ui-workflow-loop-phone.png)
![A run of a loop on a phone](images/web-ui-workflow-loop-history-phone.png)

A workflow that asks you something, and the conversation after **Run now**:

![A workflow that asks on desktop](images/web-ui-workflow-ask-desktop.png)
![Its conversation on desktop](images/web-ui-workflow-ask-run-desktop.png)
![The end of its conversation on desktop](images/web-ui-workflow-ask-run-done-desktop.png)

![A workflow that asks on a phone](images/web-ui-workflow-ask-phone.png)
![Its conversation on a phone](images/web-ui-workflow-ask-run-phone.png)

History shows, under each step that asked, what it asked and what you
answered:

![The conversation in the history on desktop](images/web-ui-workflow-ask-history-desktop.png)
![The conversation in the history on a phone](images/web-ui-workflow-ask-history-phone.png)

A question that holds steps, "Ask me to pick, one at a time", shows them
under **After each pick**. Its options are rows, so a habit is added or
removed as a row. History shows each pick with the steps that ran for it:

![A question that holds steps on desktop](images/web-ui-workflow-ask-each-desktop.png)
![Editing its options on desktop](images/web-ui-workflow-ask-each-edit-desktop.png)
![Its history on desktop](images/web-ui-workflow-ask-each-history-desktop.png)

![A question that holds steps on a phone](images/web-ui-workflow-ask-each-phone.png)
![Editing its options on a phone](images/web-ui-workflow-ask-each-edit-phone.png)
![Its history on a phone](images/web-ui-workflow-ask-each-history-phone.png)

A block can hold a whole conversation, as "Link a note to a book" does in
this loop. History shows what it asked in each round:

![A loop with a conversation in a block on desktop](images/web-ui-workflow-audible-desktop.png)
![Its history on desktop](images/web-ui-workflow-audible-history-desktop.png)

![A loop with a conversation in a block on a phone](images/web-ui-workflow-audible-phone.png)
![Its history on a phone](images/web-ui-workflow-audible-history-phone.png)

A workflow that runs one of several others, at random times, and the
history of a workflow that it ran:

![A workflow that runs another on desktop](images/web-ui-workflow-call-desktop.png)
![The history of the one it ran on desktop](images/web-ui-workflow-called-history-desktop.png)

![A workflow that runs another on a phone](images/web-ui-workflow-call-phone.png)
![The history of the one it ran on a phone](images/web-ui-workflow-called-history-phone.png)

"Kill time" started from the page: it asks which of its four parts to run,
and the part goes on in the same conversation:

![Kill time asks which one, on desktop](images/web-ui-workflow-kill-time-run-desktop.png)
![The conversation of the part, on desktop](images/web-ui-workflow-kill-time-run-done-desktop.png)

![Kill time asks which one, on a phone](images/web-ui-workflow-kill-time-run-phone.png)
![The conversation of the part, on a phone](images/web-ui-workflow-kill-time-run-feelings-phone.png)

The workflow that picks, and one of its parts, on a phone:

![Kill time on a phone](images/web-ui-workflow-kill-time-phone.png)
![Mood on a phone](images/web-ui-workflow-mood-phone.png)

What a workflow remembers:

![What a workflow remembers on desktop](images/web-ui-workflow-memory-desktop.png)
![What a workflow remembers on a phone](images/web-ui-workflow-memory-phone.png)

A workflow that cannot run says why above its steps, and a card whose block
no plugin publishes any more is outlined in red.

### Editing a workflow

**Edit** turns the cards into forms. The name of the workflow and the text of
its command can be changed there too.

![A workflow being edited](images/web-ui-workflow-edit-desktop.png)

- **A text box** holds text and the values of earlier cards, written as
  `{{ trigger.date }}`. **+ value** lists what the cards before this one
  give, and puts the one you pick where the cursor was. A box that wants a
  number or yes/no says so under it.
- **A choice** is a list to pick from.
- **Rows**, such as the properties to set, have a name and a value each.
  **Add a row** adds one, and × removes one.
- **+** on the line between two cards, and after the last, adds a step
  there. It opens the list of blocks, by plugin, with a box to search it. A
  block of a plugin that is not set up is listed, and says what to do. A loop
  has a + inside it for its own steps.
- **← →** on a card move the step earlier or later, and **Remove** takes it
  out. In a loop and on a phone the arrows point up and down. A step moves
  within its list: to put it into a loop or take it out, remove it and add it
  again.
- **Save** stores the workflow, and it runs with the new values from then
  on. If a value does not fit, or the workflow could not run, nothing is
  stored and the page says what has to change: a step that was moved before
  one it takes a value from, for example.
- **Test** runs the workflow once as it is in the editor, saved or not, and
  shows what each step got and gave, above the cards. The run is for real:
  what the steps write is written, and what they send is sent. It runs for
  the event of the workflow's last run, or, for a trigger that needs no
  question, for an event made at that moment. A workflow that has neither
  says so. A test is listed in History as "Test". A workflow that asks you
  something is not tested: save it and use **Run now**.
- **Cancel**, another workflow or another view asks before it drops changes.
- **Delete this workflow** deletes it with its history, after asking.

![The list of blocks](images/web-ui-workflow-add-step-desktop.png)

![The values a step inside a loop can use](images/web-ui-workflow-edit-values-desktop.png)

![A value that does not fit](images/web-ui-workflow-edit-refused-desktop.png)

![Editing on a phone](images/web-ui-workflow-edit-phone.png)
![The list of values on a phone](images/web-ui-workflow-edit-values-phone.png)

![A loop being edited on a phone](images/web-ui-workflow-edit-loop-phone.png)
![The list of blocks on a phone](images/web-ui-workflow-add-step-phone.png)

A branch: as it is shown, while it is edited, and in what a test did.

![A workflow with a branch](images/web-ui-workflow-branch-desktop.png)
![A branch being edited](images/web-ui-workflow-branch-edit-desktop.png)
![The side of a branch that a test ran](images/web-ui-workflow-branch-test-desktop.png)

![A workflow with a branch, on a phone](images/web-ui-workflow-branch-phone.png)
![A branch being edited on a phone](images/web-ui-workflow-branch-edit-phone.png)

What a test did:

![What a test did](images/web-ui-workflow-test-desktop.png)

![Choosing what to test with, on a phone](images/web-ui-workflow-test-choose-phone.png)
![What a test did, on a phone](images/web-ui-workflow-test-phone.png)

### Two workflows that write the same thing

Two workflows may set the same property of a note, or write the same file.
Both run, and the one that runs last wins. A workflow says so under its name
when another workflow that is on writes what it writes.

![Two workflows write the same property](images/web-ui-workflow-same-property-desktop.png)

What counts as the same is the text as it is stored: the property `weight`
of `Daily/{{ trigger.date }}.md` in both. Two paths that are written
differently and come to the same note are not noticed.

### A new workflow

**New workflow**, under the list, offers the recipes and the triggers.

![A new workflow](images/web-ui-workflow-new-desktop.png)

- **From a recipe** adds the workflow as it ships, turned on. A recipe you
  have already, or one that needs a plugin you have not set up, is listed and
  says so.
- **Start with a trigger** makes a workflow with that trigger and no steps,
  and opens it in the editor. It is off until you turn it on.

The API is `GET /app/api/workflows` (the list), `POST` on the same path (a
new workflow), `GET /app/api/workflows/catalog` (the blocks and the recipes),
`GET /app/api/workflows/<id>` (one workflow as cards), `PUT` and `DELETE` on
the same path, `GET /app/api/workflows/<id>/runs` (the traces), and `POST` on
`/app/api/workflows/<id>/enabled`, `/command`, `/run` and `/test`. All of them answer
only for a workflow of the logged-in user. A trace holds what the steps
received and returned, so it is sent only to its owner and only when the
History tab asks for it: the list carries just the time and the outcome of
the last run.

## Plugin settings

**Settings → Account** holds what is about you and not a plugin: who is
logged in, the Google link, and **Log out**. They are there, and not in the
header, so the header fits a phone screen.

![Settings → Account on a phone](images/web-ui-account-phone.png)

**Settings** in the header switches from the command list to a form for each
plugin's configuration, so a setting can be changed without going through
the plugin's chat setup again.

![Plugin settings form](images/web-ui-settings.png)

- **Generated from the config dataclass.** `spanreed.config_form` reads the
  type hints of the plugin's `UserConfig` and describes it as a form: text,
  numbers, yes/no, optional values, lists that can be added to and reordered,
  and nested groups. A field of any other type (a free-form `dict`, for
  example) is edited as JSON text. A new plugin config gets a form with no
  extra code.
- **Which plugins are listed.** The ones the user is registered to that have
  a user config with at least one field to edit.
- **Saving.** The form's values are checked against the field types, then
  the config is built the same way a saved one is loaded, so checks in the
  dataclass's `__post_init__` run too. Nothing is saved if anything fails.
  The saved config is what the plugin reads the next time it loads its
  config; a plugin that loads it only once at startup picks it up after a
  restart, as with **Reconfigure an existing plugin**.
- **Secrets.** A text field whose name looks like a secret (`token`,
  `api_key`, `password`...) is never sent to the browser. It shows an empty
  password box, and leaving it empty keeps the saved value.
- **Hidden fields.** A field marked `metadata={"config_form": "hidden"}` is
  not in the form and cannot be changed through it. Use it for values that
  only the plugin's own setup may set, like OAuth tokens or the Health
  Connect token. `"secret"` and `"plain"` override the name check, and
  `"config_help"` adds a line of help text under the field.

The API is `GET /app/api/settings` (the list), and `GET` and `PUT` on
`/app/api/settings/<plugin>`.

## Logging in

1. Run **Web UI login link** (User Profile plugin, so every user has it)
   from `/do` or the Obsidian palette. It sends a link that works once and
   expires in an hour. Opening the link shows a **Log in** button, and only
   pressing it (a POST) uses the link up. Chat apps fetch links to build
   previews, so a link that logged you in on GET would be used up by
   Telegram's preview before you tapped it.
2. Opening it starts a session: an `HttpOnly`, `Secure`, `SameSite=Lax`
   cookie on `/app`, valid for 90 days. Log in once while Telegram works and
   you keep access through a ban.
3. If no surface can deliver a link, on the server:

   ```bash
   runuser -l spanreed -c 'cd /opt/spanreed && /home/spanreed/.pyenv/bin/pyenv exec poetry run python -m spanreed.web.login_link <user_id>'
   ```

Tokens and session ids are stored only as SHA-256 digests in Redis
(`web-ui:login-token:*`, `web-ui:session:*`). Every write request needs the
`X-Spanreed-Request: 1` header, which a cross-site page can't send without a
CORS preflight, so the cookie can't be used for CSRF. An answer is accepted
only for a prompt that belongs to the logged-in user.

## nginx

Nothing to change if nginx proxies the whole site to `127.0.0.1:5000`
(which the OAuth callbacks and the `/` smoke check imply). If it proxies only
specific locations, add:

```nginx
location /app/ {
    proxy_pass http://127.0.0.1:5000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;   # the long-poll holds for 25s
    proxy_buffering off;
}
```

## Sign in with Google

A permanent way in, so you don't need a new login link for each device.

1. Log in once with a login link, then go to **Settings → Account** and
   choose **Link Google account**. That links your Google account to your
   Spanreed user.
2. From then on, **Sign in with Google** on the login page logs you in.
   A Google account that isn't linked can't sign in. **Unlink Google**
   on the same page removes the link.

How it works: OpenID Connect authorization-code flow
(`spanreed.web.google_auth`), scopes `openid email`. The `state` is
single-use, kept in Redis for 10 minutes, and bound to the browser by a
cookie, which blocks login CSRF. The ID token's signature, audience, expiry
and `nonce` are checked, and its email must be verified. Links live in Redis
as `web-ui:google-sub:{sub}` → user id and `web-ui:user:{id}:google`. A
Google account can be linked to only one Spanreed user.

**Server setup (one time):**

- OAuth client: the server uses the client JSON under the Redis key
  `web_ui_google_credentials`. If that key isn't set, it uses the client
  that Gmail already uses (`gmail_credentials`), and then the one the
  removed Google Fit integration left in Redis (`googlefit_credentials`).
- In Google Cloud Console → APIs & Services → Credentials → that OAuth
  client, add the authorized redirect URI
  `https://spanreed.ink/app/google/callback`. Until you do, Google rejects
  the sign-in with `redirect_uri_mismatch`.
- If the consent screen is in "Testing", the Google account must be one of
  its test users.

The Google button only appears when a client config is found.
