import json
from pathlib import Path

import pytest

from grader.contracts import SuiteDraft
from grader.executor import SuiteInfrastructureError
from grader.vm_executor import VmSuiteExecutor


def _suite() -> SuiteDraft:
    return SuiteDraft.from_cli_output(
        json.dumps(
            {
                "schema_version": 1,
                "status": "ready",
                "summary": "Закрытая проверка.",
                "clarification": None,
                "rubric": [{"id": "behavior", "description": "Поведение.", "weight": 1}],
                "cases": [
                    {
                        "id": "normal",
                        "rubric_id": "behavior",
                        "adapter": "python_call",
                        "target": "main:double",
                        "input": {"args": [4], "kwargs": {}, "files": []},
                        "expect": {
                            "return": 8,
                            "exception": None,
                            "stdout": None,
                            "files": [],
                            "args_after": [4],
                        },
                    },
                    {
                        "id": "boundary",
                        "rubric_id": "behavior",
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


class FakeDisposableWorker:
    def __init__(self, outputs: list[dict]) -> None:
        self.outputs = outputs
        self.requests: list[dict] = []
        self.sources: list[Path] = []

    def execute(self, source_directory: Path, request: dict) -> dict:
        self.sources.append(source_directory)
        self.requests.append(request)
        return self.outputs[len(self.requests) - 1]


def _observation(request_id: str, value, args_after) -> dict:
    return {
        "version": 1,
        "request_id": request_id,
        "status": "ok",
        "observation": {
            "return": value,
            "exception": None,
            "stdout": "",
            "stderr": "",
            "exit_code": 0,
            "args_after": args_after,
            "files": [],
        },
    }


def test_vm_executor_sends_one_input_without_expected_or_other_cases(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def double(value): return value * 2\n")
    worker = FakeDisposableWorker(
        [
            _observation("0" * 32, 8, [4]),
            _observation("1" * 32, 0, [0]),
        ]
    )
    executor = VmSuiteExecutor(worker, request_ids=iter(["0" * 32, "1" * 32]))

    result = executor.evaluate(_suite(), project)

    assert result.passed is True
    assert len(worker.requests) == 2
    assert worker.sources == [project.resolve(), project.resolve()]
    assert worker.requests[0]["input"]["args"] == [4]
    assert worker.requests[1]["input"]["args"] == [0]
    serialized = [json.dumps(request) for request in worker.requests]
    assert all("expect" not in request for request in serialized)
    assert all("Закрытая проверка" not in request for request in serialized)
    assert all("boundary" not in request for request in serialized[:1])
    assert all("normal" not in request for request in serialized[1:])


def test_vm_executor_compares_observation_only_on_trusted_side(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def double(value): return value\n")
    worker = FakeDisposableWorker(
        [
            _observation("0" * 32, 4, [4]),
            _observation("1" * 32, 0, [0]),
        ]
    )

    result = VmSuiteExecutor(
        worker,
        request_ids=iter(["0" * 32, "1" * 32]),
    ).evaluate(_suite(), project)

    assert result.passed is False
    assert result.cases[0].failures == ("return value differs",)
    assert result.cases[1].passed is True


def test_vm_executor_detects_mutated_arguments_on_trusted_side(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def double(value): return value * 2\n")
    worker = FakeDisposableWorker(
        [
            _observation("0" * 32, 8, [5]),
            _observation("1" * 32, 0, [0]),
        ]
    )

    result = VmSuiteExecutor(
        worker,
        request_ids=iter(["0" * 32, "1" * 32]),
    ).evaluate(_suite(), project)

    assert result.cases[0].failures == ("arguments differ after call",)
    assert result.cases[1].passed is True
    assert "args_after" not in worker.requests[0]
    assert "expect" not in json.dumps(worker.requests[0])


def test_vm_executor_raises_infrastructure_error_on_malformed_worker_envelope(
    tmp_path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def double(value): return value * 2\n")
    worker = FakeDisposableWorker(
        [
            {"version": 1, "request_id": "wrong", "status": "ok", "observation": {}},
            _observation("1" * 32, 0, [0]),
        ]
    )

    with pytest.raises(SuiteInfrastructureError):
        VmSuiteExecutor(
            worker,
            request_ids=iter(["0" * 32, "1" * 32]),
        ).evaluate(_suite(), project)

    assert len(worker.requests) == 1


def test_vm_executor_uses_type_aware_json_equality(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def double(value): return True\n")
    worker = FakeDisposableWorker(
        [
            _observation("0" * 32, True, [4]),
            _observation("1" * 32, 0, [0]),
        ]
    )

    result = VmSuiteExecutor(
        worker,
        request_ids=iter(["0" * 32, "1" * 32]),
    ).evaluate(_suite(), project)

    assert result.cases[0].failures == ("return value differs",)
    assert result.cases[0].observation_digest is not None


def test_vm_executor_treats_trusted_student_process_failure_as_failed_case(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def double(value): return {value}\n")
    failed = _observation("0" * 32, None, None)
    failed["observation"]["exception"] = "menti.StudentProcessFailure"
    worker = FakeDisposableWorker([failed, _observation("1" * 32, 0, [0])])

    result = VmSuiteExecutor(
        worker,
        request_ids=iter(["0" * 32, "1" * 32]),
    ).evaluate(_suite(), project)

    assert result.cases[0].passed is False
    assert result.cases[0].failures == ("student process failed",)
