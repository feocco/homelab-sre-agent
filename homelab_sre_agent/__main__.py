from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time

from .config import Config
from .docker_logs import DockerLogCollector
from .github import github_client_from_config
from .metadata import load_catalog
from .notifications import IssueNotifier, PhoneApprovalListener
from .server import SREServer
from .service import SREService
from .state import StateStore


def main() -> int:
    config = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    state = StateStore(config.state_path)
    github = github_client_from_config(config)
    service = SREService(
        config=config,
        catalog_loader=lambda: load_catalog(config.service_metadata_path, default_issue_repo=config.default_issue_repo),
        state=state,
        github=github,
        logs=DockerLogCollector(),
        issue_notifier=IssueNotifier() if config.issue_notifications_enabled else None,
    )
    start_approval_poller(config, service)
    start_phone_approval_listener(config, service)
    SREServer(config=config, service=service).serve_forever()
    return 0


def start_approval_poller(config: Config, service: SREService) -> None:
    if config.approval_poll_seconds <= 0:
        logging.getLogger("homelab-sre-agent.approvals").info("Autofix approval polling disabled")
        return

    logger = logging.getLogger("homelab-sre-agent.approvals")
    try:
        service.ensure_autofix_labels()
    except Exception:
        logger.exception("Autofix label startup ensure failed")

    def poll_forever() -> None:
        while True:
            try:
                result = service.poll_autofix_approvals()
                if result["processed"]:
                    logger.info("Autofix approval poll result: %s", result)
            except Exception:
                logger.exception("Autofix approval poll failed")
            time.sleep(config.approval_poll_seconds)

    thread = threading.Thread(target=poll_forever, name="sre-autofix-approvals", daemon=True)
    thread.start()
    logger.info("Autofix approval polling every %s seconds", config.approval_poll_seconds)


def start_phone_approval_listener(config: Config, service: SREService) -> None:
    logger = logging.getLogger("homelab-sre-agent.phone-approvals")
    if not config.phone_approvals_enabled:
        logger.info("Phone approval listener disabled")
        return

    def run_listener() -> None:
        asyncio.run(PhoneApprovalListener(service).run_forever())

    thread = threading.Thread(target=run_listener, name="sre-phone-approvals", daemon=True)
    thread.start()
    logger.info("Phone approval listener enabled")


if __name__ == "__main__":
    sys.exit(main())
