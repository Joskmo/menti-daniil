import json
import subprocess
import sys
import time
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


def test_guest_runner_rejects_forged_protocol_written_to_fd_one(tmp_path) -> None:
    forged = json.dumps(
        {
            "return": 987654,
            "exception": None,
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "args_after": [],
        },
        separators=(",", ":"),
    )
    (tmp_path / "main.py").write_text(
        "import os\n"
        "def run():\n"
        f"    os.write(1, {forged.encode()!r})\n"
        "    os._exit(0)\n"
    )

    observation = _run(
        tmp_path,
        {
            "adapter": "python_call",
            "target": "main:run",
            "input": {"args": [], "kwargs": {}},
        },
    )

    assert observation["exception"] == "menti.StudentProcessFailure"
    assert observation["return"] is None


def test_guest_runner_rejects_forged_protocol_written_to_any_open_fd(tmp_path) -> None:
    forged = json.dumps(
        {
            "return": 777,
            "exception": None,
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "args_after": [],
        },
        separators=(",", ":"),
    )
    (tmp_path / "main.py").write_text(
        "import os\n"
        "def run():\n"
        f"    payload = {forged.encode()!r}\n"
        "    for descriptor in range(3, 64):\n"
        "        try:\n"
        "            os.write(descriptor, payload)\n"
        "        except OSError:\n"
        "            pass\n"
        "    os._exit(0)\n"
    )

    observation = _run(
        tmp_path,
        {
            "adapter": "python_call",
            "target": "main:run",
            "input": {"args": [], "kwargs": {}},
        },
    )

    assert observation["exception"] == "menti.StudentProcessFailure"
    assert observation["return"] is None


def test_guest_runner_captures_raw_fd_stdout_without_protocol_confusion(tmp_path) -> None:
    (tmp_path / "main.py").write_text(
        "import os\n"
        "def run():\n"
        "    os.write(1, b'hello\\n')\n"
        "    return 42\n"
    )

    observation = _run(
        tmp_path,
        {
            "adapter": "python_call",
            "target": "main:run",
            "input": {"args": [], "kwargs": {}},
        },
    )

    assert observation["return"] == 42
    assert observation["stdout"] == "hello\n"
    assert observation["exception"] is None


def test_guest_runner_preserves_return_when_background_descendant_holds_pipes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MENTI_STUDENT_TIMEOUT_SECONDS", "0.5")
    (tmp_path / "main.py").write_text(
        "import subprocess, sys\n"
        "def run():\n"
        "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'], "
        "start_new_session=True)\n"
        "    return 42\n"
    )

    started = time.monotonic()
    observation = _run(
        tmp_path,
        {
            "adapter": "python_call",
            "target": "main:run",
            "input": {"args": [], "kwargs": {}},
        },
    )

    assert time.monotonic() - started < 1.5
    assert observation["return"] == 42
    assert observation["exception"] is None


def test_guest_runner_kills_descendant_that_escapes_process_group(tmp_path) -> None:
    (tmp_path / "main.py").write_text(
        "import subprocess, sys\n"
        "def run():\n"
        "    subprocess.Popen(\n"
        "        [sys.executable, '-c', "
        "'import pathlib,time; time.sleep(0.25); '"
        "'pathlib.Path(\"late.txt\").write_text(\"late\")'],\n"
        "        start_new_session=True,\n"
        "        stdout=subprocess.DEVNULL,\n"
        "        stderr=subprocess.DEVNULL,\n"
        "    )\n"
        "    return 42\n"
    )

    observation = _run(
        tmp_path,
        {
            "adapter": "python_call",
            "target": "main:run",
            "input": {"args": [], "kwargs": {}},
        },
    )
    time.sleep(0.5)

    assert observation["return"] == 42
    assert not (tmp_path / "late.txt").exists()


def test_guest_runner_treats_invalid_utf8_cli_output_as_student_failure(tmp_path) -> None:
    (tmp_path / "main.py").write_bytes(b"import os\nos.write(1, b'\\xff')\n")

    observation = _run(
        tmp_path,
        {
            "adapter": "cli",
            "target": "main.py",
            "input": {"argv": [], "stdin": ""},
        },
    )

    assert observation["exception"] == "menti.StudentProcessFailure"
    assert observation["exit_code"] == 1


def test_guest_runner_treats_invalid_utf8_python_output_as_student_failure(tmp_path) -> None:
    (tmp_path / "main.py").write_bytes(
        b"import os\ndef run():\n    os.write(1, b'\\xff')\n    return 1\n"
    )

    observation = _run(
        tmp_path,
        {
            "adapter": "python_call",
            "target": "main:run",
            "input": {"args": [], "kwargs": {}},
        },
    )

    assert observation["exception"] == "menti.StudentProcessFailure"
    assert observation["exit_code"] == 1


@pytest.mark.parametrize(
    ("adapter", "body", "target", "input_spec"),
    [
        (
            "python_call",
            "def run():\n    print('x' * 950_000)\n    return 1\n",
            "main:run",
            {"args": [], "kwargs": {}},
        ),
        (
            "cli",
            "print('x' * 950_000)\n",
            "main.py",
            {"argv": [], "stdin": ""},
        ),
    ],
)
def test_guest_runner_treats_oversized_student_output_as_student_failure(
    tmp_path, adapter, body, target, input_spec
) -> None:
    (tmp_path / "main.py").write_text(body)

    observation = _run(
        tmp_path,
        {
            "adapter": adapter,
            "target": target,
            "input": input_spec,
        },
    )

    assert observation["exception"] == "menti.StudentProcessFailure"
    assert observation["exit_code"] == 1
    assert observation["stdout"] == ""


def test_guest_runner_treats_aggregate_cli_output_as_student_failure(tmp_path) -> None:
    (tmp_path / "main.py").write_text(
        "import sys\nprint('x' * 600_000)\nprint('y' * 600_000, file=sys.stderr)\n"
    )

    observation = _run(
        tmp_path,
        {
            "adapter": "cli",
            "target": "main.py",
            "input": {"argv": [], "stdin": ""},
        },
    )

    assert observation["exception"] == "menti.StudentProcessFailure"
    assert observation["exit_code"] == 1


def test_guest_runner_treats_nul_cli_output_as_student_failure(tmp_path) -> None:
    (tmp_path / "main.py").write_bytes(b"import os\nos.write(1, b'a\\x00b')\n")

    observation = _run(
        tmp_path,
        {
            "adapter": "cli",
            "target": "main.py",
            "input": {"argv": [], "stdin": ""},
        },
    )

    assert observation["exception"] == "menti.StudentProcessFailure"
    assert observation["exit_code"] == 1


def test_guest_runner_cli_uses_deterministic_environment(tmp_path) -> None:
    (tmp_path / "main.py").write_text(
        "import json, os\n"
        "print(json.dumps({key: os.environ.get(key) for key in "
        "['PYTHONHASHSEED', 'TZ', 'LC_ALL']}))\n"
    )

    observation = _run(
        tmp_path,
        {
            "adapter": "cli",
            "target": "main.py",
            "input": {"argv": [], "stdin": ""},
        },
    )

    assert json.loads(observation["stdout"]) == {
        "PYTHONHASHSEED": "0",
        "TZ": "UTC",
        "LC_ALL": "C.UTF-8",
    }
