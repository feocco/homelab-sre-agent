# Read-only incident dashboard

The dashboard renders incident and operational families from local SQLite state.
The family route adds best-effort GitHub issue and pull-request details.

## Sub-features

- `GET /incidents` family list with service and time filters.
- `GET /incidents/family` detail by family type and fingerprint.
- Read-only GitHub issue and pull-request enrichment.
- HTTP 400 for missing family parameters and 404 for an unknown family.

## How to get to it (user POV)

Open `/incidents` in a browser. Follow a family link to
`/incidents/family?type=incident&fingerprint=...`.

## Driving it with HTTP

Launch an instance, assign its runtime values, seed one incident through the
`drive` command, and request the dashboard:

```bash
launch_output="$(
  .venv/bin/python .cursor/skills/verify-homelab-sre-agent/verify.py launch
)"
RUNTIME_FILE="$(printf '%s\n' "$launch_output" | sed -n 's/^runtime_file=//p')"
SERVICE_URL="$(printf '%s\n' "$launch_output" | sed -n 's/^service_url=//p')"
cleanup() {
  .venv/bin/python .cursor/skills/verify-homelab-sre-agent/verify.py cleanup \
    --runtime "$RUNTIME_FILE"
}
trap cleanup EXIT
.venv/bin/python .cursor/skills/verify-homelab-sre-agent/verify.py doctor \
  --runtime "$RUNTIME_FILE"
.venv/bin/python .cursor/skills/verify-homelab-sre-agent/verify.py drive \
  --runtime "$RUNTIME_FILE"
dashboard_html="$(curl --fail --silent --show-error "$SERVICE_URL/incidents")"
[[ "$dashboard_html" == *sample-service* ]]
```

The expected end state is HTML with the `sample-service` family row. The exit
trap stops the owned process and removes its scratch directory. For seeded
family state, follow the encoded family URL and require HTTP 200 detail HTML.

## Gotchas

The verification helper starts with empty SQLite state, so the dashboard has no
family row until incident intake succeeds. The dashboard must remain read-only.
Do not add approval, retry, issue creation, or metadata mutation controls.
