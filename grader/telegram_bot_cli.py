import os
import signal
import threading
from pathlib import Path
from typing import Any, Protocol

from grader.menti_bot import MentiBot, MentiStateStore
from grader.store import GraderStore
from grader.telegram_api import TelegramApiError, TelegramHttpTransport


class PollingBot(Protocol):
    def sync_mentor_notification(self) -> str: ...

    def sync_pending_clarification(self) -> str: ...

    def handle_update(self, update: dict[str, Any]) -> str: ...


class PollingApi(Protocol):
    def get_updates(self, *, offset: int | None = None) -> list[dict[str, Any]]: ...


def run_cycle(bot: PollingBot, api: PollingApi, *, offset: int | None) -> int | None:
    bot.sync_mentor_notification()
    bot.sync_pending_clarification()
    updates = sorted(api.get_updates(offset=offset), key=lambda item: item["update_id"])
    next_offset = offset
    for update in updates:
        bot.handle_update(update)
        candidate = update["update_id"] + 1
        next_offset = candidate if next_offset is None else max(next_offset, candidate)
    return next_offset


def run(bot: PollingBot, api: PollingApi, stop: threading.Event) -> None:
    offset: int | None = None
    while not stop.is_set():
        try:
            offset = run_cycle(bot, api, offset=offset)
        except TelegramApiError:
            stop.wait(2)


def main() -> int:
    try:
        token = _required("MENTI_TELEGRAM_BOT_TOKEN")
        grader_database = Path(_required("GRADER_DATABASE_PATH"))
        state_database = Path(_required("MENTI_TELEGRAM_STATE_PATH"))
        chat_id = _positive_integer("MENTI_TELEGRAM_CHAT_ID")
        user_id = _positive_integer("MENTI_TELEGRAM_USER_ID")
        api = TelegramHttpTransport(token)
        bot = MentiBot(
            grader_store=GraderStore(grader_database),
            state_store=MentiStateStore(state_database),
            transport=api,
            allowed_chat_id=chat_id,
            allowed_user_id=user_id,
        )
    except (OSError, RuntimeError, ValueError):
        print("Menti bot configuration is invalid")
        return 2
    stop = threading.Event()

    def request_stop(signum, frame) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    run(bot, api, stop)
    return 0


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip() or value != value.strip() or "\x00" in value:
        raise RuntimeError(f"{name} is required")
    return value


def _positive_integer(name: str) -> int:
    value = _required(name)
    if not value.isascii() or not value.isdigit():
        raise ValueError(f"{name} must be a positive integer")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
