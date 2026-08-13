import json
import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


class ContractError(ValueError):
    """The test-author response does not satisfy the machine contract."""


@dataclass(frozen=True, slots=True)
class RubricItem:
    rubric_id: str
    description: str
    weight: int


@dataclass(frozen=True, slots=True)
class GraderCase:
    case_id: str
    rubric_id: str
    adapter: str
    target: str
    input_spec: dict[str, Any]
    expect_spec: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SuiteDraft:
    status: str
    summary: str
    clarification: str | None
    rubric: tuple[RubricItem, ...]
    cases: tuple[GraderCase, ...]

    @classmethod
    def from_cli_output(cls, output: str) -> "SuiteDraft":
        payload = _extract_json_object(output)
        _require_exact_keys(
            payload,
            {
                "schema_version",
                "status",
                "summary",
                "clarification",
                "rubric",
                "cases",
            },
            "suite",
        )
        if payload["schema_version"] != 1:
            raise ContractError("unsupported schema_version")
        status = payload["status"]
        if status not in {"ready", "clarification_required"}:
            raise ContractError("invalid suite status")
        summary = _bounded_text(payload["summary"], "summary", maximum=1_000)
        clarification_value = payload["clarification"]
        clarification = None
        if clarification_value is not None:
            clarification = _bounded_text(
                clarification_value,
                "clarification",
                maximum=1_000,
            )
        rubric = _parse_rubric(payload["rubric"])
        cases = _parse_cases(payload["cases"], {item.rubric_id for item in rubric})

        if status == "ready":
            if clarification is not None or not rubric or not cases:
                raise ContractError("ready suite requires rubric and test cases")
        elif clarification is None or rubric or cases:
            raise ContractError(
                "clarification_required suite requires one question and no generated artifacts"
            )
        return cls(status, summary, clarification, rubric, cases)

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema_version": 1,
            "status": self.status,
            "summary": self.summary,
            "clarification": self.clarification,
            "rubric": [
                {
                    "id": item.rubric_id,
                    "description": item.description,
                    "weight": item.weight,
                }
                for item in self.rubric
            ],
            "cases": [
                {
                    "id": case.case_id,
                    "rubric_id": case.rubric_id,
                    "adapter": case.adapter,
                    "target": case.target,
                    "input": case.input_spec,
                    "expect": case.expect_spec,
                }
                for case in self.cases
            ],
        }
        return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))


def _extract_json_object(output: str) -> dict[str, Any]:
    if not isinstance(output, str) or len(output) > 1_000_000:
        raise ContractError("CLI output is missing or too large")
    try:
        value = json.loads(output.strip(), object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, ValueError) as error:
        raise ContractError("CLI output must contain exactly one JSON object") from error
    if not isinstance(value, dict):
        raise ContractError("CLI output must contain exactly one JSON object")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _require_exact_keys(value: Any, expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ContractError(f"{label} has unexpected fields")


def _bounded_text(value: Any, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise ContractError(f"{label} has invalid length")
    return normalized


def _parse_rubric(value: Any) -> tuple[RubricItem, ...]:
    if not isinstance(value, list) or len(value) > 20:
        raise ContractError("rubric must be a bounded list")
    result: list[RubricItem] = []
    seen: set[str] = set()
    for raw in value:
        _require_exact_keys(raw, {"id", "description", "weight"}, "rubric item")
        rubric_id = _technical_key(raw["id"], "rubric id")
        if rubric_id in seen:
            raise ContractError("rubric ids must be unique")
        weight = raw["weight"]
        if isinstance(weight, bool) or not isinstance(weight, int) or not 1 <= weight <= 100:
            raise ContractError("rubric weight must be an integer from 1 to 100")
        seen.add(rubric_id)
        result.append(
            RubricItem(
                rubric_id,
                _bounded_text(raw["description"], "rubric description", maximum=500),
                weight,
            )
        )
    return tuple(result)


def _parse_cases(value: Any, rubric_ids: set[str]) -> tuple[GraderCase, ...]:
    if not isinstance(value, list) or len(value) > 40:
        raise ContractError("cases must be a bounded list")
    result: list[GraderCase] = []
    seen: set[str] = set()
    for raw in value:
        _require_exact_keys(
            raw,
            {"id", "rubric_id", "adapter", "target", "input", "expect"},
            "test case",
        )
        case_id = _technical_key(raw["id"], "case id")
        if case_id in seen:
            raise ContractError("case ids must be unique")
        rubric_id = _technical_key(raw["rubric_id"], "case rubric id")
        if rubric_id not in rubric_ids:
            raise ContractError("test case references unknown rubric item")
        adapter = raw["adapter"]
        if adapter == "python_call":
            target = _python_target(raw["target"])
            input_spec = _python_input(raw["input"])
            expect_spec = _python_expect(raw["expect"])
        elif adapter == "cli":
            target = _cli_target(raw["target"])
            input_spec = _cli_input(raw["input"])
            expect_spec = _cli_expect(raw["expect"])
        else:
            raise ContractError("unsupported test adapter")
        seen.add(case_id)
        result.append(GraderCase(case_id, rubric_id, adapter, target, input_spec, expect_spec))
    return tuple(result)


def _technical_key(value: Any, label: str) -> str:
    key = _bounded_text(value, label, maximum=64)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", key):
        raise ContractError(f"{label} must be a lowercase ASCII key")
    return key


def _python_target(value: Any) -> str:
    target = _bounded_text(value, "python target", maximum=160)
    identifier = r"[A-Za-z][A-Za-z0-9_]*"
    if not re.fullmatch(rf"{identifier}(?:\.{identifier})*:{identifier}", target):
        raise ContractError("python target must be module.path:function")
    return target


def _cli_target(value: Any) -> str:
    target = _bounded_text(value, "CLI target", maximum=160)
    _safe_relative_path(target, "CLI target")
    if not target.endswith(".py"):
        raise ContractError("CLI target must be a relative Python file")
    return target


def _python_input(value: Any) -> dict[str, Any]:
    _require_exact_keys(value, {"args", "kwargs", "files"}, "python_call input")
    args = value["args"]
    kwargs = value["kwargs"]
    if not isinstance(args, list) or len(args) > 20:
        raise ContractError("python_call args must be a bounded list")
    if not isinstance(kwargs, dict) or len(kwargs) > 20 or not all(
        isinstance(key, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key)
        for key in kwargs
    ):
        raise ContractError("python_call kwargs must use safe identifiers")
    _validate_json_value(args, "python_call args")
    _validate_json_value(kwargs, "python_call kwargs")
    _parse_fixture_files(value["files"])
    return value


def _python_expect(value: Any) -> dict[str, Any]:
    required = {"return", "exception", "stdout", "files"}
    allowed_shapes = {
        frozenset(required),
        frozenset(required | {"args_after"}),
    }
    if not isinstance(value, dict) or set(value) not in allowed_shapes:
        raise ContractError("python_call expect has unexpected fields")
    _validate_json_value(value["return"], "python_call return")
    exception = value["exception"]
    if exception is not None and (
        not isinstance(exception, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*", exception)
    ):
        raise ContractError("expected exception must be a safe exception name")
    if exception is not None and value["return"] is not None:
        raise ContractError("exception expectation cannot also expect a return value")
    _parse_matcher(value["stdout"], "stdout")
    _parse_expected_files(value["files"])
    if "args_after" in value:
        args_after = value["args_after"]
        if not isinstance(args_after, list) or len(args_after) > 20:
            raise ContractError("python_call args_after must be a bounded list")
        _validate_json_value(args_after, "python_call args_after")
    return value


def _cli_input(value: Any) -> dict[str, Any]:
    _require_exact_keys(value, {"argv", "stdin", "files"}, "CLI input")
    argv = value["argv"]
    if not isinstance(argv, list) or len(argv) > 20 or not all(
        isinstance(item, str) and len(item) <= 256 and "\x00" not in item for item in argv
    ):
        raise ContractError("CLI argv must be a bounded string list")
    stdin = value["stdin"]
    if not isinstance(stdin, str) or len(stdin.encode()) > 65_536 or "\x00" in stdin:
        raise ContractError("CLI stdin is invalid or too large")
    _parse_fixture_files(value["files"])
    return value


def _cli_expect(value: Any) -> dict[str, Any]:
    _require_exact_keys(value, {"exit_code", "stdout", "stderr", "files"}, "CLI expect")
    exit_code = value["exit_code"]
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or not 0 <= exit_code <= 255:
        raise ContractError("CLI exit_code must be from 0 to 255")
    _parse_matcher(value["stdout"], "stdout")
    _parse_matcher(value["stderr"], "stderr")
    _parse_expected_files(value["files"])
    return value


def _parse_fixture_files(value: Any) -> None:
    if not isinstance(value, list) or len(value) > 20:
        raise ContractError("fixture files must be a bounded list")
    seen: set[str] = set()
    total = 0
    for raw in value:
        _require_exact_keys(raw, {"path", "content"}, "fixture file")
        path = _safe_relative_path(raw["path"], "fixture path")
        if path in seen:
            raise ContractError("fixture paths must be unique")
        content = raw["content"]
        if not isinstance(content, str) or len(content.encode()) > 65_536 or "\x00" in content:
            raise ContractError("fixture content is invalid or too large")
        seen.add(path)
        total += len(content.encode())
    if total > 262_144:
        raise ContractError("fixture files are too large")


def _parse_expected_files(value: Any) -> None:
    if not isinstance(value, list) or len(value) > 20:
        raise ContractError("expected files must be a bounded list")
    seen: set[str] = set()
    for raw in value:
        _require_exact_keys(raw, {"path", "content"}, "expected file")
        path = _safe_relative_path(raw["path"], "fixture path")
        if path in seen:
            raise ContractError("expected file paths must be unique")
        _parse_matcher(raw["content"], "file content")
        seen.add(path)


def _parse_matcher(value: Any, label: str) -> None:
    if value is None:
        return
    _require_exact_keys(value, {"mode", "value"}, f"{label} matcher")
    mode = value["mode"]
    if mode not in {"exact", "contains", "json"}:
        raise ContractError(f"unsupported {label} matcher")
    if mode == "json":
        _validate_json_value(value["value"], f"{label} JSON value")
    elif not isinstance(value["value"], str) or len(value["value"].encode()) > 65_536:
        raise ContractError(f"{label} matcher text is invalid or too large")


def _safe_relative_path(value: Any, label: str) -> str:
    path = _bounded_text(value, label, maximum=160)
    pure = PurePosixPath(path)
    raw_parts = path.split("/")
    if (
        "\\" in path
        or pure.is_absolute()
        or not raw_parts
        or any(part in {"", ".", ".."} or part.startswith(".") for part in raw_parts)
    ):
        raise ContractError(f"{label} must be a safe relative path")
    return path


def _validate_json_value(value: Any, label: str) -> None:
    def walk(item: Any, depth: int) -> None:
        if depth > 12:
            raise ContractError(f"{label} is nested too deeply")
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ContractError(f"{label} contains a non-finite number")
            return
        if isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
            return
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            for child in item.values():
                walk(child, depth + 1)
            return
        raise ContractError(f"{label} is not JSON-compatible")

    walk(value, 0)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError) as error:
        raise ContractError(f"{label} is not JSON-compatible") from error
    if len(encoded) > 262_144:
        raise ContractError(f"{label} is too large")
