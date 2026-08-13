import json

from grader.hermes_report_worker_cli import HermesMentorReportCoordinator
from grader.store import GraderStore


def _queue_report(store: GraderStore) -> None:
    attempt = store.enqueue_grading(
        task_id="PY-099",
        project="json",
        branch_name="task/PY-099-example",
        commit_sha="b" * 40,
        suite_hash="c" * 64,
    )
    claimed = store.claim_next_grading()
    assert claimed is not None and claimed.attempt_id == attempt.attempt_id
    assert store.complete_grading(
        claimed.attempt_id,
        claimed.lease_token,
        passed=False,
        passed_count=1,
        total_count=2,
        private_report_json=json.dumps(
            {
                "passed": 1,
                "total": 2,
                "failed_rubrics": [
                    {"id": "criterion-2", "description": "Критерий задания 2."}
                ],
            }
        ),
    )


def test_hermes_report_worker_sends_and_completes_durable_notification(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db")
    _queue_report(store)
    calls = []

    coordinator = HermesMentorReportCoordinator(
        store=store,
        target="telegram:2128573972:619880",
        sender=lambda target, text: calls.append((target, text)),
    )

    assert coordinator.process_once() == "sent"
    assert coordinator.process_once() == "idle"
    assert calls[0][0] == "telegram:2128573972:619880"
    assert "PY-099 — hidden grade" in calls[0][1]
    assert "Пройдено: 1/2" in calls[0][1]
    assert "Критерий задания 2." in calls[0][1]


def test_hermes_report_worker_requeues_failed_delivery(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db")
    _queue_report(store)

    def fail(target: str, text: str) -> None:
        raise RuntimeError("delivery failed")

    coordinator = HermesMentorReportCoordinator(
        store=store,
        target="telegram:2128573972:619880",
        sender=fail,
    )

    try:
        coordinator.process_once()
    except RuntimeError:
        pass
    else:
        raise AssertionError("delivery failure must propagate")

    notification = store.claim_next_mentor_notification()
    assert notification is not None
    assert notification.attempts == 2