from __future__ import annotations

import logging
import sys
import threading
import time

from .config import Config
from .docker_logs import DockerLogCollector
from .github import GitHubClient
from .metadata import load_catalog
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
    github = GitHubClient(
        token=config.github_token,
        api_url=config.github_api_url,
        dry_run=config.dry_run,
        timeout_seconds=config.http_timeout_seconds,
    )
    service = SREService(
        config=config,
        catalog_loader=lambda: load_catalog(config.service_metadata_path, default_issue_repo=config.default_issue_repo),
        state=state,
        github=github,
        logs=DockerLogCollector(),
    )
    start_approval_poller(config, service)
    SREServer(config=config, service=service).serve_forever()
    return 0


def start_approval_poller(config: Config, service: SREService) -> None:
    if config.approval_poll_seconds <= 0:
        logging.getLogger("homelab-sre-agent.approvals").info("Autofix approval polling disabled")
        return

    logger = logging.getLogger("homelab-sre-agent.approvals")

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


if __name__ == "__main__":
    sys.exit(main())
