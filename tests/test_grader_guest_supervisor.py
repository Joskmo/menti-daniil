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
