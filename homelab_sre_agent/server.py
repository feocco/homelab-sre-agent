from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import threading
from typing import Any
from urllib.parse import urlsplit

from .config import Config
from .service import SREService


LOGGER = logging.getLogger("homelab-sre-agent.server")
MAX_BODY_BYTES = 256 * 1024


class SREServer:
    def __init__(self, *, config: Config, service: SREService) -> None:
        self.config = config
        self.service = service
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def serve_forever(self) -> None:
        handler = self._handler()
        self.httpd = ThreadingHTTPServer((self.config.service_host, self.config.service_port), handler)
        LOGGER.info("SRE server listening on %s:%s", self.config.service_host, self.config.service_port)
        self.httpd.serve_forever()

    def _handler(self):
        config = self.config
        service = self.service

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if urlsplit(self.path).path == "/health":
                    write_json(self, HTTPStatus.OK, {"ok": True, "dry_run": config.dry_run})
                    return
                write_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_POST(self) -> None:
                if urlsplit(self.path).path != "/v1/incidents":
                    write_json(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                if config.incident_token and not authorized(self.headers.get("Authorization"), config.incident_token):
                    write_json(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                try:
                    payload = read_json_body(self)
                    result = service.handle_incident(payload)
                except ValueError as exc:
                    write_json(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except Exception:
                    LOGGER.exception("Incident handling failed")
                    write_json(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "incident handling failed"})
                    return
                write_json(self, HTTPStatus.OK, result)

            def log_message(self, format: str, *args: Any) -> None:
                LOGGER.debug(format, *args)

        return Handler


def authorized(header: str | None, expected: str) -> bool:
    if not header:
        return False
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    return hmac.compare_digest(header[len(prefix) :], expected)


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        raise ValueError("empty request body")
    if length > MAX_BODY_BYTES:
        raise ValueError("request body too large")
    raw = handler.rfile.read(length)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def write_json(handler: BaseHTTPRequestHandler, status: HTTPStatus, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
