import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from grader.contracts import GraderCase, SuiteDraft


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    rubric_id: str
    passed: bool
    failures: tuple[str, ...]
    observation_digest: str | None = None


class SuiteInfrastructureError(RuntimeError):
    """The executor could not produce a trustworthy student observation."""


@dataclass(frozen=True, slots=True)
class SuiteResult:
    cases: tuple[CaseResult, ...]

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)


class LocalSuiteExecutor:
    """Trusted benchmark executor; production student commits use the Docker backend."""

    def __init__(self, *, timeout_seconds: float = 5) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds
        self.harness = Path(__file__).with_name("python_call_harness.py").resolve()

    def evaluate(self, suite: SuiteDraft, project_directory: str | Path) -> SuiteResult:
        project = Path(project_directory).resolve()
        if not project.is_dir():
            raise ValueError("project_directory must be a directory")
        _reject_symlinks(project)
        results = tuple(self._evaluate_case(case, project) for case in suite.cases)
        return SuiteResult(results)

    def _evaluate_case(self, case: GraderCase, project: Path) -> CaseResult:
        with tempfile.TemporaryDirectory(prefix="menti-case-") as temporary:
            workspace = Path(temporary) / "project"
            shutil.copytree(project, workspace)
            for fixture in case.input_spec["files"]:
                _write_fixture(workspace, fixture["path"], fixture["content"])
            if case.adapter == "python_call":
                failures = self._run_python_call(case, workspace)
            else:
                failures = self._run_cli(case, workspace)
        return CaseResult(case.case_id, case.rubric_id, not failures, tuple(failures))

    def _run_python_call(self, case: GraderCase, workspace: Path) -> list[str]:
        request = {
            "args": case.input_spec["args"],
            "kwargs": case.input_spec["kwargs"],
        }
        completed = self._run(
            [sys.executable, str(self.harness), case.target],
            workspace,
            json.dumps(request, ensure_ascii=False, allow_nan=False),
        )
        if completed is None:
            return ["execution timed out"]
        if completed.returncode != 0:
            return ["student process exited before producing a result"]
        try:
            observed = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return ["student process produced an invalid result envelope"]
        if not isinstance(observed, dict):
            return ["student process produced an invalid result envelope"]
        failures: list[str] = []
        expected_exception = case.expect_spec["exception"]
        actual_exception = observed.get("exception")
        if expected_exception is None:
            if actual_exception is not None:
                failures.append("unexpected exception")
            elif observed.get("return") != case.expect_spec["return"]:
                failures.append("return value differs")
        elif not _exception_matches(actual_exception, expected_exception):
            failures.append("expected exception was not raised")
        if (
            "args_after" in case.expect_spec
            and observed.get("args_after") != case.expect_spec["args_after"]
        ):
            failures.append("arguments differ after call")
        _check_matcher(observed.get("stdout", ""), case.expect_spec["stdout"], "stdout", failures)
        _check_expected_files(workspace, case.expect_spec["files"], failures)
        return failures

    def _run_cli(self, case: GraderCase, workspace: Path) -> list[str]:
        command = [sys.executable, case.target, *case.input_spec["argv"]]
        completed = self._run(command, workspace, case.input_spec["stdin"])
        if completed is None:
            return ["execution timed out"]
        failures: list[str] = []
        if completed.returncode != case.expect_spec["exit_code"]:
            failures.append("exit code differs")
        _check_matcher(completed.stdout, case.expect_spec["stdout"], "stdout", failures)
        _check_matcher(completed.stderr, case.expect_spec["stderr"], "stderr", failures)
        _check_expected_files(workspace, case.expect_spec["files"], failures)
        return failures

    def _run(
        self,
        command: list[str],
        workspace: Path,
        stdin: str,
    ) -> subprocess.CompletedProcess[str] | None:
        environment = {
            "HOME": str(workspace),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "TZ": "UTC",
        }
        try:
            return subprocess.run(
                command,
                cwd=workspace,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            return None


def _reject_symlinks(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("project source must not contain symlinks")


def _write_fixture(workspace: Path, relative_path: str, content: str) -> None:
    destination = workspace / relative_path
    current = workspace
    for part in Path(relative_path).parts[:-1]:
        current /= part
        if current.exists() and current.is_symlink():
            raise ValueError("fixture path traverses a symlink")
    if destination.exists() or destination.is_symlink():
        raise ValueError("fixture path collides with project source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


def _exception_matches(actual: Any, expected: str) -> bool:
    return isinstance(actual, str) and (actual == expected or actual.endswith(f".{expected}"))


def _check_matcher(
    observed: Any,
    matcher: dict[str, Any] | None,
    label: str,
    failures: list[str],
) -> None:
    if matcher is None:
        return
    if not isinstance(observed, str):
        failures.append(f"{label} is not text")
        return
    normalized = observed.replace("\r\n", "\n")
    mode = matcher["mode"]
    expected = matcher["value"]
    if mode == "exact" and normalized != expected.replace("\r\n", "\n"):
        failures.append(f"{label} differs")
    elif mode == "contains" and expected not in normalized:
        failures.append(f"{label} does not contain expected text")
    elif mode == "json":
        try:
            parsed = json.loads(normalized)
        except json.JSONDecodeError:
            failures.append(f"{label} is not valid JSON")
        else:
            if parsed != expected:
                failures.append(f"{label} JSON differs")


def _check_expected_files(
    workspace: Path,
    expected_files: list[dict[str, Any]],
    failures: list[str],
) -> None:
    for expected_file in expected_files:
        path = workspace / expected_file["path"]
        if not path.is_file() or path.is_symlink():
            failures.append(f"expected file missing: {expected_file['path']}")
            continue
        if path.stat().st_size > 1_000_000:
            failures.append(f"expected file too large: {expected_file['path']}")
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append(f"expected file is not UTF-8: {expected_file['path']}")
            continue
        _check_matcher(
            content,
            expected_file["content"],
            f"file {expected_file['path']}",
            failures,
        )
