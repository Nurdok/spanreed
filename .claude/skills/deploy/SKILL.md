---
name: deploy
description: Deploy spanreed to production (root@spanreed.ink) — pull latest main, poetry install, restart the systemd service, then actively monitor the startup logs to confirm it came up clean. Use when the user asks to deploy, ship, release, or push spanreed live to the server.
user-invocable: true
allowed-tools:
  - Bash
  - Read
---

# /deploy — Deploy spanreed to production

Deploys the current `main` to the production server and **watches the startup
logs** to confirm the service comes back up cleanly. Target:

- Host: `root@spanreed.ink` (SSH; the key is already configured). The remote
  login shell is **zsh** — beware zsh read-only vars (`status`, `SECONDS` is
  fine).
- Code: `/opt/spanreed`, owned by the `spanreed` user
- Service: systemd unit `spanreed` (ExecStart uses
  `/home/spanreed/.pyenv/bin/pyenv exec poetry run python ...`)
- Poetry runs under pyenv at **`/home/spanreed/.pyenv/bin/pyenv`**
  (NOT `/opt/pyenv` — that path does not exist on the box).

## Preconditions

- This is a **production** change. Only run it when the user explicitly asked
  to deploy in this session.
- It deploys whatever is on `main`. Make sure the intended work is already
  merged — this only `git pull`s on the server, it never pushes.

## Steps

Run these as separate SSH calls so a failure is caught before the restart.

### 1. Pull + install (as the `spanreed` user)

```
ssh -o BatchMode=yes root@spanreed.ink 'runuser -l spanreed -c "cd /opt/spanreed && git pull && /home/spanreed/.pyenv/bin/pyenv exec poetry install"'
```

- The server's Poetry is older than the lock's format, so `poetry install`
  prints a "lock file might not be compatible" warning — that's harmless as
  long as it then installs cleanly / reports up to date.
- If `git pull` reports `Already up to date.` and nothing else changed, tell
  the user there's nothing new to deploy and ask whether to restart anyway.
- If `git pull` or `poetry install` **fails**, STOP. The service is still
  running the old code — do **not** restart (it could crash on missing/old
  deps). Report the error.

### 2. Reload + restart (only if step 1 succeeded)

```
ssh -o BatchMode=yes root@spanreed.ink 'systemctl daemon-reload && systemctl restart spanreed'
```

### 3. Monitor the startup — the important part

Actively watch the logs until startup is confirmed (or a fatal error /
timeout). Poll on the server so it returns as soon as the service is up, and
always ends within ~90s. The journal is flooded with gunicorn HTTP 404
scanner noise AND very chatty Obsidian watchdog DEBUG lines — filter both.
Note the loop var is `mres`, not `status` (zsh read-only).

```
ssh -o BatchMode=yes root@spanreed.ink 'deadline=$((SECONDS+90)); mres=TIMEOUT; while [ $SECONDS -lt $deadline ]; do log=$(journalctl -u spanreed -S -3m --no-pager); if printf "%s" "$log" | grep -qE "ModuleNotFoundError|ImportError"; then mres=FATAL; break; fi; if printf "%s" "$log" | grep -q "Started polling"; then mres=OK; break; fi; sleep 3; done; echo "MONITOR_RESULT=$mres"; echo "SERVICE=$(systemctl is-active spanreed)"; echo "=== startup markers ==="; journalctl -u spanreed -S -3m --no-pager | grep -iE "Running [0-9]+ plugins|Started polling|Started outbound|crashed for user|Outbound delivery deferred|Flood control|ModuleNotFoundError|ImportError" | tail -40'
```

Interpret the result — a plain `Traceback` is NOT necessarily a deploy
failure, because the supervisor catches per-plugin crashes and retries. Judge
the deploy on:

**Deploy succeeded when all of:**
- `MONITOR_RESULT=OK` and `SERVICE=active`
- `Running N plugins` appears and startup reached `Started polling`
- `Started outbound dispatcher for user ...` (the per-user send dispatchers
  came up; before 2026-07 this marker read `Started outbound-message
  consumer`)
- **no** `ModuleNotFoundError` / `ImportError` — especially
  `No module named 'aiolimiter'`, which means `poetry install` didn't take.

**Report separately (not a deploy failure, but surface it):**
- `... crashed for user N; restarting in 60s.` — a plugin is throwing and the
  supervisor is retrying it. Pull the full traceback
  (`journalctl -u spanreed -S -3m | grep -A25 "crashed for user"`) and report
  the root cause. This is expected supervisor behavior, not a deploy break.
- `Outbound delivery deferred (...)` — the durable queue is holding notices
  because the Telegram bot is flood-limited; they deliver once the ban lifts.

## Report

Summarize: what was pulled, whether `poetry install` changed anything, the
monitor result + service status, the key startup markers (plugins, polling,
consumers), and — separately — any supervised plugin crashes or deferred
outbound deliveries worth the user's attention.

## On failure / rollback

- `MONITOR_RESULT=FATAL` or service not `active` → surface the traceback and
  stop.
- To roll back: on the server, as the `spanreed` user,
  `cd /opt/spanreed && git reset --hard <previous-commit>`, then re-run
  `/home/spanreed/.pyenv/bin/pyenv exec poetry install`, then
  `systemctl restart spanreed`, then re-run the step-3 monitor. Confirm the
  previous commit hash with the user before any destructive reset.
