import io
import json

import pytest

from grader.telegram_api import TelegramApiError, TelegramHttpTransport

SYNTHETIC_TOKEN = "123456789:" + ("synthetic_" * 4)


class FakeResponse:
    def __init__(self, payload) -> None:
        self.stream = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def read(self, size: int) -> bytes:
        return self.stream.read(size)


class FakeOpener:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        return FakeResponse(self.responses.pop(0))


def test_telegram_transport_sends_plain_text_and_returns_message_id() -> None:
    opener = FakeOpener([{"ok": True, "result": {"message_id": 123}}])
    transport = TelegramHttpTransport(SYNTHETIC_TOKEN, opener=opener)

    assert transport.send_message(42, "Пройдено 7 из 10 проверок") == 123

    request, timeout = opener.requests[0]
    body = json.loads(request.data)
    assert body == {
        "chat_id": 42,
        "text": "Пройдено 7 из 10 проверок",
        "disable_web_page_preview": True,
    }
    assert timeout == 35


def test_telegram_transport_returns_bounded_update_batch() -> None:
    updates = [{"update_id": 8, "message": {"text": "/start"}}]
    opener = FakeOpener([{"ok": True, "result": updates}])
    transport = TelegramHttpTransport(SYNTHETIC_TOKEN, opener=opener)

    assert transport.get_updates(offset=8) == updates

    request, _ = opener.requests[0]
    assert json.loads(request.data) == {
        "offset": 8,
        "timeout": 30,
        "limit": 100,
        "allowed_updates": ["message", "callback_query"],
    }


def test_telegram_transport_uses_generic_error_without_token() -> None:
    token = SYNTHETIC_TOKEN
    opener = FakeOpener([{"ok": False, "description": f"bad {token}"}])
    transport = TelegramHttpTransport(token, opener=opener)

    with pytest.raises(TelegramApiError, match="Telegram API request failed") as raised:
        transport.send_message(42, "hello")

    assert token not in str(raised.value)
