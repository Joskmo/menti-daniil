import re

_TRANSLITERATION = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)


_PROJECT_KEY_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _slug(value: str) -> str:
    normalized = value.lower().translate(_TRANSLITERATION)
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")


def project_directory_name(project: str, max_length: int = 64) -> str:
    raw_key = project.strip()
    if not raw_key.isascii():
        raise ValueError("Project must be an ASCII technical key")
    key = raw_key.lower()
    if len(key) > max_length or not _PROJECT_KEY_PATTERN.fullmatch(key):
        raise ValueError(
            "Project must be an explicit key containing lowercase letters, "
            "digits, and single hyphens"
        )
    return key


def task_branch_name(task_id: str, title: str, max_length: int = 80) -> str:
    slug = _slug(title) or "task"
    prefix = f"task/{task_id}-"
    return prefix + slug[: max_length - len(prefix)].rstrip("-")
