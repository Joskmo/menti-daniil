import json

import pytest

from grader.author import AssignmentContext, SourceFile
from grader.contracts import SuiteDraft
from grader.mentor_proposal import (
    HermesMentorProposalAuthor,
    MentorProposalContractError,
)


class FakeBroker:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[tuple[str, str]] = []

    def complete(self, prompt: str, *, purpose: str) -> str:
        self.calls.append((prompt, purpose))
        return self.output


def _context() -> AssignmentContext:
    return AssignmentContext(
        task_id="PY-002",
        project="json",
        title="Следующий ID",
        description="Вернуть max(id) + 1.",
        source_files=(SourceFile("main.py", "def next_id(rows): return len(rows) + 1\n"),),
    )


def _suite() -> SuiteDraft:
    return SuiteDraft.from_cli_output(
        json.dumps(
            {
                "schema_version": 1,
                "status": "ready",
                "summary": "Проверяет вычисление следующего ID.",
                "clarification": None,
                "rubric": [
                    {
                        "id": "next-id",
                        "description": "Следующий ID вычисляется корректно.",
                        "weight": 1,
                    }
                ],
                "cases": [
                    {
                        "id": "unsorted",
                        "rubric_id": "next-id",
                        "adapter": "python_call",
                        "target": "main:next_id",
                        "input": {"args": [[{"id": 8}, {"id": 2}]], "kwargs": {}, "files": []},
                        "expect": {"return": 9, "exception": None, "stdout": None, "files": []},
                    }
                ],
            },
            ensure_ascii=False,
        )
    )


def _proposal_payload() -> dict:
    return {
        "schema_version": 1,
        "interpretation": "Функция возвращает следующий идентификатор без изменения входа.",
        "criteria": ["Корректно работает для непустого списка."],
        "decisions": [
            {
                "question": "Что возвращать для пустого списка?",
                "options": ["Вернуть 1.", "Вернуть 0.", "Выбросить исключение."],
                "recommended_option": 0,
                "reason": "Единица согласуется с нумерацией идентификаторов.",
            }
        ],
        "test_plan": ["Основной сценарий.", "Пустой список.", "Произвольный порядок."],
        "reference_approach": "Найти максимальный id с безопасным значением по умолчанию.",
        "reference_solution": (
            "def next_id(rows):\n"
            "    return max((row['id'] for row in rows), default=0) + 1"
        ),
        "critic_summary": "Набор покрывает заявленное поведение.",
    }


def test_proposal_author_returns_strict_bounded_mentor_review_without_raw_cases() -> None:
    broker = FakeBroker(json.dumps(_proposal_payload(), ensure_ascii=False))

    proposal = HermesMentorProposalAuthor(broker=broker).create(
        _context(),
        _suite(),
        critic_summary="Соответствует условию.",
    )

    assert proposal.interpretation.startswith("Функция возвращает")
    assert proposal.decisions[0].recommended_option == 0
    assert "return 9" not in json.dumps(proposal.to_payload(), ensure_ascii=False)
    assert broker.calls[0][1] == "mentor-proposal"
    assert "BEGIN_UNTRUSTED_HIDDEN_SUITE" in broker.calls[0][0]
    assert "Never reveal exact hidden inputs" in broker.calls[0][0]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(extra="unexpected"),
        lambda payload: payload["decisions"][0].update(recommended_option=9),
        lambda payload: payload.update(criteria=[]),
        lambda payload: payload.update(reference_solution=""),
    ],
)
def test_proposal_author_rejects_invalid_or_ambiguous_contract(mutate) -> None:
    payload = _proposal_payload()
    mutate(payload)

    with pytest.raises(MentorProposalContractError):
        HermesMentorProposalAuthor(
            broker=FakeBroker(json.dumps(payload, ensure_ascii=False))
        ).create(_context(), _suite(), critic_summary="OK")


def test_proposal_author_includes_prior_mentor_revision_as_untrusted_data() -> None:
    broker = FakeBroker(json.dumps(_proposal_payload(), ensure_ascii=False))

    HermesMentorProposalAuthor(broker=broker).create(
        _context(),
        _suite(),
        critic_summary="OK",
        mentor_revision="Для пустого списка вернуть 1; не менять вход.",
    )

    prompt = broker.calls[0][0]
    assert "BEGIN_UNTRUSTED_MENTOR_REVISION" in prompt
    assert "Для пустого списка вернуть 1" in prompt
