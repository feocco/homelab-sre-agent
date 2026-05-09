from __future__ import annotations

import re


SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([a-z0-9_]*(?:token|secret|password|passwd|api[_-]?key|authorization|credential)[a-z0-9_]*)"
    r"\s*[:=]\s*([^\s,;]+)"
)
BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
URL_CREDENTIALS = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@")


def redact_text(value: str, *, limit: int | None = None) -> str:
    redacted = URL_CREDENTIALS.sub(r"\1<redacted>@", value)
    redacted = BEARER_TOKEN.sub("Bearer <redacted>", redacted)
    redacted = SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", redacted)
    if limit is not None and len(redacted) > limit:
        return redacted[: limit - 3] + "..."
    return redacted
