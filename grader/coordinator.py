import json
from dataclasses import dataclass
from typing import Protocol

from grader.author import AssignmentContext, SourceFile
from grader.contracts import SuiteDraft
from grader.critic import CriticVerdict
from grader.store import AuthoringJob, GraderStore, SuiteVault


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    passed: bool
    reason: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.passed, bool)
            or not isinstance(self.reason, str)
            or not self.reason.strip()
            or len(self.reason) > 500
            or "\x00" in self.reason
        ):
            raise ValueError("invalid acceptance result")


class SourceLoader(Protocol):
    def load(
        self,
        project: str,
        branch_name: str,
        starter_sha: str,
    ) -> tuple[SourceFile, ...]: ...

    def load_execution(
        self,
        project: str,
        branch_name: str,
        starter_sha: str,
    ) -> tuple[SourceFile, ...]: ...


class TestAuthor(Protocol):
    def create(
        self,
        context: AssignmentContext,
        *,
        critic_feedback: dict | None = None,
    ) -> SuiteDraft: ...


class TestCritic(Protocol):
    def review(
        self,
        context: AssignmentContext,
        suite: SuiteDraft,
    ) -> CriticVerdict: ...


class SuiteAcceptanceGate(Protocol):
    def validate(
        self,
        context: AssignmentContext,
        suite: SuiteDraft,
    ) -> AcceptanceResult: ...


class AuthoringCoordinator:
    def __init__(
        self,
        *,
        store: GraderStore,
        vault: SuiteVault,
        source_loader: SourceLoader,
        author: TestAuthor,
        critic: TestCritic,
        acceptance_gate: SuiteAcceptanceGate,
        max_author_attempts: int = 3,
        author_model: str = "gpt-5.6-sol",
    ) -> None:
        if not 1 <= max_author_attempts <= 5:
            raise ValueError("max_author_attempts must be from 1 to 5")
        self.store = store
        self.vault = vault
        self.source_loader = source_loader
        self.author = author
        self.critic = critic
        self.acceptance_gate = acceptance_gate
        self.max_author_attempts = max_author_attempts
        self.author_model = author_model

    def process_once(self) -> str | None:
        job = self.store.claim_next_authoring()
        if job is None:
            return None
        if job.lease_token is None:
            raise RuntimeError("claimed authoring job has no fencing token")
        try:
            context = self._context(job)
            feedback = (
                json.loads(job.critic_feedback_json)
                if job.critic_feedback_json is not None
                else None
            )
            if feedback is not None and not isinstance(feedback, dict):
                raise RuntimeError("stored critic feedback is invalid")
            suite = self.author.create(context, critic_feedback=feedback)
            if suite.status == "clarification_required":
                assert suite.clarification is not None
                self.store.request_clarification(
                    job.task_id,
                    job.lease_token,
                    suite.clarification,
                )
                return "needs_clarification"

            verdict = self.critic.review(context, suite)
            if verdict.status == "clarification_required":
                assert verdict.clarification is not None
                self.store.request_clarification(
                    job.task_id,
                    job.lease_token,
                    verdict.clarification,
                )
                return "needs_clarification"
            if verdict.status == "rejected":
                return self._reject_or_retry(job, verdict.to_payload(), "critic-rejected")

            acceptance = self.acceptance_gate.validate(context, suite)
            if not acceptance.passed:
                feedback_payload = {
                    "schema_version": 1,
                    "status": "rejected",
                    "summary": "Deterministic acceptance gate rejected the suite.",
                    "clarification": None,
                    "issues": [
                        {
                            "code": "acceptance-gate",
                            "severity": "blocker",
                            "message": acceptance.reason,
                            "case_ids": [],
                        }
                    ],
                }
                return self._reject_or_retry(
                    job,
                    feedback_payload,
                    "acceptance-rejected",
                )

            self.store.finalize_authoring(
                job.task_id,
                job.lease_token,
                lambda: self.vault.freeze(
                    task_id=job.task_id,
                    starter_sha=job.starter_sha,
                    suite_payload=suite.to_payload(),
                    author_model=self.author_model,
                ),
            )
            return "ready"
        except Exception:
            if job.attempts >= self.max_author_attempts:
                self.store.mark_authoring_failed(
                    job.task_id,
                    job.lease_token,
                    "authoring-error",
                )
            else:
                self.store.release_authoring(job.task_id, job.lease_token)
            raise

    def _reject_or_retry(
        self,
        job: AuthoringJob,
        feedback: dict,
        error_code: str,
    ) -> str:
        assert job.lease_token is not None
        if job.attempts >= self.max_author_attempts:
            if not self.store.mark_authoring_failed(
                job.task_id,
                job.lease_token,
                error_code,
            ):
                raise RuntimeError("authoring lease was lost before failure transition")
            return "failed"
        self.store.requeue_after_critic(
            job.task_id,
            job.lease_token,
            json.dumps(feedback, ensure_ascii=False, sort_keys=True, allow_nan=False),
        )
        return "queued"

    def _context(self, job: AuthoringJob) -> AssignmentContext:
        try:
            assignment = json.loads(job.assignment_json)
        except json.JSONDecodeError as error:
            raise RuntimeError("stored assignment is invalid") from error
        if not isinstance(assignment, dict) or set(assignment) != {"title", "description"}:
            raise RuntimeError("stored assignment fields are invalid")
        title = assignment["title"]
        description = assignment["description"]
        if not isinstance(title, str) or not isinstance(description, str):
            raise RuntimeError("stored assignment text is invalid")
        if job.clarification_answer is not None:
            description = (
                f"{description}\n\n"
                "Ответ ментора на продуктовое уточнение:\n"
                f"{job.clarification_answer}"
            )
        source_files = self.source_loader.load(
            job.project,
            job.branch_name,
            job.starter_sha,
        )
        load_execution = getattr(self.source_loader, "load_execution", None)
        execution_files = (
            load_execution(job.project, job.branch_name, job.starter_sha)
            if load_execution is not None
            else source_files
        )
        return AssignmentContext(
            task_id=job.task_id,
            project=job.project,
            title=title,
            description=description,
            source_files=source_files,
            execution_files=execution_files,
        )
