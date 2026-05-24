# Diagnostic Handoff Security

The diagnostic handoff exists to give cloud Codex enough context to investigate
an approved incident without giving it access to the NAS or local network.

## Plain-English Model

The SRE agent opens a GitHub issue when an incident is reported. At that point,
the issue body stays public-safe and includes only redacted issue context plus a
local NAS diagnostic reference.

Operational/downstream incidents are different: the SRE agent still writes the
local NAS diagnostic bundle and recurrence state, but it does not create a
GitHub issue or publish an S3 handoff. Those events send cooldown-controlled
phone notifications only.

For code-fix candidate incidents, the SRE agent uploads a bounded diagnostic
summary to private S3 when it creates or updates the GitHub issue. It stores the
S3 object key in local SQLite state, not a long-lived URL.

The URL handoff happens later, when an autofix is approved and the NAS-hosted
SRE agent dispatches the Codex workflow. At dispatch time, the SRE agent creates
a short-lived pre-signed URL for the stored object. The URL is passed in the
`repository_dispatch` payload so the GitHub Actions workflow can fetch the
diagnostic context before running Codex.

S3 is not connecting to Codex. Codex is making an outbound HTTPS request to S3
using a temporary signed URL. The NAS is not exposed publicly, and Codex does
not receive SSH access, Docker socket access, or AWS credentials.

## Network Path

```text
homelab-log-watcher -> homelab-sre-agent
  local Docker log incident webhook

homelab-sre-agent -> GitHub
  create/update issue, poll approval labels, dispatch workflow

homelab-sre-agent -> S3
  outbound HTTPS upload of incident-time diagnostic summary

GitHub Actions / Codex -> S3
  outbound HTTPS GET using the pre-signed URL
```

There is no inbound path from GitHub Actions or Codex to the NAS.

## Why The Private Bucket Is Fetchable

The bucket is private and has S3 Block Public Access enabled. The object is still
fetchable by the workflow because the NAS-side SRE agent signs a pre-signed URL
using its AWS credential. That URL temporarily delegates `GetObject` permission
for one object. Anyone without the URL still has no public bucket access.

## What Is Uploaded

The S3 object is a diagnostic summary for Codex, not the raw full local log
bundle. It includes issue metadata, deployment metadata, recurrence counts, and
the local diagnostic reference. Full diagnostic bundles remain on the NAS.

Notify-only operational incidents are not uploaded to S3 in v1 because there is
no approved Codex investigation and no cloud workflow that needs the context.

The public GitHub issue still needs strict redaction because it is the durable
public record. The S3 object is private and short-lived, but it should still be
bounded and avoid unnecessary secrets because a pre-signed URL is bearer-style
access while it is valid.

## Security Controls

- S3 Block Public Access is enabled.
- Default bucket encryption is enabled.
- The bucket policy denies non-TLS requests.
- Diagnostic objects expire automatically through bucket lifecycle policy.
- The NAS IAM credential is scoped to the diagnostic prefix.
- GitHub Actions does not receive AWS credentials.
- Codex receives only a temporary URL for one object.
- If diagnostic publishing fails, Codex still runs from GitHub issue context and
  must state that diagnostics were unavailable.

## Downsides

- A pre-signed URL is bearer access until it expires. Anyone who obtains it can
  read that object during the TTL.
- Diagnostic data leaves the local network and lands in AWS S3.
- If the NAS AWS key leaks, an attacker can read/write under the allowed
  diagnostic prefix until the key is revoked.
- If diagnostic summary generation includes too much context, sensitive details
  could be exposed to the workflow.
- S3 object metadata and access events may exist in AWS billing/logging surfaces.

## Retention And Cleanup

Cleanup is handled by S3 lifecycle policy, not by the app. The bucket should
expire objects under the diagnostic prefix automatically; the current deployment
uses a 14-day expiration. This keeps delayed approvals usable for a short period
without storing diagnostic summaries indefinitely.

If an approval happens after the lifecycle window, the SRE workflow should run
without a diagnostic URL and state that the private diagnostic summary expired.
The local NAS diagnostic bundle may still exist separately, depending on NAS
cleanup policy.

## Current Tradeoff

This is a deliberate middle ground: Codex gets better incident context, but only
after explicit approval and without any route into the local network. The S3
object exists before approval, but it remains private and unreadable to Codex
until the NAS signs a short-lived URL. For now, a least-privilege long-lived IAM
access key is acceptable. If this pattern expands, move toward short-lived AWS
credentials and stricter object-level audit.
