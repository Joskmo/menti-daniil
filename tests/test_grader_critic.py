import json
from collections.abc import Callable

import pytest

from grader.author import AssignmentContext, SourceFile
from grader.contracts import SuiteDraft
from grader.critic import CriticError, CriticVerdict, HermesTestCritic
from grader.llm_broker import BrokerProtocolError


class FakeBroker:
    def __init__(self, responder: Callable[[str, str], str]) -> None:
        self.responder = responder
        self.calls: list[tuple[str, str]] = []

    def complete(self, prompt: str, *, purpose: str) -> str:
        self.calls.append((purpose, prompt))
        return self.responder(prompt, purpose)


def _context() -> AssignmentContext:
    return AssignmentContext(
        task_id="PY-002",
        project="json",
        title="Следующий ID",
        description="Вернуть 1 для пустого списка, иначе max(id) + 1.",
        source_files=(SourceFile("main.py", "def next_id(rows): return len(rows) + 1\n"),),
    )


def _suite() -> SuiteDraft:
    payload = {
        "schema_version": 1,
        "status": "ready",
        "summary": "Проверяет вычисление ID.",
        "clarification": None,
        "rubric": [
            {
                "id": "next-id",
                "description": "Следующий ID строится по максимуму.",
                "weight": 1,
            }
        ],
        "cases": [
            {
                "id": "unsorted",
                "rubric_id": "next-id",
                "adapter": "python_call",
                "target": "main:next_id",
                "input": {
                    "args": [[{"id": 8}, {"id": 2}]],
                    "kwargs": {},
                    "files": [],
                },
                "expect": {
                    "return": 9,
                    "exception": None,
                    "stdout": None,
                    "files": [],
                },
            }
        ],
    }
    return SuiteDraft.from_cli_output(json.dumps(payload))


def test_critic_approves_semantic_suite_through_independent_broker_purpose() -> None:
    output = json.dumps(
        {
            "schema_version": 1,
            "status": "approved",
            "summary": "Критерии соответствуют условию.",
            "clarification": None,
            "issues": [],
        }
    )
    broker = FakeBroker(lambda prompt, purpose: output)

    verdict = HermesTestCritic(broker=broker).review(_context(), _suite())

    assert verdict.status == "approved"
    assert verdict.to_payload() == json.loads(output)
    assert [purpose for purpose, _ in broker.calls] == ["test-critic"]
    assert "BEGIN_UNTRUSTED_ASSIGNMENT" in broker.calls[0][1]
    assert "BEGIN_UNTRUSTED_SUITE" in broker.calls[0][1]


def test_critic_contract_requires_blocking_issue_for_rejection() -> None:
    with pytest.raises(CriticError, match="blocking issue"):
        CriticVerdict.from_payload(
            {
                "schema_version": 1,
                "status": "rejected",
                "summary": "Есть замечание.",
                "clarification": None,
                "issues": [
                    {
                        "code": "weak-boundary",
                        "severity": "warning",
                        "message": "Нет проверки границы.",
                        "case_ids": ["unsorted"],
                    }
                ],
            }
        )


def test_critic_clarification_is_product_question_without_case_details() -> None:
    verdict = CriticVerdict.from_payload(
        {
            "schema_version": 1,
            "status": "clarification_required",
            "summary": "Нужно уточнить поведение.",
            "clarification": "Что должна вернуть функция для пустого списка?",
            "issues": [],
        }
    )

    assert verdict.clarification == "Что должна вернуть функция для пустого списка?"


def test_critic_fails_closed_on_invalid_json() -> None:
    broker = FakeBroker(lambda prompt, purpose: "not json")

    with pytest.raises(CriticError, match="invalid verdict"):
        HermesTestCritic(broker=broker).review(_context(), _suite())


def test_critic_fails_closed_when_broker_fails() -> None:
    def fail(prompt: str, purpose: str) -> str:
        raise BrokerProtocolError("unavailable")

    with pytest.raises(CriticError, match="broker failed"):
        HermesTestCritic(broker=FakeBroker(fail)).review(_context(), _suite())
