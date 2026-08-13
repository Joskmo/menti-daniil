import json
from dataclasses import dataclass

from grader.author import AssignmentContext, SourceFile
from grader.contracts import SuiteDraft
from grader.coordinator import AcceptanceResult, AuthoringCoordinator
from grader.critic import CriticIssue, CriticVerdict
from grader.mentor_proposal import MentorProposal
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


class FakeProposalAuthor:
    def __init__(self) -> None:
        self.revisions: list[str | None] = []

    def create(
        self,
        context: AssignmentContext,
        suite: SuiteDraft,
        *,
        critic_summary: str,
        mentor_revision: str | None = None,
    ) -> MentorProposal:
        self.revisions.append(mentor_revision)
        return MentorProposal(
            interpretation="Вернуть следующий ID.",
            criteria=("Корректный следующий ID.",),
            decisions=(),
            test_plan=("Основной сценарий.",),
            reference_approach="Найти максимальный ID.",
            reference_solution="def next_id(rows): return max(row['id'] for row in rows) + 1",
            critic_summary=critic_summary,
        )


def _coordinator(
    *,
    store: GraderStore,
    vault: SuiteVault,
    author,
    critic,
    gate,
    max_author_attempts: int = 3,
    proposal_author=None,
):
    return AuthoringCoordinator(
        store=store,
        vault=vault,
        source_loader=FakeSourceLoader(),
        author=author,
        critic=critic,
        proposal_author=proposal_author or FakeProposalAuthor(),
        acceptance_gate=gate,
        max_author_attempts=max_author_attempts,
    )


def _approved() -> CriticVerdict:
    return CriticVerdict("approved", "Соответствует условию.", None, ())


def test_coordinator_waits_for_mentor_then_freezes_only_approved_exact_draft(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    vault = SuiteVault(tmp_path / "vault")
    _enqueue(store)
    gate = FakeAcceptanceGate(AcceptanceResult(True, "accepted"))
    coordinator = _coordinator(
        store=store,
        vault=vault,
        author=FakeAuthor(_ready_suite()),
        critic=FakeCritic(_approved()),
        gate=gate,
    )

    assert coordinator.process_once() == "awaiting_mentor_approval"
    review = store.next_pending_mentor_review()
    assert review is not None
    assert store.get_authoring("PY-002").state == "awaiting_mentor_approval"
    assert gate.calls == 0
    assert not (tmp_path / "vault" / "PY-002").exists()

    assert store.approve_mentor_review(
        review.task_id, review.version, review.draft_hash, "test:mentor"
    )
    assert coordinator.process_once() == "ready"

    job = store.get_authoring("PY-002")
    assert job.state == "ready"
    assert vault.load("PY-002").suite_hash == job.suite_hash
    assert job.suite_hash == review.draft_hash
    assert gate.calls == 1


def test_coordinator_routes_author_clarification_without_calling_critic(tmp_path) -> None:
    store = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    _enqueue(store)
    critic = FakeCritic(_approved())
    coordinator = _coordinator(
        store=store,
        vault=SuiteVault(tmp_path / "vault"),
        author=FakeAuthor(_clarification_suite()),
        critic=critic,
        gate=FakeAcceptanceGate(AcceptanceResult(True, "accepted")),
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
    coordinator = _coordinator(
        store=store,
        vault=SuiteVault(tmp_path / "vault"),
        author=author,
        critic=FakeCritic(rejected),
        gate=FakeAcceptanceGate(AcceptanceResult(True, "accepted")),
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
