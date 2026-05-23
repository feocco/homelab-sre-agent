# homelab-sre-agent

Small incident coordinator for homelab log events.

`homelab-log-watcher` stays responsible for Docker log matching and incident
webhook delivery. This service receives structured incidents, enriches them with
Docker log context and deployment metadata, then creates or updates a GitHub
issue and can send the phone notification for that issue. Codex-based autofix is
opt-in per service and is triggered with a GitHub `repository_dispatch` event.

This split is intentional: `homelab-log-watcher` detects log events, while
`homelab-sre-agent` handles dedupe, recurrence history, issue lifecycle,
notifications, diagnostic handoff, and Codex dispatch safety gates.

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
- Bundles repeated episode updates into at most one GitHub comment per
  `SRE_ISSUE_COMMENT_COOLDOWN_SECONDS`.
- Keeps Codex dispatch disabled unless service metadata sets `sre.autofix:
  true` and an issue receives the `sre:autofix-approved` label.
- Polls GitHub for approved SRE issues every `SRE_APPROVAL_POLL_SECONDS`.

## Configuration

Runtime configuration lives in the private `homelab-config` repo. This public
repo documents the contract and examples only:

- [`docs/configuration.md`](docs/configuration.md): environment variables and
  `homelab-config` ownership.
- [`docs/diagnostic-security.md`](docs/diagnostic-security.md): diagnostic S3
  handoff, network path, and security tradeoffs.

When `SRE_DRY_RUN=false`, GitHub authentication must be configured. The default
is `GITHUB_AUTH_MODE=token`, which uses `GITHUB_TOKEN`. Prefer
`GITHUB_AUTH_MODE=app` for production so comments, labels, and dispatches are
attributed to the GitHub App instead of a personal token owner. App mode
requires `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, and
`GITHUB_APP_PRIVATE_KEY_B64`.

For public issue repos, keep full logs local. Set
`SRE_DIAGNOSTIC_REFERENCE_ROOT` to the NAS host path for the mounted state
directory, for example
`/volume1/docker/homelab-config/homelab-sre-agent/data/diagnostics`.

## Diagnostic Handoff

Cloud Codex does not connect to the NAS. When an approved autofix is dispatched,
the NAS-hosted SRE agent uploads a bounded diagnostic summary to private S3 over
outbound HTTPS, then gives the Codex workflow a short-lived pre-signed URL for
that one object. See [`docs/diagnostic-security.md`](docs/diagnostic-security.md).

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

Autofix is disabled by default. For a service with `sre.autofix: true`, the SRE
agent polls GitHub for `sre:autofix-approved` on open `homelab-sre` issues.
When it finds approval, it checks local incident state, cooldowns, daily limits,
and open SRE PRs before sending a `repository_dispatch` event named
`homelab-sre-investigate` to the source repo.

This keeps GitHub from needing network access to the NAS. Approval happens in
GitHub, but the NAS-hosted SRE agent polls GitHub outbound and remains the
gatekeeper.

Useful labels:

- `sre:autofix-approved`: approve one Codex dispatch.
- `sre:human-investigating`: leave approval in place but do not dispatch while
  a person is working.

Status is reported in issue comments with a `homelab-sre-agent` prefix instead
of state labels. If a dispatch is blocked, the comment should say which safety
gate blocked it and what to do next.

The SRE issue body and Codex prompt intentionally mirror the local Codex agent
principles: state behavior-affecting assumptions, make the smallest targeted
change, avoid speculative features, keep diffs surgical, verify outcomes, and
explain tradeoffs. Suppressing or downgrading an error must explain why that is
better than a fix; for connection errors, the explanation must identify the
retry/backoff path and why WARN or INFO is the right severity.

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

Pin production callers to a release tag or commit SHA of this repo's reusable
workflow. Do not use `@main` in production callers; update the pinned reference
intentionally after reviewing workflow changes.

The reusable workflow owns the Codex prompt, model, PR body policy, diagnostic
context fetch, and draft PR creation. It writes the PR body to
`codex-output/sre-pr-body.md`, then passes that file to
`peter-evans/create-pull-request` with `body-path`. The PR body should explain
the triggering issue, root cause or reason for the change, evidence, fix
details, validation, and remaining uncertainty. Human review remains the
deployment gate.

The workflow pins external Actions by commit SHA so the PR path is static and
auditable. To update an Action, intentionally change the SHA in this repo and
review the diff before rolling the reusable workflow reference forward in
caller repos.

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
