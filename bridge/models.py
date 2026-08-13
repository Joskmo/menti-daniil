from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Task:
    row_id: str
    title: str
    project: str | None
    status_id: str | None
    assignee_ids: tuple[str, ...]
    task_id: str | None
    branch_url: str | None
    pr_url: str | None
    created_at: str
    description: str = ""
