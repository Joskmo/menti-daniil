import json
import stat

import pytest

from grader.store import GraderStore, StoredSuite, SuiteVault, VaultConflictError


def _assignment() -> str:
    return json.dumps(
        {
            "title": "Следующий ID",
            "description": "Вернуть максимальный ID плюс один.",
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def test_authoring_job_is_idempotent_and_input_is_immutable(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)

    first = store.enqueue_authoring(
        task_id="PY-002",
        row_id="row-2",
        project="json",
        branch_name="task/PY-002-next-id",
        starter_sha="a" * 40,
        assignment_json=_assignment(),
    )
    duplicate = store.enqueue_authoring(
        task_id="PY-002",
        row_id="row-2",
        project="json",
        branch_name="task/PY-002-next-id",
        starter_sha="a" * 40,
        assignment_json=_assignment(),
    )

    assert duplicate == first
    with pytest.raises(ValueError, match="changed"):
        store.enqueue_authoring(
            task_id="PY-002",
            row_id="row-2",
            project="json",
            branch_name="task/PY-002-next-id",
            starter_sha="b" * 40,
            assignment_json=_assignment(),
        )


def test_authoring_lease_is_fenced_across_reclaim(tmp_path) -> None:
    now = [100.0]
    store = GraderStore(
        tmp_path / "grader.db",
        clock=lambda: now[0],
        lease_seconds=10,
    )
    store.enqueue_authoring(
        task_id="PY-002",
        row_id="row-2",
        project="json",
        branch_name="task/PY-002-next-id",
        starter_sha="a" * 40,
        assignment_json=_assignment(),
    )

    first = store.claim_next_authoring()
    assert first is not None
    assert store.claim_next_authoring() is None
    now[0] = 111.0
    second = store.claim_next_authoring()

    assert second is not None
    assert second.lease_token != first.lease_token
    assert not store.release_authoring(first.task_id, first.lease_token)
    store.mark_authoring_ready(second.task_id, second.lease_token, "f" * 64)
    assert store.get_authoring("PY-002").state == "ready"


def test_authoring_finalize_checks_fence_before_promoting_vault_output(tmp_path) -> None:
    now = [100.0]
    store = GraderStore(tmp_path / "grader.db", clock=lambda: now[0], lease_seconds=10)
    store.enqueue_authoring(
        task_id="PY-002",
        row_id="row-2",
        project="json",
        branch_name="task/PY-002-next-id",
        starter_sha="a" * 40,
        assignment_json=_assignment(),
    )
    stale = store.claim_next_authoring()
    assert stale is not None and stale.lease_token is not None
    now[0] = 111.0
    current = store.claim_next_authoring()
    assert current is not None and current.lease_token is not None
    promoted: list[str] = []

    def promote() -> StoredSuite:
        promoted.append("called")
        return StoredSuite("PY-002", "a" * 40, "f" * 64, "model", {})

    with pytest.raises(RuntimeError, match="lease is not owned"):
        store.finalize_authoring("PY-002", stale.lease_token, promote)
    assert promoted == []

    stored = store.finalize_authoring("PY-002", current.lease_token, promote)
    assert stored.suite_hash == "f" * 64
    assert promoted == ["called"]
    assert store.get_authoring("PY-002").suite_hash == "f" * 64


def test_clarification_answer_requeues_exact_revision_once(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    store.enqueue_authoring(
        task_id="PY-002",
        row_id="row-2",
        project="json",
        branch_name="task/PY-002-next-id",
        starter_sha="a" * 40,
        assignment_json=_assignment(),
    )
    job = store.claim_next_authoring()
    assert job is not None
    clarification = store.request_clarification(
        job.task_id,
        job.lease_token,
        "Что делать с пустым списком?",
    )

    assert store.answer_clarification(
        clarification.nonce,
        clarification.revision,
        "Вернуть 1.",
    )
    assert store.answer_clarification(
        clarification.nonce,
        clarification.revision,
        "Вернуть 1.",
    )
    assert not store.answer_clarification(
        clarification.nonce,
        clarification.revision,
        "Другое значение.",
    )
    queued = store.get_authoring("PY-002")
    assert queued.state == "queued"
    assert queued.clarification_answer == "Вернуть 1."
    assert store.next_pending_clarification() is None


def test_pending_clarification_can_be_discovered_by_separate_bot_process(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    store.enqueue_authoring(
        task_id="PY-002",
        row_id="row-2",
        project="json",
        branch_name="task/PY-002-next-id",
        starter_sha="a" * 40,
        assignment_json=_assignment(),
    )
    job = store.claim_next_authoring()
    clarification = store.request_clarification(
        job.task_id,
        job.lease_token,
        "Что делать с пустым списком?",
    )

    assert store.next_pending_clarification() == clarification


def test_critic_rejection_requeues_feedback_and_attempt_limit_can_fail_closed(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    store.enqueue_authoring(
        task_id="PY-002",
        row_id="row-2",
        project="json",
        branch_name="task/PY-002-next-id",
        starter_sha="a" * 40,
        assignment_json=_assignment(),
    )
    first = store.claim_next_authoring()
    assert first is not None
    feedback = json.dumps(
        {
            "status": "rejected",
            "issues": [{"code": "missing-empty-case", "message": "Нет пустого списка."}],
        },
        ensure_ascii=False,
    )

    store.requeue_after_critic(first.task_id, first.lease_token, feedback)
    second = store.claim_next_authoring()

    assert second is not None
    assert json.loads(second.critic_feedback_json or "") == json.loads(feedback)
    assert not store.mark_authoring_failed(first.task_id, first.lease_token, "critic-rejected")
    assert store.mark_authoring_failed(second.task_id, second.lease_token, "critic-rejected")
    failed = store.get_authoring("PY-002")
    assert failed.state == "failed"
    assert failed.last_error_code == "critic-rejected"


def test_grading_attempt_is_idempotent_and_fenced_by_exact_commit_and_suite(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0, lease_seconds=10)
    first = store.enqueue_grading(
        task_id="PY-002",
        project="json",
        branch_name="task/PY-002-next-id",
        commit_sha="b" * 40,
        suite_hash="c" * 64,
    )
    repeated = store.enqueue_grading(
        task_id="PY-002",
        project="json",
        branch_name="task/PY-002-next-id",
        commit_sha="b" * 40,
        suite_hash="c" * 64,
    )

    assert first.attempt_id == repeated.attempt_id
    claimed = store.claim_next_grading()
    assert claimed is not None
    assert claimed.commit_sha == "b" * 40
    assert claimed.suite_hash == "c" * 64
    report = json.dumps(
        {
            "passed": 3,
            "total": 4,
            "failed_rubrics": [
                {"id": "empty-input", "description": "Обработка пустого ввода."},
            ],
        },
        ensure_ascii=False,
    )

    assert not store.complete_grading(
        claimed.attempt_id,
        "stale-token",
        passed=False,
        passed_count=3,
        total_count=4,
        private_report_json=report,
    )
    assert store.complete_grading(
        claimed.attempt_id,
        claimed.lease_token,
        passed=False,
        passed_count=3,
        total_count=4,
        private_report_json=report,
    )
    completed = store.get_grading(claimed.attempt_id)
    assert completed.state == "completed"
    assert completed.passed is False
    assert completed.passed_count == 3
    assert completed.total_count == 4
    assert json.loads(completed.private_report_json or "") == json.loads(report)
    notification = store.claim_next_mentor_notification()
    assert notification is not None
    assert notification.notification_id == claimed.attempt_id
    assert notification.task_id == "PY-002"
    assert notification.commit_sha == "b" * 40
    assert json.loads(notification.report_json) == json.loads(report)
    assert not store.mark_mentor_notification_sent(
        notification.notification_id,
        "stale-token",
    )
    assert store.mark_mentor_notification_sent(
        notification.notification_id,
        notification.lease_token,
    )
    assert store.claim_next_mentor_notification() is None


def test_grading_retry_backoff_does_not_starve_later_commit_and_eventually_dead_letters(
    tmp_path,
) -> None:
    now = [100.0]
    store = GraderStore(
        tmp_path / "grader.db",
        clock=lambda: now[0],
        max_grading_attempts=2,
        grading_retry_base_seconds=5,
    )
    oldest = store.enqueue_grading(
        task_id="PY-002",
        project="json",
        branch_name="task/PY-002-next-id",
        commit_sha="b" * 40,
        suite_hash="c" * 64,
    )
    later = store.enqueue_grading(
        task_id="PY-002",
        project="json",
        branch_name="task/PY-002-next-id",
        commit_sha="d" * 40,
        suite_hash="c" * 64,
    )

    first = store.claim_next_grading()
    assert first is not None and first.attempt_id == oldest.attempt_id
    assert first.lease_token is not None
    assert store.release_grading(first.attempt_id, first.lease_token, "source-unavailable")

    second = store.claim_next_grading()
    assert second is not None and second.attempt_id == later.attempt_id
    assert second.lease_token is not None
    assert store.release_grading(second.attempt_id, second.lease_token, "source-unavailable")
    assert store.claim_next_grading() is None

    now[0] = 105.0
    retry = store.claim_next_grading()
    assert retry is not None and retry.attempt_id == oldest.attempt_id
    assert retry.lease_token is not None
    assert store.release_grading(retry.attempt_id, retry.lease_token, "source-unavailable")
    assert store.get_grading(oldest.attempt_id).state == "failed"


def test_grading_schema_migrates_next_attempt_at_additively(tmp_path) -> None:
    import sqlite3

    database = tmp_path / "grader.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE grading_attempts (
                attempt_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                project TEXT NOT NULL,
                branch_name TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                suite_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                lease_expires_at REAL,
                lease_token TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                passed INTEGER,
                passed_count INTEGER,
                total_count INTEGER,
                private_report_json TEXT,
                last_error_code TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(task_id, commit_sha)
            )
            """
        )

    GraderStore(database)

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(grading_attempts)")}
    assert "next_attempt_at" in columns


def test_suite_vault_read_only_consumer_never_mutates_filesystem(tmp_path, monkeypatch) -> None:
    root = tmp_path / "vault"
    writable = SuiteVault(root)
    stored = writable.freeze(
        task_id="PY-002",
        starter_sha="a" * 40,
        suite_payload={"schema_version": 1, "status": "ready"},
        author_model="test-model",
    )

    def reject_chmod(path, mode):
        raise AssertionError("read-only vault attempted chmod")

    monkeypatch.setattr("grader.store.os.chmod", reject_chmod)
    read_only = SuiteVault(root, read_only=True)

    assert read_only.load("PY-002") == stored
    with pytest.raises(PermissionError, match="read-only"):
        read_only.freeze(
            task_id="PY-002",
            starter_sha="a" * 40,
            suite_payload={"schema_version": 1, "status": "ready"},
            author_model="test-model",
        )


def test_suite_vault_freezes_hash_and_rejects_material_rewrite(tmp_path) -> None:
    root = tmp_path / "vault"
    vault = SuiteVault(root)
    suite = {"schema_version": 1, "status": "ready", "rubric": [], "cases": []}

    stored = vault.freeze(
        task_id="PY-002",
        starter_sha="a" * 40,
        suite_payload=suite,
        author_model="gpt-5.6-sol",
    )
    duplicate = vault.freeze(
        task_id="PY-002",
        starter_sha="a" * 40,
        suite_payload=suite,
        author_model="gpt-5.6-sol",
    )

    assert duplicate == stored
    assert vault.load("PY-002").suite_payload == suite
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "PY-002" / "manifest.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "PY-002" / "suite.json").stat().st_mode) == 0o600
    with pytest.raises(VaultConflictError):
        vault.freeze(
            task_id="PY-002",
            starter_sha="a" * 40,
            suite_payload={**suite, "summary": "changed"},
            author_model="gpt-5.6-sol",
        )


def test_suite_vault_crash_during_promotion_leaves_no_partial_final_suite(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "vault"
    vault = SuiteVault(root)
    original = vault._atomic_write
    calls = [0]

    def fail_second_write(directory, name, payload):
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("simulated crash window")
        return original(directory, name, payload)

    monkeypatch.setattr(vault, "_atomic_write", fail_second_write)
    with pytest.raises(OSError, match="simulated"):
        vault.freeze(
            task_id="PY-002",
            starter_sha="a" * 40,
            suite_payload={"schema_version": 1, "status": "ready"},
            author_model="gpt-5.6-sol",
        )

    assert not (root / "PY-002").exists()
    monkeypatch.setattr(vault, "_atomic_write", original)
    stored = vault.freeze(
        task_id="PY-002",
        starter_sha="a" * 40,
        suite_payload={"schema_version": 1, "status": "ready"},
        author_model="gpt-5.6-sol",
    )
    assert stored.task_id == "PY-002"


def test_suite_vault_refuses_symlinked_task_directory(tmp_path) -> None:
    root = tmp_path / "vault"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / "PY-002").symlink_to(outside, target_is_directory=True)

    with pytest.raises(VaultConflictError, match="safe directory"):
        SuiteVault(root).freeze(
            task_id="PY-002",
            starter_sha="a" * 40,
            suite_payload={"schema_version": 1},
            author_model="gpt-5.6-sol",
        )
