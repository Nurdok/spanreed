# Deployment — production server

Production is **spanreed.ink** (`root@spanreed.ink`). The app lives in
`/opt/spanreed`, owned by the service user `spanreed`, runs under the systemd
unit `spanreed`, and uses Poetry via pyenv at `/home/spanreed/.pyenv/bin/pyenv`.
The remote login shell is zsh.

## Deploying a change (runbook)

**Normal path: merge a PR into `main`.**

That triggers `.github/workflows/deploy.yml`: tests, then (main only) an SSH
deploy and an HTTP-200 smoke check against https://spanreed.ink/. **Whoever
merges needs no server credentials** — that's the point. Follow it in the
repo's Actions tab.

This is the same setup as Nurdok/bookclub (its ADRs #21 and #31). How the
credentials are held without handing anyone prod access:

| Piece | Where |
|---|---|
| `DEPLOY_SSH_KEY` | GitHub repo secret; private half of a key that exists only for deploys |
| `DEPLOY_KNOWN_HOSTS` | GitHub repo secret; pinned host key, so no MITM |
| Host / user | plain values in the workflow (`spanreed.ink` / `spanreed`); not secrets, because GitHub masks a secret's value everywhere in the run log and `spanreed` is in every journal line |
| Public half | `/home/spanreed/.ssh/authorized_keys`, pinned with `command="/opt/spanreed/deploy.sh",no-pty,no-*-forwarding` |
| Privilege | one sudoers line: `spanreed ALL=(root) NOPASSWD: /usr/bin/systemctl restart spanreed` |

The forced command means the key **cannot open a shell**: connecting with it
only ever runs [`deploy.sh`](../deploy.sh) (fetch + hard-reset to
`origin/main`, `poetry install`, restart, then a 90s watch of the journal
until `Started polling` appears, or a `ModuleNotFoundError`/`ImportError`
fails the run). The run log shows the same startup markers the `/deploy`
skill reports: plugins started, polling, outbound dispatchers, supervised
plugin crashes, deferred outbound deliveries.

Consequences: `deploy.sh` hard-resets the tree to `origin/main`, so **never
edit code directly on the server** — it will be discarded on the next deploy.

### Manual fallback (requires real server access)

If Actions is unavailable, the `/deploy` skill in
[`.claude/skills/deploy/SKILL.md`](../.claude/skills/deploy/SKILL.md) is the
hands-on runbook, and the magic line still works:

```bash
ssh root@spanreed.ink
runuser -l spanreed -c 'cd /opt/spanreed && git pull && /home/spanreed/.pyenv/bin/pyenv exec poetry install' \
  && systemctl daemon-reload && systemctl restart spanreed \
  && journalctl -f -u spanreed -S -2m --no-pager
```

Note the unit file is not managed by the repo, so `daemon-reload` is only
needed after editing it by hand; `deploy.sh` does not run it.

## One-time server setup

Run as root on the box. Steps 1–4 are the server side; step 5 is GitHub.

1. **Deploy key**, pinned to `deploy.sh`:

   ```bash
   ssh-keygen -t ed25519 -f /tmp/deploy_key -C "spanreed deploy (GitHub Actions)" -N ''
   install -d -o spanreed -g spanreed -m 700 /home/spanreed/.ssh
   printf 'command="/opt/spanreed/deploy.sh",no-pty,no-agent-forwarding,no-port-forwarding,no-X11-forwarding %s\n' \
     "$(cat /tmp/deploy_key.pub)" >> /home/spanreed/.ssh/authorized_keys
   chown spanreed:spanreed /home/spanreed/.ssh/authorized_keys
   chmod 600 /home/spanreed/.ssh/authorized_keys
   cat /tmp/deploy_key            # -> GitHub repo secret DEPLOY_SSH_KEY, then:
   shred -u /tmp/deploy_key /tmp/deploy_key.pub
   ```

2. **Restart privilege** — the single sudoers line (`visudo -f`):

   ```bash
   echo 'spanreed ALL=(root) NOPASSWD: /usr/bin/systemctl restart spanreed' > /etc/sudoers.d/spanreed-deploy
   chmod 440 /etc/sudoers.d/spanreed-deploy
   visudo -c
   ```

3. **Journal access**, so the startup watch in `deploy.sh` works unprivileged:

   ```bash
   usermod -aG systemd-journal spanreed
   ```

4. **Make sure the checkout can run the script**: `deploy.sh` must be on
   `main` and the server checkout must track `origin/main`
   (`git -C /opt/spanreed rev-parse --abbrev-ref @{u}`). The first time, pull
   it in by hand once (`runuser -l spanreed -c 'cd /opt/spanreed && git pull'`)
   so the forced command exists before the first Actions run.

5. **GitHub repo secrets** (Settings → Secrets and variables → Actions):

   | Secret | Value |
   |---|---|
   | `DEPLOY_SSH_KEY` | the private key from step 1 |
   | `DEPLOY_KNOWN_HOSTS` | `ssh-keyscan spanreed.ink 2>/dev/null` output (pins the host key) |

   These must be **repository** secrets, not `production` environment
   secrets, unless the environment exists with them set — see below.

6. **Test the pin** from any machine that has the private key:

   ```bash
   ssh -i deploy_key -o IdentitiesOnly=yes spanreed@spanreed.ink
   ```

   should run a deploy and print `==> deployed`; `ssh ... spanreed@spanreed.ink ls`
   should *also* run a deploy (the forced command ignores the requested
   command), never a shell.

## Why the gate is a ruleset, not the environment

The `deploy` job declares `environment: production`, but that alone gates
nothing: GitHub auto-creates a missing environment with no rules, and
Required reviewers is not offered for private repos on this plan. What holds
is a **branch ruleset on `main`** (Settings → Rules → Rulesets) requiring a
pull request, with an empty bypass list so it applies to the owner too. So:
open a PR, merge it, and the merge deploys. Direct pushes to `main` are
rejected — including the owner's.

Threat model (from bookclub ADR #31): agents read issues, PR comments and CI
logs, all of which an outsider can write, so anything an agent can reach a
prompt injection can reach. Cloud agents also cannot reach port 22 and have
no secrets store, so an SSH key handed to one would be both readable and
useless. Hence the credential lives in Actions, and a merge by a person is
what triggers a deploy.

**Known gap** (same as bookclub): `deploy.sh` is inside the tree it resets, so
the forced command executes whatever `main` says — push access to `main` is
effectively code execution as `spanreed`. The ruleset blunts it. The fix is to
relocate the deploy logic to a root-owned `/usr/local/bin/spanreed-deploy`
and repin the key.
