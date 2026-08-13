from types import SimpleNamespace

from bridge.main import _create_webhook
from bridge.store import SQLiteStore


def test_startup_wires_opt_in_grader_into_github_webhook(tmp_path) -> None:
    settings = SimpleNamespace(
        github_webhook_secret="secret",
        repository="Joskmo/menti-daniil",
        base_branch="main",
        review_status_id="review",
        done_status_id="done",
        in_progress_status_id="in-progress",
    )
    store = SQLiteStore(tmp_path / "bridge.db")
    yonote = object()
    grader = object()

    webhook = _create_webhook(settings, store, yonote, grader)

    assert webhook.grader is grader
