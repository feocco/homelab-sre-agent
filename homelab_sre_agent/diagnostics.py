from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any

from .metadata import ServiceMetadata
from .github import IssueResult
from .redact import redact_text
from .state import RecurrenceSummary


@dataclass(frozen=True)
class S3DiagnosticPublisher:
    bucket: str
    prefix: str
    url_ttl_seconds: int
    region_name: str | None = None
    s3_client: Any | None = None

    def publish(
        self,
        *,
        service: ServiceMetadata,
        issue: IssueResult,
        state_key: str,
        diagnostic_reference: str,
        recurrence: RecurrenceSummary,
        now: datetime,
    ) -> str:
        client = self.s3_client or self._client()
        key = "/".join(
            [
                self.prefix.strip("/"),
                service.name,
                f"issue-{issue.number}",
                f"{now.strftime('%Y%m%dT%H%M%SZ')}.json",
            ]
        )
        body = diagnostic_payload(
            service=service,
            issue=issue,
            state_key=state_key,
            diagnostic_reference=diagnostic_reference,
            recurrence=recurrence,
            generated_at=now,
        )
        client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(body, indent=2, sort_keys=True).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        return str(
            client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=self.url_ttl_seconds,
            )
        )

    def _client(self) -> Any:
        import boto3

        return boto3.client("s3", region_name=self.region_name)


def diagnostic_payload(
    *,
    service: ServiceMetadata,
    issue: IssueResult,
    state_key: str,
    diagnostic_reference: str,
    recurrence: RecurrenceSummary,
    generated_at: datetime,
) -> dict[str, Any]:
    return {
        "generated_at": generated_at.astimezone().isoformat(timespec="seconds"),
        "issue": {"repo": issue.repo, "number": issue.number, "url": issue.url},
        "service": {
            "name": service.name,
            "source_repo": service.source_repo,
            "deploy_repo": service.deploy_repo,
            "deploy_path": service.deploy_path,
            "runbook_url": service.runbook_url,
        },
        "incident": {
            "state_fingerprint": state_key,
            "local_diagnostic_reference": redact_text(diagnostic_reference, limit=1000),
        },
        "recurrence": {
            "total_count": recurrence.total_count,
            "first_seen_at": recurrence.first_seen_at,
            "latest_seen_at": recurrence.latest_seen_at,
            "last_24h_count": recurrence.last_24h_count,
            "last_7d_count": recurrence.last_7d_count,
            "last_14d_count": recurrence.last_14d_count,
            "average_gap_seconds": recurrence.average_gap_seconds,
            "likelihood_next_14_days": recurrence.likelihood,
        },
        "access_model": {
            "source": "redacted S3 handoff",
            "nas_public_access": False,
            "cloud_codex_ssh": False,
        },
    }
