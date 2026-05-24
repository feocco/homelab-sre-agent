from __future__ import annotations

from pathlib import Path

import pytest

from homelab_sre_agent.routing_metadata import (
    RoutingMetadataError,
    apply_routing_metadata,
    extract_latest_suggestion,
    load_suggestion,
)


def write_services(path: Path) -> None:
    path.write_text(
        """defaults:
  issue_repo: feocco/homelab-config

services:
  plant-monitor:
    containers:
      - plant-monitor
    sre:
      enabled: true
      autofix: true

  homelab-sre-agent:
    containers:
      - homelab-sre-agent
    sre:
      enabled: true
      autofix: true
      routing:
        operational_dependencies:
          - dependency: homelab-functions
            pattern: "homelab.client.HomelabFunctionsError: 500 Internal Server Error"
            reason: "homelab-functions is unavailable."
""",
        encoding="utf-8",
    )


def test_valid_suggestion_adds_operational_dependency_rule(tmp_path: Path) -> None:
    services_path = tmp_path / "services.yaml"
    write_services(services_path)

    result = apply_routing_metadata(
        services_path=services_path,
        suggestion=load_suggestion(
            """
source_issue_url: https://github.com/feocco/plant-monitor/issues/44
service: plant-monitor
dependency: home-assistant
pattern: "aiohttp.client_exceptions.ClientConnectorError: Cannot connect to host ha.example:443"
reason: "Home Assistant was temporarily unreachable and the client already retries."
evidence: "Retry loop continued after the connection failure."
"""
        ),
    )

    text = services_path.read_text(encoding="utf-8")
    assert result.changed is True
    assert "routing:" in text
    assert "operational_dependencies:" in text
    assert "dependency: home-assistant" in text
    assert 'pattern: "aiohttp.client_exceptions.ClientConnectorError: Cannot connect to host ha.example:443"' in text
    assert 'reason: "Home Assistant was temporarily unreachable and the client already retries."' in text


def test_missing_required_fields_fail_validation() -> None:
    with pytest.raises(RoutingMetadataError, match="missing required field: pattern"):
        load_suggestion(
            """
source_issue_url: https://github.com/feocco/plant-monitor/issues/44
service: plant-monitor
dependency: home-assistant
reason: "Temporary dependency outage."
evidence: "Connection recovered after retry."
"""
        )


def test_unknown_service_fails_validation(tmp_path: Path) -> None:
    services_path = tmp_path / "services.yaml"
    write_services(services_path)

    with pytest.raises(RoutingMetadataError, match="unknown service: unknown-service"):
        apply_routing_metadata(
            services_path=services_path,
            suggestion=load_suggestion(
                """
source_issue_url: https://github.com/feocco/example/issues/1
service: unknown-service
dependency: home-assistant
pattern: "temporary failure"
reason: "Temporary dependency outage."
evidence: "Connection recovered after retry."
"""
            ),
        )


def test_duplicate_rule_is_not_added_twice(tmp_path: Path) -> None:
    services_path = tmp_path / "services.yaml"
    write_services(services_path)

    result = apply_routing_metadata(
        services_path=services_path,
        suggestion=load_suggestion(
            """
source_issue_url: https://github.com/feocco/homelab-sre-agent/issues/9
service: homelab-sre-agent
dependency: homelab-functions
pattern: "homelab.client.HomelabFunctionsError: 500 Internal Server Error"
reason: "homelab-functions is unavailable."
evidence: "Existing rule already covers this dependency outage."
"""
        ),
    )

    text = services_path.read_text(encoding="utf-8")
    assert result.changed is False
    assert text.count("homelab.client.HomelabFunctionsError") == 1


def test_extract_latest_suggestion_from_issue_comments() -> None:
    comments = [
        {
            "body": """
Earlier note.

```yaml
not: relevant
```
"""
        },
        {
            "body": """
**homelab-sre-agent**

Operational routing metadata suggestion:

```yaml
source_issue_url: https://github.com/feocco/plant-monitor/issues/44
service: plant-monitor
dependency: home-assistant
pattern: "temporary failure"
reason: "Home Assistant was temporarily unreachable."
evidence: "The request succeeded on retry."
```
"""
        },
    ]

    suggestion = extract_latest_suggestion(comments)

    assert suggestion.service == "plant-monitor"
    assert suggestion.dependency == "home-assistant"
