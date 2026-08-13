import json

from grader.store import GraderStore


def _enqueue(store: GraderStore) -> None:
    store.enqueue_authoring(
        task_id="PY-002",
        row_id="row-2",
        project="json",
        branch_name="task/PY-002-next-id",
        starter_sha="a" * 40,
        assignment_json=json.dumps(
            {"title": "Следующий ID", "description": "Вернуть max(id) + 1."}
        ),
    )


def _draft() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "status": "ready",
            "summary": "Проверяет следующий ID.",
            "clarification": None,
            "rubric": [
                {"id": "next-id", "description": "Корректный ID.", "weight": 1}
            ],
            "cases": [
                {
                    "id": "unsorted",
                    "rubric_id": "next-id",
                    "adapter": "python_call",
                    "target": "main:next_id",
                    "input": {"args": [[{"id": 8}]], "kwargs": {}, "files": []},
                    "expect": {"return": 9, "exception": None, "stdout": None, "files": []},
                }
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _proposal() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "interpretation": "Вернуть следующий идентификатор.",
            "criteria": ["Корректный результат."],
            "decisions": [],
            "test_plan": ["Основной сценарий."],
            "reference_approach": "Найти максимум.",
            "reference_solution": "def next_id(rows): return max(row['id'] for row in rows) + 1",
            "critic_summary": "Одобрено.",
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def test_draft_waits_for_exact_versioned_mentor_approval_and_stale_actions_fail(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    _enqueue(store)
    claimed = store.claim_next_authoring()

    review = store.submit_mentor_review(
        claimed.task_id,
        claimed.lease_token,
        suite_json=_draft(),
        proposal_json=_proposal(),
        critic_verdict_json=json.dumps(
            {
                "schema_version": 1,
                "status": "approved",
                "summary": "OK",
                "clarification": None,
                "issues": [],
            }
        ),
    )

    assert review.task_id == "PY-002"
    assert review.version == 1
    assert len(review.draft_hash) == 64
    assert store.get_authoring("PY-002").state == "awaiting_mentor_approval"
    assert store.claim_next_authoring() is None
    assert store.next_pending_mentor_review() == review

    assert store.request_mentor_revision(
        review.task_id,
        review.version,
        review.draft_hash,
        "Для пустого списка вернуть 1.",
    )
    revised = store.get_authoring("PY-002")
    assert revised.state == "queued"
    assert revised.mentor_revision == "Для пустого списка вернуть 1."
    assert not store.approve_mentor_review(
        review.task_id,
        review.version,
        review.draft_hash,
    )

    claimed_again = store.claim_next_authoring()
    review_v2 = store.submit_mentor_review(
        claimed_again.task_id,
        claimed_again.lease_token,
        suite_json=_draft(),
        proposal_json=_proposal(),
        critic_verdict_json=json.dumps(
            {
                "schema_version": 1,
                "status": "approved",
                "summary": "OK",
                "clarification": None,
                "issues": [],
            }
        ),
    )
    assert review_v2.version == 2
    assert store.approve_mentor_review(
        review_v2.task_id,
        review_v2.version,
        review_v2.draft_hash,
    )
    assert store.get_authoring("PY-002").state == "queued_for_acceptance"
    assert store.claim_next_approved_authoring().mentor_review_version == 2


def test_mentor_can_pause_exact_current_proposal_without_losing_it(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    _enqueue(store)
    claimed = store.claim_next_authoring()
    review = store.submit_mentor_review(
        claimed.task_id,
        claimed.lease_token,
        suite_json=_draft(),
        proposal_json=_proposal(),
        critic_verdict_json='{"schema_version":1,"status":"approved","summary":"OK","clarification":null,"issues":[]}',
    )

    assert store.pause_mentor_review(review.task_id, review.version, review.draft_hash)
    assert store.get_authoring(review.task_id).state == "mentor_paused"
    assert store.next_pending_mentor_review() is None
    assert not store.approve_mentor_review(review.task_id, review.version, review.draft_hash)
    assert store.resume_mentor_review(review.task_id)
    assert store.next_pending_mentor_review() == review
