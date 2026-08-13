import os
import signal
import threading
from pathlib import Path
from typing import Protocol

from grader.grading_coordinator import GradingCoordinator
from grader.source import GitProjectExporter
from grader.store import GraderStore, SuiteVault
from grader.vm_executor import VmSuiteExecutor
from grader.vm_launcher import UnixVmLauncherClient


class GradingProcessor(Protocol):
    def process_once(self) -> str: ...


def run_cycle(coordinator: GradingProcessor) -> str:
    return coordinator.process_once()


def run(coordinator: GradingProcessor, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            result = run_cycle(coordinator)
        except RuntimeError:
            stop.wait(2)
            continue
        if result == "idle":
            stop.wait(1)


def build_from_environment() -> GradingCoordinator:
    database = Path(_required("GRADER_DATABASE_PATH"))
    vault = SuiteVault(Path(_required("GRADER_SUITE_VAULT_PATH")), read_only=True)
    exporter = GitProjectExporter(
        _required("GRADER_GIT_REPOSITORY"),
        Path(_required("GRADER_GIT_CACHE_PATH")),
    )
    worker = UnixVmLauncherClient(
        Path(_required("GRADER_LAUNCHER_SOCKET")),
        timeout_seconds=30,
    )
    return GradingCoordinator(
        store=GraderStore(database),
        vault=vault,
        exporter=exporter,
        executor=VmSuiteExecutor(worker),
    )


def main() -> int:
    try:
        coordinator = build_from_environment()
    except (OSError, RuntimeError, ValueError):
        print("Grading worker configuration is invalid")
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


if __name__ == "__main__":
    raise SystemExit(main())
