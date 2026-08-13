import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

_TOKEN = re.compile(r"[0-9]{6,12}:[A-Za-z0-9_-]{30,80}")
_MAX_RESPONSE_BYTES = 1_000_000


class TelegramApiError(RuntimeError):
    pass


class TelegramHttpTransport:
    def __init__(
        self,
        token: str,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("Telegram bot token format is invalid")
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._opener = opener

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        if (
            isinstance(chat_id, bool)
            or not isinstance(chat_id, int)
            or chat_id == 0
            or not isinstance(text, str)
            or not text.strip()
            or "\x00" in text
            or len(text) > 4_000
        ):
            raise ValueError("Telegram message is invalid")
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            _validate_reply_markup(reply_markup)
            payload["reply_markup"] = reply_markup
        result = self._call("sendMessage", payload, timeout=35)
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
            raise TelegramApiError("Telegram API returned an invalid message")
        return message_id

    def get_updates(self, *, offset: int | None = None) -> list[dict[str, Any]]:
        if offset is not None and (
            isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
        ):
            raise ValueError("Telegram update offset is invalid")
        payload: dict[str, Any] = {
            "timeout": 30,
            "limit": 100,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout=35)
        if not isinstance(result, list) or len(result) > 100:
            raise TelegramApiError("Telegram API returned an invalid update batch")
        updates = []
        for update in result:
            if not isinstance(update, dict):
                raise TelegramApiError("Telegram API returned an invalid update")
            update_id = update.get("update_id")
            if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
                raise TelegramApiError("Telegram API returned an invalid update")
            updates.append(update)
        return updates

    def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        if (
            not isinstance(callback_query_id, str)
            or not callback_query_id
            or len(callback_query_id) > 200
            or "\x00" in callback_query_id
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > 200
            or "\x00" in text
        ):
            raise ValueError("Telegram callback answer is invalid")
        self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
            timeout=35,
        )

    def _call(self, method: str, payload: dict[str, Any], *, timeout: int) -> Any:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            raise TelegramApiError("Telegram API request failed") from error
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise TelegramApiError("Telegram API response is too large")
        try:
            envelope = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TelegramApiError("Telegram API returned invalid JSON") from error
        if not isinstance(envelope, dict) or envelope.get("ok") is not True:
            raise TelegramApiError("Telegram API request failed")
        if "result" not in envelope:
            raise TelegramApiError("Telegram API response has no result")
        return envelope["result"]


def _validate_reply_markup(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"inline_keyboard"}:
        raise ValueError("Telegram reply markup is invalid")
    rows = value["inline_keyboard"]
    if not isinstance(rows, list) or not rows or len(rows) > 4:
        raise ValueError("Telegram reply markup is invalid")
    for row in rows:
        if not isinstance(row, list) or not row or len(row) > 3:
            raise ValueError("Telegram reply markup is invalid")
        for button in row:
            if not isinstance(button, dict) or set(button) != {"text", "callback_data"}:
                raise ValueError("Telegram reply markup is invalid")
            text = button["text"]
            data = button["callback_data"]
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > 64
                or not isinstance(data, str)
                or not data
                or len(data.encode("utf-8")) > 64
            ):
                raise ValueError("Telegram reply markup is invalid")
