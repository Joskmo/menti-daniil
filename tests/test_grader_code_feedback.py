import json

import pytest

from grader.code_feedback import CodeFeedbackContractError, HermesCodeFeedbackAuthor


class FakeBroker:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[tuple[str, str]] = []

    def complete(self, prompt: str, *, purpose: str) -> str:
        self.calls.append((prompt, purpose))
        return self.output


def _input() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "task_id": "PY-002",
            "commit_sha": "b" * 40,
            "assignment": {
                "title": "Следующий ID",
                "description": "Вернуть max(id) + 1.",
            },
            "mentor_proposal": None,
            "grade": {"passed": 1, "total": 2},
            "source_files": [
                {"path": "main.py", "content": "def next_id(rows): return len(rows) + 1\n"}
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _output() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "summary": "Решение короткое, но неверно работает для разреженных ID.",
            "strengths": ["Функция небольшая и читаемая."],
            "weaknesses": ["main.py:next_id использует длину вместо максимального ID."],
            "recommendations": ["Вычислять максимум значений id и обработать пустой список."],
        },
        ensure_ascii=False,
    )


def test_feedback_author_analyzes_exact_snapshot_without_hidden_suite_data() -> None:
    broker = FakeBroker(_output())

    feedback = HermesCodeFeedbackAuthor(broker=broker).create(_input())

    assert feedback.summary.startswith("Решение короткое")
    assert feedback.strengths == ("Функция небольшая и читаемая.",)
    prompt, purpose = broker.calls[0]
    assert purpose == "code-feedback"
    assert "PY-002" in prompt
    assert "b" * 40 in prompt
    assert "private-normal-vector" not in prompt
    assert "exact hidden inputs" in prompt


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": 1,
            "summary": "x",
            "strengths": [],
            "weaknesses": [],
            "recommendations": [],
        },
        {
            "schema_version": 1,
            "summary": "x",
            "strengths": ["ok"],
            "weaknesses": ["bad"],
            "recommendations": ["fix"],
            "extra": True,
        },
    ],
)
def test_feedback_author_rejects_invalid_contract(payload) -> None:
    with pytest.raises(CodeFeedbackContractError):
        HermesCodeFeedbackAuthor(broker=FakeBroker(json.dumps(payload))).create(_input())


def test_feedback_author_rejects_trailing_json_object() -> None:
    with pytest.raises(CodeFeedbackContractError):
        HermesCodeFeedbackAuthor(broker=FakeBroker(_output() + "{}")).create(_input())
