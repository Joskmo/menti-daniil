from grader.menti_bot import MentiBot, MentiStateStore
from grader.store import GraderStore


class FakeTelegram:
    def __init__(self) -> None:
        self.sent = []

    def send_message(self, chat_id: int, text: str) -> int:
        message_id = 100 + len(self.sent)
        self.sent.append((chat_id, text, message_id))
        return message_id


def _setup(tmp_path):
    grader = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    grader.enqueue_authoring(
        task_id="PY-002",
        row_id="row-2",
        project="json",
        branch_name="task/PY-002-next-id",
        starter_sha="a" * 40,
        assignment_json='{"title":"next id"}',
    )
    job = grader.claim_next_authoring()
    clarification = grader.request_clarification(
        job.task_id,
        job.lease_token,
        "Что делать с пустым списком?",
    )
    telegram = FakeTelegram()
    state = MentiStateStore(tmp_path / "menti.db")
    bot = MentiBot(
        grader_store=grader,
        state_store=state,
        transport=telegram,
        allowed_chat_id=42,
        allowed_user_id=7,
    )
    prompt_id = bot.send_clarification(
        task_id=clarification.task_id,
        nonce=clarification.nonce,
        revision=clarification.revision,
        question=clarification.question,
    )
    return grader, clarification, telegram, state, bot, prompt_id


def _message(update_id: int, text: str, reply_to: int | None = None, *, user_id=7):
    message = {
        "message_id": update_id + 1_000,
        "chat": {"id": 42, "type": "private"},
        "from": {"id": user_id},
        "text": text,
    }
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": message}


def test_bot_delivers_durable_private_grade_report_once(tmp_path) -> None:
    grader = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    attempt = grader.enqueue_grading(
        task_id="PY-002",
        project="json",
        branch_name="task/PY-002-next-id",
        commit_sha="b" * 40,
        suite_hash="c" * 64,
    )
    claimed = grader.claim_next_grading()
    report = (
        '{"failed_rubrics":[{"description":"Критерий задания 1.",'
        '"id":"criterion-1"}],"passed":3,"total":4}'
    )
    assert grader.complete_grading(
        attempt.attempt_id,
        claimed.lease_token,
        passed=False,
        passed_count=3,
        total_count=4,
        private_report_json=report,
    )
    telegram = FakeTelegram()
    bot = MentiBot(
        grader_store=grader,
        state_store=MentiStateStore(tmp_path / "menti.db"),
        transport=telegram,
        allowed_chat_id=42,
        allowed_user_id=7,
    )

    assert bot.sync_mentor_notification() == "sent"
    assert bot.sync_mentor_notification() == "idle"

    assert telegram.sent == [
        (
            42,
            "PY-002 — hidden grade\n"
            "Пройдено: 3/4\n"
            "Не пройдены категории поведения:\n"
            "• Критерий задания 1.\n"
            "Commit: bbbbbbbbbbbb\n"
            "Попытка доставки: 1",
            100,
        )
    ]


def test_bot_syncs_durable_pending_clarification_without_duplicate_send(tmp_path) -> None:
    _, clarification, telegram, state, bot, _ = _setup(tmp_path)

    assert bot.sync_pending_clarification() == "waiting"
    assert len(telegram.sent) == 1
    current = state.get(42)
    assert state.clear_exact(current)
    assert bot.sync_pending_clarification() == "sent"
    refreshed = state.get(42)
    assert refreshed.nonce == clarification.nonce
    assert refreshed.revision == clarification.revision
    assert len(telegram.sent) == 2


def test_menti_accepts_only_reply_to_current_prompt_and_deduplicates_update(tmp_path) -> None:
    grader, _, telegram, state, bot, first_prompt = _setup(tmp_path)
    conversation = state.get(42)
    second_prompt = bot.send_clarification(
        task_id=conversation.task_id,
        nonce=conversation.nonce,
        revision=conversation.revision,
        question=conversation.question,
    )

    assert bot.handle_update(_message(1, "Вернуть 1.", first_prompt)) == "stale"
    assert grader.get_authoring("PY-002").state == "needs_clarification"
    assert bot.handle_update(_message(2, "Вернуть 1.", second_prompt)) == "answered"
    sent_count = len(telegram.sent)
    assert bot.handle_update(_message(2, "Другое.", second_prompt)) == "duplicate"
    assert len(telegram.sent) == sent_count
    assert grader.get_authoring("PY-002").clarification_answer == "Вернуть 1."


def test_cancel_clears_active_flow_and_start_rebinds_to_new_prompt(tmp_path) -> None:
    grader, _, _, _, bot, first_prompt = _setup(tmp_path)

    assert bot.handle_update(_message(10, "/cancel")) == "paused"
    assert bot.handle_update(_message(11, "Старый ответ", first_prompt)) == "expired"
    assert grader.get_authoring("PY-002").state == "needs_clarification"
    assert bot.handle_update(_message(12, "/start")) == "resumed"
    resumed = bot.state_store.get(42)
    assert resumed.prompt_message_id != first_prompt
    assert bot.handle_update(_message(13, "Вернуть 1.", resumed.prompt_message_id)) == "answered"


def test_unauthorized_user_cannot_change_state_or_answer(tmp_path) -> None:
    grader, _, telegram, state, bot, prompt_id = _setup(tmp_path)
    before = state.get(42)

    assert bot.handle_update(_message(20, "Вернуть 999.", prompt_id, user_id=99)) == "ignored"

    assert state.get(42) == before
    assert grader.get_authoring("PY-002").state == "needs_clarification"
    assert len(telegram.sent) == 1
    assert (tmp_path / "menti.db").stat().st_mode & 0o777 == 0o600
