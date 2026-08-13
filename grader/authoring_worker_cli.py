import os
import signal
import threading
from pathlib import Path
from typing import Protocol

from grader.acceptance import StarterFailureGate
from grader.author import HermesTestAuthor
from grader.coordinator import AuthoringCoordinator
from grader.critic import HermesTestCritic
from grader.llm_broker import UnixLlmBrokerClient
from grader.source import GitSourceLoader
from grader.store import GraderStore, SuiteVault
from grader.vm_executor import VmSuiteExecutor
from grader.vm_launcher import UnixVmLauncherClient


class AuthoringProcessor(Protocol):
    def process_once(self) -> str | None: ...


def run_cycle(coordinator: AuthoringProcessor) -> str | None:
    return coordinator.process_once()


def run(coordinator: AuthoringProcessor, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            result = run_cycle(coordinator)
        except Exception:  # noqa: BLE001 - durable job state controls bounded retries
            stop.wait(2)
            continue
        if result is None:
            stop.wait(1)


def build_from_environment() -> AuthoringCoordinator:
    store = GraderStore(Path(_required("GRADER_DATABASE_PATH")))
    vault = SuiteVault(Path(_required("GRADER_SUITE_VAULT_PATH")))
    source_loader = GitSourceLoader(
        _required("GRADER_GIT_REPOSITORY"),
        Path(_required("GRADER_GIT_CACHE_PATH")),
    )
    broker = UnixLlmBrokerClient(
        Path(_required("GRADER_LLM_BROKER_SOCKET")),
        timeout_seconds=240,
    )
    vm_worker = UnixVmLauncherClient(
        Path(_required("GRADER_LAUNCHER_SOCKET")),
        timeout_seconds=30,
    )
    return AuthoringCoordinator(
        store=store,
        vault=vault,
        source_loader=source_loader,
        author=HermesTestAuthor(broker=broker),
        critic=HermesTestCritic(broker=broker),
        acceptance_gate=StarterFailureGate(VmSuiteExecutor(vm_worker)),
    )


def main() -> int:
    try:
        coordinator = build_from_environment()
    except (OSError, RuntimeError, ValueError):
        print("Authoring worker configuration is invalid")
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
