# Web UI for commands

`https://spanreed.ink/app/` runs the same commands as the Telegram `/do` menu
and the Obsidian palette, in a browser. It exists for when Telegram flood
control blocks the bot: a command started here never touches Telegram.

![Web UI on desktop, mid-flow in Manage plugins](images/web-ui-desktop.png)

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
  25s; presence lasts 60s), broadcast traffic from plugins also reaches the
  web UI, so it can answer a prompt that Telegram failed to deliver.
- **Transport.** Plain HTTP: JSON calls plus a long-poll on
  `/app/api/events`. No WebSocket, so nothing to change in nginx. The feed
  is in-process memory, like the engine's pending prompts, so it resets on
  restart; the page reloads its state when that happens. `notify()` is also
  kept in a small Redis inbox (`web-surface:{user_id}:inbox`).

The routes are a Quart blueprint (`spanreed.web.routes`) registered on the
existing Web UI plugin app on `127.0.0.1:5000`, which nginx already proxies
for the OAuth callbacks.

## Logging in

1. Run **Web UI login link** (User Profile plugin, so every user has it)
   from `/do` or the Obsidian palette. It sends a link that works once and
   expires in an hour.
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
