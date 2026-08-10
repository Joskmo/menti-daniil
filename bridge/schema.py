from dataclasses import dataclass
from typing import Protocol


class Transport(Protocol):
    def post(self, method: str, payload: dict) -> dict: ...


@dataclass(frozen=True, slots=True)
class DatabaseProperty:
    id: str
    title: str
    type: str
    order: int
    width: int = 180


class YonoteSchemaManager:
    def __init__(self, board_id: str, transport: Transport) -> None:
        self.board_id = board_id
        self.transport = transport

    def ensure(self, desired: list[DatabaseProperty]) -> None:
        info = self.transport.post("documents.info", {"id": self.board_id})
        existing = (info.get("data") or {}).get("properties") or {}
        changes = []
        for prop in desired:
            if prop.id in existing:
                continue
            changes.extend(self._add_property_changes(prop))

        if not changes:
            return
        result = self.transport.post(
            "v2/database/transaction", {self.board_id: changes}
        )
        if not (result.get("ok") or result.get("success")):
            raise RuntimeError(f"Yonote schema transaction failed: {result!r}")

    def _add_property_changes(self, prop: DatabaseProperty) -> list[dict]:
        return [
            {
                "path": f"databases.{self.board_id}.properties.{prop.id}",
                "op": "add",
                "val": {
                    "id": prop.id,
                    "v": 1,
                    "title": prop.title,
                    "type": prop.type,
                    "config": {},
                    "o": prop.order,
                    "visible": "alwaysShow",
                },
            },
            {
                "path": f"views.{self.board_id}.viewProperties.{prop.id}",
                "op": "add",
                "val": {
                    "id": prop.id,
                    "visible": True,
                    "viewConfig": {
                        "align": "left",
                        "width": prop.width,
                        "shareLinkedDocuments": False,
                        "showDocumentIcon": True,
                    },
                    "o": prop.order,
                    "v": 1,
                },
            },
        ]
