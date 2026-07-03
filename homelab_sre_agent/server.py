from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
import hmac
import json
import logging
import threading
from typing import Any
from urllib.parse import urlsplit

from .config import Config
from .dashboard import render_family_from_state, render_index_from_state
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
                parsed = urlsplit(self.path)
                if parsed.path == "/health":
                    write_json(self, HTTPStatus.OK, {"ok": True, "dry_run": config.dry_run})
                    return
                if parsed.path == "/docs":
                    write_html(self, HTTPStatus.OK, docs_page(config))
                    return
                if parsed.path == "/openapi.json":
                    write_json(self, HTTPStatus.OK, openapi_document())
                    return
                if parsed.path == "/incidents":
                    write_html(self, HTTPStatus.OK, render_index_from_state(service.state, parsed.query))
                    return
                if parsed.path == "/incidents/family":
                    try:
                        body = render_family_from_state(service.state, service.github, parsed.query)
                    except ValueError as exc:
                        write_html(self, HTTPStatus.BAD_REQUEST, error_page("Bad request", str(exc)))
                        return
                    if body is None:
                        write_html(self, HTTPStatus.NOT_FOUND, error_page("Not found", "Incident family not found."))
                        return
                    write_html(self, HTTPStatus.OK, body)
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


def write_html(handler: BaseHTTPRequestHandler, status: HTTPStatus, body: str) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def error_page(title: str, message: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>{title}</title></head>
<body><h1>{title}</h1><p>{message}</p><p><a href="/incidents">incidents</a></p></body>
</html>
"""


def docs_page(config: Config) -> str:
    auth_text = (
        "Requires <code>Authorization: Bearer &lt;SRE_INCIDENT_TOKEN&gt;</code> when "
        "<code>SRE_INCIDENT_TOKEN</code> is configured. Without a token, the endpoint accepts trusted internal callers."
    )
    dry_run_text = "enabled" if config.dry_run else "disabled"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>homelab-sre-agent API docs</title>
  <style>
    :root {{
      color-scheme: light dark;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    body {{
      margin: 0 auto;
      max-width: 52rem;
      padding: 2rem 1.25rem 3rem;
      line-height: 1.5;
    }}
    h1, h2 {{ line-height: 1.2; }}
    code {{ font-family: "SFMono-Regular", Menlo, Monaco, monospace; }}
    .endpoint {{
      border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
      border-radius: 6px;
      padding: 1rem;
      margin: 1rem 0;
    }}
    .method {{
      display: inline-block;
      min-width: 4.5rem;
      font-weight: 700;
    }}
  </style>
</head>
<body>
  <h1>homelab-sre-agent API docs</h1>
  <p>
    This service receives structured incidents from <code>homelab-log-watcher</code>,
    records local diagnostic bundles, manages issue state, and can dispatch
    Codex workflows with GitHub <code>repository_dispatch</code> after approval.
  </p>
  <p>
    Machine-readable schema: <a href="/openapi.json"><code>/openapi.json</code></a>.
    Runtime mode: dry-run is <strong>{escape(dry_run_text)}</strong>.
  </p>

  <h2>HTTP endpoints</h2>

  <section class="endpoint">
    <div><span class="method">GET</span><code>/health</code></div>
    <p>Returns a small JSON health response and whether dry-run mode is active.</p>
  </section>

  <section class="endpoint">
    <div><span class="method">POST</span><code>/v1/incidents</code></div>
    <p>Accepts the incident webhook payload emitted by <code>homelab-log-watcher</code>.</p>
    <p>{auth_text}</p>
    <p>
      Successful responses report the incident outcome, linked issue details,
      local diagnostic bundle reference, and any autofix dispatch status.
    </p>
  </section>

  <section class="endpoint">
    <div><span class="method">GET</span><code>/incidents</code></div>
    <p>Read-only HTML dashboard over local state, issue links, approval tokens, and dispatch history.</p>
  </section>

  <section class="endpoint">
    <div><span class="method">GET</span><code>/incidents/family</code></div>
    <p>HTML detail view for one incident family. Supply <code>type</code> and <code>fingerprint</code> query parameters.</p>
  </section>

  <h2>Non-HTTP workflow actions</h2>
  <p>
    Phone approval handling and Codex dispatch are internal workflows, not public HTTP endpoints.
    Diagnostic bundles are written locally and may also be published to object storage when configured.
  </p>
</body>
</html>
"""


def openapi_document() -> dict[str, Any]:
    incident_properties = {
        "container_id": {"type": ["string", "null"]},
        "container_name": {"type": "string"},
        "image": {"type": "string"},
        "severity": {"type": "string"},
        "matched_pattern": {"type": "string"},
        "line": {"type": "string"},
        "normalized_line": {"type": "string"},
        "fingerprint": {"type": "string"},
        "occurred_at": {"type": "string"},
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "homelab-sre-agent",
            "version": "0.1.0",
            "description": "Incident intake, health, and local dashboard endpoints for the homelab SRE agent.",
        },
        "jsonSchemaDialect": "https://spec.openapis.org/oas/3.1/dialect/base",
        "servers": [{"url": "/"}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "opaque token",
                    "description": "Used by POST /v1/incidents when SRE_INCIDENT_TOKEN is configured.",
                }
            },
            "schemas": {
                "HealthResponse": {
                    "type": "object",
                    "required": ["ok", "dry_run"],
                    "properties": {
                        "ok": {"type": "boolean"},
                        "dry_run": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
                "ErrorResponse": {
                    "type": "object",
                    "required": ["error"],
                    "properties": {"error": {"type": "string"}},
                    "additionalProperties": False,
                },
                "IncidentPayload": {
                    "type": "object",
                    "required": ["version", "source", "detected_at", "incident"],
                    "properties": {
                        "version": {"type": "integer"},
                        "source": {"type": "string"},
                        "detected_at": {"type": "string"},
                        "incident": {
                            "type": "object",
                            "required": ["container_name", "fingerprint"],
                            "properties": incident_properties,
                            "additionalProperties": True,
                        },
                    },
                    "additionalProperties": True,
                },
            },
        },
        "paths": {
            "/health": {
                "get": {
                    "summary": "Health check",
                    "responses": {
                        "200": {
                            "description": "Service health and dry-run mode.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/HealthResponse"}
                                }
                            },
                        }
                    },
                }
            },
            "/docs": {
                "get": {
                    "summary": "Human-readable API docs",
                    "responses": {
                        "200": {
                            "description": "Browser-friendly service docs.",
                            "content": {"text/html": {"schema": {"type": "string"}}},
                        }
                    },
                }
            },
            "/openapi.json": {
                "get": {
                    "summary": "OpenAPI document",
                    "responses": {
                        "200": {
                            "description": "OpenAPI 3.1 schema for public HTTP endpoints.",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    },
                }
            },
            "/incidents": {
                "get": {
                    "summary": "Incident dashboard",
                    "description": "Read-only HTML dashboard for recent incident families, issue state, and dispatch history.",
                    "responses": {
                        "200": {
                            "description": "Dashboard HTML.",
                            "content": {"text/html": {"schema": {"type": "string"}}},
                        }
                    },
                }
            },
            "/incidents/family": {
                "get": {
                    "summary": "Incident family detail",
                    "parameters": [
                        {
                            "name": "type",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                            "description": "Family type from the dashboard, such as incident or operational.",
                        },
                        {
                            "name": "fingerprint",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                            "description": "Stored family fingerprint.",
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Family detail HTML.",
                            "content": {"text/html": {"schema": {"type": "string"}}},
                        },
                        "400": {
                            "description": "Missing or invalid query parameters.",
                            "content": {
                                "text/html": {"schema": {"type": "string"}}
                            },
                        },
                        "404": {
                            "description": "No matching family exists.",
                            "content": {
                                "text/html": {"schema": {"type": "string"}}
                            },
                        },
                    },
                }
            },
            "/v1/incidents": {
                "post": {
                    "summary": "Ingest one incident webhook",
                    "description": (
                        "Accepts the structured incident payload from homelab-log-watcher. "
                        "Authorization uses a bearer token when SRE_INCIDENT_TOKEN is configured; "
                        "otherwise the endpoint accepts trusted internal callers."
                    ),
                    "security": [{"bearerAuth": []}, {}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/IncidentPayload"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Incident processed, ignored, or reused successfully.",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        },
                        "400": {
                            "description": "Invalid JSON or missing required incident fields.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                                }
                            },
                        },
                        "401": {
                            "description": "Bearer token missing or invalid when token auth is enabled.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                                }
                            },
                        },
                        "500": {
                            "description": "Unexpected incident handling failure.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                                }
                            },
                        },
                    },
                }
            },
        },
    }
