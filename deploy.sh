#!/usr/bin/env bash
# Production deploy, run AS the spanreed user on the server.
#
# This is the only thing the GitHub Actions deploy key may run: its
# authorized_keys entry pins command="/opt/spanreed/deploy.sh", so a leaked key
# can deploy but cannot get a shell (see docs/deployment.md).
#
# Mirrors .claude/skills/deploy/SKILL.md: pull, poetry install, restart, then
# watch the journal until the bot is polling (or a fatal import error).
set -euo pipefail

cd /opt/spanreed
PYENV=/home/spanreed/.pyenv/bin/pyenv

echo "==> git pull"
git fetch --quiet origin main
git reset --hard --quiet origin/main
git log -1 --oneline

echo "==> dependencies"
# The server's Poetry is older than the lock format and warns about it;
# harmless as long as it then installs / reports up to date.
"$PYENV" exec poetry install

echo "==> restart"
since=$(date '+%Y-%m-%d %H:%M:%S')
sudo -n /usr/bin/systemctl restart spanreed

echo "==> startup"
# Poll the journal (only lines since the restart) until startup is confirmed,
# a fatal import error shows up, or 90s pass. Needs the spanreed user in the
# systemd-journal group.
deadline=$((SECONDS + 90))
result=TIMEOUT
while [ "$SECONDS" -lt "$deadline" ]; do
  log=$(journalctl -u spanreed -S "$since" --no-pager)
  if grep -qE "ModuleNotFoundError|ImportError" <<<"$log"; then
    result=FATAL
    break
  fi
  if grep -q "Started polling" <<<"$log"; then
    result=OK
    break
  fi
  sleep 3
done

echo "MONITOR_RESULT=$result"
echo "SERVICE=$(systemctl is-active spanreed || true)"
echo "=== startup markers ==="
# Filter out gunicorn 404 scanner noise and the chatty Obsidian watchdog.
journalctl -u spanreed -S "$since" --no-pager \
  | grep -iE "Running [0-9]+ plugins|Started polling|Started outbound|crashed for user|Outbound delivery deferred|Flood control|ModuleNotFoundError|ImportError|Traceback" \
  | tail -40 || true

if [ "$result" != OK ]; then
  echo "==> deploy FAILED ($result) — service is $(systemctl is-active spanreed || true)" >&2
  exit 1
fi
systemctl is-active --quiet spanreed

echo "==> deployed"
