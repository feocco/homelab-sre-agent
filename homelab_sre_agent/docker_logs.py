from __future__ import annotations

from datetime import datetime
from typing import Any

import docker


class DockerLogCollector:
    def __init__(self, docker_client: Any | None = None) -> None:
        self.docker_client = docker_client or docker.from_env()

    def collect(
        self,
        *,
        container_id: str | None,
        container_name: str | None,
        tail: int,
        since: datetime | None = None,
    ) -> str:
        container = self._get_container(container_id=container_id, container_name=container_name)
        kwargs: dict[str, Any] = {"stdout": True, "stderr": True, "timestamps": True}
        if tail:
            kwargs["tail"] = tail
        if since is not None:
            kwargs["since"] = since
        raw = container.logs(**kwargs)
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)

    def collect_many(
        self,
        *,
        container_names: tuple[str, ...],
        incident_container_id: str | None,
        incident_container_name: str,
        tail: int,
        since: datetime | None = None,
    ) -> str:
        names = list(dict.fromkeys([*container_names, incident_container_name]))
        parts: list[str] = []
        for name in names:
            container_id = incident_container_id if name == incident_container_name else None
            try:
                logs = self.collect(container_id=container_id, container_name=name, tail=tail, since=since)
            except Exception as exc:
                logs = f"Could not collect Docker logs for {name}: {exc}"
            parts.append(f"===== {name} =====\n{logs.strip() or '<no logs>'}")
        return "\n\n".join(parts)

    def _get_container(self, *, container_id: str | None, container_name: str | None) -> Any:
        errors: list[str] = []
        for candidate in (container_id, container_name):
            if not candidate:
                continue
            try:
                return self.docker_client.containers.get(candidate)
            except Exception as exc:
                errors.append(str(exc))
        raise RuntimeError("; ".join(errors) or "container id/name missing")
