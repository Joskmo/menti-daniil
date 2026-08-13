import json

from grader.code_feedback import CodeFeedback
from grader.code_feedback_worker_cli import CodeFeedbackCoordinator
from grader.store import GraderStore


class FakeAuthor:
    def __init__(self) -> None:
        self.inputs = []

    def create(self, input_json: str) -> CodeFeedback:
        self.inputs.append(json.loads(input_json))
        return CodeFeedback(
            summary="Итог.",
            strengths=("Сильная сторона.",),
            weaknesses=("Слабое место.",),
            recommendations=("Рекомендация.",),
        )


def _queue(store: GraderStore) -> str:
    attempt = store.enqueue_grading(
        task_id="PY-002",
        project="json",
        branch_name="task/PY-002-next-id",
        commit_sha="b" * 40,
        suite_hash="c" * 64,
    )
    claimed = store.claim_next_grading()
    assert claimed is not None
    input_json = json.dumps(
        {
            "schema_version": 1,
            "task_id": "PY-002",
            "commit_sha": "b" * 40,
            "assignment": {},
            "mentor_proposal": None,
            "grade": {"passed": 1, "total": 2},
            "source_files": [{"path": "main.py", "content": "exact"}],
        }
    )
    assert store.complete_grading(
        claimed.attempt_id,
        claimed.lease_token,
        passed=False,
        passed_count=1,
        total_count=2,
        private_report_json=(
            '{"failed_rubrics":[{"description":"Критерий 1.","id":"criterion-1"}],'
            '"passed":1,"total":2}'
        ),
        feedback_input_json=input_json,
    )
    return attempt.attempt_id


def test_feedback_worker_processes_exact_persisted_input_and_queues_delivery(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    attempt_id = _queue(store)
    author = FakeAuthor()
    coordinator = CodeFeedbackCoordinator(store=store, author=author)

    assert coordinator.process_once() == "completed"
    assert coordinator.process_once() == "idle"

    assert author.inputs[0]["commit_sha"] == "b" * 40
    assert author.inputs[0]["source_files"][0]["content"] == "exact"
    notification = store.claim_next_feedback_notification()
    assert notification is not None
    assert notification.notification_id == attempt_id
    assert json.loads(notification.feedback_json)["summary"] == "Итог."
