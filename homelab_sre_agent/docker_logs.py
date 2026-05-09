from __future__ import annotations

from typing import Any

import docker


class DockerLogCollector:
    def __init__(self, docker_client: Any | None = None) -> None:
        self.docker_client = docker_client or docker.from_env()

    def collect(self, *, container_id: str | None, container_name: str | None, tail: int) -> str:
        container = self._get_container(container_id=container_id, container_name=container_name)
        raw = container.logs(stdout=True, stderr=True, timestamps=True, tail=tail)
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace")
        return str(raw)

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
