from dataclasses import dataclass
from typing import Protocol

from bridge.domain import task_branch_name
from bridge.models import Task
from bridge.store import SQLiteStore


class YonoteGateway(Protocol):
    def list_tasks(self) -> list[Task]: ...

    def update_fields(self, row_id: str, fields: dict[str, str]) -> None: ...


class GitHubGateway(Protocol):
    def ensure_branch(self, branch: str) -> str: ...


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    in_progress_status_id: str
    assignee_id: str


class BridgeService:
    def __init__(
        self,
        settings: BridgeSettings,
        yonote: YonoteGateway,
        github: GitHubGateway,
        store: SQLiteStore,
    ) -> None:
        self.settings = settings
        self.yonote = yonote
        self.github = github
        self.store = store

    def backfill_task_ids(self) -> None:
        tasks = sorted(self.yonote.list_tasks(), key=lambda task: task.created_at)
        for task in tasks:
            if self.settings.assignee_id not in task.assignee_ids:
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
            if self.settings.assignee_id not in task.assignee_ids:
                continue
            if task.branch_url:
                continue

            mapping = self.store.get_or_allocate(task.row_id, task.task_id)
            if mapping.branch_name is None:
                mapping = self.store.reserve_branch(
                    task.row_id,
                    task_branch_name(mapping.task_id, task.title),
                )
            assert mapping.branch_name is not None
            branch_name = mapping.branch_name
            branch_url = mapping.branch_url or self.github.ensure_branch(branch_name)
            if not mapping.branch_url:
                self.store.record_branch(task.row_id, branch_name, branch_url)
            self.yonote.update_fields(
                task.row_id,
                {"task_id": mapping.task_id, "branch_url": branch_url},
            )

    def _register_existing_task_ids(self, tasks: list[Task]) -> None:
        for task in tasks:
            if self.settings.assignee_id not in task.assignee_ids:
                continue
            if task.task_id:
                self.store.get_or_allocate(task.row_id, task.task_id)
