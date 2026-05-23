# Configuration

Runtime configuration for the deployed service belongs in the private
`homelab-config` repo:

- Non-secret values: `homelab-sre-agent/.env.config`
- Secret contract: `service-secrets.yaml`
- Local ignored secret values: `homelab-sre-agent/.env`
- NAS-rendered runtime file: `/volume1/docker/homelab-config/homelab-sre-agent/.env`

The public app repo should keep only examples and the code contract.

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
```

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
