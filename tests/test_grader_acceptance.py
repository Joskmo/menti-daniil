import json

from grader.acceptance import StarterFailureGate
from grader.author import AssignmentContext, SourceFile
from grader.contracts import SuiteDraft
from grader.executor import CaseResult, SuiteResult


def _context() -> AssignmentContext:
    return AssignmentContext(
        task_id="PY-002",
        project="json",
        title="Следующий ID",
        description="Вернуть максимальный ID плюс один.",
        source_files=(SourceFile("main.py", "def next_id(rows): return len(rows) + 1\n"),),
    )


def _suite() -> SuiteDraft:
    return SuiteDraft.from_cli_output(
        json.dumps(
            {
                "schema_version": 1,
                "status": "ready",
                "summary": "Проверяет ID.",
                "clarification": None,
                "rubric": [{"id": "next-id", "description": "ID.", "weight": 1}],
                "cases": [
                    {
                        "id": "unsorted",
                        "rubric_id": "next-id",
                        "adapter": "python_call",
                        "target": "main:next_id",
                        "input": {
                            "args": [[{"id": 8}, {"id": 2}]],
                            "kwargs": {},
                            "files": [],
                        },
                        "expect": {
                            "return": 9,
                            "exception": None,
                            "stdout": None,
                            "files": [],
                        },
                    }
                ],
            }
        )
    )


class FakeExecutor:
    def __init__(self, results: list[SuiteResult]) -> None:
        self.results = results
        self.calls = 0

    def evaluate(self, suite: SuiteDraft, project_directory) -> SuiteResult:
        result = self.results[self.calls]
        self.calls += 1
        assert (project_directory / "main.py").read_text().startswith("def next_id")
        return result


def _result(passed: bool, failures: tuple[str, ...] = ()) -> SuiteResult:
    return SuiteResult((CaseResult("unsorted", "next-id", passed, failures),))


def test_acceptance_requires_starter_failure_and_repeatable_result() -> None:
    failure = _result(False, ("return value differs",))
    executor = FakeExecutor([failure, failure])

    result = StarterFailureGate(executor).validate(_context(), _suite())

    assert result.passed is True
    assert executor.calls == 2


def test_acceptance_rejects_suite_that_does_not_catch_starter_defect() -> None:
    passing = _result(True)

    result = StarterFailureGate(FakeExecutor([passing, passing])).validate(_context(), _suite())

    assert result.passed is False
    assert "starter" in result.reason


def test_acceptance_rejects_flaky_or_incomplete_executor_report() -> None:
    passing = _result(True)
    failure = _result(False, ("return value differs",))
    flaky = StarterFailureGate(FakeExecutor([failure, passing])).validate(_context(), _suite())
    incomplete = StarterFailureGate(FakeExecutor([SuiteResult(()), SuiteResult(())])).validate(
        _context(), _suite()
    )

    assert flaky.passed is False
    assert "not deterministic" in flaky.reason
    assert incomplete.passed is False
    assert "complete" in incomplete.reason


def test_acceptance_rejects_different_raw_observations_with_same_failure() -> None:
    first = SuiteResult(
        (
            CaseResult(
                "unsorted",
                "next-id",
                False,
                ("return value differs",),
                observation_digest="a" * 64,
            ),
        )
    )
    second = SuiteResult(
        (
            CaseResult(
                "unsorted",
                "next-id",
                False,
                ("return value differs",),
                observation_digest="b" * 64,
            ),
        )
    )

    result = StarterFailureGate(FakeExecutor([first, second])).validate(
        _context(), _suite()
    )

    assert result.passed is False
    assert "not deterministic" in result.reason


def test_acceptance_materializes_private_execution_files_not_only_llm_snapshot() -> None:
    context = AssignmentContext(
        task_id="PY-002",
        project="json",
        title="Следующий ID",
        description="Вернуть максимальный ID плюс один.",
        source_files=(SourceFile("main.py", "def next_id(rows): return 1\n"),),
        execution_files=(
            SourceFile("main.py", "def next_id(rows): return 1\n"),
            SourceFile("settings.ini", "mode=strict\n"),
        ),
    )

    class ExecutionSnapshotExecutor:
        def evaluate(self, suite: SuiteDraft, project_directory) -> SuiteResult:
            assert (project_directory / "settings.ini").read_text() == "mode=strict\n"
            return _result(False, ("return value differs",))

    result = StarterFailureGate(ExecutionSnapshotExecutor()).validate(context, _suite())

    assert result.passed is True
