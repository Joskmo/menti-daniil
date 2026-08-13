import json

from bridge.models import Task
from grader.store import GraderStore, GradingAttempt


class SuiteNotReadyError(RuntimeError):
    pass


class SQLiteGraderGateway:
    def __init__(self, store: GraderStore) -> None:
        self.store = store

    def ensure_suite(self, task: Task, branch_name: str, starter_sha: str) -> str:
        if task.task_id is None or task.project is None:
            raise ValueError("grader task requires stable task and project identities")
        assignment_json = json.dumps(
            {
                "title": task.title,
                "description": task.description,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        job = self.store.enqueue_authoring(
            task_id=task.task_id,
            row_id=task.row_id,
            project=task.project,
            branch_name=branch_name,
            starter_sha=starter_sha,
            assignment_json=assignment_json,
        )
        return job.state

    def enqueue_commit(
        self,
        task_id: str,
        project: str,
        branch_name: str,
        commit_sha: str,
    ) -> GradingAttempt | None:
        try:
            job = self.store.get_authoring(task_id)
        except KeyError as error:
            raise SuiteNotReadyError("hidden suite identity is not registered yet") from error
        if job.project != project or job.branch_name != branch_name:
            raise ValueError("grading push does not match the immutable authoring job")
        if job.state != "ready" or job.suite_hash is None:
            raise SuiteNotReadyError("hidden suite is not ready")
        return self.store.enqueue_grading(
            task_id=task_id,
            project=project,
            branch_name=branch_name,
            commit_sha=commit_sha,
            suite_hash=job.suite_hash,
        )
