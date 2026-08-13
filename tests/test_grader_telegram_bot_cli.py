from grader.telegram_bot_cli import run_cycle


class FakeBot:
    def __init__(self) -> None:
        self.updates = []
        self.syncs = 0
        self.review_syncs = 0
        self.report_syncs = 0

    def sync_mentor_notification(self):
        self.report_syncs += 1
        return "idle"

    def sync_pending_mentor_review(self):
        self.review_syncs += 1
        return "idle"

    def sync_pending_clarification(self):
        self.syncs += 1
        return "idle"

    def handle_update(self, update):
        self.updates.append(update["update_id"])
        return "ignored"


class FakeApi:
    def __init__(self, updates) -> None:
        self.updates = updates
        self.offsets = []

    def get_updates(self, *, offset=None):
        self.offsets.append(offset)
        return self.updates


def test_telegram_cycle_syncs_pending_and_advances_after_highest_update() -> None:
    bot = FakeBot()
    api = FakeApi([{"update_id": 12}, {"update_id": 10}])

    assert run_cycle(bot, api, offset=8) == 13

    assert bot.report_syncs == 1
    assert bot.review_syncs == 1
    assert bot.syncs == 1
    assert bot.updates == [10, 12]
    assert api.offsets == [8]


def test_telegram_cycle_keeps_offset_for_empty_batch() -> None:
    bot = FakeBot()
    api = FakeApi([])

    assert run_cycle(bot, api, offset=8) == 8
