import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from grader.store import GraderStore, MentorNotification

_TASK_ID = re.compile(r"PY-[0-9]{3,}")
_NONCE = re.compile(r"[A-Za-z0-9_-]{16,64}")


class TelegramTransport(Protocol):
    def send_message(self, chat_id: int, text: str) -> int: ...


@dataclass(frozen=True, slots=True)
class Conversation:
    chat_id: int
    user_id: int
    task_id: str
    nonce: str
    revision: int
    question: str
    prompt_message_id: int
    state: str


class MentiStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_updates (
                    update_id INTEGER PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    chat_id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    task_id TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    question TEXT NOT NULL,
                    prompt_message_id INTEGER NOT NULL,
                    state TEXT NOT NULL
                );
                """
            )
        self.path.chmod(0o600)

    def is_processed(self, update_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM processed_updates WHERE update_id = ?", (update_id,)
            ).fetchone()
        return row is not None

    def mark_processed(self, update_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO processed_updates (update_id) VALUES (?)",
                (update_id,),
            )

    def activate(self, conversation: Conversation) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO conversations (
                    chat_id, user_id, task_id, nonce, revision, question,
                    prompt_message_id, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
                ON CONFLICT(chat_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    task_id = excluded.task_id,
                    nonce = excluded.nonce,
                    revision = excluded.revision,
                    question = excluded.question,
                    prompt_message_id = excluded.prompt_message_id,
                    state = 'active'
                """,
                (
                    conversation.chat_id,
                    conversation.user_id,
                    conversation.task_id,
                    conversation.nonce,
                    conversation.revision,
                    conversation.question,
                    conversation.prompt_message_id,
                ),
            )

    def get(self, chat_id: int) -> Conversation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return None if row is None else Conversation(**dict(row))

    def pause(self, chat_id: int, user_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE conversations SET state = 'paused'
                WHERE chat_id = ? AND user_id = ? AND state = 'active'
                """,
                (chat_id, user_id),
            )
        return cursor.rowcount == 1

    def clear_exact(self, conversation: Conversation) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM conversations
                WHERE chat_id = ? AND user_id = ? AND nonce = ? AND revision = ?
                  AND prompt_message_id = ?
                """,
                (
                    conversation.chat_id,
                    conversation.user_id,
                    conversation.nonce,
                    conversation.revision,
                    conversation.prompt_message_id,
                ),
            )
        return cursor.rowcount == 1


class MentiBot:
    def __init__(
        self,
        *,
        grader_store: GraderStore,
        state_store: MentiStateStore,
        transport: TelegramTransport,
        allowed_chat_id: int,
        allowed_user_id: int,
    ) -> None:
        self.grader_store = grader_store
        self.state_store = state_store
        self.transport = transport
        self.allowed_chat_id = allowed_chat_id
        self.allowed_user_id = allowed_user_id

    def sync_mentor_notification(self) -> str:
        notification = self.grader_store.claim_next_mentor_notification()
        if notification is None:
            return "idle"
        try:
            text = _format_mentor_notification(notification)
            self.transport.send_message(self.allowed_chat_id, text)
        except Exception:
            self.grader_store.release_mentor_notification(
                notification.notification_id,
                notification.lease_token,
            )
            raise
        if not self.grader_store.mark_mentor_notification_sent(
            notification.notification_id,
            notification.lease_token,
        ):
            raise RuntimeError("mentor notification lease was lost after delivery")
        return "sent"

    def sync_pending_clarification(self) -> str:
        clarification = self.grader_store.next_pending_clarification()
        if clarification is None:
            return "idle"
        current = self.state_store.get(self.allowed_chat_id)
        if (
            current is not None
            and current.nonce == clarification.nonce
            and current.revision == clarification.revision
        ):
            return "waiting"
        self.send_clarification(
            task_id=clarification.task_id,
            nonce=clarification.nonce,
            revision=clarification.revision,
            question=clarification.question,
        )
        return "sent"

    def send_clarification(
        self,
        *,
        task_id: str,
        nonce: str,
        revision: int,
        question: str,
    ) -> int:
        if (
            not _TASK_ID.fullmatch(task_id)
            or not _NONCE.fullmatch(nonce)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision <= 0
            or not isinstance(question, str)
            or not question.strip()
            or len(question.encode("utf-8")) > 2_000
        ):
            raise ValueError("clarification notification is invalid")
        text = f"{task_id} — нужно уточнение\n\n{question.strip()}\n\nОтветь reply-сообщением."
        prompt_message_id = self.transport.send_message(self.allowed_chat_id, text)
        self.state_store.activate(
            Conversation(
                chat_id=self.allowed_chat_id,
                user_id=self.allowed_user_id,
                task_id=task_id,
                nonce=nonce,
                revision=revision,
                question=question.strip(),
                prompt_message_id=prompt_message_id,
                state="active",
            )
        )
        return prompt_message_id

    def handle_update(self, update: dict[str, Any]) -> str:
        update_id = update.get("update_id")
        if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
            return "invalid"
        if self.state_store.is_processed(update_id):
            return "duplicate"
        message = update.get("message")
        if not isinstance(message, dict) or not self._authorized(message):
            self.state_store.mark_processed(update_id)
            return "ignored"
        chat_id = self.allowed_chat_id
        text = message.get("text")
        if text == "/cancel":
            self.state_store.pause(chat_id, self.allowed_user_id)
            self.state_store.mark_processed(update_id)
            self.transport.send_message(chat_id, "Диалог поставлен на паузу. /start — продолжить.")
            return "paused"
        conversation = self.state_store.get(chat_id)
        if text == "/start":
            self.state_store.mark_processed(update_id)
            if conversation is None:
                self.transport.send_message(chat_id, "Сейчас нет вопроса, ожидающего ответа.")
                return "idle"
            self.send_clarification(
                task_id=conversation.task_id,
                nonce=conversation.nonce,
                revision=conversation.revision,
                question=conversation.question,
            )
            return "resumed"
        if conversation is None or conversation.state != "active":
            self.state_store.mark_processed(update_id)
            return "expired"
        reply = message.get("reply_to_message")
        reply_id = reply.get("message_id") if isinstance(reply, dict) else None
        if reply_id != conversation.prompt_message_id:
            self.state_store.mark_processed(update_id)
            self.transport.send_message(
                chat_id,
                "Это устаревший вопрос. Ответь на последнее сообщение Menti.",
            )
            return "stale"
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 1_000:
            self.state_store.mark_processed(update_id)
            self.transport.send_message(chat_id, "Нужен короткий текстовый ответ до 1000 символов.")
            return "invalid-answer"
        accepted = self.grader_store.answer_clarification(
            conversation.nonce,
            conversation.revision,
            text.strip(),
        )
        self.state_store.mark_processed(update_id)
        if not accepted:
            self.state_store.clear_exact(conversation)
            self.transport.send_message(chat_id, "Этот вопрос уже закрыт или устарел.")
            return "expired"
        self.state_store.clear_exact(conversation)
        self.transport.send_message(chat_id, f"Ответ для {conversation.task_id} принят.")
        return "answered"

    def _authorized(self, message: dict[str, Any]) -> bool:
        chat = message.get("chat")
        sender = message.get("from")
        return (
            isinstance(chat, dict)
            and isinstance(sender, dict)
            and chat.get("id") == self.allowed_chat_id
            and chat.get("type") == "private"
            and sender.get("id") == self.allowed_user_id
        )


def _format_mentor_notification(notification: MentorNotification) -> str:
    try:
        report = json.loads(notification.report_json)
    except json.JSONDecodeError as error:
        raise RuntimeError("mentor notification report is invalid") from error
    if not isinstance(report, dict) or set(report) != {
        "passed",
        "total",
        "failed_rubrics",
    }:
        raise RuntimeError("mentor notification report fields are invalid")
    passed = report["passed"]
    total = report["total"]
    failed = report["failed_rubrics"]
    if (
        isinstance(passed, bool)
        or isinstance(total, bool)
        or not isinstance(passed, int)
        or not isinstance(total, int)
        or not 0 <= passed <= total
        or not isinstance(failed, list)
    ):
        raise RuntimeError("mentor notification report values are invalid")
    lines = [
        f"{notification.task_id} — hidden grade",
        f"Пройдено: {passed}/{total}",
    ]
    if failed:
        lines.append("Не пройдены категории поведения:")
        for item in failed:
            if not isinstance(item, dict) or not isinstance(item.get("description"), str):
                raise RuntimeError("mentor notification rubric is invalid")
            description = " ".join(item["description"].split())
            candidate = f"• {description}"
            if len("\n".join([*lines, candidate])) > 3_400:
                lines.append("• …")
                break
            lines.append(candidate)
    else:
        lines.append("Все категории поведения пройдены.")
    lines.extend(
        [
            f"Commit: {notification.commit_sha[:12]}",
            f"Попытка доставки: {notification.attempts}",
        ]
    )
    return "\n".join(lines)
