from __future__ import annotations

from dataclasses import dataclass
import hashlib
from html import escape
from typing import Protocol
from urllib.parse import parse_qs, urlencode

from .github import GitHubIssueDetails, GitHubPullRequest
from .state import DashboardFamilyDetail, DashboardFamilySummary, StateStore, parse_dt, utc_now


class GitHubDashboardClient(Protocol):
    def get_issue_details(self, *, repo: str, issue_number: int) -> GitHubIssueDetails | None: ...

    def find_pull_request_by_branch(
        self,
        *,
        repo: str,
        branch: str,
        state: str = "all",
    ) -> GitHubPullRequest | None: ...


@dataclass(frozen=True)
class DashboardFilters:
    family_type: str
    service_name: str | None
    window: str
    limit: int

    @property
    def since(self):
        now = utc_now()
        if self.window == "1d":
            return now.replace(microsecond=0) - window_delta(days=1)
        if self.window == "7d":
            return now.replace(microsecond=0) - window_delta(days=7)
        if self.window == "14d":
            return now.replace(microsecond=0) - window_delta(days=14)
        if self.window == "30d":
            return now.replace(microsecond=0) - window_delta(days=30)
        return None


@dataclass(frozen=True)
class DashboardEnrichment:
    issue: GitHubIssueDetails | None
    pull_requests: tuple[GitHubPullRequest, ...]
    expected_branches: tuple[str, ...]
    error: str | None


def window_delta(*, days: int):
    from datetime import timedelta

    return timedelta(days=days)


def render_index_from_state(state: StateStore, query: str) -> str:
    filters = filters_from_query(query)
    families = state.list_dashboard_families(
        family_type=filters.family_type,
        service_name=filters.service_name,
        since=filters.since,
        limit=filters.limit,
    )
    return render_index(families, filters)


def render_family_from_state(
    state: StateStore,
    github: GitHubDashboardClient,
    query: str,
) -> str | None:
    params = parse_qs(query)
    family_type = first(params, "type", "")
    fingerprint = first(params, "fingerprint", "")
    if not family_type or not fingerprint:
        raise ValueError("type and fingerprint are required")
    detail = state.get_dashboard_family(family_type=family_type, fingerprint=fingerprint)
    if detail is None:
        return None
    return render_family_detail(detail, enrich_family(detail, github))


def filters_from_query(query: str) -> DashboardFilters:
    params = parse_qs(query)
    family_type = first(params, "type", "all").strip().lower()
    if family_type not in {"all", "incident", "operational"}:
        family_type = "all"
    service_name = first(params, "service", "").strip() or None
    window = first(params, "window", "all").strip().lower()
    if window not in {"all", "1d", "7d", "14d", "30d"}:
        window = "all"
    try:
        limit = int(first(params, "limit", "100"))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 500))
    return DashboardFilters(family_type=family_type, service_name=service_name, window=window, limit=limit)


def render_index(families: list[DashboardFamilySummary], filters: DashboardFilters) -> str:
    incident_count = sum(1 for item in families if item.family_type == "incident")
    operational_count = sum(1 for item in families if item.family_type == "operational")
    observation_count = sum(item.count for item in families)
    body = [
        "<header>",
        "<h1>SRE Incidents</h1>",
        '<a class="health" href="/health">health</a>',
        "</header>",
        '<section class="summary">',
        summary_card("families", str(len(families))),
        summary_card("incident", str(incident_count)),
        summary_card("operational", str(operational_count)),
        summary_card("observations", str(observation_count)),
        "</section>",
        render_filters(filters),
        '<table class="families">',
        "<thead><tr>"
        "<th>type</th><th>family</th><th>service</th><th>count</th><th>first seen</th><th>last seen</th>"
        "<th>issue/work item</th><th>dispatch</th><th>latest example</th>"
        "</tr></thead>",
        "<tbody>",
    ]
    if not families:
        body.append('<tr><td colspan="9" class="empty">No families match the selected filters.</td></tr>')
    for family in families:
        body.append(render_family_row(family))
    body.extend(["</tbody>", "</table>"])
    return page("SRE Incidents", "\n".join(body))


def render_family_detail(detail: DashboardFamilyDetail, enrichment: DashboardEnrichment) -> str:
    family = detail.summary
    title = family_name(family)
    body = [
        "<header>",
        "<div>",
        f"<h1>{esc(title)}</h1>",
        f'<p class="subhead">{service_filter_link(family.service_name)}</p>',
        "</div>",
        '<a class="health" href="/incidents">incidents</a>',
        "</header>",
        '<section class="summary">',
        summary_card("type", family.family_type),
        summary_card("service", family.service_name),
        summary_card("count", str(family.count)),
        summary_card("first seen", family.first_seen_at),
        summary_card("last seen", family.last_seen_at),
        "</section>",
        '<section class="panel">',
        "<h2>Family</h2>",
        "<dl>",
        f"<dt>state key</dt><dd><code>{esc(family.fingerprint)}</code></dd>",
        f"<dt>severity</dt><dd>{esc(family.severity or 'n/a')}</dd>",
        f"<dt>dependency</dt><dd>{esc(family.dependency or 'n/a')}</dd>",
        f"<dt>reason</dt><dd>{esc(family.reason or 'n/a')}</dd>",
        f"<dt>24h / 7d / 14d</dt><dd>{window_counts(family)}</dd>",
        f"<dt>source fingerprints</dt><dd>{esc(str(family.source_fingerprint_count or 'n/a'))}</dd>",
        "</dl>",
        "</section>",
        render_work_item(family, enrichment),
        render_dispatches(detail, enrichment),
        render_approvals(detail),
        render_observations(detail),
    ]
    return page(f"{title} SRE Family", "\n".join(body))


def enrich_family(detail: DashboardFamilyDetail, github: GitHubDashboardClient) -> DashboardEnrichment:
    family = detail.summary
    if family.family_type != "incident" or family.issue_number is None:
        return DashboardEnrichment(issue=None, pull_requests=(), expected_branches=(), error=None)

    issue = None
    pull_requests: list[GitHubPullRequest] = []
    expected_branches: list[str] = []
    try:
        issue = github.get_issue_details(repo=family.issue_repo, issue_number=family.issue_number)
        for dispatch in detail.dispatches:
            branch = expected_dispatch_branch(issue_number=family.issue_number, fingerprint=family.fingerprint)
            expected_branches.append(f"{dispatch.source_repo}:{branch}")
            pull_request = github.find_pull_request_by_branch(repo=dispatch.source_repo, branch=branch, state="all")
            if pull_request is not None:
                pull_requests.append(pull_request)
    except Exception as exc:
        return DashboardEnrichment(
            issue=issue,
            pull_requests=tuple(pull_requests),
            expected_branches=tuple(expected_branches),
            error=str(exc),
        )
    return DashboardEnrichment(
        issue=issue,
        pull_requests=tuple(pull_requests),
        expected_branches=tuple(expected_branches),
        error=None,
    )


def expected_dispatch_branch(*, issue_number: int, fingerprint: str) -> str:
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
    return f"codex/sre-{issue_number}-{digest}"


def render_family_row(family: DashboardFamilySummary) -> str:
    url = "/incidents/family?" + urlencode({"type": family.family_type, "fingerprint": family.fingerprint})
    return (
        "<tr>"
        f"<td><span class=\"badge {esc(family.family_type)}\">{esc(family.family_type)}</span></td>"
        f"<td><a class=\"family-link\" href=\"{esc(url)}\">{esc(family_name(family))}</a></td>"
        f"<td>{service_filter_link(family.service_name)}</td>"
        f"<td>{render_count(family)}</td>"
        f"<td>{esc(short_time(family.first_seen_at))}</td>"
        f"<td>{esc(short_time(family.last_seen_at))}</td>"
        f"<td>{render_issue_link(family)}</td>"
        f"<td>{render_dispatch_status(family)}</td>"
        f"<td><code>{esc(truncate(family.latest_line, 180))}</code></td>"
        "</tr>"
    )


def family_name(family: DashboardFamilySummary) -> str:
    if family.family_type == "operational":
        if family.dependency:
            return f"{family.dependency} operational dependency"
        return "Operational dependency"
    line = compact_line(family.latest_line)
    if line:
        return truncate(line, 96)
    if family.issue_number is not None:
        return f"Issue #{family.issue_number}"
    return f"{family.service_name} incident family"


def service_filter_link(service_name: str) -> str:
    url = "/incidents?" + urlencode({"service": service_name})
    return f'<a class="service-link" href="{esc(url)}">{esc(service_name)}</a>'


def compact_line(value: str) -> str:
    return " ".join(value.split())


def render_filters(filters: DashboardFilters) -> str:
    return (
        '<form class="filters" method="get" action="/incidents">'
        f'<label>type <select name="type">{options(("all", "incident", "operational"), filters.family_type)}</select></label>'
        f'<label>window <select name="window">{options(("all", "1d", "7d", "14d", "30d"), filters.window)}</select></label>'
        f'<label>service <input name="service" value="{esc(filters.service_name or "")}"></label>'
        f'<label>limit <input name="limit" inputmode="numeric" value="{filters.limit}"></label>'
        "<button type=\"submit\">Apply</button>"
        "</form>"
    )


def render_work_item(family: DashboardFamilySummary, enrichment: DashboardEnrichment) -> str:
    rows = [
        "<section class=\"panel\">",
        "<h2>Work Item</h2>",
        "<dl>",
        f"<dt>issue</dt><dd>{render_issue_link(family)}</dd>",
    ]
    if enrichment.issue is not None:
        rows.extend(
            [
                f"<dt>issue state</dt><dd>{esc(enrichment.issue.state)}</dd>",
                f"<dt>issue labels</dt><dd>{esc(', '.join(enrichment.issue.labels) or 'none')}</dd>",
            ]
        )
    elif family.issue_number is not None:
        rows.append("<dt>issue state</dt><dd>unknown</dd>")
    if enrichment.error:
        rows.append(f"<dt>GitHub enrichment</dt><dd>unavailable: {esc(enrichment.error)}</dd>")
    rows.extend(["</dl>", "</section>"])
    return "\n".join(rows)


def render_dispatches(detail: DashboardFamilyDetail, enrichment: DashboardEnrichment) -> str:
    rows = [
        '<section class="panel">',
        "<h2>Dispatches</h2>",
        '<table><thead><tr><th>created</th><th>source repo</th><th>expected branch</th><th>PR</th></tr></thead><tbody>',
    ]
    if not detail.dispatches:
        rows.append('<tr><td colspan="4" class="empty">No dispatches recorded.</td></tr>')
    for dispatch in detail.dispatches:
        branch = ""
        if detail.summary.issue_number is not None:
            branch = expected_dispatch_branch(issue_number=detail.summary.issue_number, fingerprint=detail.summary.fingerprint)
        pull_request = next((item for item in enrichment.pull_requests if item.repo == dispatch.source_repo), None)
        rows.append(
            "<tr>"
            f"<td>{esc(short_time(dispatch.created_at))}</td>"
            f"<td>{esc(dispatch.source_repo)}</td>"
            f"<td><code>{esc(branch or 'n/a')}</code></td>"
            f"<td>{render_pull_request(pull_request)}</td>"
            "</tr>"
        )
    rows.extend(["</tbody></table>", "</section>"])
    return "\n".join(rows)


def render_approvals(detail: DashboardFamilyDetail) -> str:
    rows = [
        '<section class="panel">',
        "<h2>Approvals</h2>",
        '<table><thead><tr><th>created</th><th>used</th><th>issue</th></tr></thead><tbody>',
    ]
    if not detail.approvals:
        rows.append('<tr><td colspan="3" class="empty">No approval tokens recorded.</td></tr>')
    for approval in detail.approvals:
        rows.append(
            "<tr>"
            f"<td>{esc(short_time(approval.created_at))}</td>"
            f"<td>{esc(short_time(approval.used_at) if approval.used_at else 'unused')}</td>"
            f"<td>{link(approval.issue_url, f'{approval.issue_repo}#{approval.issue_number}')}</td>"
            "</tr>"
        )
    rows.extend(["</tbody></table>", "</section>"])
    return "\n".join(rows)


def render_observations(detail: DashboardFamilyDetail) -> str:
    rows = [
        '<section class="panel">',
        "<h2>Examples</h2>",
        '<table><thead><tr><th>seen</th><th>severity</th><th>diagnostic</th><th>line</th></tr></thead><tbody>',
    ]
    if not detail.observations:
        rows.append('<tr><td colspan="4" class="empty">No examples recorded.</td></tr>')
    for observation in detail.observations:
        rows.append(
            "<tr>"
            f"<td>{esc(short_time(observation.observed_at))}</td>"
            f"<td>{esc(observation.severity)}</td>"
            f"<td>{render_diagnostic(observation.diagnostic_reference)}</td>"
            f"<td><code>{esc(observation.representative_line)}</code></td>"
            "</tr>"
        )
    rows.extend(["</tbody></table>", "</section>"])
    return "\n".join(rows)


def render_count(family: DashboardFamilySummary) -> str:
    if family.family_type == "incident":
        return (
            f"<strong>{family.count}</strong>"
            f"<span>{family.last_24h_count or 0} / {family.last_7d_count or 0} / {family.last_14d_count or 0}</span>"
        )
    return f"<strong>{family.count}</strong><span>n/a</span>"


def window_counts(family: DashboardFamilySummary) -> str:
    if family.family_type != "incident":
        return "n/a"
    return esc(f"{family.last_24h_count or 0} / {family.last_7d_count or 0} / {family.last_14d_count or 0}")


def render_issue_link(family: DashboardFamilySummary) -> str:
    if family.issue_url and family.issue_number is not None:
        return link(family.issue_url, f"{family.issue_repo}#{family.issue_number}")
    if family.family_type == "operational":
        return "operational"
    return "none"


def render_dispatch_status(family: DashboardFamilySummary) -> str:
    if family.dispatch_count <= 0:
        return "none"
    latest = f", latest {short_time(family.last_dispatch_at)}" if family.last_dispatch_at else ""
    return esc(f"{family.dispatch_count}{latest}")


def render_pull_request(pull_request: GitHubPullRequest | None) -> str:
    if pull_request is None:
        return "unknown"
    return link(pull_request.url, f"{pull_request.repo}#{pull_request.number} ({pull_request.state})")


def render_diagnostic(reference: str) -> str:
    if reference.startswith("http://") or reference.startswith("https://"):
        return link(reference, "open")
    return f"<code>{esc(reference or 'n/a')}</code>"


def summary_card(label: str, value: str) -> str:
    return f'<div class="card"><span>{esc(label)}</span><strong>{esc(value)}</strong></div>'


def link(url: str, label: str) -> str:
    return f'<a href="{esc(url)}" rel="noreferrer">{esc(label)}</a>'


def options(values: tuple[str, ...], selected: str) -> str:
    return "".join(
        f'<option value="{esc(value)}"{" selected" if value == selected else ""}>{esc(value)}</option>'
        for value in values
    )


def short_time(value: str | None) -> str:
    if not value:
        return "n/a"
    parsed = parse_dt(value)
    if parsed is None:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def first(params: dict[str, list[str]], name: str, default: str) -> str:
    values = params.get(name)
    if not values:
        return default
    return values[0]


def esc(value: str) -> str:
    return escape(value, quote=True)


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --text: #18212f;
      --muted: #657083;
      --line: #d8dee8;
      --blue: #1f5fbf;
      --green: #276749;
      --amber: #8a5a00;
    }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 20px 24px 12px;
    }}
    h1, h2 {{
      margin: 0;
      letter-spacing: 0;
    }}
    h1 {{
      font-size: 24px;
    }}
    h2 {{
      font-size: 16px;
      margin-bottom: 12px;
    }}
    a {{
      color: var(--blue);
      text-decoration: none;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    .family-link {{
      font-weight: 600;
    }}
    .subhead {{
      margin: 4px 0 0;
      color: var(--muted);
    }}
    .health {{
      color: var(--muted);
    }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 10px;
      padding: 0 24px 14px;
    }}
    .card, .panel, .filters {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .card {{
      padding: 12px;
    }}
    .card span, td span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
    }}
    .card strong {{
      display: block;
      margin-top: 4px;
      font-size: 20px;
    }}
    .filters {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: end;
      margin: 0 24px 14px;
      padding: 12px;
    }}
    label {{
      display: grid;
      gap: 4px;
      color: var(--muted);
      font-size: 12px;
    }}
    select, input, button {{
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #ffffff;
      color: var(--text);
      font: inherit;
      padding: 4px 8px;
    }}
    button {{
      background: var(--text);
      color: #ffffff;
      cursor: pointer;
    }}
    table {{
      width: calc(100% - 48px);
      margin: 0 24px 20px;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    .panel table {{
      width: 100%;
      margin: 0;
      border-radius: 0;
      border-left: 0;
      border-right: 0;
      border-bottom: 0;
    }}
    th, td {{
      padding: 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
      background: #fbfcfe;
    }}
    code {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }}
    .badge {{
      display: inline-block;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      font-weight: 600;
    }}
    .incident {{
      color: var(--blue);
      background: #eaf1ff;
    }}
    .operational {{
      color: var(--amber);
      background: #fff3d6;
    }}
    .panel {{
      margin: 0 24px 16px;
      padding: 14px;
    }}
    dl {{
      display: grid;
      grid-template-columns: minmax(120px, 220px) 1fr;
      gap: 8px 12px;
      margin: 0;
    }}
    dt {{
      color: var(--muted);
    }}
    dd {{
      margin: 0;
      overflow-wrap: anywhere;
    }}
    .empty {{
      color: var(--muted);
    }}
    @media (max-width: 760px) {{
      header, .summary, .filters, .panel {{
        margin-left: 0;
        margin-right: 0;
        padding-left: 14px;
        padding-right: 14px;
      }}
      table {{
        display: block;
        width: auto;
        margin: 0 14px 16px;
        overflow-x: auto;
      }}
      dl {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""
