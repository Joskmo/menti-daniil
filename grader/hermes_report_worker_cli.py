import os
import signal
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from grader.menti_bot import _format_mentor_notification
from grader.store import GraderStore

MessageSender = Callable[[str, str], None]


class HermesMentorReportCoordinator:
    def __init__(self, *, store: GraderStore, target: str, sender: MessageSender) -> None:
        if (
            not isinstance(target, str)
            or not target.startswith("telegram:")
            or len(target) > 128
            or any(character.isspace() for character in target)
        ):
            raise ValueError("Hermes mentor target is invalid")
        self.store = store
        self.target = target
        self.sender = sender

    def process_once(self) -> str:
        notification = self.store.claim_next_mentor_notification()
        if notification is None:
            return "idle"
        try:
            self.sender(self.target, _format_mentor_notification(notification))
        except Exception:
            self.store.release_mentor_notification(
                notification.notification_id,
                notification.lease_token,
            )
            raise
        if not self.store.mark_mentor_notification_sent(
            notification.notification_id,
            notification.lease_token,
        ):
            raise RuntimeError("mentor notification lease was lost after delivery")
        return "sent"


def hermes_send(target: str, text: str) -> None:
    subprocess.run(
        ["hermes", "send", "--to", target, "--quiet", text],
        check=True,
        timeout=30,
    )


def build_from_environment() -> HermesMentorReportCoordinator:
    return HermesMentorReportCoordinator(
        store=GraderStore(Path(_required("GRADER_DATABASE_PATH"))),
        target=_required("MENTI_HERMES_TARGET"),
        sender=hermes_send,
    )


def run(coordinator: HermesMentorReportCoordinator, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            outcome = coordinator.process_once()
        except (OSError, RuntimeError, subprocess.SubprocessError):
            stop.wait(2)
            continue
        if outcome == "idle":
            stop.wait(1)


def main() -> int:
    try:
        coordinator = build_from_environment()
    except (OSError, RuntimeError, ValueError):
        print("Hermes mentor report worker configuration is invalid")
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