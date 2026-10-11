# agent-loop: прогресс

План: [plan.md](plan.md). Постоянные требования к коду: [../AGENTS.md](../AGENTS.md).

Порядок работы: коммитит и пушит только человек, агент предлагает сообщение. Этап 1 — в `main`, дальше каждый шаг в своей ветке.

## Этап 1. Каркас

### 1.1 Проект, конфиг, CLI-заглушки, лог — готово 2026-10-11

Сделано:
- `pyproject.toml`: Python ≥ 3.12, сборка hatchling, точка входа `loop` (`agent_loop.cli:main`), dev-зависимости `pytest`, `ruff`. Внешних зависимостей нет: `jsonschema` добавим, когда появится первая схема.
- `src/agent_loop/config.py`: настройки `Settings` (язык, модели и effort ролей, пути, пороги из плана). Слои: значения по умолчанию → `$LOOP_HOME/.env` (по умолчанию `~/.agent-loop/.env`) → переменные окружения `LOOP_*` → `loop.toml` → флаги CLI. Для каждой настройки запоминается источник.
  - Профиль ищется так: `--profile` → `<repo>/loop.toml` → `<knowledge_dir>/<имя репозитория>/loop.toml`; два найденных профиля — ошибка.
  - Из `loop.toml` пока читаются только ключи настроек; таблицы `[commands]`, `[timeouts]`, `[env]` и ключи `deps_files`, `secret_patterns` молча пропускаются — их разберёт профиль на шаге 3.1 (там же проверка неизвестных ключей).
  - Неизвестный ключ `LOOP_*` в `.env` — ошибка. Относительные пути считаются от файла, где заданы. `knowledge_dir` в профиле запрещён (по нему профиль и ищется).
  - Проверки: язык `ru`/`en`, effort из списка CLI, целые > 0, доли в [0, 1), мягкие пороги меньше жёстких, `context_file` существует.
- `src/agent_loop/log.py`: лог в stderr, строка `HH:MM:SS [run …] [task …] [stage] сообщение`; уровень пишется с WARNING; `log.scope(run=, task=, stage=)` вкладывается и дополняет внешнюю область.
- `src/agent_loop/cli.py`: глобальные флаги `-v`, `--profile`, `--doc-lang`, `--model-planner/-implementer/-critic`; команды `plan`, `approve`, `run`, `resume`, `answer`, `status`, `stats`, `selfcheck`, `gc` — заглушки (код 1); `config` печатает действующие настройки с источниками. Ошибка конфига — код 2.
- `.env.example` со всеми ключами; тест проверяет, что он валиден.
- `AGENTS.md`: правило про ~1000 строк переписано — задача не влезает → остановиться и предложить разбиение. В плане (раздел 8) под это добавлен статус исполнителя `too_big`.

Проверка:
```
uv sync
uv run pytest -q            # 42 теста
uv run ruff check . && uv run ruff format --check .
uv run loop config
```

### 1.2 `store`: схема SQLite, миграции, переходы состояний — готово 2026-10-11

Сделано:
- `src/agent_loop/store.py`, класс `Store(path)`; путь к базе передаёт вызывающий (будет `cfg.home / "loop.db"`). Таблицы: `runs`, `tasks` (карточка — JSON, `seq` нумеруется в пределах прогона), `steps` (вид шага, чекпоинт HEAD, `session_id`, исход, `detail` JSON, начало и конец), `transitions` (журнал смен статуса прогонов и задач с причиной).
- Миграции: список скриптов `MIGRATIONS`, номер применённой — `PRAGMA user_version`. Номер читается и все недостающие скрипты применяются в одной транзакции `BEGIN IMMEDIATE`: два процесса не мигрируют одновременно, упавшая миграция откатывается целиком. База новее кода — ошибка с просьбой обновить agent-loop.
- Переходы: `RUN_TRANSITIONS` (`planning → approved → running ⇄ paused → done`) и `TASK_TRANSITIONS` по диаграмме раздела 4 плана, плюс `dropped` (отмена при перепланировании) и пауза из `gating`/`fixing`/`reviewing`. `move_run`/`move_task` в одной транзакции проверяют текущий статус, меняют его и пишут запись в `transitions`; Ctrl+C посреди перехода откатывает его. `reason` хранится до следующего перехода.
- Шаги: `start_step` пишет начало до запуска, `finish_step` — исход; повторное завершение шага — ошибка. Возобновление после паузы берёт вид и `session_id` последнего шага задачи.
- Соединение в режиме `autocommit=True` (транзакции открывает только `Store`), WAL — чтобы `loop status` и другие циклы читали, не блокируя запись.
- Чего нет: снимков лимитов (появятся миграцией в 2.1 вместе с `budget`), хеша плана (4.2), `pid` прогона (2.3), HEAD прогона для проверки ветки (3.1), правки карточки при `modify` (4.2). Каждый шаг добавляет свою миграцию.

Проверка:
```
uv run pytest -q            # 60 тестов
uv run ruff check . && uv run ruff format --check .
```

### 1.3 `agent`: адаптер `claude`, `loop selfcheck` — готово 2026-10-11

Сделано:
- `src/agent_loop/agent.py` — единственное место, знающее флаги `claude` и формат `stream-json`.
  - `Session`: промпт, модель, рабочий каталог, файл настроек, effort, файлы правил, JSON-схема ответа, запрещённые инструменты, `session_id`, `resume`. `session_id` (uuid) создаётся до старта и передаётся как `--session-id`, при `resume=True` — как `--resume`: вызывающий пишет его в БД до запуска процесса, поэтому отдельная запись после `init` не понадобилась.
  - Всегда: `-p --output-format stream-json --verbose --setting-sources project --settings <file> --permission-prompts none`.
  - `Process(session, log_path)` — контекстный менеджер. Запускает `claude` в своей группе процессов (`process_group=0`), окружение только из `PATH`, `HOME`, `USER`, `LANG`, `TMPDIR`. Промпт идёт через stdin из файла `<log>.prompt.md`; файлы правил склеиваются в `<log>.system.md` (CLI берёт только последний `--append-system-prompt-file`); stderr — в `<log>.stderr`; сырой поток дописывается в `<log>` построчно.
  - `events()` отдаёт `Init`, `Usage` (заполнение контекста по сообщению основной цепочки, сообщения субагентов пропускаются), `Limits` (окна `five_hour`/`seven_day`, каждое может отсутствовать), `Result`. Остальные события пропускаются. Отсутствие ожидаемого поля — `ProtocolError`. Поток кончился без `result` и без `stop()` — `AgentError` с хвостом stderr.
  - `stop()` — SIGTERM всей группе; выход из блока `with` добивает ещё живой процесс (иначе Ctrl+C оставил бы сироту: SIGINT терминала в чужую группу не приходит).
- `loop selfcheck`: две короткие сессии на `haiku` в `~/.agent-loop/selfcheck/` (там же сырые потоки). Первая проверяет, что до модели доходят CLAUDE.md рабочего каталога и оба файла правил (ответ по схеме), что пришли `init` с нашим `session_id`, usage и `rate_limit_event`. Вторая — `--resume` той же сессии. Успех пишет версию CLI в `~/.agent-loop/selfcheck.json`; сверять её перед `loop run` будет команда `run`. Все найденные проблемы выводятся одной ошибкой, код 1.
- Тесты гоняют поддельный `claude` (скрипт в `PATH`, поток по образцу реального 2.1.296); реальный CLI в тестах не вызывается.
- Находки о CLI — в плане, раздел 2, «Находки шага 1.3»; в разделе 10 окружение дополнено `USER` и `TMPDIR`.
- Чего нет: таймаута простоя потока (2.3), `loop-settings.json` с allow-списком, sandbox и hook (2.2; selfcheck пишет себе минимальные настройки: `dontAsk`, без auto-memory), переменных окружения из профиля (3.1).

Проверка:
```
uv run pytest -q            # 76 тестов
uv run ruff check . && uv run ruff format --check .
uv run loop selfcheck       # ~10 с, реальный claude на haiku
```

Этап 1 закрыт. Дальше `main` закрыт для прямых коммитов: этап 2 идёт по веткам, по шагу на ветку.

## Этап 2. Guard и бюджет

### 2.1 `budget`: потолки лимитов, время возобновления, `limits.json` — готово 2026-10-11

Ветка `guard-n-budget`.

Сделано:
- `src/agent_loop/budget.py`, формулы раздела 5 плана поверх `agent.Limits`; пороги — из `config.Settings` (`start_allowance_5h`, `reserve_5h`, `reserve_7d`, `step_margin`). Время — unix-секунды, часовой пояс не участвует: неделя отсчитывается от `seven_day.resets_at − 7 суток`.
  - `cap_5h`, `cap_7d` — потолок окна на момент `now` (публичные: понадобятся guard'у в 2.2).
  - `check(limits, now, settings) -> Verdict(allowed, resume_at, summary)`. Шаг разрешён, если в каждом окне `u < cap − step_margin`. Окно, которого нет или чей `resets_at` уже прошёл, не ограничивает. `summary` — строка для лога: `5h 40%/50% · 7d 5%/9%` (расход/потолок).
  - `resume_at` — первая секунда, когда кривая оставит место под шаг: для 5h из формулы рампы (+1 с, потому что сравнение строгое), для 7d — начало первых суток с запасом; если кривая не догонит до конца окна — `resets_at`. Заблокированы оба окна — берётся позднее время. Это оценка без учёта ручного расхода за время паузы: после возобновления проверка повторяется на свежих данных.
  - `save_limits` / `load_limits` — `~/.agent-loop/limits.json` (`budget.LIMITS_FILE`), запись через временный файл с pid и `os.replace`. Окно, которого нет в событии, сохраняет прежнее значение — иначе такое событие стёрло бы недельный расход и бюджет решил бы, что неделя свободна.
- `store`: миграция 2 — таблица `limit_snapshots` (прогон, шаг или `NULL` для пробного вызова вне шага, оба окна, время). `record_limits(run_id, step_id, limits)`, `limit_snapshots(run_id)`. `store` не импортирует `agent` во время выполнения: `Limits` — только аннотация.
- Тесты: границы рампы 5h, понедельник 17:00 и смена суток недели, ожидание сброса в обоих окнах, отсутствующие и уже сброшенные окна, оба окна сразу, файл лимитов (круговая запись, нет временных файлов, слияние окон); в `store` — миграция старой базы с данными и снимки с пустым окном.
- Чего нет: оценки `step_margin` по истории (раздел 5 плана) — посчитаем, когда будут снимки реальных прогонов и ответ на S1b (повторяется ли `rate_limit_event` в длинной сессии); пока берётся `step_margin` из конфига. Ожидания до `resume_at` и пробного вызова при отсутствии данных — это команда `run` (2.3 и этап 3). Порога жёсткой остановки guard'а (+2% к потолку) — добавит 2.2.

Проверка:
```
uv run pytest -q            # 99 тестов
uv run ruff check . && uv run ruff format --check .
```

### 2.2 `guard`: hook `PreToolUse`, `loop-settings.json`, спайк S7 — готово 2026-10-11

Ветка `guard-n-budget` (та же, что 2.1).

Сделано:
- `src/agent_loop/guard.py` — hook, который CLI вызывает перед каждым инструментом: `python -I -m agent_loop.guard <run_dir>` интерпретатором цикла (`-I`: модули рабочего репозитория не подменяют наши). Всё, что hook знает, лежит в каталоге прогона `runs/<run>/`:
  - `policy.json` — пороги контекста и лимитов из `Settings`, путь к общему `limits.json`, абсолютный путь handoff;
  - `live.json` — заполнение контекста, оркестратор пишет его из событий `Usage` (`write_live`);
  - `stop` — просьба оркестратора свернуться: `interrupt` или `timeout` (`request_stop`);
  - `guard.json` — что hook решил (`outcome` → `State(reason, hard, denied)`); по нему оркестратор узнаёт причину паузы.
- `prepare(run_dir, home, settings, workdir)` перед каждой сессией, в том числе `--resume`: пишет `settings.json` (база `loop-settings.json` + hook), `policy.json`, обнуляет `live.json`, удаляет `stop` и `guard.json` прошлой сессии. Возвращает путь для `Session.settings`.
- Решение (`pressure` + `respond`): сначала жёсткие причины — контекст ≥ `ctx_hard_tokens`, лимит выше потолка на 2% и больше; затем мягкие — stop-файл, контекст ≥ `ctx_soft_tokens`, лимит на потолке. Мягкая — `deny` с инструкцией записать `.loop/handoff.md` и завершить ход; запись handoff (`Write`/`Edit`) и `StructuredOutput` проходят. После 3 отклонённых вызовов — жёсткая. Жёсткая — `continue: false` и `deny`; запись handoff и `StructuredOutput` не отклоняются, сессия завершается после них.
- `budget.overrun` — насколько расход выше потолка в худшем окне; `files.write_atomic` — атомарная запись, общая для `limits.json`, `live.json`, `stop`, `guard.json`.
- `src/agent_loop/loop-settings.json`: `dontAsk`, без auto-memory; allow — `Read`, `Glob`, `Grep`, `Edit(./**)` (покрывает `Write`), `TodoWrite`; deny — `WebFetch`, `WebSearch`, чтение `.env*`/`*.pem`/`*.key`, `git commit/push/reset/checkout/switch/rebase/merge/stash/clean`, `rm -rf`, `curl`, `wget`; sandbox для Bash без выхода из него (`allowUnsandboxedCommands: false`, `failIfUnavailable: true`), localhost открыт (`allowLocalBinding`); таймауты Bash 2/10 мин.
- Спайк S7 — в плане, раздел 2: сеть наружу закрыта, localhost открыт, `cat .env` блокирует sandbox, guard останавливает реальную сессию мягко и жёстко. Скрипт спайка в репозиторий не входит.
- Тесты: решения guard на границах, подсчёт отказов между вызовами, handoff по относительному пути, чтение общего `limits.json`, очистка состояния прошлой сессии, запуск hook отдельным процессом из каталога с `json.py`.
- Чего нет:
  - Проверки hook в `loop selfcheck`: дрейф формата hook'а CLI сейчас не ловится. Нужна третья сессия selfcheck с `guard.prepare`; мешает циклический импорт (`selfcheck` в `agent`, `guard` импортирует `agent`) — selfcheck стоит вынести в свой модуль. Предлагаю сделать в 2.3.
  - Allow-списка команд профиля (`make <цели>`): Bash в sandbox и так разрешён целиком (`autoAllowBashIfSandboxed`), deny-правила действуют. Команды профиля — 3.1.
  - Записи `live.json` и `limits.json` из потока событий и реакции на `guard.outcome` — это цикл шага (2.3).
  - `live.json` после `prepare` равен 0, поэтому в возобновлённой сессии первый вызов инструмента может пройти до того, как оркестратор запишет свежий контекст; запас между мягкой и жёсткой границей (30k) это покрывает.

Проверка:
```
uv run pytest -q            # 126 тестов
uv run ruff check . && uv run ruff format --check .
```

### 2.3 Шаг под контролем: события, таймауты, Ctrl+C — готово 2026-10-11

Ветка `guard-n-budget`. Исходный шаг 2.3 не влезал в ~1000 строк и разбит на три: 2.3 (этот), 2.4 (WIP и возобновление), 2.5 (selfcheck); в плане, раздел 14, то же.

Сделано:
- `src/agent_loop/step.py`, `step.run(session, cfg, store, run_id, step_id) -> Outcome` — одна сессия шага. Каталог прогона — `step.run_dir(home, run_id)` (`~/.agent-loop/runs/<run>/`); вызывающий перед этим делает `guard.prepare` и кладёт возвращённый файл настроек в `Session.settings`. Сырой поток — `runs/<run>/step-<step_id>.jsonl`.
  - События: `Usage` → `guard.write_live`; `Limits` → `budget.save_limits` (общий `limits.json`) и `store.record_limits` со ссылкой на шаг. Строка лога `ctx 18k/150k · 5h 3%/…` — на каждом `Limits` и при пересечении очередных 10k контекста.
  - Таймауты (`Deadlines`, монотонное время, отдельный поток): шаг дольше `step_timeout_min` → `guard.request_stop("timeout")`; агент не свернулся за `STOP_GRACE` (5 мин) после просьбы → SIGTERM группе. Поток молчит дольше `BASH_MAX_TIMEOUT_MS` из настроек сессии + 2 мин → SIGTERM, причина `idle`.
  - Ctrl+C: на время сессии свой обработчик SIGINT. Первый — `request_stop("interrupt")` и отсчёт `STOP_GRACE`, второй — SIGTERM. После шага прежний обработчик возвращается.
  - Итог `Outcome(status, reason, result, context_tokens)`: `paused` с причиной из `guard.json`, если guard вмешался (она важнее способа завершения: после паузы по контексту нужна новая сессия), иначе с причиной нашего SIGTERM; `failed` при `is_error`; иначе `completed`. Шаг завершается в `store` с `detail`: причина, контекст, `terminal_reason`, ходы, стоимость и ответ по схеме (`answer`) — ответ исполнителя переживёт падение оркестратора между сессией и коммитом. CLI вышел без `result` — шаг `failed` с текстом ошибки, `AgentError` летит дальше.
- Тесты: поддельный `claude` и формы событий вынесены из `test_agent.py` в `tests/fakes.py` и `tests/conftest.py`. Подделка умеет посылать Ctrl+C родителю (`"interrupt"`, ждёт stop-файл) и спрашивать настоящий hook из `--settings` (`"tool:<имя>"`). Проверены: запись `live.json`/`limits.json`/снимков, мягкая остановка по Ctrl+C и жёсткая по контексту через настоящий hook, второй Ctrl+C, молчащий поток, ошибка API, выход без результата, решения `Deadlines` на границах.
- Чего нет: прогона на реальном CLI (на этом шаге его не запускали). Проверить — короткой сессией на `haiku` через `guard.prepare` + `step.run` в пустом каталоге: должен прийти `completed`, в `runs/<run>/` — `live.json` и поток, в БД — снимок лимитов.

Проверка:
```
uv run pytest -q            # 140 тестов
uv run ruff check . && uv run ruff format --check .
```

Дальше: 2.4 — WIP-ref `refs/loop/wip/<run>/<task>` через временный индекс (`git stash create` не берёт новые неотслеживаемые файлы; брать отслеживаемые изменения и неотслеживаемые файлы, появившиеся за шаг), выбор способа возобновления по `Outcome.reason` (`--resume` для budget/interrupt/timeout/idle, новая сессия с handoff для context, вторая пауза по контексту на задаче — `replan`), `session_id` в `store.start_step` (сейчас пишется только в `finish_step`, а `kill -9` посреди сессии его потеряет), pid прогона и pgid сессии в `store` (миграция 3), доуборка мёртвых прогонов при старте `loop`, `loop gc`. Потом 2.5 — selfcheck в своём модуле и третья сессия с hook (см. «Чего нет» в 2.2).
