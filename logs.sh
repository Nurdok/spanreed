#!/usr/bin/env bash
# Read-only access to the production journal for GitHub Actions.
#
# This is the only thing the logs key may run: its authorized_keys entry pins
# command="/opt/spanreed/logs.sh", so the key cannot open a shell or run
# anything else (see docs/deployment.md). The workflow passes its inputs as
# the SSH command, which the forced command receives in SSH_ORIGINAL_COMMAND:
#
#   <since> <lines> [<pattern>]
#
# since:   a journalctl -S value: -30m, -2h, -1d, or YYYY-MM-DDTHH:MM[:SS]
#          (the T keeps it one word; it becomes a space for journalctl)
# lines:   how many of the last matching lines to print (1-2000)
# pattern: optional extended regex; only matching lines are printed
set -euo pipefail

read -r since lines pattern <<<"${SSH_ORIGINAL_COMMAND:-}"

since_re='^(-[0-9]{1,4}[mhd]|[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{2}:[0-9]{2}(:[0-9]{2})?)?)$'
if ! [[ "${since:-}" =~ $since_re ]]; then
  echo "logs.sh: bad 'since' value: '${since:-}'" >&2
  exit 2
fi
since="${since/T/ }"
if ! [[ "${lines:-}" =~ ^[0-9]{1,4}$ ]] || [ "$lines" -lt 1 ] || [ "$lines" -gt 2000 ]; then
  echo "logs.sh: 'lines' must be 1-2000, got '${lines:-}'" >&2
  exit 2
fi

echo "=== journalctl -u spanreed -S '$since'${pattern:+ | grep -E '$pattern'} | tail -n $lines ==="
if [ -n "${pattern:-}" ]; then
  # `--` keeps a pattern that starts with "-" from being read as an option.
  journalctl -u spanreed -S "$since" --no-pager -o short-iso \
    | { grep -E -- "$pattern" || true; } \
    | tail -n "$lines"
else
  journalctl -u spanreed -S "$since" --no-pager -o short-iso | tail -n "$lines"
fi
