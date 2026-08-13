import json

from grader.store import GraderStore


def _feedback_input() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "task_id": "PY-002",
            "commit_sha": "b" * 40,
            "assignment": {"title": "Задача", "description": "Условие"},
            "mentor_proposal": None,
            "grade": {"passed": 1, "total": 2},
            "source_files": [
                {"path": "main.py", "content": "def solve(): return 'exact snapshot'\n"}
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _complete(store: GraderStore):
    attempt = store.enqueue_grading(
        task_id="PY-002",
        project="json",
        branch_name="task/PY-002-next-id",
        commit_sha="b" * 40,
        suite_hash="c" * 64,
    )
    claimed = store.claim_next_grading()
    assert claimed is not None
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
                    {"id": "criterion-1", "description": "Критерий задания 1."}
                ],
            }
        ),
        feedback_input_json=_feedback_input(),
    )
    return attempt


def test_grade_completion_atomically_queues_independent_exact_snapshot_feedback(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    attempt = _complete(store)

    completed = store.get_grading(attempt.attempt_id)
    publication = store.claim_next_check_publication()
    grade_notification = store.claim_next_mentor_notification()
    feedback = store.claim_next_code_feedback()

    assert completed.state == "completed"
    assert publication is not None and publication.commit_sha == "b" * 40
    assert grade_notification is not None and grade_notification.commit_sha == "b" * 40
    assert feedback is not None
    assert feedback.feedback_id == attempt.attempt_id
    assert feedback.task_id == "PY-002"
    assert feedback.commit_sha == "b" * 40
    assert "exact snapshot" in feedback.input_json


def test_feedback_retry_and_completion_do_not_change_completed_grade(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    attempt = _complete(store)
    feedback = store.claim_next_code_feedback()
    assert feedback is not None

    assert store.release_code_feedback(
        feedback.feedback_id,
        feedback.lease_token,
        "feedback-provider-failure",
        delay_seconds=0,
    )
    retry = store.claim_next_code_feedback()
    assert retry is not None and retry.attempts == 2
    output = json.dumps(
        {
            "schema_version": 1,
            "summary": "Итог.",
            "strengths": ["Сильная сторона."],
            "weaknesses": ["Слабое место."],
            "recommendations": ["Рекомендация."],
        },
        ensure_ascii=False,
    )
    assert store.complete_code_feedback(retry.feedback_id, retry.lease_token, output)

    completed = store.get_grading(attempt.attempt_id)
    notification = store.claim_next_feedback_notification()
    assert completed.state == "completed"
    assert completed.passed is False
    assert notification is not None
    assert notification.commit_sha == "b" * 40
    assert json.loads(notification.feedback_json)["summary"] == "Итог."


def test_oversized_advisory_feedback_is_skipped_without_blocking_grade(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    attempt = store.enqueue_grading(
        task_id="PY-002",
        project="json",
        branch_name="task/PY-002-next-id",
        commit_sha="b" * 40,
        suite_hash="c" * 64,
    )
    claimed = store.claim_next_grading()
    assert claimed is not None

    assert store.complete_grading(
        attempt.attempt_id,
        claimed.lease_token,
        passed=True,
        passed_count=1,
        total_count=1,
        private_report_json='{"failed_rubrics":[],"passed":1,"total":1}',
        feedback_input_json='{"oversized":"' + ("x" * 500_001) + '"}',
    )
    completed = store.get_grading(attempt.attempt_id)
    assert completed.state == "completed"
    assert completed.passed is True
    assert store.claim_next_check_publication() is not None
    assert store.claim_next_mentor_notification() is not None
    assert store.claim_next_code_feedback() is None


def test_feedback_completion_rejects_stale_lease(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    _complete(store)
    feedback = store.claim_next_code_feedback()
    assert feedback is not None

    assert not store.complete_code_feedback(
        feedback.feedback_id,
        "d" * 64,
        '{"schema_version":1,"summary":"x","strengths":["s"],"weaknesses":["w"],"recommendations":["r"]}',
    )
