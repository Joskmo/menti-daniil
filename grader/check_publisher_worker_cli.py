import os
import signal
import threading
from pathlib import Path
from typing import Protocol

from grader.check_publication import CheckPublicationCoordinator
from grader.github_app import (
    GitHubAppTokenProvider,
    GitHubHttpChecksTransport,
    StaticGitHubTokenProvider,
)
from grader.github_checks import GitHubHiddenGradePublisher, GitHubHiddenGradeStatusPublisher
from grader.store import GraderStore


class PublicationProcessor(Protocol):
    def process_once(self) -> str: ...


def run_cycle(coordinator: PublicationProcessor) -> str:
    return coordinator.process_once()


def run(coordinator: PublicationProcessor, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            result = run_cycle(coordinator)
        except RuntimeError:
            stop.wait(2)
            continue
        if result == "idle":
            stop.wait(1)


def build_from_environment() -> CheckPublicationCoordinator:
    store = GraderStore(Path(_required("GRADER_DATABASE_PATH")))
    existing_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if existing_token:
        tokens = StaticGitHubTokenProvider(existing_token)
        publisher_class = GitHubHiddenGradeStatusPublisher
    else:
        tokens = GitHubAppTokenProvider(
            app_id=_positive_integer("GITHUB_APP_ID"),
            installation_id=_positive_integer("GITHUB_APP_INSTALLATION_ID"),
            private_key_path=Path(_required("GITHUB_APP_PRIVATE_KEY_PATH")),
        )
        publisher_class = GitHubHiddenGradePublisher
    publisher = publisher_class(
        GitHubHttpChecksTransport(tokens),
        _required("GITHUB_REPOSITORY_OWNER"),
        _required("GITHUB_REPOSITORY_NAME"),
    )
    return CheckPublicationCoordinator(store=store, publisher=publisher)


def main() -> int:
    try:
        coordinator = build_from_environment()
    except (OSError, RuntimeError, ValueError):
        print("Check publisher worker configuration is invalid")
        return 2
    stop = threading.Event()

    def request_stop(signum, frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run(coordinator, stop)
    return 0


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip() or value != value.strip() or "\x00" in value:
        raise RuntimeError(f"{name} is required")
    return value


def _positive_integer(name: str) -> int:
    value = _required(name)
    if not value.isascii() or not value.isdigit():
        raise ValueError(f"{name} must be a positive integer")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())