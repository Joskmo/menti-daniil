import json
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bridge.models import Task


def _url_value(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        url = value.get("url")
        return url if isinstance(url, str) else None
    return None


class Transport(Protocol):
    def post(self, method: str, payload: dict) -> dict: ...


class HttpTransport:
    def __init__(self, base_url: str, token: str, timeout: float = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def post(self, method: str, payload: dict) -> dict:
        request = Request(
            f"{self.base_url}/api/{method}",
            method="POST",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            message = f"Yonote {method} failed with HTTP {error.code}: {detail}"
            raise RuntimeError(message) from error


@dataclass(frozen=True, slots=True)
class YonoteConfig:
    board_id: str
    status_property_id: str
    assignee_property_id: str
    task_id_property_id: str
    branch_property_id: str
    pr_property_id: str


class YonoteClient:
    def __init__(self, config: YonoteConfig, transport: Transport) -> None:
        self.config = config
        self.transport = transport

    def list_tasks(self) -> list[Task]:
        rows: list[dict] = []
        offset = 0
        while True:
            body = self.transport.post(
                "documents.list",
                {
                    "parentDocumentId": self.config.board_id,
                    "type": ["row"],
                    "limit": 100,
                    "offset": offset,
                },
            )
            page = body.get("data") or []
            rows.extend(page)
            if len(page) < 100:
                break
            offset += len(page)

        return [self._task(row) for row in rows]

    def update_fields(self, row_id: str, fields: dict[str, str]) -> None:
        property_ids = {
            "task_id": self.config.task_id_property_id,
            "branch_url": self.config.branch_property_id,
            "pr_url": self.config.pr_property_id,
        }
        changes = []
        for name, value in fields.items():
            if name == "status_id":
                changes.append(
                    {
                        "path": f"rows.{row_id}.values.{self.config.status_property_id}",
                        "op": "update",
                        "val": [value],
                    }
                )
                continue
            property_id = property_ids[name]
            property_value: str | dict[str, str] = value
            if name in {"branch_url", "pr_url"}:
                property_value = {"title": value, "url": value}
            changes.append(
                {
                    "path": f"rows.{row_id}.values.{property_id}",
                    "op": "add",
                    "val": property_value,
                }
            )

        body = self.transport.post(
            "v2/database/transaction", {self.config.board_id: changes}
        )
        if not (body.get("ok") or body.get("success")):
            raise RuntimeError(f"Yonote transaction failed: {body!r}")

    def _task(self, row: dict) -> Task:
        values = row.get("values") or {}
        status_values = values.get(self.config.status_property_id) or []
        assignee_values = values.get(self.config.assignee_property_id) or []
        return Task(
            row_id=row["id"],
            title=row.get("title") or "Без названия",
            status_id=status_values[0] if status_values else None,
            assignee_ids=tuple(assignee_values),
            task_id=values.get(self.config.task_id_property_id),
            branch_url=_url_value(values.get(self.config.branch_property_id)),
            pr_url=_url_value(values.get(self.config.pr_property_id)),
            created_at=row.get("createdAt") or "",
        )
