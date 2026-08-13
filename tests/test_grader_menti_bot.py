from grader.menti_bot import MentiBot, MentiStateStore
from grader.store import GraderStore


class FakeTelegram:
    def __init__(self) -> None:
        self.sent = []
        self.callbacks = []

    def send_message(self, chat_id: int, text: str, *, reply_markup=None) -> int:
        message_id = 100 + len(self.sent)
        item = (
            (chat_id, text, message_id)
            if reply_markup is None
            else (chat_id, text, message_id, reply_markup)
        )
        self.sent.append(item)
        return message_id

    def answer_callback_query(self, callback_query_id: str, text: str) -> None:
        self.callbacks.append((callback_query_id, text))


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


def _callback(update_id: int, data: str, *, user_id=7, message_id=100):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": user_id},
            "message": {
                "message_id": message_id,
                "chat": {"id": 42, "type": "private"},
            },
            "data": data,
        },
    }


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


def _mentor_review(store: GraderStore):
    store.enqueue_authoring(
        task_id="PY-003",
        row_id="row-3",
        project="json",
        branch_name="task/PY-003-review",
        starter_sha="d" * 40,
        assignment_json='{"title":"Review"}',
    )
    claimed = store.claim_next_authoring()
    assert claimed is not None
    return store.submit_mentor_review(
        claimed.task_id,
        claimed.lease_token,
        suite_json='{"schema_version":1}',
        proposal_json=(
            '{"schema_version":1,"interpretation":"Вернуть следующий ID.",'
            '"criteria":["Корректный ID."],"decisions":[],'
            '"test_plan":["Основной сценарий."],'
            '"reference_approach":"Найти максимум.",'
            '"reference_solution":"def next_id(rows): return 1",'
            '"critic_summary":"Одобрено."}'
        ),
        critic_verdict_json='{"status":"approved"}',
    )


def test_bot_sends_mentor_proposal_with_versioned_approve_revision_pause_buttons(tmp_path) -> None:
    grader = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    review = _mentor_review(grader)
    telegram = FakeTelegram()
    state = MentiStateStore(tmp_path / "menti.db")
    bot = MentiBot(
        grader_store=grader,
        state_store=state,
        transport=telegram,
        allowed_chat_id=42,
        allowed_user_id=7,
    )

    assert bot.sync_pending_mentor_review() == "sent"
    _, text, message_id, markup = telegram.sent[0]
    assert "PY-003 — согласование скрытой проверки" in text
    assert "Трактовка" in text
    assert "Предлагаемое правильное решение" in text
    buttons = markup["inline_keyboard"][0]
    actions = {button["text"]: button["callback_data"] for button in buttons}

    assert (
        bot.handle_update(_callback(100, actions["Утвердить"], message_id=message_id))
        == "approved"
    )
    assert grader.get_authoring(review.task_id).state == "queued_for_acceptance"
    assert (
        bot.handle_update(_callback(101, actions["Утвердить"], message_id=message_id))
        == "expired"
    )


def test_revision_button_requires_reply_to_current_prompt_and_invalidates_old_callback(
    tmp_path,
) -> None:
    grader = GraderStore(tmp_path / "grader.db", clock=lambda: 100.0)
    review = _mentor_review(grader)
    telegram = FakeTelegram()
    state = MentiStateStore(tmp_path / "menti.db")
    bot = MentiBot(
        grader_store=grader,
        state_store=state,
        transport=telegram,
        allowed_chat_id=42,
        allowed_user_id=7,
    )
    assert bot.sync_pending_mentor_review() == "sent"
    _, _, proposal_message, markup = telegram.sent[0]
    actions = {
        button["text"]: button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
    }

    assert bot.handle_update(
        _callback(110, actions["Изменить"], message_id=proposal_message)
    ) == "revision-requested"
    revision_prompt = telegram.sent[-1][2]
    assert bot.handle_update(_message(111, "Вернуть 1.", proposal_message)) == "stale"
    assert bot.handle_update(_message(112, "Вернуть 1.", revision_prompt)) == "revised"
    job = grader.get_authoring(review.task_id)
    assert job.state == "queued"
    assert job.mentor_revision == "Вернуть 1."
    assert bot.handle_update(
        _callback(113, actions["Утвердить"], message_id=proposal_message)
    ) == "expired"
