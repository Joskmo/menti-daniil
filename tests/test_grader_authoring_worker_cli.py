from grader.authoring_worker_cli import run_cycle


class FakeCoordinator:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = 0

    def process_once(self):
        self.calls += 1
        return self.result


def test_authoring_worker_cycle_processes_exactly_one_job() -> None:
    coordinator = FakeCoordinator("ready")

    assert run_cycle(coordinator) == "ready"
    assert coordinator.calls == 1
