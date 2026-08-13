import json
from pathlib import Path

from grader.contracts import SuiteDraft
from grader.executor import LocalSuiteExecutor


def _suite(case: dict[str, object]) -> SuiteDraft:
    return SuiteDraft.from_cli_output(
        json.dumps(
            {
                "schema_version": 1,
                "status": "ready",
                "summary": "Benchmark suite.",
                "clarification": None,
                "rubric": [
                    {
                        "id": "behavior",
                        "description": "Проверяет требуемое поведение.",
                        "weight": 1,
                    }
                ],
                "cases": [case],
            }
        )
    )


def _write_project(root: Path, main: str) -> Path:
    root.mkdir()
    (root / "main.py").write_text(main)
    return root


def test_python_call_case_passes_reference_and_fails_broken(tmp_path: Path) -> None:
    reference = _write_project(
        tmp_path / "reference",
        "def next_id(rows):\n    return max((row['id'] for row in rows), default=0) + 1\n",
    )
    broken = _write_project(
        tmp_path / "broken",
        "def next_id(rows):\n    return rows[-1]['id'] + 1\n",
    )
    suite = _suite(
        {
            "id": "unsorted-identifiers",
            "rubric_id": "behavior",
            "adapter": "python_call",
            "target": "main:next_id",
            "input": {"args": [[{"id": 8}, {"id": 2}]], "kwargs": {}, "files": []},
            "expect": {"return": 9, "exception": None, "stdout": None, "files": []},
        }
    )
    executor = LocalSuiteExecutor(timeout_seconds=2)

    reference_result = executor.evaluate(suite, reference)
    broken_result = executor.evaluate(suite, broken)

    assert reference_result.passed is True
    assert broken_result.passed is False
    assert broken_result.cases[0].failures == ("return value differs",)


def test_python_call_detects_argument_mutation_without_executable_helper(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path / "project",
        (
            "def next_id(rows):\n"
            "    rows.sort(key=lambda row: row['id'])\n"
            "    return rows[-1]['id'] + 1\n"
        ),
    )
    original = [{"id": 8}, {"id": 2}]
    suite = _suite(
        {
            "id": "preserves-input",
            "rubric_id": "behavior",
            "adapter": "python_call",
            "target": "main:next_id",
            "input": {"args": [original], "kwargs": {}, "files": []},
            "expect": {
                "return": 9,
                "exception": None,
                "stdout": None,
                "files": [],
                "args_after": [original],
            },
        }
    )

    result = LocalSuiteExecutor(timeout_seconds=2).evaluate(suite, project)

    assert result.cases[0].failures == ("arguments differ after call",)


def test_cli_case_checks_stdin_stdout_and_output_file(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path / "project",
        """import json
from pathlib import Path

name = input()
Path("result.json").write_text(json.dumps({"name": name}), encoding="utf-8")
print(f"saved:{name}")
""",
    )
    suite = _suite(
        {
            "id": "writes-result",
            "rubric_id": "behavior",
            "adapter": "cli",
            "target": "main.py",
            "input": {"argv": [], "stdin": "Анна\n", "files": []},
            "expect": {
                "exit_code": 0,
                "stdout": {"mode": "exact", "value": "saved:Анна\n"},
                "stderr": {"mode": "exact", "value": ""},
                "files": [
                    {
                        "path": "result.json",
                        "content": {"mode": "json", "value": {"name": "Анна"}},
                    }
                ],
            },
        }
    )

    result = LocalSuiteExecutor(timeout_seconds=2).evaluate(suite, project)

    assert result.passed is True


def test_each_case_runs_in_a_fresh_workspace(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path / "project",
        """from pathlib import Path


def first_run():
    marker = Path("marker")
    existed = marker.exists()
    marker.write_text("created")
    return existed
""",
    )
    case = {
        "id": "fresh-workspace",
        "rubric_id": "behavior",
        "adapter": "python_call",
        "target": "main:first_run",
        "input": {"args": [], "kwargs": {}, "files": []},
        "expect": {"return": False, "exception": None, "stdout": None, "files": []},
    }
    payload = json.loads(
        json.dumps(
            {
                "schema_version": 1,
                "status": "ready",
                "summary": "Fresh workspace.",
                "clarification": None,
                "rubric": [
                    {"id": "behavior", "description": "Изоляция.", "weight": 1}
                ],
                "cases": [case, {**case, "id": "fresh-workspace-again"}],
            }
        )
    )
    suite = SuiteDraft.from_cli_output(json.dumps(payload))

    result = LocalSuiteExecutor(timeout_seconds=2).evaluate(suite, project)

    assert result.passed is True
    assert len(result.cases) == 2


def test_timeout_fails_case_without_hanging_suite(tmp_path: Path) -> None:
    project = _write_project(
        tmp_path / "project",
        "def never_returns():\n    while True:\n        pass\n",
    )
    suite = _suite(
        {
            "id": "timeout",
            "rubric_id": "behavior",
            "adapter": "python_call",
            "target": "main:never_returns",
            "input": {"args": [], "kwargs": {}, "files": []},
            "expect": {"return": None, "exception": None, "stdout": None, "files": []},
        }
    )

    result = LocalSuiteExecutor(timeout_seconds=0.1).evaluate(suite, project)

    assert result.passed is False
    assert result.cases[0].failures == ("execution timed out",)
