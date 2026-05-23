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

## Docs

Runtime configuration lives in the private `homelab-config` repo. This public
repo documents the contract and examples only:

- [`docs/architecture.md`](docs/architecture.md): event pipeline, service
  boundaries, and external connections.
- [`docs/configuration.md`](docs/configuration.md): environment variables and
  `homelab-config` ownership.
- [`docs/diagnostic-security.md`](docs/diagnostic-security.md): diagnostic S3
  handoff, network path, and security tradeoffs.

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
