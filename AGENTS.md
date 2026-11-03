# Repository instructions

## Work locally

Use Python 3.12 or newer. Create a local virtual environment and install the
application with its test dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

Start a safe local instance from the repository root:

```bash
mkdir -p data/local/diagnostics
SRE_STATE_PATH="$PWD/data/local/sre-agent.sqlite3" \
SRE_SERVICE_METADATA_PATH="$PWD/examples/services.yaml" \
SRE_DIAGNOSTIC_DIR="$PWD/data/local/diagnostics" \
SRE_DIAGNOSTIC_REFERENCE_ROOT="$PWD/data/local/diagnostics" \
SRE_DRY_RUN=true \
SRE_APPROVAL_POLL_SECONDS=0 \
SERVICE_HOST=127.0.0.1 \
SERVICE_PORT=8094 \
.venv/bin/python -m homelab_sre_agent
```

This command requires a reachable local Docker daemon because the production
log adapter connects during startup. Use the project-local verification command
when Docker is unavailable.

Check that the process is ready:

```bash
curl --fail --silent --show-error http://127.0.0.1:8094/health
```

Run the full test suite:

```bash
.venv/bin/python -m pytest
```

Run the project-local HTTP verification:

```bash
.venv/bin/python .cursor/skills/verify-homelab-sre-agent/verify.py run
```

## Keep ownership clear

- `homelab_sre_agent/server.py` owns HTTP routing, bearer-token checks,
  response status codes, and the OpenAPI document. Change the route test and
  OpenAPI response together when the HTTP contract changes.
- `homelab_sre_agent/service.py` owns incident coordination, recurrence,
  diagnostics, issue lifecycle, approval gates, and dispatch policy.
- `homelab_sre_agent/state.py` owns SQLite schema and persistence.
- `homelab_sre_agent/config.py` and `homelab_sre_agent/metadata.py` parse
  environment and service metadata at their boundaries.
- `github.py`, `docker_logs.py`, `diagnostics.py`, and `notifications.py` own
  external-system adapters. Keep network and Docker behavior out of the
  service.
- `dashboard.py` owns read-only dashboard rendering. Do not add mutation,
  approval, or retry actions to dashboard routes.
- `examples/` and `docs/` describe the public contract. Private deployment
  values and service mappings belong in `homelab-config`.

## Follow the test conventions

Tests run with pytest and use `unittest.TestCase`. Use temporary directories,
ephemeral port `0`, real SQLite state, and fakes at production boundaries.
Drive HTTP behavior through `SREServer` instead of calling its handler methods
directly. Assert caller-visible responses and external effects, such as a
captured GitHub dispatch. Do not require Docker, GitHub, S3, Home Assistant, or
other network services.

## Protect secrets and deployments

Never commit `.env` files, tokens, private keys, pre-signed production URLs, or
captured production logs. Keep verification evidence synthetic and
secret-free. Dry-run is the local default. Do not deploy, push images, change
private infrastructure, or write to external services while testing this
repository.

Never build custom auth. If auth is needed, reuse the homelab IdP. Bots use bearer tokens.

For pstack work, use one implementer and one reviewer. Do not fan out unless
Joe asks.
