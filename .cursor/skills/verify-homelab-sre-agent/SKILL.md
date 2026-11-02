---
name: verify-homelab-sre-agent
description: Launch and drive the homelab-sre-agent HTTP service with isolated local state and mocked external boundaries. Use after changing incident intake, dispatch, diagnostics, auth, health, OpenAPI, or dashboard behavior.
---

# Verify homelab-sre-agent

## Launch

From the repository root, run the complete verification:

```bash
.venv/bin/python .cursor/skills/verify-homelab-sre-agent/verify.py run
```

The helper starts the real `SREServer` and `SREService` in a child process. It
uses port `0`, a unique SQLite database, and a unique scratch directory. It
mocks GitHub, Docker log collection, and diagnostic publishing. It does not
contact those systems.

For a paused debugging run, start only the service:

```bash
.venv/bin/python .cursor/skills/verify-homelab-sre-agent/verify.py launch
```

Copy the printed `runtime_file` value into `RUNTIME_FILE`. The runtime file
contains the process ID, local URL, scratch path, and evidence path. It does not
contain the bearer token.

## Doctor

Check a launched instance before driving it:

```bash
.venv/bin/python .cursor/skills/verify-homelab-sre-agent/verify.py doctor --runtime "$RUNTIME_FILE"
```

Doctor sends `GET /health` and requires HTTP 200, `ok: true`, and
`dry_run: false`. The false dry-run value proves that safety comes from the
mocked production boundaries, not from bypassing service logic. Before the
request, Doctor verifies the process command and Linux start identity recorded
in the runtime file.

## Drive

Drive the HTTP path:

```bash
.venv/bin/python .cursor/skills/verify-homelab-sre-agent/verify.py drive --runtime "$RUNTIME_FILE"
```

The helper reads the ephemeral token from a mode-0600 scratch file and sends it
as a bearer token without printing it. It posts the complete sample incident to
`POST /v1/incidents`. After HTTP 200, it sends a one-shot approval signal to the
mocked GitHub boundary, waits for the real service approval poll, and reads the
captured GitHub `repository_dispatch`. It then posts malformed JSON through the
same endpoint.

The drive passes only when intake returns 200, exactly one dispatch exists, the
dispatch has 12 logical properties, the dispatch has at most 10 top-level
properties, and malformed JSON returns 422 with an error body. See
[`features/README.md`](features/README.md) for the maintained API feature map.

## Evidence

Each run writes evidence under:

```text
/tmp/homelab-sre-agent-verification-evidence/<run-id>/
```

`results.json` contains the health response, complete synthetic incident,
intake response, captured dispatch, property counts, malformed response, and
cleanup result. `transcript.txt` contains the concise secret-free proof for a
pull request. `server.log` contains local server output. The bearer token is
never copied into evidence.

Proof must include the real HTTP action and the resulting dispatch. Do not use
an internal service call as a substitute. Keep mocks at the GitHub, Docker log,
and diagnostic publisher boundaries.

## Cleanup

The `run` command always attempts cleanup, including after a failed drive. It
signals only the child process it started, waits for exit, and removes that
run's scratch directory. It leaves the evidence directory intact.

Clean up a paused debugging run:

```bash
.venv/bin/python .cursor/skills/verify-homelab-sre-agent/verify.py cleanup --runtime "$RUNTIME_FILE"
```

Cleanup verifies the process command before signaling a process from a runtime
file. It also verifies the recorded Linux start identity immediately before
each signal. Do not kill by process name.

## Helpers

`.cursor/skills/verify-homelab-sre-agent/verify.py` is the executable helper.
Use `run` for review evidence. Use `launch`, `doctor`, `drive`, and `cleanup`
only to inspect a failed step. Run `--help` for the command list:

```bash
.venv/bin/python .cursor/skills/verify-homelab-sre-agent/verify.py --help
```
