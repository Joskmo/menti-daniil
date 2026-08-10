import hashlib
import hmac
import threading
import urllib.request

from bridge.github_webhook import GitHubWebhookHandler
from bridge.store import SQLiteStore
from bridge.web import WebApplication, create_server, serve_until_stopped


class FakeGitHubWebhook:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, bytes]] = []

    def handle(
        self, event: str, delivery_id: str, signature: str, payload: bytes
    ) -> bool:
        self.calls.append((event, delivery_id, signature, payload))
        return True


class BlockingGitHubWebhook:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def handle(
        self, event: str, delivery_id: str, signature: str, payload: bytes
    ) -> bool:
        self.started.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test did not release webhook")
        return True


class FakeService:
    def __init__(self) -> None:
        self.reconciliations = 0
        self.updates: list[tuple[str, dict[str, str]]] = []

    def reconcile_once(self) -> None:
        self.reconciliations += 1

    def update_fields(self, row_id: str, fields: dict[str, str]) -> None:
        self.updates.append((row_id, fields))


def test_health_endpoint() -> None:
    app = WebApplication(FakeGitHubWebhook(), FakeService(), "yonote-secret")

    response = app.handle("GET", "/healthz", {}, b"")

    assert response.status == 200
    assert response.body == b'{"status":"ok"}'


def test_github_endpoint_forwards_signed_payload() -> None:
    github = FakeGitHubWebhook()
    app = WebApplication(github, FakeService(), "yonote-secret")

    response = app.handle(
        "POST",
        "/webhooks/github",
        {
            "x-github-event": "pull_request",
            "x-github-delivery": "delivery-1",
            "x-hub-signature-256": "sha256=abc",
        },
        b"{}",
    )

    assert response.status == 202
    assert github.calls == [("pull_request", "delivery-1", "sha256=abc", b"{}")]


def test_busy_github_delivery_returns_retryable_status(tmp_path) -> None:
    secret = "test-secret"
    payload = b"{}"
    store = SQLiteStore(tmp_path / "bridge.db")
    assert store.claim_delivery("busy-delivery").state == "claimed"
    handler = GitHubWebhookHandler(
        secret=secret,
        repository="Joskmo/menti-daniil",
        base_branch="main",
        review_status_id="review",
        done_status_id="done",
        in_progress_status_id="in-progress",
        store=store,
        yonote=FakeService(),
    )
    app = WebApplication(handler, FakeService(), "yonote-secret")
    signature = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()

    response = app.handle(
        "POST",
        "/webhooks/github",
        {
            "x-github-event": "ping",
            "x-github-delivery": "busy-delivery",
            "x-hub-signature-256": signature,
        },
        payload,
    )

    assert response.status == 503


def test_yonote_secret_path_triggers_reconciliation() -> None:
    service = FakeService()
    app = WebApplication(FakeGitHubWebhook(), service, "yonote-secret")

    response = app.handle("POST", "/webhooks/yonote/yonote-secret", {}, b"{}")

    assert response.status == 200
    assert service.reconciliations == 1


def test_created_server_drains_non_daemon_request_threads() -> None:
    app = WebApplication(FakeGitHubWebhook(), FakeService(), "yonote-secret")
    server = create_server(app, "127.0.0.1", 0)

    try:
        assert not server.daemon_threads
        assert server.block_on_close
    finally:
        server.server_close()


def test_shutdown_waits_for_active_webhook_request() -> None:
    webhook = BlockingGitHubWebhook()
    app = WebApplication(webhook, FakeService(), "yonote-secret")
    server = create_server(app, "127.0.0.1", 0)
    stop = threading.Event()
    coordinator = threading.Thread(target=serve_until_stopped, args=(server, stop))
    response_status: list[int] = []

    def post_webhook() -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/webhooks/github",
            data=b"{}",
            headers={
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": "delivery-drain",
                "X-Hub-Signature-256": "sha256=test",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:  # noqa: S310
            response_status.append(response.status)

    coordinator.start()
    client = threading.Thread(target=post_webhook)
    client.start()
    assert webhook.started.wait(timeout=1)

    stop.set()
    coordinator.join(timeout=0.1)
    assert coordinator.is_alive()

    webhook.release.set()
    client.join(timeout=1)
    coordinator.join(timeout=1)

    assert not client.is_alive()
    assert not coordinator.is_alive()
    assert response_status == [202]


class FakeServer:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.shutdown_requested = threading.Event()
        self.serve_thread_id: int | None = None
        self.shutdown_thread_id: int | None = None
        self.closed = False

    def serve_forever(self) -> None:
        self.serve_thread_id = threading.get_ident()
        self.started.set()
        self.shutdown_requested.wait()

    def shutdown(self) -> None:
        self.shutdown_thread_id = threading.get_ident()
        self.shutdown_requested.set()

    def server_close(self) -> None:
        self.closed = True


def test_serve_until_stopped_shuts_down_from_control_thread() -> None:
    server = FakeServer()
    stop = threading.Event()
    coordinator = threading.Thread(target=serve_until_stopped, args=(server, stop))

    coordinator.start()
    assert server.started.wait(timeout=1)
    stop.set()
    coordinator.join(timeout=1)

    assert not coordinator.is_alive()
    assert server.shutdown_thread_id != server.serve_thread_id
    assert server.closed
