# Python mentoring — Даниил

Учебный Python-монорепозиторий для совместной работы ментора и менти.

## Структура

Каждый учебный проект размещается в своей директории. Несколько задач одного
проекта работают с одним и тем же кодом:

```text
projects/
└── json/
    ├── README.md
    ├── main.py
    └── test_solution.py
```

Общие правила разработки описаны в [CONTRIBUTING.md](CONTRIBUTING.md).

## Основной процесс

1. Задача создаётся в Yonote, получает значение поля `Проект` и номер `PY-XXX`.
2. Для неё создаётся ветка `task/PY-XXX-short-title` от актуальной `main`.
3. Если проекта ещё нет, интеграция добавляет в ветку каталог
   `projects/<project>/` с `README.md` и `main.py`.
4. Все изменения выполняются только в ветке задачи.
5. Результат отправляется в `main` через pull request.
6. Перед слиянием должны пройти CI и review ментора.

Прямой push в `main` разрешён только владельцу репозитория.

## Менторские задачи

Карточки, назначенные Арсению, запускают тот же branch workflow, что и карточки
Даниила. Ментор может использовать свою task-ветку, чтобы добавить в проект
намеренно неправильный или незавершённый код, а затем выполнить admin merge в
`main`, даже если `quality` падает. Красная `main` в таком сценарии является
стартовым состоянием следующего задания, а не сбоем интеграции.

Следующая карточка Даниила создаёт ветку от этой актуальной `main`. Для PR
Даниила bypass не применяется: он должен исправить код, вернуть `quality` в
зелёное состояние и пройти review ментора.

Поле `Проект` — стабильный технический ключ: только латинские строчные буквы,
цифры и одиночные дефисы, не более 64 символов (например, `json` или
`currency-converter`). Разные проекты должны иметь разные ключи.

## Автоматическая скрытая проверка

Для карточек Даниила bridge фиксирует точный стартовый commit и передаёт задачу
закрытому grader. TestAuthor составляет декларативный draft, а независимый
TestCritic проверяет его качество. Затем приватный Telegram-бот показывает ментору
bounded summary трактовки, критериев и тест-плана. Отдельные кнопки открывают
полные спорные варианты и рекомендуемое правильное решение. Ментор утверждает exact
draft, просит новую версию, ставит proposal на паузу или окончательно отменяет
задачу. Приостановленный proposal возвращается командой `/resume PY-…`. Только
утверждённая exact version проходит повторяемую проверку starter в одноразовой
QEMU/KVM VM и замораживается в immutable vault. Старый authoring worker не может
обойти approval: DB допускает freeze только из exact approved acceptance lease.

Каждый следующий push в task-ветку попадает в durable очередь и проверяется в
новой VM без сети. В GitHub публикуется обязательный status `hidden-grade` только
с итогом pass/fail; подробный `N/M` остаётся в приватном Telegram-боте ментора.
После оценки отдельный LLM-worker анализирует сохранённый snapshot того же exact
commit и присылает сильные стороны, слабые места и рекомендации. Этот анализ не
задерживает и не изменяет deterministic grade. Hidden suite и ожидаемые значения
не записываются в GitHub и не передаются в feedback worker.

### Запуск grader profile

1. Скопировать grader-переменные из `.env.example` в локальный `.env`, включая
   отдельный BotFather token и разрешённые mentor chat/user IDs.
2. Создать каталоги state, vault, Git cache, launcher socket, broker socket и bot
   data; владельцем должен быть UID `1000`, mode — `0700`.
3. Настроить один credential provider для GitHub status publisher. Для
   двухпользовательского MVP достаточно локального `GITHUB_TOKEN`; GitHub App
   остаётся optional заменой.
4. Запустить LLM broker через Hermes Python вне Compose, направив socket в
   `GRADER_BROKER_RUN_DIR`.
5. Убедиться, что legacy `menti-hermes-report.service` остановлен: grade reports
   должен потреблять только `menti-grader-bot`.
6. Запустить сервисы:

```bash
docker compose --profile grader up -d --build
```

Проверка состояния:

```bash
docker compose --profile grader ps
docker compose --profile grader logs --tail=100 \
  menti-authoring-worker menti-grading-worker menti-code-feedback-worker \
  menti-check-publisher menti-grader-bot
```

`hidden-grade` следует добавлять в branch protection только после успешного
первого live Check Run, чтобы не заблокировать PR до установки GitHub App.
