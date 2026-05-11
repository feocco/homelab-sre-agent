# homelab-sre-agent

Small incident coordinator for homelab log events.

`homelab-log-watcher` stays responsible for Docker log matching and phone
notifications. This service receives structured incident webhooks, enriches
them with Docker log context and deployment metadata, then creates or updates a
GitHub issue. Codex-based autofix is opt-in per service and is triggered with a
GitHub `repository_dispatch` event.

## Defaults

- Runs on `SERVICE_PORT=8094`.
- Stores state in SQLite at `SRE_STATE_PATH`.
- Stores full local diagnostic bundles under `SRE_DIAGNOSTIC_DIR`; GitHub
  issues only include a redacted representative line and a local bundle
  reference.
- Reads private service metadata from `SRE_SERVICE_METADATA_PATH` on every
  incident.
- Ignores containers that do not match an explicitly enabled metadata service.
- Starts in `SRE_DRY_RUN=true`, recording intended GitHub actions without
  writing to GitHub.
- Creates one issue per incident episode, so traceback bursts update the same
  issue instead of creating one issue per matching log line.
- Keeps Codex dispatch disabled unless service metadata sets `sre.autofix:
  true` and an issue receives the `sre:autofix-approved` label.
- Polls GitHub for approved SRE issues every `SRE_APPROVAL_POLL_SECONDS`.

## Configuration

```text
SRE_STATE_PATH=/app/state/sre-agent.sqlite3
SRE_SERVICE_METADATA_PATH=/app/config/services.yaml
SRE_DIAGNOSTIC_DIR=/app/state/diagnostics
SRE_DIAGNOSTIC_REFERENCE_ROOT=/app/state/diagnostics
SRE_INCIDENT_TOKEN=replace_me
SRE_DEFAULT_ISSUE_REPO=feocco/homelab-config
SRE_DRY_RUN=true
SRE_DOCKER_LOG_TAIL=200
SRE_DOCKER_LOG_LOOKBACK_SECONDS=600
SRE_EPISODE_WINDOW_SECONDS=120
SRE_DIAGNOSTIC_MAX_BYTES=1000000
SRE_INVESTIGATION_COOLDOWN_SECONDS=86400
SRE_CODEX_GLOBAL_DAILY_LIMIT=3
SRE_APPROVAL_POLL_SECONDS=300
SRE_ISSUE_NOTIFICATIONS_ENABLED=false
SRE_PHONE_APPROVALS_ENABLED=false
SRE_HTTP_TIMEOUT_SECONDS=10
GITHUB_TOKEN=replace_me
GITHUB_API_URL=https://api.github.com
HOMELAB_FUNCTIONS_URL=http://nasfeo:8091
HOMELAB_FUNCTIONS_TOKEN=replace_me
HA_URL=https://homeassistant.example
HA_LONG_LIVED_TOKEN=replace_me
SERVICE_HOST=0.0.0.0
SERVICE_PORT=8094
LOG_LEVEL=INFO
```

When `SRE_DRY_RUN=false`, `GITHUB_TOKEN` must have permission to create issues
in the configured issue repos and dispatch workflows in autofix-enabled source
repos.

For public issue repos, keep full logs local. Set
`SRE_DIAGNOSTIC_REFERENCE_ROOT` to the NAS host path for the mounted state
directory, for example
`/volume1/docker/homelab-config/homelab-sre-agent/data/diagnostics`.

## Incident API

`POST /v1/incidents` accepts the payload emitted by `homelab-log-watcher`.
Authorization uses `Authorization: Bearer <SRE_INCIDENT_TOKEN>` when a token is
configured.

## Metadata

Private deployment metadata belongs in `homelab-config`, not this public app
repo. See `examples/services.yaml` for the supported shape.

The metadata file is mounted read-only into the container and reloaded for each
incident. A normal `homelab-config` deploy is enough to update mappings,
runbook links, and `sre.autofix` choices; the SRE agent does not need a code
change for metadata-only edits.

Services must set `sre.enabled: true` to create or update issues. Unknown
containers, and metadata entries without `sre.enabled: true`, are ignored by
the SRE agent.

## Codex Autofix

Autofix is disabled by default. For a service with `sre.autofix: true`, new
issues are labeled `sre:autofix-pending`. The SRE agent polls GitHub for
`sre:autofix-approved` on open `homelab-sre` issues. When it finds approval, it
checks local incident state, cooldowns, daily limits, and open SRE PRs before
sending a `repository_dispatch` event named `homelab-sre-investigate` to the
source repo.

This keeps GitHub from needing network access to the NAS. Approval happens in
GitHub, but the NAS-hosted SRE agent polls GitHub outbound and remains the
gatekeeper.

Useful labels:

- `sre:autofix-pending`: code fix is available but waiting for approval.
- `sre:autofix-approved`: approve one Codex dispatch.
- `sre:autofix-started`: SRE agent accepted approval and dispatched Codex, or
  found an existing SRE PR/dispatch.
- `sre:autofix-blocked`: SRE agent could not dispatch because a safety gate
  failed.
- `sre:human-investigating`: leave approval in place but do not dispatch while
  a person is working.

## Phone Notifications

Set `SRE_ISSUE_NOTIFICATIONS_ENABLED=true` to send a phone notification when a
new SRE issue is created. The notification includes an `Open issue` button. If
`SRE_PHONE_APPROVALS_ENABLED=true` and the service is autofix eligible, it also
includes an `Approve autofix` button.

Phone approval listens to Home Assistant `mobile_app_notification_action`
events through the shared `homelab.NotificationActionRouter` helper. Pressing
the button only applies `sre:autofix-approved`; the normal polling loop still
performs safety checks before dispatching Codex.

The target repo should include a small dispatch wrapper like
`examples/homelab-sre-investigate.yml`, backed by an `OPENAI_API_KEY` secret and
a token that can create draft PRs. The wrapper calls the reusable workflow in
this repo at `.github/workflows/homelab-sre-codex.yml`.

The reusable workflow owns the Codex prompt, model, PR body policy, and draft PR
creation. It writes the PR body to `.codex/sre-pr-body.md`, then passes that
file to `peter-evans/create-pull-request` with `body-path`. The PR body should
explain the triggering issue, root cause or reason for the change, fix details,
validation, and remaining risk. Human review remains the deployment gate.

## Run Locally

```bash
python -m pip install -e .[test]
python -m pytest
python -m homelab_sre_agent
```

Health check:

```bash
curl http://localhost:8094/health
```
