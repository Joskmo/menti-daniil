import pytest

from grader.check_publication import CheckPublicationCoordinator
from grader.store import GraderStore


class FakePublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    def publish_result(self, *, attempt_id: str, commit_sha: str, passed: bool) -> None:
        self.calls.append(
            {"attempt_id": attempt_id, "commit_sha": commit_sha, "passed": passed}
        )
        if self.fail:
            raise RuntimeError("provider unavailable")


def _completed_attempt(store: GraderStore):
    store.enqueue_authoring(
        task_id="PY-002",
        row_id="row-2",
        project="json",
        branch_name="task/PY-002-next-id",
        starter_sha="a" * 40,
        assignment_json='{"description":"d","title":"t"}',
    )
    job = store.claim_next_authoring()
    assert job is not None and job.lease_token is not None
    review = store.submit_mentor_review(
        job.task_id,
        job.lease_token,
        suite_json='{"suite":"approved"}',
        proposal_json='{"proposal":"approved"}',
        critic_verdict_json='{"status":"approved"}',
    )
    assert store.approve_mentor_review(
        review.task_id, review.version, review.draft_hash, "test:mentor"
    )
    accepting = store.claim_next_approved_authoring()
    assert accepting is not None and accepting.lease_token is not None
    store.mark_authoring_ready(
        accepting.task_id,
        accepting.lease_token,
        review.draft_hash,
    )
    attempt = store.enqueue_grading(
        task_id="PY-002",
        project="json",
        branch_name="task/PY-002-next-id",
        commit_sha="b" * 40,
        suite_hash=review.draft_hash,
    )
    claimed = store.claim_next_grading()
    assert claimed is not None and claimed.lease_token is not None
    assert store.complete_grading(
        attempt.attempt_id,
        claimed.lease_token,
        passed=False,
        passed_count=1,
        total_count=2,
        private_report_json='{"failed_rubrics":[],"passed":1,"total":2}',
    )
    return attempt


def test_check_publication_worker_publishes_only_coarse_result(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    attempt = _completed_attempt(store)
    publisher = FakePublisher()

    coordinator = CheckPublicationCoordinator(store=store, publisher=publisher)

    assert coordinator.process_once() == "published"
    assert publisher.calls == [
        {
            "attempt_id": attempt.attempt_id,
            "commit_sha": "b" * 40,
            "passed": False,
        }
    ]
    assert store.claim_next_check_publication() is None


def test_check_publication_failure_is_backed_off_without_blocking_later_work(tmp_path) -> None:
    now = [100.0]
    store = GraderStore(tmp_path / "grader.db", clock=lambda: now[0])
    _completed_attempt(store)

    with pytest.raises(RuntimeError, match="failed closed"):
        CheckPublicationCoordinator(store=store, publisher=FakePublisher(fail=True)).process_once()

    assert store.claim_next_check_publication() is None
    now[0] = 131.0
    assert store.claim_next_check_publication() is not None