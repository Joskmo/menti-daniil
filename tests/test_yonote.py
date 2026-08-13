from bridge.yonote import YonoteClient, YonoteConfig


class FakeTransport:
    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def post(self, method: str, payload: dict) -> dict:
        self.calls.append((method, payload))
        return self.responses[method]


def config() -> YonoteConfig:
    return YonoteConfig(
        board_id="board",
        status_property_id="status-prop",
        assignee_property_id="assignee-prop",
        project_property_id="project-prop",
        task_id_property_id="task-id-prop",
        branch_property_id="branch-prop",
        pr_property_id="pr-prop",
    )


def test_list_tasks_maps_yonote_row_values() -> None:
    transport = FakeTransport(
        {
            "documents.list": {
                "data": [
                    {
                        "id": "row-1",
                        "title": "Пустой JSON",
                        "text": "Программа должна корректно обрабатывать пустой JSON.",
                        "createdAt": "2026-08-09T12:00:00Z",
                        "values": {
                            "status-prop": ["planned"],
                            "assignee-prop": ["daniil"],
                            "project-prop": "json",
                            "task-id-prop": "PY-001",
                            "branch-prop": {
                                "title": "Task branch",
                                "url": "https://github.test/tree/task/PY-001-pustoy-json",
                            },
                            "pr-prop": {
                                "title": "Pull request",
                                "url": "https://github.test/pull/1",
                            },
                        },
                    }
                ]
            }
        }
    )

    tasks = YonoteClient(config(), transport).list_tasks()

    assert len(tasks) == 1
    assert tasks[0].row_id == "row-1"
    assert tasks[0].description == "Программа должна корректно обрабатывать пустой JSON."
    assert tasks[0].status_id == "planned"
    assert tasks[0].assignee_ids == ("daniil",)
    assert tasks[0].project == "json"
    assert tasks[0].task_id == "PY-001"
    assert tasks[0].branch_url == "https://github.test/tree/task/PY-001-pustoy-json"
    assert tasks[0].pr_url == "https://github.test/pull/1"


def test_list_tasks_tolerates_legacy_and_malformed_url_values() -> None:
    transport = FakeTransport(
        {
            "documents.list": {
                "data": [
                    {
                        "id": "legacy-row",
                        "title": "Legacy",
                        "createdAt": "",
                        "values": {
                            "branch-prop": "https://github.test/tree/legacy",
                            "pr-prop": {"title": "Missing URL"},
                        },
                    },
                    {
                        "id": "malformed-row",
                        "title": "Malformed",
                        "createdAt": "",
                        "values": {
                            "project-prop": {"unexpected": "json"},
                            "branch-prop": {"url": 42},
                            "pr-prop": ["unexpected"],
                        },
                    },
                ]
            }
        }
    )

    legacy, malformed = YonoteClient(config(), transport).list_tasks()

    assert legacy.branch_url == "https://github.test/tree/legacy"
    assert legacy.pr_url is None
    assert malformed.project is None
    assert malformed.branch_url is None
    assert malformed.pr_url is None


def test_update_fields_uses_property_level_atomic_changes() -> None:
    transport = FakeTransport({"v2/database/transaction": {"ok": True}})
    client = YonoteClient(config(), transport)

    client.update_fields(
        "row-1",
        {
            "task_id": "PY-001",
            "branch_url": "https://github.test/tree/task/PY-001-pustoy-json",
            "pr_url": "https://github.test/pull/1",
        },
    )

    assert transport.calls == [
        (
            "v2/database/transaction",
            {
                "board": [
                    {
                        "path": "rows.row-1.values.task-id-prop",
                        "op": "add",
                        "val": "PY-001",
                    },
                    {
                        "path": "rows.row-1.values.branch-prop",
                        "op": "add",
                        "val": {
                            "title": "https://github.test/tree/task/PY-001-pustoy-json",
                            "url": "https://github.test/tree/task/PY-001-pustoy-json",
                        },
                    },
                    {
                        "path": "rows.row-1.values.pr-prop",
                        "op": "add",
                        "val": {
                            "title": "https://github.test/pull/1",
                            "url": "https://github.test/pull/1",
                        },
                    },
                ]
            },
        )
    ]
