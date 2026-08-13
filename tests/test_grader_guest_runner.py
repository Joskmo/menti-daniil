import json
import subprocess
import sys
from pathlib import Path

import pytest


def _completed(project: Path, request: dict) -> subprocess.CompletedProcess[str]:
    runner = Path(__file__).parents[1] / "grader" / "guest_case_runner.py"
    return subprocess.run(
        [sys.executable, str(runner)],
        cwd=project,
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )


def _run(project: Path, request: dict) -> dict:
    completed = _completed(project, request)
    assert completed.returncode == 0
    return json.loads(completed.stdout)


def test_guest_runner_imports_student_only_inside_runner_process(tmp_path) -> None:
    (tmp_path / "main.py").write_text("def double(value): return value * 2\n")

    result = _run(
        tmp_path,
        {
            "adapter": "python_call",
            "target": "main:double",
            "input": {"args": [4], "kwargs": {}},
        },
    )

    assert result == {
        "return": 8,
        "exception": None,
        "stdout": "",
        "stderr": "",
        "exit_code": 0,
        "args_after": [4],
    }


def test_guest_runner_captures_student_exception_without_traceback(tmp_path) -> None:
    (tmp_path / "main.py").write_text(
        "def explode():\n    raise ValueError('private details')\n"
    )

    result = _run(
        tmp_path,
        {
            "adapter": "python_call",
            "target": "main:explode",
            "input": {"args": [], "kwargs": {}},
        },
    )

    assert result["return"] is None
    assert result["exception"] == "builtins.ValueError"
    assert "private details" not in json.dumps(result)


@pytest.mark.parametrize(
    "body",
    [
        "def produce(): return {1, 2}\n",
        "def produce(): return float('nan')\n",
    ],
)
def test_guest_runner_rejects_non_json_return_without_forging_student_exception(
    tmp_path, body: str
) -> None:
    (tmp_path / "main.py").write_text(body)

    completed = _completed(
        tmp_path,
        {
            "adapter": "python_call",
            "target": "main:produce",
            "input": {"args": [], "kwargs": {}},
        },
    )

    assert completed.returncode == 0
    observation = json.loads(completed.stdout)
    assert observation == {
        "return": None,
        "exception": "menti.StudentProcessFailure",
        "stdout": "",
        "stderr": "",
        "exit_code": 1,
        "args_after": None,
    }
