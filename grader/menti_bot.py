import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from grader.mentor_proposal import MentorProposal
from grader.store import FeedbackNotification, GraderStore, MentorNotification, MentorReview

_TASK_ID = re.compile(r"PY-[0-9]{3,}")
_NONCE = re.compile(r"[A-Za-z0-9_-]{16,64}")
_HASH = re.compile(r"[0-9a-f]{64}")
_CALLBACK = re.compile(
    r"review:(approve|revise|options|solution|pause|cancel):([A-Za-z0-9_-]{16,32})"
)


class TelegramTransport(Protocol):
    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> int: ...

    def answer_callback_query(self, callback_query_id: str, text: str) -> None: ...


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
    kind: str = "clarification"
    draft_hash: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewAction:
    token: str
    chat_id: int
    user_id: int
    task_id: str
    version: int
    draft_hash: str
    message_id: int
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
                    state TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'clarification',
                    draft_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS review_actions (
                    token TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    task_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    draft_hash TEXT NOT NULL,
                    message_id INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL,
                    UNIQUE(task_id, version, draft_hash)
                );
                """
            )
            conversation_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(conversations)")
            }
            for column, declaration in (
                ("kind", "TEXT NOT NULL DEFAULT 'clarification'"),
                ("draft_hash", "TEXT"),
            ):
                if column not in conversation_columns:
                    connection.execute(
                        f"ALTER TABLE conversations ADD COLUMN {column} {declaration}"
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
                    prompt_message_id, state, kind, draft_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    task_id = excluded.task_id,
                    nonce = excluded.nonce,
                    revision = excluded.revision,
                    question = excluded.question,
                    prompt_message_id = excluded.prompt_message_id,
                    state = 'active',
                    kind = excluded.kind,
                    draft_hash = excluded.draft_hash
                """,
                (
                    conversation.chat_id,
                    conversation.user_id,
                    conversation.task_id,
                    conversation.nonce,
                    conversation.revision,
                    conversation.question,
                    conversation.prompt_message_id,
                    conversation.kind,
                    conversation.draft_hash,
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

    def prepare_review(
        self,
        review: MentorReview,
        *,
        chat_id: int,
        user_id: int,
    ) -> ReviewAction:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM review_actions
                WHERE task_id = ? AND version = ? AND draft_hash = ?
                  AND state IN ('preparing', 'active')
                """,
                (review.task_id, review.version, review.draft_hash),
            ).fetchone()
            if existing is not None:
                return ReviewAction(**dict(existing))
            connection.execute(
                """
                UPDATE review_actions SET state = 'expired'
                WHERE task_id = ? AND state IN ('preparing', 'active')
                """,
                (review.task_id,),
            )
            connection.execute(
                """
                DELETE FROM review_actions
                WHERE task_id = ? AND version = ? AND draft_hash = ?
                  AND state IN ('closed', 'expired')
                """,
                (review.task_id, review.version, review.draft_hash),
            )
            token = secrets.token_urlsafe(12)
            connection.execute(
                """
                INSERT INTO review_actions (
                    token, chat_id, user_id, task_id, version, draft_hash,
                    message_id, state
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 'preparing')
                """,
                (
                    token,
                    chat_id,
                    user_id,
                    review.task_id,
                    review.version,
                    review.draft_hash,
                ),
            )
        return ReviewAction(
            token,
            chat_id,
            user_id,
            review.task_id,
            review.version,
            review.draft_hash,
            0,
            "preparing",
        )

    def bind_review_message(self, token: str, message_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE review_actions SET message_id = ?, state = 'active'
                WHERE token = ? AND state = 'preparing'
                """,
                (message_id, token),
            )
            return cursor.rowcount == 1

    def review_action(self, token: str) -> ReviewAction | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_actions WHERE token = ?", (token,)
            ).fetchone()
        return None if row is None else ReviewAction(**dict(row))

    def close_review(self, token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE review_actions SET state = 'closed'
                WHERE token = ? AND state = 'active'
                """,
                (token,),
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

    def sync_feedback_notification(self) -> str:
        notification = self.grader_store.claim_next_feedback_notification()
        if notification is None:
            return "idle"
        try:
            self.transport.send_message(
                self.allowed_chat_id,
                _format_feedback_notification(notification),
            )
        except Exception:
            self.grader_store.release_feedback_notification(
                notification.notification_id,
                notification.lease_token,
            )
            raise
        if not self.grader_store.mark_feedback_notification_sent(
            notification.notification_id,
            notification.lease_token,
        ):
            raise RuntimeError("feedback notification lease was lost after delivery")
        return "sent"

    def sync_pending_clarification(self) -> str:
        clarification = self.grader_store.next_pending_clarification()
        if clarification is None:
            return "idle"
        current = self.state_store.get(self.allowed_chat_id)
        if current is not None and current.kind == "mentor_revision" and current.state == "active":
            return "busy"
        if (
            current is not None
            and current.kind == "clarification"
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

    def sync_pending_mentor_review(self) -> str:
        review = self.grader_store.next_pending_mentor_review()
        if review is None:
            return "idle"
        conversation = self.state_store.get(self.allowed_chat_id)
        if conversation is not None and conversation.state == "active":
            return "busy"
        action = self.state_store.prepare_review(
            review,
            chat_id=self.allowed_chat_id,
            user_id=self.allowed_user_id,
        )
        if action.state == "active" and action.message_id > 0:
            return "waiting"
        proposal = MentorProposal.from_output(review.proposal_json)
        text = _format_mentor_review(review, proposal)
        markup = _review_markup(action.token)
        message_id = self.transport.send_message(
            self.allowed_chat_id,
            text,
            reply_markup=markup,
        )
        if not self.state_store.bind_review_message(action.token, message_id):
            raise RuntimeError("mentor review action was lost after delivery")
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
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            result = self._handle_callback(callback)
            self.state_store.mark_processed(update_id)
            return result
        message = update.get("message")
        if not isinstance(message, dict) or not self._authorized_message(message):
            self.state_store.mark_processed(update_id)
            return "ignored"
        result = self._handle_message(message)
        self.state_store.mark_processed(update_id)
        return result

    def _handle_message(self, message: dict[str, Any]) -> str:
        chat_id = self.allowed_chat_id
        text = message.get("text")
        conversation = self.state_store.get(chat_id)
        if text == "/cancel":
            if conversation is not None and conversation.kind == "mentor_revision":
                assert conversation.draft_hash is not None
                self.grader_store.cancel_mentor_revision(
                    conversation.task_id,
                    conversation.revision,
                    conversation.draft_hash,
                )
                self.state_store.clear_exact(conversation)
                self.transport.send_message(chat_id, "Изменение отменено. Proposal снова активен.")
                return "revision-cancelled"
            self.state_store.pause(chat_id, self.allowed_user_id)
            self.transport.send_message(chat_id, "Диалог поставлен на паузу. /start — продолжить.")
            return "paused"
        if isinstance(text, str) and text.startswith("/resume "):
            task_id = text.removeprefix("/resume ").strip().upper()
            if not _TASK_ID.fullmatch(task_id):
                self.transport.send_message(chat_id, "Формат: /resume PY-003")
                return "invalid-resume"
            if not self.grader_store.resume_mentor_review(task_id):
                self.transport.send_message(
                    chat_id, "Нет приостановленного proposal для этой задачи."
                )
                return "expired"
            self.transport.send_message(chat_id, f"{task_id}: proposal снова активен.")
            return "resumed-review"
        if text == "/start":
            if conversation is None:
                self.transport.send_message(chat_id, "Сейчас нет вопроса, ожидающего ответа.")
                return "idle"
            if conversation.kind != "clarification":
                self.transport.send_message(chat_id, "Ответь на актуальный запрос изменений.")
                return "waiting"
            self.send_clarification(
                task_id=conversation.task_id,
                nonce=conversation.nonce,
                revision=conversation.revision,
                question=conversation.question,
            )
            return "resumed"
        if conversation is None or conversation.state != "active":
            return "expired"
        reply = message.get("reply_to_message")
        reply_id = reply.get("message_id") if isinstance(reply, dict) else None
        if reply_id != conversation.prompt_message_id:
            self.transport.send_message(
                chat_id,
                "Это устаревший вопрос. Ответь на последнее сообщение Menti.",
            )
            return "stale"
        maximum = 4_000 if conversation.kind == "mentor_revision" else 1_000
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > maximum:
            self.transport.send_message(
                chat_id,
                f"Нужен текстовый ответ до {maximum} символов.",
            )
            return "invalid-answer"
        if conversation.kind == "mentor_revision":
            assert conversation.draft_hash is not None
            accepted = self.grader_store.request_mentor_revision(
                conversation.task_id,
                conversation.revision,
                conversation.draft_hash,
                text.strip(),
            )
            self.state_store.clear_exact(conversation)
            if not accepted:
                self.transport.send_message(chat_id, "Этот proposal уже закрыт или устарел.")
                return "expired"
            self.transport.send_message(
                chat_id,
                f"Правки для {conversation.task_id} приняты. Готовлю новую версию.",
            )
            return "revised"
        accepted = self.grader_store.answer_clarification(
            conversation.nonce,
            conversation.revision,
            text.strip(),
        )
        if not accepted:
            self.state_store.clear_exact(conversation)
            self.transport.send_message(chat_id, "Этот вопрос уже закрыт или устарел.")
            return "expired"
        self.state_store.clear_exact(conversation)
        self.transport.send_message(chat_id, f"Ответ для {conversation.task_id} принят.")
        return "answered"

    def _handle_callback(self, callback: dict[str, Any]) -> str:
        if not self._authorized_callback(callback):
            return "ignored"
        callback_id = callback.get("id")
        data = callback.get("data")
        message = callback.get("message")
        match = _CALLBACK.fullmatch(data) if isinstance(data, str) else None
        if not isinstance(callback_id, str) or match is None or not isinstance(message, dict):
            return "invalid"
        action_name, token = match.groups()
        action = self.state_store.review_action(token)
        message_id = message.get("message_id")
        if (
            action is None
            or action.state != "active"
            or action.chat_id != self.allowed_chat_id
            or action.user_id != self.allowed_user_id
            or action.message_id != message_id
        ):
            self.transport.answer_callback_query(callback_id, "Эта кнопка уже устарела.")
            return "expired"
        if action_name == "approve":
            accepted = self.grader_store.approve_mentor_review(
                action.task_id,
                action.version,
                action.draft_hash,
                f"telegram:user:{self.allowed_user_id}:chat:{self.allowed_chat_id}",
            )
            self.state_store.close_review(token)
            if not accepted:
                self.transport.answer_callback_query(callback_id, "Proposal уже изменился.")
                return "expired"
            self.transport.answer_callback_query(callback_id, "Утверждено")
            self.transport.send_message(
                self.allowed_chat_id,
                f"{action.task_id}: версия {action.version} утверждена. Запускаю KVM acceptance.",
            )
            return "approved"
        if action_name in {"options", "solution"}:
            try:
                job = self.grader_store.get_authoring(action.task_id)
            except KeyError:
                job = None
            if (
                job is None
                or job.state != "awaiting_mentor_approval"
                or job.mentor_review_version != action.version
                or job.mentor_draft_hash != action.draft_hash
                or job.mentor_proposal_json is None
            ):
                self.state_store.close_review(token)
                self.transport.answer_callback_query(callback_id, "Proposal уже изменился.")
                return "expired"
            proposal = MentorProposal.from_output(job.mentor_proposal_json)
            detail = (
                _format_mentor_options(action.task_id, proposal)
                if action_name == "options"
                else _format_mentor_solution(action.task_id, proposal)
            )
            for chunk in _telegram_chunks(detail):
                self.transport.send_message(self.allowed_chat_id, chunk)
            self.transport.answer_callback_query(callback_id, "Показано")
            return "showed-options" if action_name == "options" else "showed-solution"
        if action_name == "cancel":
            accepted = self.grader_store.cancel_mentor_review(
                action.task_id, action.version, action.draft_hash
            )
            self.state_store.close_review(token)
            self.transport.answer_callback_query(
                callback_id, "Задача отменена" if accepted else "Proposal уже изменился."
            )
            if not accepted:
                return "expired"
            self.transport.send_message(
                self.allowed_chat_id,
                f"{action.task_id}: подготовка hidden suite отменена.",
            )
            return "cancelled-task"
        if action_name == "pause":
            accepted = self.grader_store.pause_mentor_review(
                action.task_id, action.version, action.draft_hash
            )
            self.state_store.close_review(token)
            self.transport.answer_callback_query(
                callback_id, "Поставлено на паузу" if accepted else "Proposal уже изменился."
            )
            return "paused-review" if accepted else "expired"
        conversation = self.state_store.get(self.allowed_chat_id)
        if conversation is not None and conversation.state == "active":
            self.transport.answer_callback_query(
                callback_id, "Сначала ответь на текущий вопрос или нажми /cancel."
            )
            return "busy"
        accepted = self.grader_store.begin_mentor_revision(
            action.task_id, action.version, action.draft_hash
        )
        if not accepted:
            self.state_store.close_review(token)
            self.transport.answer_callback_query(callback_id, "Proposal уже изменился.")
            return "expired"
        try:
            prompt_id = self.transport.send_message(
                self.allowed_chat_id,
                (
                    f"{action.task_id} — что изменить в трактовке, критериях, тест-плане "
                    "или правильном решении?\n\nОтветь reply-сообщением."
                ),
            )
        except Exception:
            self.grader_store.cancel_mentor_revision(
                action.task_id, action.version, action.draft_hash
            )
            raise
        self.state_store.close_review(token)
        self.state_store.activate(
            Conversation(
                chat_id=self.allowed_chat_id,
                user_id=self.allowed_user_id,
                task_id=action.task_id,
                nonce=token,
                revision=action.version,
                question="Изменения mentor proposal",
                prompt_message_id=prompt_id,
                state="active",
                kind="mentor_revision",
                draft_hash=action.draft_hash,
            )
        )
        self.transport.answer_callback_query(callback_id, "Жду твои правки")
        return "revision-requested"

    def _authorized_message(self, message: dict[str, Any]) -> bool:
        chat = message.get("chat")
        sender = message.get("from")
        return (
            isinstance(chat, dict)
            and isinstance(sender, dict)
            and chat.get("id") == self.allowed_chat_id
            and chat.get("type") == "private"
            and sender.get("id") == self.allowed_user_id
        )

    def _authorized_callback(self, callback: dict[str, Any]) -> bool:
        sender = callback.get("from")
        message = callback.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        return (
            isinstance(sender, dict)
            and sender.get("id") == self.allowed_user_id
            and isinstance(chat, dict)
            and chat.get("id") == self.allowed_chat_id
            and chat.get("type") == "private"
        )


def _review_markup(token: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Утвердить", "callback_data": f"review:approve:{token}"},
                {"text": "Изменить", "callback_data": f"review:revise:{token}"},
            ],
            [
                {"text": "Варианты", "callback_data": f"review:options:{token}"},
                {"text": "Решение", "callback_data": f"review:solution:{token}"},
            ],
            [
                {"text": "Пауза", "callback_data": f"review:pause:{token}"},
                {"text": "Отменить задачу", "callback_data": f"review:cancel:{token}"},
            ],
        ]
    }


def _bounded_section(title: str, body: str, maximum: int) -> str:
    text = f"{title}:\n{body.strip()}"
    return text if len(text) <= maximum else text[: maximum - 1].rstrip() + "…"


def _format_mentor_review(review: MentorReview, proposal: MentorProposal) -> str:
    criteria = "\n".join(f"• {item}" for item in proposal.criteria)
    test_plan = "\n".join(f"• {item}" for item in proposal.test_plan)
    sections = [
        f"{review.task_id} — согласование скрытой проверки",
        f"Проект: {review.project}",
        f"Starter: {review.starter_sha[:12]}",
        f"Версия proposal: {review.version}",
        "",
        _bounded_section("Трактовка", proposal.interpretation, 850),
        "",
        _bounded_section("Критерии", criteria, 1_250),
        "",
        _bounded_section("План скрытой проверки", test_plan, 1_200),
        "",
        _bounded_section("Рекомендуемый подход", proposal.reference_approach, 450),
        "",
        _bounded_section("TestCritic", proposal.critic_summary, 300),
        "",
        "Полные спорные варианты и reference solution доступны отдельными кнопками.",
    ]
    text = "\n".join(sections)
    return text if len(text) <= 3_950 else text[:3_949].rstrip() + "…"


def _format_mentor_options(task_id: str, proposal: MentorProposal) -> str:
    lines = [f"{task_id} — варианты трактовки"]
    if not proposal.decisions:
        lines.append("Материальных развилок не обнаружено.")
    for number, decision in enumerate(proposal.decisions, start=1):
        lines.extend(["", f"{number}. {decision.question}"])
        for index, option in enumerate(decision.options):
            marker = "рекомендую" if index == decision.recommended_option else "вариант"
            lines.append(f"• {option} — {marker}")
        lines.append(f"Почему: {decision.reason}")
    return "\n".join(lines)


def _format_mentor_solution(task_id: str, proposal: MentorProposal) -> str:
    return "\n\n".join(
        [
            f"{task_id} — предлагаемое правильное решение",
            _bounded_section("Подход", proposal.reference_approach, 2_100),
            _bounded_section("Код", proposal.reference_solution, 8_050),
        ]
    )


def _telegram_chunks(text: str, maximum: int = 3_950) -> tuple[str, ...]:
    chunks: list[str] = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= maximum:
            chunks.append(remaining)
            break
        boundary = remaining.rfind("\n", 0, maximum + 1)
        if boundary < maximum // 2:
            boundary = maximum
        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip("\n")
    return tuple(chunks)


def _format_feedback_notification(notification: FeedbackNotification) -> str:
    try:
        payload = json.loads(notification.feedback_json)
    except json.JSONDecodeError as error:
        raise RuntimeError("feedback notification is invalid") from error
    expected = {
        "schema_version",
        "summary",
        "strengths",
        "weaknesses",
        "recommendations",
    }
    if not isinstance(payload, dict) or set(payload) != expected or payload["schema_version"] != 1:
        raise RuntimeError("feedback notification fields are invalid")
    for key in ("strengths", "weaknesses", "recommendations"):
        if not isinstance(payload[key], list) or any(
            not isinstance(item, str) for item in payload[key]
        ):
            raise RuntimeError("feedback notification list is invalid")
    summary = payload["summary"]
    if not isinstance(summary, str):
        raise RuntimeError("feedback notification summary is invalid")
    lines = [
        f"{notification.task_id} — разбор кода",
        f"Commit: {notification.commit_sha[:12]}",
        "",
        summary,
        "",
        "Сильные стороны:",
        *[f"• {item}" for item in payload["strengths"]],
        "",
        "Слабые места:",
        *[f"• {item}" for item in payload["weaknesses"]],
        "",
        "Рекомендации:",
        *[f"• {item}" for item in payload["recommendations"]],
    ]
    text = "\n".join(lines)
    return text if len(text) <= 3_950 else text[:3_947].rstrip() + "…"


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
