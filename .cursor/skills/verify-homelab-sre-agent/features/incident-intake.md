# Authenticated incident intake

The service accepts a log-watcher incident, records local state and diagnostics,
creates or reuses an issue, and dispatches an approved autofix investigation.

## Sub-features

- Bearer-token authorization when `SRE_INCIDENT_TOKEN` is configured.
- A 12-property incident shape with three envelope properties and nine
  properties under `incident`.
- Service metadata matching, SQLite recurrence state, and diagnostic creation.
- Approved GitHub `repository_dispatch` with eight base properties and four
  nested diagnostic properties.
- HTTP 422 with a JSON error body for an incident body that cannot be processed.

## How to get to it (user POV)

Send `POST /v1/incidents` with `Content-Type: application/json` and
`Authorization: Bearer <token>`. The caller is normally
`homelab-log-watcher`.

## Driving it with HTTP

Run the complete helper:

```bash
.venv/bin/python .cursor/skills/verify-homelab-sre-agent/verify.py run
```

The expected end state is one HTTP 200 intake response and one captured
`homelab-sre-investigate` dispatch. The dispatch must have 12 logical
properties and no more than 10 top-level properties. A second request with
malformed JSON must return 422 and a nonempty `error` value.

## Gotchas

Intake does not dispatch until an issue receives `sre:autofix-approved`. The
mocked GitHub boundary supplies that external approval after issue creation,
then the real `poll_autofix_approvals` service path dispatches it. Keep the four
diagnostic properties nested under `diagnostic`; flattening them exceeds
GitHub's 10-property top-level limit.
