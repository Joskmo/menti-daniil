import pytest

from bridge.domain import project_directory_name, task_branch_name


def test_task_branch_name_transliterates_cyrillic_and_normalizes_symbols() -> None:
    assert task_branch_name("PY-007", "Тип данных input()") == "task/PY-007-tip-dannyh-input"


def test_task_branch_name_keeps_the_branch_bounded() -> None:
    branch = task_branch_name("PY-123", "Очень длинное название " * 20)

    assert branch.startswith("task/PY-123-")
    assert len(branch) <= 80


def test_project_directory_name_normalizes_explicit_project_key() -> None:
    assert project_directory_name(" JSON ") == "json"
    assert project_directory_name("my-project") == "my-project"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "  ",
        "../",
        "a/b",
        "C++",
        "Учебный проект",
        "ß",
        "K",
        "ſ",
        "ﬀ",
        "a" * 65,
    ],
)
def test_project_directory_name_rejects_noncanonical_keys(value: str) -> None:
    with pytest.raises(ValueError, match="Project"):
        project_directory_name(value)
