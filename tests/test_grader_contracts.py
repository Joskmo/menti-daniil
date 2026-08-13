import json

import pytest

from grader.contracts import ContractError, SuiteDraft


def _ready_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "status": "ready",
        "summary": "Проверяет поведение добавления записи.",
        "clarification": None,
        "rubric": [
            {
                "id": "unique-id",
                "description": "Новый идентификатор не повторяется.",
                "weight": 1,
            }
        ],
        "cases": [
            {
                "id": "unsorted-identifiers",
                "rubric_id": "unique-id",
                "adapter": "python_call",
                "target": "main:next_id",
                "input": {"args": [[{"id": 7}, {"id": 2}]], "kwargs": {}, "files": []},
                "expect": {
                    "return": 8,
                    "exception": None,
                    "stdout": None,
                    "files": [],
                },
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_suite_draft_parses_declarative_python_call_case() -> None:
    draft = SuiteDraft.from_cli_output(json.dumps(_ready_payload()))

    assert draft.status == "ready"
    assert draft.cases[0].case_id == "unsorted-identifiers"
    assert draft.cases[0].adapter == "python_call"
    assert draft.rubric[0].rubric_id == "unique-id"


def test_suite_draft_can_expect_python_arguments_after_call() -> None:
    case = _ready_payload()["cases"][0].copy()  # type: ignore[index,union-attr]
    case["expect"] = {
        "return": 8,
        "exception": None,
        "stdout": None,
        "files": [],
        "args_after": [[{"id": 7}, {"id": 2}]],
    }

    draft = SuiteDraft.from_cli_output(json.dumps(_ready_payload(cases=[case])))

    assert draft.cases[0].expect_spec["args_after"] == [[{"id": 7}, {"id": 2}]]


def test_suite_draft_parses_declarative_cli_case() -> None:
    case = {
        "id": "empty-file",
        "rubric_id": "unique-id",
        "adapter": "cli",
        "target": "main.py",
        "input": {
            "argv": [],
            "stdin": "Иван\nИванов\n",
            "files": [{"path": "data/users.json", "content": "[]\n"}],
        },
        "expect": {
            "exit_code": 0,
            "stdout": {"mode": "contains", "value": "Ваш id: 1"},
            "stderr": None,
            "files": [
                {
                    "path": "data/users.json",
                    "content": {"mode": "json", "value": [{"id": 1}]},
                }
            ],
        },
    }

    draft = SuiteDraft.from_cli_output(json.dumps(_ready_payload(cases=[case])))

    assert draft.cases[0].adapter == "cli"
    assert draft.cases[0].target == "main.py"


@pytest.mark.parametrize(
    "target",
    [
        "../main.py",
        "/tmp/main.py",
        "project\\main.py",
        "main.txt",
        "-m pytest",
    ],
)
def test_suite_draft_rejects_unsafe_cli_targets(target: str) -> None:
    case = {
        "id": "unsafe-target",
        "rubric_id": "unique-id",
        "adapter": "cli",
        "target": target,
        "input": {"argv": [], "stdin": "", "files": []},
        "expect": {"exit_code": 0, "stdout": None, "stderr": None, "files": []},
    }

    with pytest.raises(ContractError, match="CLI target"):
        SuiteDraft.from_cli_output(json.dumps(_ready_payload(cases=[case])))


@pytest.mark.parametrize(
    "path",
    ["../secret", "/tmp/escape", "data/../../escape", "data\\users.json", ".git/config"],
)
def test_suite_draft_rejects_unsafe_fixture_paths(path: str) -> None:
    case = _ready_payload()["cases"][0].copy()  # type: ignore[index,union-attr]
    case["input"] = {
        "args": [],
        "kwargs": {},
        "files": [{"path": path, "content": "payload"}],
    }

    with pytest.raises(ContractError, match="fixture path"):
        SuiteDraft.from_cli_output(json.dumps(_ready_payload(cases=[case])))


def test_case_must_reference_existing_rubric_item() -> None:
    case = _ready_payload()["cases"][0].copy()  # type: ignore[index,union-attr]
    case["rubric_id"] = "missing-rubric"

    with pytest.raises(ContractError, match="unknown rubric"):
        SuiteDraft.from_cli_output(json.dumps(_ready_payload(cases=[case])))


def test_ready_suite_must_contain_cases_and_rubric() -> None:
    with pytest.raises(ContractError, match="ready suite"):
        SuiteDraft.from_cli_output(json.dumps(_ready_payload(cases=[], rubric=[])))


def test_suite_draft_accepts_clarification_without_cases() -> None:
    payload = {
        "schema_version": 1,
        "status": "clarification_required",
        "summary": "Не определено поведение при повреждённом JSON.",
        "clarification": "Что должна делать программа при повреждённом JSON-файле?",
        "rubric": [],
        "cases": [],
    }

    draft = SuiteDraft.from_cli_output(json.dumps(payload))

    assert draft.status == "clarification_required"
    assert draft.clarification == payload["clarification"]


@pytest.mark.parametrize(
    "output",
    [
        "session_id: ignored\n" + json.dumps(_ready_payload()),
        "```json\n" + json.dumps(_ready_payload()) + "\n```",
        json.dumps(_ready_payload()) + "\n" + json.dumps({"second": "object"}),
    ],
)
def test_cli_output_rejects_any_content_outside_one_json_object(output: str) -> None:
    with pytest.raises(ContractError, match="one JSON object"):
        SuiteDraft.from_cli_output(output)
