import os
from pathlib import Path

from grader.guest_supervisor import GuestSupervisor


def test_guest_supervisor_runs_student_as_bounded_child_and_observes_requested_files(
    tmp_path,
) -> None:
    input_root = tmp_path / "input"
    source = input_root / "source"
    source.mkdir(parents=True)
    (source / "main.py").write_text(
        "from pathlib import Path\n"
        "def save(value):\n"
        "    Path('result.txt').write_text(value)\n"
        "    return len(value)\n"
    )
    execution = {
        "version": 1,
        "request_id": "a" * 32,
        "adapter": "python_call",
        "target": "main:save",
        "input": {
            "args": ["hello"],
            "kwargs": {},
            "files": [{"path": "seed.txt", "content": "seed"}],
        },
        "observe_files": ["result.txt"],
    }
    workspace = tmp_path / "workspace"
    runner = Path(__file__).parents[1] / "grader" / "guest_case_runner.py"
    supervisor = GuestSupervisor(
        runner=runner,
        student_uid=os.getuid(),
        student_gid=os.getgid(),
        timeout_seconds=3,
    )

    response = supervisor.execute(input_root, workspace, execution)

    assert response["status"] == "ok"
    assert response["request_id"] == "a" * 32
    assert response["observation"]["return"] == 5
    assert response["observation"]["args_after"] == ["hello"]
    assert response["observation"]["files"] == [
        {
            "path": "result.txt",
            "present": True,
            "content": "hello",
            "truncated": False,
        }
    ]
    assert (workspace / "seed.txt").read_text() == "seed"


def test_guest_supervisor_rejects_expected_value_in_execution(tmp_path) -> None:
    input_root = tmp_path / "input"
    (input_root / "source").mkdir(parents=True)
    (input_root / "source" / "main.py").write_text("pass\n")
    execution = {
        "version": 1,
        "request_id": "a" * 32,
        "adapter": "python_call",
        "target": "main:run",
        "input": {"args": [], "kwargs": {}, "files": []},
        "observe_files": [],
        "expect": {"return": 1},
    }
    runner = Path(__file__).parents[1] / "grader" / "guest_case_runner.py"

    response = GuestSupervisor(
        runner=runner,
        student_uid=os.getuid(),
        student_gid=os.getgid(),
    ).execute(input_root, tmp_path / "workspace", execution)

    assert response["status"] == "error"
    assert response["observation"] is None


def test_guest_supervisor_rejects_fixture_that_overwrites_student_source(tmp_path) -> None:
    input_root = tmp_path / "input"
    source = input_root / "source"
    source.mkdir(parents=True)
    (source / "main.py").write_text("def run(): return 'student'\n")
    execution = {
        "version": 1,
        "request_id": "a" * 32,
        "adapter": "python_call",
        "target": "main:run",
        "input": {
            "args": [],
            "kwargs": {},
            "files": [{"path": "main.py", "content": "def run(): return 'fixture'\n"}],
        },
        "observe_files": [],
    }
    runner = Path(__file__).parents[1] / "grader" / "guest_case_runner.py"

    response = GuestSupervisor(
        runner=runner,
        student_uid=os.getuid(),
        student_gid=os.getgid(),
    ).execute(input_root, tmp_path / "workspace", execution)

    assert response["status"] == "error"
    assert response["observation"] is None


def test_guest_supervisor_reports_unserializable_student_result_as_observation(tmp_path) -> None:
    input_root = tmp_path / "input"
    source = input_root / "source"
    source.mkdir(parents=True)
    (source / "main.py").write_text("def run(): return {1, 2}\n")
    execution = {
        "version": 1,
        "request_id": "b" * 32,
        "adapter": "python_call",
        "target": "main:run",
        "input": {"args": [], "kwargs": {}, "files": []},
        "observe_files": [],
    }
    runner = Path(__file__).parents[1] / "grader" / "guest_case_runner.py"

    response = GuestSupervisor(
        runner=runner,
        student_uid=os.getuid(),
        student_gid=os.getgid(),
    ).execute(input_root, tmp_path / "workspace", execution)

    assert response["status"] == "ok"
    assert response["observation"]["exception"] == "menti.StudentProcessFailure"


def test_guest_supervisor_reports_student_hard_exit_as_failed_observation(tmp_path) -> None:
    input_root = tmp_path / "input"
    source = input_root / "source"
    source.mkdir(parents=True)
    (source / "main.py").write_text("import os\ndef run(): os._exit(7)\n")
    execution = {
        "version": 1,
        "request_id": "c" * 32,
        "adapter": "python_call",
        "target": "main:run",
        "input": {"args": [], "kwargs": {}, "files": []},
        "observe_files": [],
    }
    runner = Path(__file__).parents[1] / "grader" / "guest_case_runner.py"

    response = GuestSupervisor(
        runner=runner,
        student_uid=os.getuid(),
        student_gid=os.getgid(),
    ).execute(input_root, tmp_path / "workspace", execution)

    assert response["status"] == "ok"
    assert response["observation"]["exception"] == "menti.StudentProcessFailure"


def test_guest_supervisor_reports_student_timeout_as_failed_observation(tmp_path) -> None:
    input_root = tmp_path / "input"
    source = input_root / "source"
    source.mkdir(parents=True)
    (source / "main.py").write_text("def run():\n    while True: pass\n")
    execution = {
        "version": 1,
        "request_id": "d" * 32,
        "adapter": "python_call",
        "target": "main:run",
        "input": {"args": [], "kwargs": {}, "files": []},
        "observe_files": [],
    }
    runner = Path(__file__).parents[1] / "grader" / "guest_case_runner.py"

    response = GuestSupervisor(
        runner=runner,
        student_uid=os.getuid(),
        student_gid=os.getgid(),
        timeout_seconds=1,
    ).execute(input_root, tmp_path / "workspace", execution)

    assert response["status"] == "ok"
    assert response["observation"]["exception"] == "menti.StudentProcessFailure"


def test_guest_supervisor_truncates_aggregate_oversized_observed_files(
    tmp_path,
) -> None:
    input_root = tmp_path / "input"
    source = input_root / "source"
    source.mkdir(parents=True)
    (source / "main.py").write_text(
        "from pathlib import Path\n"
        "def run():\n"
        "    for name in ('one.txt', 'two.txt', 'three.txt'):\n"
        "        Path(name).write_text('x' * 700_000)\n"
        "    return 1\n"
    )
    execution = {
        "version": 1,
        "request_id": "f" * 32,
        "adapter": "python_call",
        "target": "main:run",
        "input": {"args": [], "kwargs": {}, "files": []},
        "observe_files": ["one.txt", "two.txt", "three.txt"],
    }
    runner = Path(__file__).parents[1] / "grader" / "guest_case_runner.py"

    response = GuestSupervisor(
        runner=runner,
        student_uid=os.getuid(),
        student_gid=os.getgid(),
    ).execute(input_root, tmp_path / "workspace", execution)

    assert response["status"] == "ok"
    assert response["observation"]["exception"] is None
    assert response["observation"]["files"] == [
        {"path": path, "present": True, "content": None, "truncated": True}
        for path in ("one.txt", "two.txt", "three.txt")
    ]


def test_guest_supervisor_treats_nul_observed_file_as_student_failure(tmp_path) -> None:
    input_root = tmp_path / "input"
    source = input_root / "source"
    source.mkdir(parents=True)
    (source / "main.py").write_text(
        "from pathlib import Path\n"
        "def run():\n"
        "    Path('result.txt').write_bytes(b'a\\x00b')\n"
        "    return 1\n"
    )
    execution = {
        "version": 1,
        "request_id": "0" * 32,
        "adapter": "python_call",
        "target": "main:run",
        "input": {"args": [], "kwargs": {}, "files": []},
        "observe_files": ["result.txt"],
    }
    runner = Path(__file__).parents[1] / "grader" / "guest_case_runner.py"

    response = GuestSupervisor(
        runner=runner,
        student_uid=os.getuid(),
        student_gid=os.getgid(),
    ).execute(input_root, tmp_path / "workspace", execution)

    assert response["status"] == "ok"
    assert response["observation"]["exception"] == "menti.StudentProcessFailure"
    assert response["observation"]["files"] == []


def test_guest_supervisor_truncates_serial_unsafe_observed_file(tmp_path) -> None:
    input_root = tmp_path / "input"
    source = input_root / "source"
    source.mkdir(parents=True)
    (source / "main.py").write_text(
        "from pathlib import Path\n"
        "def run():\n"
        "    Path('result.txt').write_text('x' * 900_000)\n"
        "    return 1\n"
    )
    execution = {
        "version": 1,
        "request_id": "1" * 32,
        "adapter": "python_call",
        "target": "main:run",
        "input": {"args": [], "kwargs": {}, "files": []},
        "observe_files": ["result.txt"],
    }
    runner = Path(__file__).parents[1] / "grader" / "guest_case_runner.py"

    response = GuestSupervisor(
        runner=runner,
        student_uid=os.getuid(),
        student_gid=os.getgid(),
    ).execute(input_root, tmp_path / "workspace", execution)

    assert response["status"] == "ok"
    assert response["observation"]["files"] == [
        {
            "path": "result.txt",
            "present": True,
            "content": None,
            "truncated": True,
        }
    ]


def test_guest_supervisor_preserves_carriage_returns_in_observed_file(tmp_path) -> None:
    input_root = tmp_path / "input"
    source = input_root / "source"
    source.mkdir(parents=True)
    (source / "main.py").write_text(
        "from pathlib import Path\n"
        "def run():\n"
        "    Path('result.txt').write_bytes(b'a\\rb')\n"
        "    return 1\n"
    )
    execution = {
        "version": 1,
        "request_id": "2" * 32,
        "adapter": "python_call",
        "target": "main:run",
        "input": {"args": [], "kwargs": {}, "files": []},
        "observe_files": ["result.txt"],
    }
    runner = Path(__file__).parents[1] / "grader" / "guest_case_runner.py"

    response = GuestSupervisor(
        runner=runner,
        student_uid=os.getuid(),
        student_gid=os.getgid(),
    ).execute(input_root, tmp_path / "workspace", execution)

    assert response["status"] == "ok"
    assert response["observation"]["files"][0]["content"] == "a\rb"


def test_guest_supervisor_preserves_five_second_cpu_budget(tmp_path) -> None:
    input_root = tmp_path / "input"
    source = input_root / "source"
    source.mkdir(parents=True)
    (source / "main.py").write_text(
        "import time\n"
        "def run():\n"
        "    started = time.process_time()\n"
        "    while time.process_time() - started < 3.4:\n"
        "        pass\n"
        "    return 7\n"
    )
    execution = {
        "version": 1,
        "request_id": "3" * 32,
        "adapter": "python_call",
        "target": "main:run",
        "input": {"args": [], "kwargs": {}, "files": []},
        "observe_files": [],
    }
    runner = Path(__file__).parents[1] / "grader" / "guest_case_runner.py"

    response = GuestSupervisor(
        runner=runner,
        student_uid=os.getuid(),
        student_gid=os.getgid(),
    ).execute(input_root, tmp_path / "workspace", execution)

    assert response["status"] == "ok"
    assert response["observation"]["return"] == 7
    assert response["observation"]["exception"] is None


def test_guest_supervisor_preserves_five_second_student_budget(tmp_path) -> None:
    input_root = tmp_path / "input"
    source = input_root / "source"
    source.mkdir(parents=True)
    (source / "main.py").write_text(
        "import time\ndef run():\n    time.sleep(3.2)\n    return 7\n"
    )
    execution = {
        "version": 1,
        "request_id": "f" * 32,
        "adapter": "python_call",
        "target": "main:run",
        "input": {"args": [], "kwargs": {}, "files": []},
        "observe_files": [],
    }
    runner = Path(__file__).parents[1] / "grader" / "guest_case_runner.py"

    response = GuestSupervisor(
        runner=runner,
        student_uid=os.getuid(),
        student_gid=os.getgid(),
    ).execute(input_root, tmp_path / "workspace", execution)

    assert response["status"] == "ok"
    assert response["observation"]["return"] == 7
    assert response["observation"]["exception"] is None


def test_guest_supervisor_keeps_trusted_runner_crash_as_infrastructure_error(tmp_path) -> None:
    input_root = tmp_path / "input"
    source = input_root / "source"
    source.mkdir(parents=True)
    (source / "main.py").write_text("def run(): return 1\n")
    broken_runner = tmp_path / "broken_runner.py"
    broken_runner.write_text("raise SystemExit(7)\n")
    execution = {
        "version": 1,
        "request_id": "e" * 32,
        "adapter": "python_call",
        "target": "main:run",
        "input": {"args": [], "kwargs": {}, "files": []},
        "observe_files": [],
    }

    response = GuestSupervisor(
        runner=broken_runner,
        student_uid=os.getuid(),
        student_gid=os.getgid(),
    ).execute(input_root, tmp_path / "workspace", execution)

    assert response["status"] == "error"
    assert response["observation"] is None
