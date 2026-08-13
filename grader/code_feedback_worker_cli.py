import json
import os
import signal
import threading
from pathlib import Path
from typing import Protocol

from grader.code_feedback import (
    CodeFeedback,
    CodeFeedbackContractError,
    HermesCodeFeedbackAuthor,
)
from grader.llm_broker import UnixLlmBrokerClient
from grader.store import GraderStore


class FeedbackAuthor(Protocol):
    def create(self, input_json: str) -> CodeFeedback: ...


class CodeFeedbackCoordinator:
    def __init__(self, *, store: GraderStore, author: FeedbackAuthor) -> None:
        self.store = store
        self.author = author

    def process_once(self) -> str:
        job = self.store.claim_next_code_feedback()
        if job is None:
            return "idle"
        try:
            feedback = self.author.create(job.input_json)
            payload = json.dumps(
                feedback.to_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if not self.store.complete_code_feedback(
                job.feedback_id,
                job.lease_token,
                payload,
            ):
                raise RuntimeError("feedback lease was lost before completion")
        except (CodeFeedbackContractError, RuntimeError, OSError, ValueError) as error:
            if not self.store.release_code_feedback(
                job.feedback_id,
                job.lease_token,
                "feedback-generation-failure",
            ):
                raise RuntimeError(
                    "feedback lease was lost while handling failure"
                ) from error
            raise
        return "completed"


def run(coordinator: CodeFeedbackCoordinator, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            result = coordinator.process_once()
        except Exception:  # noqa: BLE001 - durable queue controls independent retries
            stop.wait(2)
            continue
        if result == "idle":
            stop.wait(1)


def build_from_environment() -> CodeFeedbackCoordinator:
    broker = UnixLlmBrokerClient(
        Path(_required("GRADER_LLM_BROKER_SOCKET")),
        timeout_seconds=240,
    )
    return CodeFeedbackCoordinator(
        store=GraderStore(Path(_required("GRADER_DATABASE_PATH"))),
        author=HermesCodeFeedbackAuthor(broker=broker),
    )


def main() -> int:
    try:
        coordinator = build_from_environment()
    except (OSError, RuntimeError, ValueError):
        print("Code feedback worker configuration is invalid")
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
