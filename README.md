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
- Reads private service metadata from `SRE_SERVICE_METADATA_PATH` on every
  incident.
- Starts in `SRE_DRY_RUN=true`, recording intended GitHub actions without
  writing to GitHub.
- Creates one issue per service/fingerprint.
- Keeps Codex dispatch disabled unless service metadata sets `sre.autofix:
  true`.

## Configuration

```text
SRE_STATE_PATH=/app/state/sre-agent.sqlite3
SRE_SERVICE_METADATA_PATH=/app/config/services.yaml
SRE_INCIDENT_TOKEN=replace_me
SRE_DEFAULT_ISSUE_REPO=feocco/homelab-config
SRE_DRY_RUN=true
SRE_DOCKER_LOG_TAIL=200
SRE_INVESTIGATION_COOLDOWN_SECONDS=86400
SRE_CODEX_GLOBAL_DAILY_LIMIT=3
SRE_HTTP_TIMEOUT_SECONDS=10
GITHUB_TOKEN=replace_me
GITHUB_API_URL=https://api.github.com
SERVICE_HOST=0.0.0.0
SERVICE_PORT=8094
LOG_LEVEL=INFO
```

When `SRE_DRY_RUN=false`, `GITHUB_TOKEN` must have permission to create issues
in the configured issue repos and dispatch workflows in autofix-enabled source
repos.

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

## Codex Autofix

Autofix is disabled by default. For a service with `sre.autofix: true`, the SRE
agent sends a `repository_dispatch` event named `homelab-sre-investigate` to the
source repo. The target repo should include a workflow like
`examples/homelab-sre-investigate.yml`, backed by an `OPENAI_API_KEY` secret.

The workflow should create a draft PR only. Human review remains the deployment
gate.

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
