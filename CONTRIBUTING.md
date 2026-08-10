# Правила работы

## Получение репозитория

```bash
git clone git@github.com:Joskmo/menti-daniil.git
cd menti-daniil
python -m venv .venv
```

Активация окружения:

```bash
# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Установка инструментов разработки:

```bash
python -m pip install -r requirements-dev.txt
```

## Начало задачи

Ветка всегда создаётся от актуальной `main`:

```bash
git fetch origin
git switch main
git pull --ff-only origin main
git switch -c task/PY-001-short-title
```

Если интеграция Yonote уже создала удалённую ветку:

```bash
git fetch origin
git switch --track origin/task/PY-001-short-title
```

Интеграция берёт имя каталога из поля Yonote `Проект`. Например, все задачи с
`Проект = json` работают с `projects/json/`. Если такого проекта ещё нет в
`main`, первая task-ветка получает `README.md` и минимальный `main.py`.
Существующие файлы проекта автоматически не изменяются.

`Проект` задаётся как стабильный технический ключ: только латинские строчные
буквы, цифры и одиночные дефисы, максимум 64 символа. Не используйте свободный
текст, слеши и знаки пунктуации: значение не транслитерируется и не обрезается,
а отклоняется, чтобы разные проекты случайно не попали в одну директорию.

## Коммиты и публикация

```bash
git add <изменённые-файлы>
git commit -m "feat(PY-001): краткое описание"
git push -u origin HEAD
```

После push нужно открыть pull request в `main`.

## Перед pull request

```bash
ruff check .
pytest
```

## Правила

- Одна задача Yonote — одна ветка — один pull request.
- Все задачи с одинаковым полем `Проект` работают в общей директории.
- Формат ветки: `task/PY-XXX-short-title`.
- Нельзя выполнять прямой push в `main`.
- Нельзя делать force-push в `main` и удалять `main`.
- В PR должна быть ссылка на задачу Yonote.
- Замечания review исправляются новыми коммитами в той же ветке.
