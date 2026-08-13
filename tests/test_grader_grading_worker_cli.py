from grader.grading_worker_cli import run_cycle


class FakeCoordinator:
    def __init__(self, result: str) -> None:
        self.result = result
        self.calls = 0

    def process_once(self):
        self.calls += 1
        return self.result


def test_grading_worker_cycle_processes_exactly_one_attempt() -> None:
    coordinator = FakeCoordinator("completed")

    assert run_cycle(coordinator) == "completed"
    assert coordinator.calls == 1
