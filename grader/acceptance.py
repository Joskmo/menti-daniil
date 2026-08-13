import tempfile
from pathlib import Path
from typing import Protocol

from grader.author import AssignmentContext
from grader.contracts import SuiteDraft
from grader.coordinator import AcceptanceResult
from grader.executor import SuiteResult


class SuiteExecutor(Protocol):
    def evaluate(
        self,
        suite: SuiteDraft,
        project_directory: str | Path,
    ) -> SuiteResult: ...


class StarterFailureGate:
    def __init__(self, executor: SuiteExecutor) -> None:
        self.executor = executor

    def validate(
        self,
        context: AssignmentContext,
        suite: SuiteDraft,
    ) -> AcceptanceResult:
        with tempfile.TemporaryDirectory(prefix="menti-acceptance-") as temporary:
            project = Path(temporary) / "project"
            project.mkdir(mode=0o700)
            assert context.execution_files is not None
            for source_file in context.execution_files:
                destination = project / source_file.path
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination.write_text(source_file.content, encoding="utf-8")
            first = self.executor.evaluate(suite, project)
            second = self.executor.evaluate(suite, project)

        if not self._complete(first, suite) or not self._complete(second, suite):
            return AcceptanceResult(False, "executor did not return a complete suite report")
        if first != second:
            return AcceptanceResult(False, "suite result is not deterministic across repeated runs")
        if first.passed:
            return AcceptanceResult(False, "suite does not detect a defect in the pinned starter")
        infrastructure_failures = {
            "student process exited before producing a result",
            "student process produced an invalid result envelope",
        }
        failed_cases = [case for case in first.cases if not case.passed]
        if failed_cases and all(
            case.failures and set(case.failures) <= infrastructure_failures
            for case in failed_cases
        ):
            return AcceptanceResult(False, "all starter failures are executor protocol failures")
        return AcceptanceResult(True, "pinned starter fails the suite deterministically")

    @staticmethod
    def _complete(result: SuiteResult, suite: SuiteDraft) -> bool:
        expected = [(case.case_id, case.rubric_id) for case in suite.cases]
        observed = [(case.case_id, case.rubric_id) for case in result.cases]
        if observed != expected:
            return False
        return all(bool(case.failures) != case.passed for case in result.cases)
