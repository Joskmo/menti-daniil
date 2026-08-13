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

    def prepare_branch(
        self,
        branch: str,
        project_directory: str,
    ) -> str:
        self.created.append(branch)
        self.requests.append((branch, project_directory))
        return "a" * 40

    def ensure_prepared_branch(self, branch: str, starter_sha: str) -> str:
        assert branch in self.created
        assert starter_sha == "a" * 40
        return f"https://github.com/Joskmo/menti-daniil/tree/{branch}"


class FakeGrader:
    def __init__(self, state: str = "queued") -> None:
        self.state = state
        self.requests: list[tuple[Task, str, str]] = []

    def ensure_suite(self, task: Task, branch_name: str, starter_sha: str) -> str:
        self.requests.append((task, branch_name, starter_sha))
        return self.state


def test_learner_branch_url_is_withheld_until_hidden_suite_is_ready(tmp_path) -> None:
    task = Task(
        row_id="row-learner",
        title="Исправить ID",
        project="json",
        status_id="in-progress",
        assignee_ids=("daniil",),
        task_id=None,
        branch_url=None,
        pr_url=None,
        created_at="2026-08-10T12:00:00Z",
        description="Вернуть максимальный ID плюс один.",
    )
    yonote = FakeYonote([task])
    github = FakeGitHub()
    grader = FakeGrader()
    service = BridgeService(
        settings=BridgeSettings(in_progress_status_id="in-progress", assignee_id="daniil"),
        yonote=yonote,
        github=github,
        store=SQLiteStore(tmp_path / "bridge.db"),
        grader=grader,
    )

    service.reconcile_once()

    assert github.created == ["task/PY-001-ispravit-id"]
    assert yonote.updates == []
    assert grader.requests[0][0].description == "Вернуть максимальный ID плюс один."
    assert grader.requests[0][2] == "a" * 40

    grader.state = "ready"
    service.reconcile_once()

    assert github.created == ["task/PY-001-ispravit-id"]
    assert yonote.updates == [
        (
            "row-learner",
            {
                "task_id": "PY-001",
                "branch_url": (
                    "https://github.com/Joskmo/menti-daniil/tree/task/PY-001-ispravit-id"
                ),
            },
        )
    ]


def test_reconcile_creates_one_branch_and_records_it_idempotently(tmp_path) -> None:
    task = Task(
        row_id="row-1",
        title="Тип данных input()",
        project="json",
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
    assert github.requests == [("task/PY-001-tip-dannyh-input", "json")]
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


def test_existing_branch_with_confirmed_starter_reconciles_grader_registration(
    tmp_path,
) -> None:
    branch = "task/PY-001-sushchestvuyushchaya-zadacha"
    branch_url = f"https://github.com/Joskmo/menti-daniil/tree/{branch}"
    task = Task(
        row_id="row-legacy",
        title="Существующая задача",
        project="json",
        status_id="in-progress",
        assignee_ids=("daniil",),
        task_id="PY-001",
        branch_url=branch_url,
        pr_url=None,
        created_at="2026-08-09T12:00:00Z",
        description="Исправить существующее решение.",
    )
    yonote = FakeYonote([task])
    github = FakeGitHub()
    grader = FakeGrader("ready")
    store = SQLiteStore(tmp_path / "bridge.db")
    store.get_or_allocate("row-legacy", "PY-001")
    store.reserve_branch("row-legacy", branch, "json")
    store.record_branch("row-legacy", branch, branch_url)
    store.record_starter_sha("row-legacy", "a" * 40)
    service = BridgeService(
        settings=BridgeSettings(in_progress_status_id="in-progress", assignee_id="daniil"),
        yonote=yonote,
        github=github,
        store=store,
        grader=grader,
    )

    service.reconcile_once()

    assert github.created == []
    assert grader.requests == [(task, branch, "a" * 40)]
    assert yonote.updates == []


def test_existing_branch_without_confirmed_starter_is_not_guessed(tmp_path) -> None:
    branch = "task/PY-001-existing"
    task = Task(
        row_id="row-legacy",
        title="Существующая задача",
        project="json",
        status_id="in-progress",
        assignee_ids=("daniil",),
        task_id="PY-001",
        branch_url=f"https://github.com/Joskmo/menti-daniil/tree/{branch}",
        pr_url=None,
        created_at="2026-08-09T12:00:00Z",
    )
    store = SQLiteStore(tmp_path / "bridge.db")
    store.get_or_allocate("row-legacy", "PY-001")
    store.reserve_branch("row-legacy", branch, "json")
    grader = FakeGrader("ready")
    github = FakeGitHub()
    service = BridgeService(
        settings=BridgeSettings(in_progress_status_id="in-progress", assignee_id="daniil"),
        yonote=FakeYonote([task]),
        github=github,
        store=store,
        grader=grader,
    )

    service.reconcile_once()

    assert github.created == []
    assert grader.requests == []
    mapping = store.find_by_row("row-legacy")
    assert mapping is not None and mapping.starter_sha is None


def test_reconcile_creates_branch_for_mentor_assignee(tmp_path) -> None:
    task = Task(
        row_id="row-mentor",
        title="Подготовить ошибочный JSON-код",
        project="json",
        status_id="in-progress",
        assignee_ids=("arseny",),
        task_id=None,
        branch_url=None,
        pr_url=None,
        created_at="2026-08-10T12:00:00Z",
    )
    yonote = FakeYonote([task])
    github = FakeGitHub()
    service = BridgeService(
        settings=BridgeSettings(
            in_progress_status_id="in-progress",
            assignee_id="daniil",
            mentor_assignee_id="arseny",
        ),
        yonote=yonote,
        github=github,
        store=SQLiteStore(tmp_path / "bridge.db"),
    )

    service.reconcile_once()

    assert github.created == ["task/PY-001-podgotovit-oshibochnyy-json-kod"]
    assert yonote.updates == [
        (
            "row-mentor",
            {
                "task_id": "PY-001",
                "branch_url": (
                    "https://github.com/Joskmo/menti-daniil/tree/"
                    "task/PY-001-podgotovit-oshibochnyy-json-kod"
                ),
            },
        )
    ]


def test_reconcile_ignores_tasks_not_in_progress(tmp_path) -> None:
    task = Task(
        row_id="row-1",
        title="Пустой JSON",
        project="json",
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


def test_invalid_project_does_not_block_other_tasks(tmp_path) -> None:
    invalid = Task(
        row_id="row-invalid",
        title="Некорректный проект",
        project="../",
        status_id="in-progress",
        assignee_ids=("daniil",),
        task_id=None,
        branch_url=None,
        pr_url=None,
        created_at="2026-08-09T12:00:00Z",
    )
    valid = replace(
        invalid,
        row_id="row-valid",
        title="Корректная задача",
        project="json",
        created_at="2026-08-09T13:00:00Z",
    )
    yonote = FakeYonote([invalid, valid])
    github = FakeGitHub()
    service = BridgeService(
        settings=BridgeSettings(in_progress_status_id="in-progress", assignee_id="daniil"),
        yonote=yonote,
        github=github,
        store=SQLiteStore(tmp_path / "bridge.db"),
    )

    service.reconcile_once()

    assert github.requests == [("task/PY-001-korrektnaya-zadacha", "json")]
    assert yonote.updates == [
        (
            "row-valid",
            {
                "task_id": "PY-001",
                "branch_url": (
                    "https://github.com/Joskmo/menti-daniil/tree/"
                    "task/PY-001-korrektnaya-zadacha"
                ),
            },
        )
    ]


def test_backfill_assigns_stable_ids_oldest_first(tmp_path) -> None:
    newer = Task(
        row_id="row-2",
        title="Новая задача",
        project="json",
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
        project="json",
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
        project="json",
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
        project="json",
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
        self.calls: list[tuple[str, str]] = []

    def prepare_branch(
        self,
        branch: str,
        project_directory: str,
    ) -> str:
        return "a" * 40

    def ensure_prepared_branch(self, branch: str, starter_sha: str) -> str:
        self.calls.append((branch, starter_sha))
        if len(self.calls) == 1:
            raise RuntimeError("connection lost after remote accepted push")
        return f"https://github.com/Joskmo/menti-daniil/tree/{branch}"


def test_branch_intent_survives_ambiguous_push_and_card_edits(tmp_path) -> None:
    task = Task(
        row_id="row-1",
        title="Первоначальное название",
        project="json",
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
    assert mapping.project_name == "json"

    yonote.tasks["row-1"] = replace(
        task,
        title="Переименованная задача",
        project=None,
    )
    service.reconcile_once()

    assert github.calls == [
        ("task/PY-001-pervonachalnoe-nazvanie", "a" * 40),
        ("task/PY-001-pervonachalnoe-nazvanie", "a" * 40),
    ]
