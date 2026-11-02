# OpenAPI document

The service publishes its HTTP contract as OpenAPI 3.1 JSON.

## Sub-features

- Bearer authentication scheme for incident intake.
- Incident request schema with all nine nested incident properties.
- Response contracts for health, intake, docs, and dashboard routes.
- HTTP 422 error response for incident bodies that cannot be processed.

## How to get to it (user POV)

Send `GET /openapi.json`. Open `/docs` for the short browser-readable endpoint
summary.

## Driving it with HTTP

Launch a debugging instance, then request the document:

```bash
curl --fail --silent --show-error "$SERVICE_URL/openapi.json"
```

The expected end state is an OpenAPI `3.1.0` document whose
`POST /v1/incidents` response map contains `200`, `401`, `422`, and `500`.

## Gotchas

`openapi_document` lives in `server.py` rather than a generated file. Change the
route behavior, OpenAPI response, and HTTP test in the same edit.
