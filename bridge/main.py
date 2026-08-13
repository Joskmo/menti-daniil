import logging
import signal
import threading

from bridge.config import Settings
from bridge.github_git import GitBranchClient
from bridge.github_webhook import GitHubApiPullRequestLookup, GitHubWebhookHandler
from bridge.schema import DatabaseProperty, YonoteSchemaManager
from bridge.service import BridgeService, BridgeSettings
from bridge.store import SQLiteStore
from bridge.web import WebApplication, create_server, serve_until_stopped
from bridge.yonote import HttpTransport, YonoteClient, YonoteConfig
from grader.gateway import SQLiteGraderGateway
from grader.store import GraderStore

_LOG = logging.getLogger("menti-github-bridge")


def _create_webhook(
    settings: Settings,
    store: SQLiteStore,
    yonote: YonoteClient,
    grader: SQLiteGraderGateway | None,
) -> GitHubWebhookHandler:
    return GitHubWebhookHandler(
        settings.github_webhook_secret,
        settings.repository,
        settings.base_branch,
        settings.review_status_id,
        settings.done_status_id,
        settings.in_progress_status_id,
        store,
        yonote,
        GitHubApiPullRequestLookup(),
        grader,
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings.from_env()
    transport = HttpTransport(settings.yonote_base_url, settings.yonote_api_key)
    schema = YonoteSchemaManager(settings.board_id, transport)
    schema.ensure(
        [
            DatabaseProperty(settings.project_property_id, "Проект", "text", 2, 160),
            DatabaseProperty(settings.task_id_property_id, "ID задачи", "text", 4, 130),
            DatabaseProperty(settings.branch_property_id, "GitHub branch", "url", 5, 240),
            DatabaseProperty(settings.pr_property_id, "Pull request", "url", 6, 240),
        ]
    )

    yonote = YonoteClient(
        YonoteConfig(
            board_id=settings.board_id,
            status_property_id=settings.status_property_id,
            assignee_property_id=settings.assignee_property_id,
            project_property_id=settings.project_property_id,
            task_id_property_id=settings.task_id_property_id,
            branch_property_id=settings.branch_property_id,
            pr_property_id=settings.pr_property_id,
        ),
        transport,
    )
    store = SQLiteStore(settings.database_path)
    grader = (
        SQLiteGraderGateway(GraderStore(settings.grader_database_path))
        if settings.grader_database_path is not None
        else None
    )
    github = GitBranchClient(
        repository_ssh=settings.repository_ssh,
        repository_web=settings.repository_web,
        base_branch=settings.base_branch,
        git_dir=settings.git_dir,
        ssh_key=settings.ssh_key,
        known_hosts=settings.known_hosts,
    )
    service = BridgeService(
        BridgeSettings(
            settings.in_progress_status_id,
            settings.assignee_id,
            settings.mentor_assignee_id,
        ),
        yonote,
        github,
        store,
        grader,
    )
    webhook = _create_webhook(settings, store, yonote, grader)
    app = WebApplication(webhook, service, settings.yonote_webhook_path_secret)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    poller = threading.Thread(
        target=_poll_forever,
        args=(service, settings.poll_interval, stop),
        daemon=True,
        name="yonote-poller",
    )
    poller.start()
    webhook_retrier = threading.Thread(
        target=_retry_webhooks_forever,
        args=(webhook, settings.poll_interval, stop),
        daemon=True,
        name="github-webhook-retrier",
    )
    webhook_retrier.start()
    server = create_server(app, settings.host, settings.port)
    _LOG.info("bridge listening on %s:%s", settings.host, settings.port)
    serve_until_stopped(server, stop)
    poller.join(timeout=settings.poll_interval + 1)
    webhook_retrier.join(timeout=31)


def _retry_webhooks_forever(
    webhook: GitHubWebhookHandler,
    interval: int,
    stop: threading.Event,
) -> None:
    while not stop.is_set():
        try:
            processed = webhook.retry_pending_once()
        except Exception:
            _LOG.exception("Persisted GitHub webhook retry failed")
            stop.wait(interval)
            continue
        if not processed:
            stop.wait(interval)


def _poll_forever(service: BridgeService, interval: int, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            service.reconcile_once()
        except Exception:
            _LOG.exception("Yonote reconciliation failed")
        stop.wait(interval)


if __name__ == "__main__":
    main()
