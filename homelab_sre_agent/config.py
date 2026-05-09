from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Config:
    state_path: Path
    service_metadata_path: Path
    incident_token: str | None
    github_token: str | None
    github_api_url: str
    default_issue_repo: str
    dry_run: bool
    docker_log_tail: int
    investigation_cooldown_seconds: int
    codex_global_daily_limit: int
    service_host: str
    service_port: int
    http_timeout_seconds: float
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            state_path=Path(os.environ.get("SRE_STATE_PATH", "/app/state/sre-agent.sqlite3")),
            service_metadata_path=Path(os.environ.get("SRE_SERVICE_METADATA_PATH", "/app/config/services.yaml")),
            incident_token=optional(os.environ.get("SRE_INCIDENT_TOKEN")),
            github_token=optional(os.environ.get("GITHUB_TOKEN")),
            github_api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            default_issue_repo=os.environ.get("SRE_DEFAULT_ISSUE_REPO", "feocco/homelab-config"),
            dry_run=parse_bool(os.environ.get("SRE_DRY_RUN"), True),
            docker_log_tail=parse_int("SRE_DOCKER_LOG_TAIL", 200),
            investigation_cooldown_seconds=parse_int("SRE_INVESTIGATION_COOLDOWN_SECONDS", 86400),
            codex_global_daily_limit=parse_int("SRE_CODEX_GLOBAL_DAILY_LIMIT", 3),
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
