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

- Host: `root@spanreed.ink` (SSH; the key is already configured)
- Code: `/opt/spanreed`, owned by the `spanreed` user
- Service: systemd unit `spanreed`
- Poetry runs under pyenv: `/opt/pyenv/bin/pyenv exec poetry`

## Canonical command (what the user runs manually)

```
runuser -l spanreed -c " cd /opt/spanreed; git pull; /opt/pyenv/bin/pyenv exec poetry install" && systemctl daemon-reload && systemctl restart spanreed && journalctl -f -u spanreed -S -2m --no-pager
```

The final `journalctl -f` follows forever, so don't run it verbatim — instead
**follow the logs on a timeout** (step 3) so the monitoring ends on its own.

## Preconditions

- This is a **production** change. Only run it when the user explicitly asked
  to deploy in this session.
- It deploys whatever is on `main`. Make sure the intended work is already
  merged — this only `git pull`s on the server, it never pushes.

## Steps

Run these as separate SSH calls so a failure is caught before the restart.

### 1. Pull + install (as the `spanreed` user)

```
ssh -o BatchMode=yes root@spanreed.ink 'runuser -l spanreed -c "cd /opt/spanreed && git pull && /opt/pyenv/bin/pyenv exec poetry install"'
```

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

Actively watch the logs until startup is confirmed (or an error / timeout).
Poll on the server so it returns as soon as the service is up, and always
ends within ~90s. The journal is flooded with gunicorn HTTP 404 scanner
noise — filter it out.

```
ssh -o BatchMode=yes root@spanreed.ink 'deadline=$((SECONDS+90)); status=TIMEOUT; while [ $SECONDS -lt $deadline ]; do log=$(journalctl -u spanreed -S -3m --no-pager); if printf "%s" "$log" | grep -qE "Traceback|ModuleNotFoundError|ImportError|Failed to"; then status=ERROR; break; fi; if printf "%s" "$log" | grep -q "Started polling"; then status=OK; break; fi; sleep 3; done; echo "MONITOR_RESULT=$status"; echo "=== startup log ==="; journalctl -u spanreed -S -3m --no-pager | grep -ivE "GET /|POST /|HTTP/|\.php|favicon|robots.txt" | tail -n 150'
```

If it prints `MONITOR_RESULT=TIMEOUT`, the service hadn't finished starting —
run the same monitor command once more before concluding it's stuck.

From the captured log, confirm all of:

- `systemctl is-active` would be `active` (startup reached `Started polling`)
- `Running N plugins` appears, listing `withings`, `hevy`, `telegram-bot`,
  `spanreed-monitor`, etc.
- the durable-queue consumers started:
  `Started outbound-message consumer for user ...`
- **no** `Traceback`, `ImportError`, or `ModuleNotFoundError`. In particular
  `No module named 'aiolimiter'` means `poetry install` didn't pick up the
  new dependency — go back to step 1.

## Report

Summarize:

- what was pulled (commit range, or "already up to date")
- whether `poetry install` changed anything
- the monitor result and service status
- the key startup lines you saw (plugins running, polling started, consumers
  started), and any errors

If the Telegram bot is still under a flood-control ban, note that the startup
notice and any queued confirmations will be delivered once it lifts — a quiet
first few minutes is not a failure. (See the `telegram-flood-control` memory.)

## On failure / rollback

- `MONITOR_RESULT=ERROR` or service not `active` → surface the traceback from
  the captured log and stop.
- To roll back: on the server, as the `spanreed` user,
  `cd /opt/spanreed && git reset --hard <previous-commit>`, then re-run
  `/opt/pyenv/bin/pyenv exec poetry install`, then
  `systemctl restart spanreed`, then re-run the step-3 monitor. Confirm the
  previous commit hash with the user before any destructive reset.
