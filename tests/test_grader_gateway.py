import json

import pytest

from bridge.models import Task
from grader.gateway import SQLiteGraderGateway, SuiteNotReadyError
from grader.store import GraderStore


def test_gateway_enqueues_public_assignment_snapshot_idempotently(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    gateway = SQLiteGraderGateway(store)
    task = Task(
        row_id="row-2",
        title="Следующий ID",
        project="json",
        status_id="in-progress",
        assignee_ids=("daniil",),
        task_id="PY-002",
        branch_url=None,
        pr_url=None,
        created_at="2026-08-10T12:00:00Z",
        description="Вернуть максимальный ID плюс один.",
    )

    assert gateway.ensure_suite(task, "task/PY-002-next-id", "a" * 40) == "queued"
    assert gateway.ensure_suite(task, "task/PY-002-next-id", "a" * 40) == "queued"

    job = store.get_authoring("PY-002")
    assert json.loads(job.assignment_json) == {
        "description": "Вернуть максимальный ID плюс один.",
        "title": "Следующий ID",
    }


def test_gateway_missing_authoring_identity_is_retryable(tmp_path) -> None:
    gateway = SQLiteGraderGateway(GraderStore(tmp_path / "grader.db"))

    with pytest.raises(SuiteNotReadyError, match="identity"):
        gateway.enqueue_commit(
            "PY-002",
            "json",
            "task/PY-002-next-id",
            "b" * 40,
        )


def test_gateway_enqueues_exact_commit_only_after_suite_is_ready(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    gateway = SQLiteGraderGateway(store)
    task = Task(
        row_id="row-2",
        title="Следующий ID",
        project="json",
        status_id="in-progress",
        assignee_ids=("daniil",),
        task_id="PY-002",
        branch_url=None,
        pr_url=None,
        created_at="2026-08-10T12:00:00Z",
        description="Вернуть максимальный ID плюс один.",
    )
    branch = "task/PY-002-next-id"
    gateway.ensure_suite(task, branch, "a" * 40)

    with pytest.raises(SuiteNotReadyError):
        gateway.enqueue_commit("PY-002", "json", branch, "b" * 40)

    claimed = store.claim_next_authoring()
    assert claimed is not None and claimed.lease_token is not None
    store.mark_authoring_ready("PY-002", claimed.lease_token, "c" * 64)

    attempt = gateway.enqueue_commit("PY-002", "json", branch, "b" * 40)
    assert attempt is not None
    assert attempt.commit_sha == "b" * 40
    assert attempt.suite_hash == "c" * 64
    assert gateway.enqueue_commit("PY-002", "json", branch, "b" * 40) == attempt
