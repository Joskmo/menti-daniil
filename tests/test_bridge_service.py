import sqlite3
from dataclasses import replace

import pytest

from bridge.models import Task
from bridge.service import BridgeService, BridgeSettings
from bridge.store import SQLiteStore


class FakeYonote:
    def __init__(self, tasks: list[Task]) -> None:
        self.tasks = {task.row_id: task for task in tasks}
        self.updates: list[tuple[str, dict[str, str]]] = []

    def list_tasks(self) -> list[Task]:
        return list(self.tasks.values())

    def update_fields(self, row_id: str, fields: dict[str, str]) -> None:
        self.updates.append((row_id, fields))
        self.tasks[row_id] = replace(
            self.tasks[row_id],
            task_id=fields.get("task_id", self.tasks[row_id].task_id),
            branch_url=fields.get("branch_url", self.tasks[row_id].branch_url),
        )


class FakeGitHub:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.requests: list[tuple[str, str]] = []

    def ensure_branch(self, branch: str, task_title: str) -> str:
        self.created.append(branch)
        self.requests.append((branch, task_title))
        return f"https://github.com/Joskmo/menti-daniil/tree/{branch}"


def test_reconcile_creates_one_branch_and_records_it_idempotently(tmp_path) -> None:
    task = Task(
        row_id="row-1",
        title="Тип данных input()",
        status_id="in-progress",
        assignee_ids=("daniil",),
        task_id=None,
        branch_url=None,
        pr_url=None,
        created_at="2026-08-09T12:00:00Z",
    )
    yonote = FakeYonote([task])
    github = FakeGitHub()
    service = BridgeService(
        settings=BridgeSettings(in_progress_status_id="in-progress", assignee_id="daniil"),
        yonote=yonote,
        github=github,
        store=SQLiteStore(tmp_path / "bridge.db"),
    )

    service.reconcile_once()
    service.reconcile_once()

    assert github.created == ["task/PY-001-tip-dannyh-input"]
    assert github.requests == [
        ("task/PY-001-tip-dannyh-input", "Тип данных input()")
    ]
    assert yonote.updates == [
        (
            "row-1",
            {
                "task_id": "PY-001",
                "branch_url": (
                    "https://github.com/Joskmo/menti-daniil/tree/"
                    "task/PY-001-tip-dannyh-input"
                ),
            },
        )
    ]


def test_reconcile_ignores_tasks_not_in_progress(tmp_path) -> None:
    task = Task(
        row_id="row-1",
        title="Пустой JSON",
        status_id="planned",
        assignee_ids=("daniil",),
        task_id="PY-001",
        branch_url=None,
        pr_url=None,
        created_at="2026-08-09T12:00:00Z",
    )
    yonote = FakeYonote([task])
    github = FakeGitHub()
    service = BridgeService(
        settings=BridgeSettings(in_progress_status_id="in-progress", assignee_id="daniil"),
        yonote=yonote,
        github=github,
        store=SQLiteStore(tmp_path / "bridge.db"),
    )

    service.reconcile_once()

    assert github.created == []
    assert yonote.updates == []


def test_backfill_assigns_stable_ids_oldest_first(tmp_path) -> None:
    newer = Task(
        row_id="row-2",
        title="Новая задача",
        status_id="planned",
        assignee_ids=("daniil",),
        task_id=None,
        branch_url=None,
        pr_url=None,
        created_at="2026-08-09T13:00:00Z",
    )
    older = replace(
        newer,
        row_id="row-1",
        title="Старая задача",
        created_at="2026-08-09T12:00:00Z",
    )
    yonote = FakeYonote([newer, older])
    service = BridgeService(
        settings=BridgeSettings(in_progress_status_id="in-progress", assignee_id="daniil"),
        yonote=yonote,
        github=FakeGitHub(),
        store=SQLiteStore(tmp_path / "bridge.db"),
    )

    service.backfill_task_ids()

    assert yonote.updates == [
        ("row-1", {"task_id": "PY-001"}),
        ("row-2", {"task_id": "PY-002"}),
    ]


def test_reconcile_registers_existing_ids_before_allocating_new_one(tmp_path) -> None:
    existing = Task(
        row_id="row-existing",
        title="Уже запланировано",
        status_id="planned",
        assignee_ids=("daniil",),
        task_id="PY-007",
        branch_url=None,
        pr_url=None,
        created_at="2026-08-09T13:00:00Z",
    )
    active = replace(
        existing,
        row_id="row-active",
        title="Новая активная задача",
        status_id="in-progress",
        task_id=None,
        created_at="2026-08-09T12:00:00Z",
    )
    yonote = FakeYonote([active, existing])
    github = FakeGitHub()
    service = BridgeService(
        settings=BridgeSettings(in_progress_status_id="in-progress", assignee_id="daniil"),
        yonote=yonote,
        github=github,
        store=SQLiteStore(tmp_path / "bridge.db"),
    )

    service.reconcile_once()

    assert github.created == ["task/PY-008-novaya-aktivnaya-zadacha"]
    assert yonote.updates[0][1]["task_id"] == "PY-008"


def test_reconcile_fails_closed_on_duplicate_existing_task_ids(tmp_path) -> None:
    first = Task(
        row_id="row-1",
        title="Первая",
        status_id="planned",
        assignee_ids=("daniil",),
        task_id="PY-001",
        branch_url=None,
        pr_url=None,
        created_at="2026-08-09T12:00:00Z",
    )
    second = replace(first, row_id="row-2", title="Вторая")
    service = BridgeService(
        settings=BridgeSettings(in_progress_status_id="in-progress", assignee_id="daniil"),
        yonote=FakeYonote([first, second]),
        github=FakeGitHub(),
        store=SQLiteStore(tmp_path / "bridge.db"),
    )

    with pytest.raises(sqlite3.IntegrityError):
        service.reconcile_once()


class AmbiguousPushGitHub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def ensure_branch(self, branch: str, task_title: str) -> str:
        self.calls.append(branch)
        if len(self.calls) == 1:
            raise RuntimeError("connection lost after remote accepted push")
        return f"https://github.com/Joskmo/menti-daniil/tree/{branch}"


def test_branch_intent_survives_ambiguous_push_and_title_change(tmp_path) -> None:
    task = Task(
        row_id="row-1",
        title="Первоначальное название",
        status_id="in-progress",
        assignee_ids=("daniil",),
        task_id=None,
        branch_url=None,
        pr_url=None,
        created_at="2026-08-09T12:00:00Z",
    )
    yonote = FakeYonote([task])
    github = AmbiguousPushGitHub()
    store = SQLiteStore(tmp_path / "bridge.db")
    service = BridgeService(
        settings=BridgeSettings(in_progress_status_id="in-progress", assignee_id="daniil"),
        yonote=yonote,
        github=github,
        store=store,
    )

    with pytest.raises(RuntimeError, match="connection lost"):
        service.reconcile_once()
    mapping = store.get_or_allocate("row-1")
    assert mapping.branch_name == "task/PY-001-pervonachalnoe-nazvanie"

    yonote.tasks["row-1"] = replace(task, title="Переименованная задача")
    service.reconcile_once()

    assert github.calls == [
        "task/PY-001-pervonachalnoe-nazvanie",
        "task/PY-001-pervonachalnoe-nazvanie",
    ]
