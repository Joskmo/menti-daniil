import hashlib
import json
import re
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

from grader.contracts import GraderCase, SuiteDraft
from grader.executor import CaseResult, SuiteInfrastructureError, SuiteResult

_REQUEST_ID = re.compile(r"[0-9a-f]{32}")
_MAX_TEXT_BYTES = 1_000_000


class DisposableVmWorker(Protocol):
    def execute(self, source_directory: Path, request: dict[str, Any]) -> dict[str, Any]: ...


class VmSuiteExecutor:
    def __init__(
        self,
        worker: DisposableVmWorker,
        *,
        request_ids: Iterator[str] | None = None,
    ) -> None:
        self.worker = worker
        self.request_ids = request_ids

    def evaluate(self, suite: SuiteDraft, project_directory: str | Path) -> SuiteResult:
        if suite.status != "ready":
            raise ValueError("only ready suites can be evaluated")
        project = Path(project_directory).resolve()
        if project.is_symlink() or not project.is_dir():
            raise ValueError("project_directory must be a real directory")
        for candidate in project.rglob("*"):
            if candidate.is_symlink():
                raise ValueError("student source must not contain symlinks")
        return SuiteResult(tuple(self._evaluate_case(case, project) for case in suite.cases))

    def _evaluate_case(self, case: GraderCase, project: Path) -> CaseResult:
        request_id = self._request_id()
        request = {
            "version": 1,
            "request_id": request_id,
            "adapter": case.adapter,
            "target": case.target,
            "input": json.loads(
                json.dumps(case.input_spec, ensure_ascii=False, allow_nan=False)
            ),
            "observe_files": [item["path"] for item in case.expect_spec["files"]],
        }
        try:
            response = self.worker.execute(project, request)
            observation = _validate_response(response, request_id)
            failures = _compare(case, observation)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise SuiteInfrastructureError(
                "VM worker did not return a trusted observation"
            ) from error
        digest = hashlib.sha256(
            json.dumps(
                observation,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        return CaseResult(
            case.case_id,
            case.rubric_id,
            not failures,
            tuple(failures),
            observation_digest=digest,
        )

    def _request_id(self) -> str:
        value = next(self.request_ids) if self.request_ids is not None else secrets.token_hex(16)
        if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value):
            raise ValueError("request id source returned an invalid identifier")
        return value


def _validate_response(response: Any, request_id: str) -> dict[str, Any]:
    if not isinstance(response, dict) or set(response) != {
        "version",
        "request_id",
        "status",
        "observation",
    }:
        raise ValueError("invalid worker response fields")
    if response["version"] != 1 or response["request_id"] != request_id:
        raise ValueError("worker response identity mismatch")
    if response["status"] != "ok":
        raise ValueError("worker did not complete the request")
    observation = response["observation"]
    if not isinstance(observation, dict) or set(observation) != {
        "return",
        "exception",
        "stdout",
        "stderr",
        "exit_code",
        "args_after",
        "files",
    }:
        raise ValueError("invalid worker observation fields")
    try:
        json.dumps(observation["return"], ensure_ascii=False, allow_nan=False)
        json.dumps(observation["args_after"], ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("worker values are not JSON-safe") from error
    if observation["args_after"] is not None and not isinstance(
        observation["args_after"], list
    ):
        raise ValueError("worker arguments are invalid")
    exception = observation["exception"]
    if exception is not None and (
        not isinstance(exception, str)
        or len(exception) > 256
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*", exception)
    ):
        raise ValueError("worker exception name is invalid")
    for field in ("stdout", "stderr"):
        value = observation[field]
        if (
            not isinstance(value, str)
            or "\x00" in value
            or len(value.encode("utf-8")) > _MAX_TEXT_BYTES
        ):
            raise ValueError("worker text observation is invalid")
    exit_code = observation["exit_code"]
    if (
        isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or not -255 <= exit_code <= 255
    ):
        raise ValueError("worker exit code is invalid")
    files = observation["files"]
    if not isinstance(files, list) or len(files) > 20:
        raise ValueError("worker file observation is invalid")
    seen = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "present",
            "content",
            "truncated",
        }:
            raise ValueError("worker file observation fields are invalid")
        path = item["path"]
        if not isinstance(path, str) or path in seen:
            raise ValueError("worker file observation path is invalid")
        seen.add(path)
        if not isinstance(item["present"], bool) or not isinstance(item["truncated"], bool):
            raise ValueError("worker file observation flags are invalid")
        content = item["content"]
        if content is not None and (
            not isinstance(content, str)
            or len(content.encode("utf-8")) > _MAX_TEXT_BYTES
            or "\x00" in content
        ):
            raise ValueError("worker file observation content is invalid")
    return observation


def _compare(case: GraderCase, observed: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if observed["exception"] == "menti.StudentProcessFailure":
        return ["student process failed"]
    if case.adapter == "python_call":
        expected_exception = case.expect_spec["exception"]
        actual_exception = observed["exception"]
        if expected_exception is None:
            if actual_exception is not None:
                failures.append("unexpected exception")
            elif not _json_equal(observed["return"], case.expect_spec["return"]):
                failures.append("return value differs")
        elif not _exception_matches(actual_exception, expected_exception):
            failures.append("expected exception was not raised")
        if (
            "args_after" in case.expect_spec
            and not _json_equal(observed["args_after"], case.expect_spec["args_after"])
        ):
            failures.append("arguments differ after call")
        _check_matcher(observed["stdout"], case.expect_spec["stdout"], "stdout", failures)
    else:
        if observed["exit_code"] != case.expect_spec["exit_code"]:
            failures.append("exit code differs")
        _check_matcher(observed["stdout"], case.expect_spec["stdout"], "stdout", failures)
        _check_matcher(observed["stderr"], case.expect_spec["stderr"], "stderr", failures)
    observed_files = {item["path"]: item for item in observed["files"]}
    for expected in case.expect_spec["files"]:
        item = observed_files.get(expected["path"])
        if item is None or not item["present"]:
            failures.append(f"expected file missing: {expected['path']}")
        elif item["truncated"] or item["content"] is None:
            failures.append(f"expected file unreadable: {expected['path']}")
        else:
            _check_matcher(
                item["content"],
                expected["content"],
                f"file {expected['path']}",
                failures,
            )
    return failures


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
            if not _json_equal(parsed, expected):
                failures.append(f"{label} JSON differs")


def _json_equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    return left == right
