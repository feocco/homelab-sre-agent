# Configuration

Runtime configuration for the deployed service belongs in the private
`homelab-config` repo:

- Non-secret values: `homelab-sre-agent/.env.config`
- Secret contract: `service-secrets.yaml`
- Local ignored secret values: `homelab-sre-agent/.env`
- NAS-rendered runtime file: `/volume1/docker/homelab-config/homelab-sre-agent/.env`

The public app repo should keep only examples and the code contract.

## Defaults

- Runs on `SERVICE_PORT=8094`.
- Stores state in SQLite at `SRE_STATE_PATH`.
- Stores full local diagnostic bundles under `SRE_DIAGNOSTIC_DIR`; GitHub
  issues include only a redacted representative line and local bundle reference.
- Reads private service metadata from `SRE_SERVICE_METADATA_PATH` on every
  incident.
- Ignores containers that do not match an explicitly enabled metadata service.
- Starts in `SRE_DRY_RUN=true`, recording intended GitHub actions without
  writing to GitHub.
- Creates one issue per incident family/episode so traceback bursts and repeat
  observations update the same issue.
- Routes obvious downstream dependency failures to notify-only operational
  records instead of source-repo GitHub issues.
- Bundles repeated episode updates into at most one GitHub comment per
  `SRE_ISSUE_COMMENT_COOLDOWN_SECONDS`.
- Bundles repeated operational notifications into at most one phone notification
  per `SRE_OPERATIONAL_NOTIFICATION_COOLDOWN_SECONDS`.
- Keeps Codex dispatch disabled unless service metadata sets `sre.autofix:
  true` and an issue receives the `sre:autofix-approved` label.
- Polls GitHub for approved SRE issues every `SRE_APPROVAL_POLL_SECONDS`.

## Core Runtime Variables

```text
SRE_STATE_PATH=/app/state/sre-agent.sqlite3
SRE_SERVICE_METADATA_PATH=/app/config/services.yaml
SRE_DIAGNOSTIC_DIR=/app/state/diagnostics
SRE_DIAGNOSTIC_REFERENCE_ROOT=/volume1/docker/homelab-config/homelab-sre-agent/data/diagnostics
SRE_DEFAULT_ISSUE_REPO=feocco/homelab-config
SRE_DRY_RUN=false
SRE_DOCKER_LOG_TAIL=200
SRE_INVESTIGATION_COOLDOWN_SECONDS=86400
SRE_ISSUE_COMMENT_COOLDOWN_SECONDS=3600
SRE_OPERATIONAL_NOTIFICATION_COOLDOWN_SECONDS=3600
SRE_CODEX_GLOBAL_DAILY_LIMIT=3
SRE_APPROVAL_POLL_SECONDS=120
SRE_ISSUE_NOTIFICATIONS_ENABLED=true
SRE_PHONE_APPROVALS_ENABLED=true
SRE_HTTP_TIMEOUT_SECONDS=10
GITHUB_AUTH_MODE=app
GITHUB_APP_ID=<app-id>
GITHUB_APP_INSTALLATION_ID=<installation-id>
GITHUB_API_URL=https://api.github.com
HOMELAB_FUNCTIONS_URL=http://nasfeo:8091
SERVICE_HOST=0.0.0.0
SERVICE_PORT=8094
LOG_LEVEL=INFO
```

## Diagnostic Handoff Variables

```text
SRE_DIAGNOSTIC_PUBLISH_ENABLED=true
SRE_DIAGNOSTIC_S3_BUCKET=feocco-homelab-sre-diagnostics-010746654656
SRE_DIAGNOSTIC_S3_REGION=us-east-1
SRE_DIAGNOSTIC_S3_PREFIX=diagnostics
SRE_DIAGNOSTIC_URL_TTL_SECONDS=3600
SRE_DIAGNOSTIC_RETENTION_DAYS=14
```

Diagnostic S3 objects are uploaded when a code-fix issue is created or updated.
The app stores the S3 object key and signs a temporary URL only after autofix is
approved. Object cleanup belongs to the S3 bucket lifecycle policy; the current
bucket policy expires diagnostic objects after 14 days.
`SRE_DIAGNOSTIC_RETENTION_DAYS` should match that lifecycle window so dispatch
payloads can tell Codex when the private diagnostic summary is expected to
expire.

Required secrets:

```text
AWS_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY
GITHUB_APP_PRIVATE_KEY_B64
HOMELAB_FUNCTIONS_TOKEN
SRE_INCIDENT_TOKEN
```

Keep secret values out of git. Upload them through the `homelab-config` secret
workflow and validate with:

```bash
./scripts/check-service-secrets
./scripts/test-deploy-tooling
```

## Service Routing Metadata

Service metadata can classify dependency failures as operational notify-only
events. These events still write local diagnostics and recurrence state, but do
not create a source-repo issue or dispatch Codex.

```yaml
services:
  homelab-sre-agent:
    containers: [homelab-sre-agent]
    source:
      repo: feocco/homelab-sre-agent
    sre:
      enabled: true
      routing:
        operational_dependencies:
          - dependency: homelab-functions
            pattern: "homelab.client.HomelabFunctionsError: 500 Internal Server Error"
            reason: "homelab-functions is unavailable; route as an operational dependency alert."
```

Rules are matched against the representative log line before issue creation.
Use them for clear downstream/service-unavailable cases, not for unknown
exceptions that need source-code investigation.
