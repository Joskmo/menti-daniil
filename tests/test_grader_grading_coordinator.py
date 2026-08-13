import json
from pathlib import Path

import pytest

from grader.contracts import SuiteDraft
from grader.executor import CaseResult, SuiteInfrastructureError, SuiteResult
from grader.grading_coordinator import GradingCoordinator
from grader.store import GraderStore, SuiteVault


def _suite() -> SuiteDraft:
    return SuiteDraft.from_cli_output(
        json.dumps(
            {
                "schema_version": 1,
                "status": "ready",
                "summary": "Закрытая проверка.",
                "clarification": None,
                "rubric": [
                    {"id": "normal", "description": "Обычный случай.", "weight": 1},
                    {"id": "boundary", "description": "Граничный случай.", "weight": 1},
                ],
                "cases": [
                    {
                        "id": "private-normal-vector",
                        "rubric_id": "normal",
                        "adapter": "python_call",
                        "target": "main:double",
                        "input": {"args": [4], "kwargs": {}, "files": []},
                        "expect": {
                            "return": 8,
                            "exception": None,
                            "stdout": None,
                            "files": [],
                        },
                    },
                    {
                        "id": "private-boundary-vector",
                        "rubric_id": "boundary",
                        "adapter": "python_call",
                        "target": "main:double",
                        "input": {"args": [0], "kwargs": {}, "files": []},
                        "expect": {
                            "return": 0,
                            "exception": None,
                            "stdout": None,
                            "files": [],
                        },
                    },
                ],
            }
        )
    )


class FakeExporter:
    def export(
        self,
        project: str,
        branch_name: str,
        starter_sha: str,
        commit_sha: str,
        destination: Path,
    ) -> Path:
        assert project == "json"
        assert branch_name == "task/PY-002-next-id"
        assert starter_sha == "a" * 40
        assert commit_sha == "b" * 40
        destination.mkdir()
        (destination / "main.py").write_text("def double(value): return value\n")
        return destination


class FakeExecutor:
    def evaluate(self, suite: SuiteDraft, project_directory: Path) -> SuiteResult:
        assert (project_directory / "main.py").exists()
        return SuiteResult(
            (
                CaseResult("private-normal-vector", "normal", False, ("return differs",)),
                CaseResult("private-boundary-vector", "boundary", True, ()),
            )
        )


def test_grading_coordinator_queues_coarse_result_and_stores_rubric_only_report(
    tmp_path,
) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    vault = SuiteVault(tmp_path / "vault")
    stored = vault.freeze(
        task_id="PY-002",
        starter_sha="a" * 40,
        suite_payload=_suite().to_payload(),
        author_model="gpt-5.6-sol",
    )
    attempt = store.enqueue_grading(
        task_id="PY-002",
        project="json",
        branch_name="task/PY-002-next-id",
        commit_sha="b" * 40,
        suite_hash=stored.suite_hash,
    )
    coordinator = GradingCoordinator(
        store=store,
        vault=vault,
        exporter=FakeExporter(),
        executor=FakeExecutor(),
    )

    assert coordinator.process_once() == "completed"

    completed = store.get_grading(attempt.attempt_id)
    report = json.loads(completed.private_report_json or "")
    assert report == {
        "passed": 1,
        "total": 2,
        "failed_rubrics": [
            {"id": "criterion-1", "description": "Критерий задания 1."},
        ],
    }
    publication = store.claim_next_check_publication()
    assert publication is not None
    assert publication.publication_id == attempt.attempt_id
    assert publication.commit_sha == "b" * 40
    assert publication.passed is False


def test_private_report_never_copies_llm_rubric_text_or_hidden_values(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    vault = SuiteVault(tmp_path / "vault")
    payload = _suite().to_payload()
    payload["rubric"][0]["id"] = "secret-input-999"
    payload["rubric"][0]["description"] = "Для входа 999 ожидается секрет 12345."
    payload["cases"][0]["rubric_id"] = "secret-input-999"
    suite = SuiteDraft.from_cli_output(json.dumps(payload, ensure_ascii=False))
    stored = vault.freeze(
        task_id="PY-002",
        starter_sha="a" * 40,
        suite_payload=suite.to_payload(),
        author_model="gpt-5.6-sol",
    )
    attempt = store.enqueue_grading(
        task_id="PY-002",
        project="json",
        branch_name="task/PY-002-next-id",
        commit_sha="b" * 40,
        suite_hash=stored.suite_hash,
    )

    class HiddenTextExecutor:
        def evaluate(self, suite: SuiteDraft, project_directory: Path) -> SuiteResult:
            return SuiteResult(
                tuple(
                    CaseResult(
                        case.case_id,
                        case.rubric_id,
                        case.case_id == "private-boundary-vector",
                        ()
                        if case.case_id == "private-boundary-vector"
                        else ("return differs",),
                    )
                    for case in suite.cases
                )
            )

    assert GradingCoordinator(
        store=store,
        vault=vault,
        exporter=FakeExporter(),
        executor=HiddenTextExecutor(),
    ).process_once() == "completed"

    report = store.get_grading(attempt.attempt_id).private_report_json or ""
    assert "999" not in report
    assert "12345" not in report
    assert "secret" not in report
    assert json.loads(report)["failed_rubrics"] == [
        {"id": "criterion-1", "description": "Критерий задания 1."}
    ]


class InfrastructureFailureExecutor:
    def evaluate(self, suite: SuiteDraft, project_directory: Path) -> SuiteResult:
        raise SuiteInfrastructureError("launcher unavailable")


def test_grading_infrastructure_failure_is_retryable_without_result_outboxes(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    vault = SuiteVault(tmp_path / "vault")
    stored = vault.freeze(
        task_id="PY-002",
        starter_sha="a" * 40,
        suite_payload=_suite().to_payload(),
        author_model="gpt-5.6-sol",
    )
    attempt = store.enqueue_grading(
        task_id="PY-002",
        project="json",
        branch_name="task/PY-002-next-id",
        commit_sha="b" * 40,
        suite_hash=stored.suite_hash,
    )
    coordinator = GradingCoordinator(
        store=store,
        vault=vault,
        exporter=FakeExporter(),
        executor=InfrastructureFailureExecutor(),
    )

    with pytest.raises(RuntimeError, match="failed closed"):
        coordinator.process_once()

    released = store.get_grading(attempt.attempt_id)
    assert released.state == "queued"
    assert released.private_report_json is None
    assert store.claim_next_check_publication() is None
    assert store.claim_next_mentor_notification() is None
