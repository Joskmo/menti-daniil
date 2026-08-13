import json
import tempfile
from pathlib import Path
from typing import Protocol

from grader.contracts import SuiteDraft
from grader.executor import SuiteResult
from grader.store import GraderStore, GradingAttempt, SuiteVault


class ProjectExporter(Protocol):
    def export(
        self,
        project: str,
        branch_name: str,
        starter_sha: str,
        commit_sha: str,
        destination: str | Path,
    ) -> Path: ...


class SuiteExecutor(Protocol):
    def evaluate(self, suite: SuiteDraft, project_directory: str | Path) -> SuiteResult: ...


class GradingCoordinator:
    def __init__(
        self,
        *,
        store: GraderStore,
        vault: SuiteVault,
        exporter: ProjectExporter,
        executor: SuiteExecutor,
    ) -> None:
        self.store = store
        self.vault = vault
        self.exporter = exporter
        self.executor = executor

    def process_once(self) -> str:
        attempt = self.store.claim_next_grading()
        if attempt is None:
            return "idle"
        if attempt.lease_token is None:
            raise RuntimeError("claimed grading attempt has no fencing token")
        try:
            self._process(attempt)
        except Exception as error:
            released = self.store.release_grading(
                attempt.attempt_id,
                attempt.lease_token,
                "grading-infrastructure-failure",
            )
            if not released:
                raise RuntimeError("grading lease was lost while handling failure") from error
            raise RuntimeError("grading attempt failed closed") from error
        return "completed"

    def _process(self, attempt: GradingAttempt) -> None:
        stored = self.vault.load(attempt.task_id)
        if stored.suite_hash != attempt.suite_hash:
            raise RuntimeError("grading suite does not match the immutable attempt")
        suite = SuiteDraft.from_cli_output(
            json.dumps(stored.suite_payload, ensure_ascii=False, allow_nan=False)
        )
        if suite.status != "ready":
            raise RuntimeError("only ready hidden suites can grade commits")
        with tempfile.TemporaryDirectory(prefix="menti-grade-") as temporary:
            destination = Path(temporary) / "project"
            project_directory = self.exporter.export(
                attempt.project,
                attempt.branch_name,
                stored.starter_sha,
                attempt.commit_sha,
                destination,
            )
            try:
                feedback_sources = _feedback_source_snapshot(project_directory)
            except (OSError, RuntimeError, ValueError):
                feedback_sources = None
            result = self.executor.evaluate(suite, project_directory)
        passed_count, total_count, failed_rubrics = _summarize(suite, result)
        passed = passed_count == total_count
        private_report = json.dumps(
            {
                "passed": passed_count,
                "total": total_count,
                "failed_rubrics": failed_rubrics,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            feedback_input = self._feedback_input(
                attempt,
                passed_count=passed_count,
                total_count=total_count,
                source_files=feedback_sources,
            )
        except Exception:
            feedback_input = None
        completed = self.store.complete_grading(
            attempt.attempt_id,
            attempt.lease_token,
            passed=passed,
            passed_count=passed_count,
            total_count=total_count,
            private_report_json=private_report,
            feedback_input_json=feedback_input,
        )
        if not completed:
            raise RuntimeError("grading lease was lost before completion")

    def _feedback_input(
        self,
        attempt: GradingAttempt,
        *,
        passed_count: int,
        total_count: int,
        source_files: list[dict[str, str]] | None,
    ) -> str | None:
        if source_files is None:
            return None
        try:
            job = self.store.get_authoring(attempt.task_id)
            assignment = json.loads(job.assignment_json)
            mentor_proposal = (
                json.loads(job.mentor_proposal_json)
                if job.mentor_proposal_json is not None
                else None
            )
        except (KeyError, json.JSONDecodeError):
            return None
        if not isinstance(assignment, dict) or mentor_proposal is not None and not isinstance(
            mentor_proposal, dict
        ):
            return None
        payload = {
            "schema_version": 1,
            "task_id": attempt.task_id,
            "commit_sha": attempt.commit_sha,
            "assignment": assignment,
            "mentor_proposal": mentor_proposal,
            "grade": {"passed": passed_count, "total": total_count},
            "source_files": source_files,
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


def _summarize(
    suite: SuiteDraft,
    result: SuiteResult,
) -> tuple[int, int, list[dict[str, str]]]:
    expected = {case.case_id: case.rubric_id for case in suite.cases}
    observed = {case.case_id: case.rubric_id for case in result.cases}
    if len(observed) != len(result.cases) or observed != expected:
        raise RuntimeError("executor returned an incomplete or mismatched case report")
    passed_count = sum(case.passed for case in result.cases)
    failed = {case.rubric_id for case in result.cases if not case.passed}
    failed_rubrics = [
        {
            "id": f"criterion-{position}",
            "description": f"Критерий задания {position}.",
        }
        for position, item in enumerate(suite.rubric, start=1)
        if item.rubric_id in failed
    ]
    return passed_count, len(result.cases), failed_rubrics


def _feedback_source_snapshot(project_directory: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    total = 0
    for path in sorted(project_directory.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(project_directory).as_posix()
        try:
            content = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError):
            continue
        size = len(content.encode("utf-8"))
        if size > 100_000 or total + size > 400_000:
            continue
        records.append({"path": relative, "content": content})
        total += size
        if len(records) >= 100:
            break
    if not records:
        raise RuntimeError("graded project has no bounded feedback-visible source files")
    return records
