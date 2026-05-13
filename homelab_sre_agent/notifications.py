from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Callable

from .github import IssueResult
from .metadata import ServiceMetadata

if TYPE_CHECKING:
    from .service import SREService


LOGGER = logging.getLogger("homelab-sre-agent.notifications")
SRE_APPROVE_ACTION_PREFIX = "HOMELAB_SRE_APPROVE"
PHONE_APPROVAL_TOKEN_TTL_SECONDS = 7 * 86400


class IssueNotifier:
    def __init__(self, notify_func: Callable[..., dict[str, Any]] | None = None) -> None:
        if notify_func is None:
            import homelab

            notify_func = homelab.notify_joe
        self.notify_func = notify_func

    def send_issue_created(
        self,
        *,
        service: ServiceMetadata,
        incident: Any,
        analysis: Any,
        issue: IssueResult,
        sanitized_line: str,
        approval_action: str | None,
    ) -> dict[str, Any]:
        buttons: list[dict[str, str]] = [
            {
                "title": "Open issue",
                "action": "URI",
                "uri": issue.url,
            }
        ]
        if approval_action:
            buttons.append(
                {
                    "title": "Approve autofix",
                    "action": approval_action,
                }
            )

        autofix_status = "waiting for approval" if approval_action else "not enabled"
        message = "\n".join(
            [
                f"Issue: {service.issue_repo}#{issue.number}",
                f"Severity: {incident.severity}",
                f"Summary: {analysis.summary}",
                f"Log: {truncate(sanitized_line, 500)}",
                f"Autofix: {autofix_status}",
            ]
        )
        return self.notify_func(
            f"SRE issue - {service.name}",
            message,
            tag=f"homelab-sre-{service.name}-{issue.number}",
            group="homelab-sre",
            url=issue.url,
            buttons=buttons,
        )


class PhoneApprovalListener:
    def __init__(self, service: SREService, *, reconnect_seconds: int = 15) -> None:
        self.service = service
        self.reconnect_seconds = reconnect_seconds

    async def run_forever(self) -> None:
        while True:
            try:
                await self._run_connected()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Phone approval listener failed; reconnecting soon")
                await asyncio.sleep(self.reconnect_seconds)

    async def _run_connected(self) -> None:
        import homelab

        router = homelab.NotificationActionRouter()
        router.register(SRE_APPROVE_ACTION_PREFIX, self._handle_approval)

        async with homelab.HomeAssistantWebSocketClient.from_env() as ha:
            async def route_event(event: dict[str, Any]) -> None:
                if router.handle_event(event):
                    LOGGER.info("Handled SRE phone approval action")

            ha.add_event_handler(route_event)
            await ha.subscribe_events("mobile_app_notification_action")
            LOGGER.info("Listening for SRE phone approval actions")
            await ha.wait_closed()
            LOGGER.warning("Phone approval listener disconnected; reconnecting")

    def _handle_approval(self, token: str, event: dict[str, Any]) -> None:
        result = self.service.approve_autofix_from_phone(token)
        LOGGER.info("Phone approval result: %s", result)


def make_approval_action(token: str) -> str:
    import homelab

    return homelab.NotificationActionRouter.make_action(SRE_APPROVE_ACTION_PREFIX, token)


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1]}..."
