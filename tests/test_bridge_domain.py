from bridge.domain import task_branch_name


def test_task_branch_name_transliterates_cyrillic_and_normalizes_symbols() -> None:
    assert task_branch_name("PY-007", "Тип данных input()") == "task/PY-007-tip-dannyh-input"


def test_task_branch_name_keeps_the_branch_bounded() -> None:
    branch = task_branch_name("PY-123", "Очень длинное название " * 20)

    assert branch.startswith("task/PY-123-")
    assert len(branch) <= 80
