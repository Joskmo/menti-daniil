import json
from dataclasses import dataclass

from grader.author import AssignmentContext, SourceFile
from grader.contracts import SuiteDraft
from grader.coordinator import AcceptanceResult, AuthoringCoordinator
from grader.critic import CriticIssue, CriticVerdict
from grader.store import GraderStore, SuiteVault


def _ready_suite() -> SuiteDraft:
    return SuiteDraft.from_cli_output(
        json.dumps(
            {
                "schema_version": 1,
                "status": "ready",
                "summary": "Проверяет следующий ID.",
                "clarification": None,
                "rubric": [
                    {"id": "next-id", "description": "Корректный ID.", "weight": 1}
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
        )
    )


def _clarification_suite() -> SuiteDraft:
    return SuiteDraft.from_cli_output(
        json.dumps(
            {
                "schema_version": 1,
                "status": "clarification_required",
                "summary": "Не определён пустой список.",
                "clarification": "Что должна вернуть функция для пустого списка?",
                "rubric": [],
                "cases": [],
            }
        )
    )


def _enqueue(store: GraderStore) -> None:
    store.enqueue_authoring(
        task_id="PY-002",
        row_id="row-2",
        project="json",
        branch_name="task/PY-002-next-id",
        starter_sha="a" * 40,
        assignment_json=json.dumps(
            {"title": "Следующий ID", "description": "Вернуть max(id) + 1."}
        ),
    )


class FakeSourceLoader:
    def load(
        self,
        project: str,
        branch_name: str,
        starter_sha: str,
    ) -> tuple[SourceFile, ...]:
        assert (project, branch_name, starter_sha) == (
            "json",
            "task/PY-002-next-id",
            "a" * 40,
        )
        return (SourceFile("main.py", "def next_id(rows): return len(rows) + 1\n"),)


class FakeAuthor:
    def __init__(self, suite: SuiteDraft) -> None:
        self.suite = suite
        self.feedback: list[dict | None] = []

    def create(
        self,
        context: AssignmentContext,
        *,
        critic_feedback: dict | None = None,
    ) -> SuiteDraft:
        self.feedback.append(critic_feedback)
        return self.suite


@dataclass
class FakeCritic:
    verdict: CriticVerdict
    calls: int = 0

    def review(self, context: AssignmentContext, suite: SuiteDraft) -> CriticVerdict:
        self.calls += 1
        return self.verdict


class FakeAcceptanceGate:
    def __init__(self, result: AcceptanceResult) -> None:
        self.result = result
        self.calls = 0

    def validate(self, context: AssignmentContext, suite: SuiteDraft) -> AcceptanceResult:
        self.calls += 1
        return self.result


def _approved() -> CriticVerdict:
    return CriticVerdict("approved", "Соответствует условию.", None, ())


def test_coordinator_freezes_only_critic_and_acceptance_approved_suite(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    vault = SuiteVault(tmp_path / "vault")
    _enqueue(store)
    gate = FakeAcceptanceGate(AcceptanceResult(True, "accepted"))
    coordinator = AuthoringCoordinator(
        store=store,
        vault=vault,
        source_loader=FakeSourceLoader(),
        author=FakeAuthor(_ready_suite()),
        critic=FakeCritic(_approved()),
        acceptance_gate=gate,
        max_author_attempts=3,
    )

    assert coordinator.process_once() == "ready"

    job = store.get_authoring("PY-002")
    assert job.state == "ready"
    assert vault.load("PY-002").suite_hash == job.suite_hash
    assert gate.calls == 1


def test_coordinator_routes_author_clarification_without_calling_critic(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    _enqueue(store)
    critic = FakeCritic(_approved())
    coordinator = AuthoringCoordinator(
        store=store,
        vault=SuiteVault(tmp_path / "vault"),
        source_loader=FakeSourceLoader(),
        author=FakeAuthor(_clarification_suite()),
        critic=critic,
        acceptance_gate=FakeAcceptanceGate(AcceptanceResult(True, "accepted")),
    )

    assert coordinator.process_once() == "needs_clarification"

    job = store.get_authoring("PY-002")
    assert job.state == "needs_clarification"
    assert job.clarification_question == "Что должна вернуть функция для пустого списка?"
    assert critic.calls == 0


def test_coordinator_requeues_critic_feedback_then_fails_at_bounded_limit(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    _enqueue(store)
    rejected = CriticVerdict(
        "rejected",
        "Не покрыт пустой список.",
        None,
        (
            CriticIssue(
                "missing-empty-case",
                "blocker",
                "Нет проверки пустого списка.",
                (),
            ),
        ),
    )
    author = FakeAuthor(_ready_suite())
    coordinator = AuthoringCoordinator(
        store=store,
        vault=SuiteVault(tmp_path / "vault"),
        source_loader=FakeSourceLoader(),
        author=author,
        critic=FakeCritic(rejected),
        acceptance_gate=FakeAcceptanceGate(AcceptanceResult(True, "accepted")),
        max_author_attempts=2,
    )

    assert coordinator.process_once() == "queued"
    assert coordinator.process_once() == "failed"

    job = store.get_authoring("PY-002")
    assert job.state == "failed"
    assert job.attempts == 2
    assert author.feedback[0] is None
    assert author.feedback[1] is not None
    assert author.feedback[1]["status"] == "rejected"
