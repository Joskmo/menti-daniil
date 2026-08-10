from bridge.schema import DatabaseProperty, YonoteSchemaManager


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post(self, method: str, payload: dict) -> dict:
        self.calls.append((method, payload))
        if method == "documents.info":
            return {"data": {"properties": {"title": {"id": "title"}}}}
        return {"ok": True}


def test_schema_manager_adds_missing_property_to_database_and_default_view() -> None:
    transport = FakeTransport()
    manager = YonoteSchemaManager("board", transport)

    manager.ensure(
        [DatabaseProperty(id="task-prop", title="ID задачи", type="text", order=4)]
    )

    assert transport.calls[-1] == (
        "v2/database/transaction",
        {
            "board": [
                {
                    "path": "databases.board.properties.task-prop",
                    "op": "add",
                    "val": {
                        "id": "task-prop",
                        "v": 1,
                        "title": "ID задачи",
                        "type": "text",
                        "config": {},
                        "o": 4,
                        "visible": "alwaysShow",
                    },
                },
                {
                    "path": "views.board.viewProperties.task-prop",
                    "op": "add",
                    "val": {
                        "id": "task-prop",
                        "visible": True,
                        "viewConfig": {
                            "align": "left",
                            "width": 180,
                            "shareLinkedDocuments": False,
                            "showDocumentIcon": True,
                        },
                        "o": 4,
                        "v": 1,
                    },
                },
            ]
        },
    )
