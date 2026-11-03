# homelab-sre-agent API feature map

Use the HTTP helper in the parent skill for these user-facing features.

- [`incident-intake.md`](incident-intake.md) covers authenticated incident
  intake, issue coordination, diagnostic handoff, and repository dispatch.
- [`health.md`](health.md) covers service readiness.
- [`openapi.md`](openapi.md) covers the machine-readable HTTP contract.
- [`incident-dashboard.md`](incident-dashboard.md) covers the read-only
  incident list and family detail pages.

The primary verification run drives incident intake because it crosses the
HTTP, service, SQLite, diagnostic, approval, and GitHub dispatch boundaries.
When another mapped feature changes, add its journey to the helper or run its
documented HTTP checks against a launched instance.
