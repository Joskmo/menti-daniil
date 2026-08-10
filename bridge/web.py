import json
import logging
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

from bridge.github_webhook import DeliveryInProgressError

_LOG = logging.getLogger("menti-github-bridge.web")


class GitHubWebhook(Protocol):
    def handle(
        self, event: str, delivery_id: str, signature: str, payload: bytes
    ) -> bool: ...


class Reconciler(Protocol):
    def reconcile_once(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: bytes
    content_type: str = "application/json"


class WebApplication:
    def __init__(
        self,
        github_webhook: GitHubWebhook,
        reconciler: Reconciler,
        yonote_path_secret: str,
    ) -> None:
        self.github_webhook = github_webhook
        self.reconciler = reconciler
        self.yonote_path_secret = yonote_path_secret

    def handle(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> Response:
        if method == "GET" and path == "/healthz":
            return Response(200, b'{"status":"ok"}')

        if method == "POST" and path == "/webhooks/github":
            try:
                accepted = self.github_webhook.handle(
                    headers["x-github-event"],
                    headers["x-github-delivery"],
                    headers["x-hub-signature-256"],
                    body,
                )
            except KeyError:
                return self._json(400, {"error": "missing GitHub headers"})
            except PermissionError:
                return self._json(401, {"error": "invalid signature"})
            except DeliveryInProgressError:
                return self._json(503, {"error": "delivery already processing"})
            except Exception:
                _LOG.exception("GitHub webhook processing failed; persisted for retry")
                return self._json(503, {"error": "delivery persisted for retry"})
            return self._json(202, {"accepted": accepted})

        yonote_path = f"/webhooks/yonote/{self.yonote_path_secret}"
        if method == "POST" and path == yonote_path:
            self.reconciler.reconcile_once()
            return self._json(200, {"accepted": True})

        return self._json(404, {"error": "not found"})

    @staticmethod
    def _json(status: int, value: dict) -> Response:
        return Response(status, json.dumps(value, separators=(",", ":")).encode())


class StoppableServer(Protocol):
    def serve_forever(self) -> None: ...

    def shutdown(self) -> None: ...

    def server_close(self) -> None: ...


def create_server(app: WebApplication, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self._dispatch(b"")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_048_576:
                self.send_error(413)
                return
            self._dispatch(self.rfile.read(length))

        def _dispatch(self, body: bytes) -> None:
            headers = {key.lower(): value for key, value in self.headers.items()}
            response = app.handle(self.command, self.path, headers, body)
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = False
    server.block_on_close = True
    return server


def serve_until_stopped(server: StoppableServer, stop: threading.Event) -> None:
    serve_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="http-server",
    )
    serve_thread.start()
    try:
        stop.wait()
    finally:
        server.shutdown()
        serve_thread.join()
        server.server_close()
