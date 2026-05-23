from __future__ import annotations

import base64
from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Config:
    state_path: Path
    service_metadata_path: Path
    diagnostic_dir: Path
    diagnostic_reference_root: str
    incident_token: str | None
    github_token: str | None
    github_auth_mode: str
    github_app_id: str | None
    github_app_installation_id: str | None
    github_app_private_key: str | None
    github_api_url: str
    default_issue_repo: str
    dry_run: bool
    docker_log_tail: int
    docker_log_lookback_seconds: int
    episode_window_seconds: int
    diagnostic_max_bytes: int
    investigation_cooldown_seconds: int
    issue_comment_cooldown_seconds: int
    codex_global_daily_limit: int
    approval_poll_seconds: int
    issue_notifications_enabled: bool
    phone_approvals_enabled: bool
    diagnostic_publish_enabled: bool
    diagnostic_s3_bucket: str | None
    diagnostic_s3_region: str | None
    diagnostic_s3_prefix: str
    diagnostic_url_ttl_seconds: int
    service_host: str
    service_port: int
    http_timeout_seconds: float
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        state_path = Path(os.environ.get("SRE_STATE_PATH", "/app/state/sre-agent.sqlite3"))
        diagnostic_dir = Path(os.environ.get("SRE_DIAGNOSTIC_DIR", str(state_path.parent / "diagnostics")))
        return cls(
            state_path=state_path,
            service_metadata_path=Path(os.environ.get("SRE_SERVICE_METADATA_PATH", "/app/config/services.yaml")),
            diagnostic_dir=diagnostic_dir,
            diagnostic_reference_root=os.environ.get("SRE_DIAGNOSTIC_REFERENCE_ROOT", str(diagnostic_dir)),
            incident_token=optional(os.environ.get("SRE_INCIDENT_TOKEN")),
            github_token=optional(os.environ.get("GITHUB_TOKEN")),
            github_auth_mode=os.environ.get("GITHUB_AUTH_MODE", "token").strip().lower() or "token",
            github_app_id=optional(os.environ.get("GITHUB_APP_ID")),
            github_app_installation_id=optional(os.environ.get("GITHUB_APP_INSTALLATION_ID")),
            github_app_private_key=github_app_private_key_from_env(),
            github_api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            default_issue_repo=os.environ.get("SRE_DEFAULT_ISSUE_REPO", "feocco/homelab-config"),
            dry_run=parse_bool(os.environ.get("SRE_DRY_RUN"), True),
            docker_log_tail=parse_int("SRE_DOCKER_LOG_TAIL", 200),
            docker_log_lookback_seconds=parse_int("SRE_DOCKER_LOG_LOOKBACK_SECONDS", 600),
            episode_window_seconds=parse_int("SRE_EPISODE_WINDOW_SECONDS", 120),
            diagnostic_max_bytes=parse_int("SRE_DIAGNOSTIC_MAX_BYTES", 1_000_000),
            investigation_cooldown_seconds=parse_int("SRE_INVESTIGATION_COOLDOWN_SECONDS", 86400),
            issue_comment_cooldown_seconds=parse_int("SRE_ISSUE_COMMENT_COOLDOWN_SECONDS", 3600),
            codex_global_daily_limit=parse_int("SRE_CODEX_GLOBAL_DAILY_LIMIT", 3),
            approval_poll_seconds=parse_int("SRE_APPROVAL_POLL_SECONDS", 300),
            issue_notifications_enabled=parse_bool(os.environ.get("SRE_ISSUE_NOTIFICATIONS_ENABLED"), False),
            phone_approvals_enabled=parse_bool(os.environ.get("SRE_PHONE_APPROVALS_ENABLED"), False),
            diagnostic_publish_enabled=parse_bool(os.environ.get("SRE_DIAGNOSTIC_PUBLISH_ENABLED"), False),
            diagnostic_s3_bucket=optional(os.environ.get("SRE_DIAGNOSTIC_S3_BUCKET")),
            diagnostic_s3_region=optional(os.environ.get("SRE_DIAGNOSTIC_S3_REGION")),
            diagnostic_s3_prefix=os.environ.get("SRE_DIAGNOSTIC_S3_PREFIX", "diagnostics").strip().strip("/") or "diagnostics",
            diagnostic_url_ttl_seconds=parse_int("SRE_DIAGNOSTIC_URL_TTL_SECONDS", 3600),
            service_host=os.environ.get("SERVICE_HOST", "0.0.0.0"),
            service_port=parse_int("SERVICE_PORT", 8094),
            http_timeout_seconds=float(os.environ.get("SRE_HTTP_TIMEOUT_SECONDS", "10")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )


def optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or stripped == "replace_me":
        return None
    return stripped


def github_app_private_key_from_env() -> str | None:
    raw = optional(os.environ.get("GITHUB_APP_PRIVATE_KEY"))
    if raw:
        return raw.replace("\\n", "\n")
    encoded = optional(os.environ.get("GITHUB_APP_PRIVATE_KEY_B64"))
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded).decode("utf-8")
    except Exception as exc:
        raise ValueError("GITHUB_APP_PRIVATE_KEY_B64 must be valid base64-encoded UTF-8") from exc


def parse_bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{name} must be >= 0")
    return parsed
