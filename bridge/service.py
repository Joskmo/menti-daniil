from dataclasses import dataclass, replace
from typing import Protocol

from bridge.domain import project_directory_name, task_branch_name
from bridge.models import Task
from bridge.store import SQLiteStore


class YonoteGateway(Protocol):
    def list_tasks(self) -> list[Task]: ...

    def update_fields(self, row_id: str, fields: dict[str, str]) -> None: ...


class GitHubGateway(Protocol):
    def prepare_branch(
        self,
        branch: str,
        project_directory: str,
    ) -> str: ...

    def ensure_prepared_branch(self, branch: str, starter_sha: str) -> str: ...


class GraderGateway(Protocol):
    def ensure_suite(self, task: Task, branch_name: str, starter_sha: str) -> str: ...


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    in_progress_status_id: str
    assignee_id: str
    mentor_assignee_id: str | None = None

    def accepts_assignee(self, assignee_ids: tuple[str, ...]) -> bool:
        return self.assignee_id in assignee_ids or (
            self.mentor_assignee_id is not None
            and self.mentor_assignee_id in assignee_ids
        )


class BridgeService:
    def __init__(
        self,
        settings: BridgeSettings,
        yonote: YonoteGateway,
        github: GitHubGateway,
        store: SQLiteStore,
        grader: GraderGateway | None = None,
    ) -> None:
        self.settings = settings
        self.yonote = yonote
        self.github = github
        self.store = store
        self.grader = grader

    def backfill_task_ids(self) -> None:
        tasks = sorted(self.yonote.list_tasks(), key=lambda task: task.created_at)
        for task in tasks:
            if not self.settings.accepts_assignee(task.assignee_ids):
                continue
            mapping = self.store.get_or_allocate(task.row_id, task.task_id)
            if not task.task_id:
                self.yonote.update_fields(task.row_id, {"task_id": mapping.task_id})

    def reconcile_once(self) -> None:
        tasks = sorted(self.yonote.list_tasks(), key=lambda task: task.created_at)
        self._register_existing_task_ids(tasks)
        for task in tasks:
            if task.status_id != self.settings.in_progress_status_id:
                continue
            if not self.settings.accepts_assignee(task.assignee_ids):
                continue
            mapping = self.store.find_by_row(task.row_id)
            if task.branch_url:
                if mapping is None or mapping.branch_name is None:
                    continue
                if mapping.project_name is None:
                    if not task.project:
                        continue
                    try:
                        project_directory = project_directory_name(task.project)
                    except ValueError:
                        continue
                    mapping = self.store.reserve_branch(
                        task.row_id,
                        mapping.branch_name,
                        project_directory,
                    )
                if mapping.starter_sha is None:
                    continue
                assert mapping.branch_name is not None
                if self.grader is not None and self.settings.assignee_id in task.assignee_ids:
                    assert mapping.project_name is not None
                    grader_task = replace(
                        task,
                        task_id=mapping.task_id,
                        project=mapping.project_name,
                    )
                    self.grader.ensure_suite(
                        grader_task,
                        mapping.branch_name,
                        mapping.starter_sha,
                    )
                continue
            has_reserved_intent = bool(
                mapping and mapping.branch_name and mapping.project_name
            )
            if not has_reserved_intent:
                if not task.project:
                    continue
                try:
                    project_directory = project_directory_name(task.project)
                except ValueError:
                    continue
                mapping = mapping or self.store.get_or_allocate(
                    task.row_id,
                    task.task_id,
                )
                mapping = self.store.reserve_branch(
                    task.row_id,
                    mapping.branch_name
                    or task_branch_name(mapping.task_id, task.title),
                    project_directory,
                )
            assert mapping is not None
            assert mapping.branch_name is not None
            assert mapping.project_name is not None
            branch_name = mapping.branch_name
            if mapping.starter_sha is None:
                starter_sha = self.github.prepare_branch(
                    branch_name,
                    mapping.project_name,
                )
                mapping = self.store.record_starter_sha(task.row_id, starter_sha)
            assert mapping.starter_sha is not None
            branch_url = mapping.branch_url or self.github.ensure_prepared_branch(
                branch_name,
                mapping.starter_sha,
            )
            if not mapping.branch_url:
                mapping = self.store.record_branch(
                    task.row_id,
                    branch_name,
                    branch_url,
                )
            if self.grader is not None and self.settings.assignee_id in task.assignee_ids:
                assert mapping.starter_sha is not None
                grader_task = replace(
                    task,
                    task_id=mapping.task_id,
                    project=mapping.project_name,
                )
                suite_state = self.grader.ensure_suite(
                    grader_task,
                    branch_name,
                    mapping.starter_sha,
                )
                if suite_state != "ready":
                    continue
            self.yonote.update_fields(
                task.row_id,
                {"task_id": mapping.task_id, "branch_url": branch_url},
            )

    def _register_existing_task_ids(self, tasks: list[Task]) -> None:
        for task in tasks:
            if not self.settings.accepts_assignee(task.assignee_ids):
                continue
            if task.task_id:
                self.store.get_or_allocate(task.row_id, task.task_id)
