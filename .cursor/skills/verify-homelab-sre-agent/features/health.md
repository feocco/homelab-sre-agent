# Service health

The health endpoint reports whether the HTTP process is ready and whether its
configuration uses dry-run mode.

## Sub-features

- HTTP 200 readiness response.
- JSON `ok` and `dry_run` values.

## How to get to it (user POV)

Send `GET /health` on the service port. This route does not require the incident
bearer token.

## Driving it with HTTP

For a helper-launched instance, run:

```bash
.venv/bin/python .cursor/skills/verify-homelab-sre-agent/verify.py doctor --runtime "$RUNTIME_FILE"
```

The expected end state is HTTP 200 with `{"ok": true, "dry_run": false}`.

## Gotchas

Health proves that the HTTP process answers with the expected configuration. It
does not prove incident intake, SQLite writes, or GitHub dispatch. Use the full
`run` command for those effects.
