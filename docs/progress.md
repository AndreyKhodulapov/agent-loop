# agent-loop: прогресс

План: [plan.md](plan.md). Постоянные требования к коду: [../AGENTS.md](../AGENTS.md).

Порядок работы: коммитит и пушит только человек, агент предлагает сообщение. Этап 1 — в `main`, дальше каждый этап в своей ветке: все шаги этапа — коммиты в ней.

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

Этап 1 закрыт. Дальше `main` закрыт для прямых коммитов: этап идёт в своей ветке (этап 2 — `guard-n-budget`).

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

### 2.4 WIP-ref, выбор возобновления, доуборка мёртвых прогонов, `loop gc` — готово 2026-10-11

Ветка `guard-n-budget`.

Сделано:
- `src/agent_loop/git.py` — git-команды цикла: `git()` (ошибка — `GitError` с stderr), `head`, `untracked` (неотслеживаемые и не игнорируемые файлы от корня), `wip_ref(run_id, task_seq)` → `refs/loop/wip/<run>/<seq>`, `save_wip(repo, ref, untracked_before)`. WIP — коммит поверх HEAD через временный индекс (`GIT_INDEX_FILE`): изменения отслеживаемых файлов (`add --update`, в том числе удаления) и неотслеживаемые файлы, которых не было в `untracked_before`. Индекс и рабочий каталог не трогаются; `--literal-pathspecs` (имена со `[`, `*`), `--no-gpg-sign` (запрос подписи повесил бы цикл).
- `store`: миграция 3 — `steps.untracked` (JSON-список неотслеживаемых файлов до шага, по умолчанию `[]`). `start_step(task_id, kind, checkpoint, untracked, session_id=None)` пишет `session_id` до старта: после `kill -9` сессию можно найти и возобновить. `finish_step` больше не принимает `session_id`. Новые `runs(status)`, `open_steps(run_id)`, константа `DB_FILE`.
- `step.how_to_resume(steps) -> Resume(mode, session_id)` по всем шагам задачи: последняя пауза не по контексту → `session` (`--resume` той же сессии); по контексту → `handoff` (новая сессия); вторая пауза по контексту на задаче (`CONTEXT_PAUSES_TO_REPLAN`) → `replan`.
- `src/agent_loop/recovery.py`:
  - `hold(path)` — эксклюзивный `flock` без ожидания, файл держится открытым всё время прогона, внутри pid для оператора. Живость прогона — эта блокировка (`runs/<run>/lock`), а не pid: ОС снимает её при любой смерти процесса, и её не спутать с чужим процессом с тем же pid после перезагрузки. Прогон обязан взять блокировку до перехода в `running`.
  - `reap(store, home)` — для каждого `running`-прогона со свободной блокировкой (под ней же, с перечитыванием статуса — два `loop` не доубирают один прогон дважды): осиротевшая сессия незавершённого шага ищется в `ps` по `session_id` в командной строке и получает SIGTERM всей группе, через `ORPHAN_GRACE` (10 с) — SIGKILL; работа сохраняется в WIP-ref задачи; шаг завершается как `paused` с причиной `died` (дальше `how_to_resume` даёт `--resume`); задача и прогон — в `paused(died)`. Шаг закрывается только после сохранения WIP: если git упал, следующий `loop` повторит.
- `cli`: `loop gc` — доуборка по запросу («no dead runs», если нечего). Команды, работающие с прогонами (`plan`, `approve`, `run`, `resume`, `answer`, `status`, `stats`), сначала доубирают мёртвые прогоны; `config` и `selfcheck` — нет. Финализатор: SIGTERM и SIGHUP превращаются в `SystemExit`, стек разматывается, и `agent.Process` гасит группу `claude` (по умолчанию сессия пережила бы оркестратор). Учёт в БД после такого выхода делает `reap` при следующем старте — один путь для всех аварийных выходов.
- План: раздел 3 (уборка после аварии — блокировка вместо pid), раздел 6 (WIP через временный индекс), раздел 13 (`runs/<run>/lock`), раздел 14 (2.4).
- Тесты: `untracked` и игнор, WIP (изменение, удаление, новый файл с `[` в имени, чужой `.env` не попадает, индекс и HEAD на месте), пустой WIP; `how_to_resume` на всех ветках; `hold` эксклюзивна и освобождается; `reap` гасит «осиротевший» процесс с `--session-id` в argv, сохраняет WIP, ставит паузы, после чего `how_to_resume` даёт `--resume`; живой прогон не трогается; `loop selfcheck`, убитый SIGTERM, гасит поддельный `claude` (без финализатора тест падает); миграция старой базы с шагом.
- Чего нет:
  - Проверки на реальном CLI: в этой сессии её не запускали. Главное, что надо подтвердить, — у процесса реального `claude` в `ps -A -o pgid=,command=` есть отдельный токен с `session_id`, иначе `reap` осиротевшую сессию не найдёт. Проверить: `guard.prepare` + `start_step` + `step.run` на `haiku` в пустом каталоге и параллельно `recovery._groups(session.session_id)` — должна найтись одна группа, итог `completed`, в БД снимок лимитов.
  - Вызывающего кода: `hold` перед `running`, `save_wip` на обычной паузе, `how_to_resume` при возобновлении, промпт для `--resume` и для handoff — это команда `run` и сборка промпта (этап 3).
  - `down` профиля в финализаторе и в `reap`, lock рабочего каталога `.git/agent-loop.lock` (для него подойдёт `recovery.hold`) — 3.1, вместе с профилем.
  - Сессия, которая умерла до `init` (оркестратор убит сразу после `start_step`): `how_to_resume` всё равно предложит `--resume` несуществующей сессии. Что CLI отвечает на `--resume` неизвестного id, не проверено; решать в этапе 3, когда будет реакция на ошибку возобновления.
  - SIGTERM в узкое окно внутри `agent.Process.__init__` (процесс уже запущен, блок `with` ещё не начат) оставит сессию сиротой до следующего `reap`.

Проверка:
```
uv run pytest -q            # 153 теста
uv run ruff check . && uv run ruff format --check .
uv run loop gc
```

### 2.5 `loop selfcheck` в своём модуле, проверка guard на реальном CLI — готово 2026-10-11

Ветка `guard-n-budget`.

Сделано:
- `src/agent_loop/selfcheck.py` — selfcheck вынесен из `agent` (`agent.selfcheck` → `selfcheck.run`, `SELFCHECK_MODEL` → `selfcheck.MODEL`, `SELFCHECK_FILE` → `selfcheck.RESULT_FILE`). Циклического импорта больше нет: `selfcheck` стоит над `agent`, `guard` и `recovery`.
- Третья сессия `guard`: `guard.prepare` (настройки цикла, sandbox и hook) и stop-файл `interrupt` до старта; агента просят прочитать файл. Проверяется: hook остановил сессию по stop-файлу (`guard.json`, причина `interrupt`), агент записал `.loop/handoff.md`, ответ по схеме пришёл (`StructuredOutput` пропущен), на `init` сессия находится в `ps` по `session_id` (`recovery.session_groups`, бывшая `_groups`). Файл лимитов для этой сессии — в каталоге selfcheck и не существует: реальный расход не превращает просьбу остановиться в остановку по бюджету.
- Общие проверки каждой сессии (`init` с нашим id, usage, `rate_limit_event`, нет ошибки) — в `_check_session`, проверки ответа — у каждой сессии свои.
- Подделка `claude`: сценарий `hooked` для новой сессии с hook в `--settings`, команды `sleep:<секунды>` и `handoff`. Тесты selfcheck — в `tests/test_selfcheck.py`: успешный прогон (hook отклонил `Read` и пропустил `StructuredOutput`), список всех проблем, включая три проблемы guard и ненайденную в `ps` сессию, независимость от реального `limits.json`.
- На реальном CLI (2.1.296, `haiku`), 20 с: все три сессии зелёные. В потоке `guard`: `Read` отклонён hook'ом, `Write` handoff, `StructuredOutput`, `terminal_reason: completed`, `guard.json` — `interrupt`, мягко, 1 отказ. Этим закрыта проверка из 2.4: сессия реального `claude` находится в `ps` по `session_id` (находка — в плане, раздел 2).
- Там же вручную проверен `step.run` из 2.3 на реальном CLI (скрипт вне репозитория, отдельный `home`): `completed`, `live.json` с контекстом 21k, `limits.json` и снимок лимитов в БД, `detail` шага заполнен.

Проверка:
```
uv run pytest -q            # 154 теста
uv run ruff check . && uv run ruff format --check .
uv run loop selfcheck       # ~20 с, реальный claude на haiku, три сессии
```

Этап 2 закрыт. Ревью этапа 2 от 2026-10-11 дало список правок — этап 2а ниже, он идёт перед этапом 3.

## Этап 2а. Правки по ревью этапа 2 — готово 2026-10-11

Ветка `guard-n-budget`, смержена в `main` (PR #1). Ревью этапа 2 от 2026-10-11 проверило и признало верными: формулы `cap_5h`/`cap_7d`/`resume_at` и границы; счётчик отказов, пропуск `StructuredOutput` и handoff в hook; идемпотентность `finish_step`; защиту `reap` от параллельного `loop`; восстановление обработчика SIGINT; выход по SIGTERM через `SystemExit` с гашением группы.

Сделано:
1. **Простой потока считается по любой строке.** `agent.Process.last_output` — монотонное время последней строки `stream-json`, разобранной или нет (результаты инструментов `user` адаптер не отдаёт, но они признак жизни). Поток-наблюдатель `step._Watch` берёт его перед каждой проверкой; `Deadlines.last_event` → `last_output`. Тест: три паузы по 0,6 с между строками `user` при таймауте простоя 1,2 с — `completed`.
2. **Guard при своём падении блокирует вызов.** `guard.main` ловит любое исключение, пишет в stderr причину и просьбу завершить ход, фиксирует `guard.json` с причиной `guard_error` (жёстко) и выходит с кодом 2 (`guard.BLOCK`): CLI отклоняет вызов, текст доходит до агента, `step._outcome` даёт `paused(guard_error)`. Тест: hook отдельным процессом без `live.json`.
3. **`reap` переживает сломанный прогон.** `GitError` при сохранении WIP одного прогона — ошибка в лог, прогон остаётся `running`, шаг открытым, следующий `loop` повторит; остальные прогоны доубираются. Находка по ходу: у удалённого репозитория `subprocess` падает с `FileNotFoundError` (нет `cwd`) ещё до git — `git.git` превращает `OSError` в `GitError`. Тест: прогон с удалённым репозиторием рядом с нормальным.
4. **Эскалация до SIGKILL.** `agent.Process.stop()` после SIGTERM ждёт `agent.ORPHAN_GRACE` (10 с, перенесена из `recovery`) и добивает группу `kill()`; третий Ctrl+C в `step` — сразу `kill()` (второй ещё ждёт выхода после SIGTERM). Тесты: подделка с `ignore-term` — и в `agent`, и второй Ctrl+C в `step`.
5. **Git оркестратора не исполняет код из репозитория.** Каждый вызов `git.git` идёт с `-c core.hooksPath=/dev/null -c core.fsmonitor=false` (`git.SAFE_CONFIG`). Тест: `save_wip` в репозитории с hook'ами `post-index-change`, `reference-transaction` и `core.fsmonitor`, пишущими маркер, — маркера нет, а обычный `git update-ref` его создаёт. `Edit(.git/**)` в deny.
6. **Мелочи.**
   - `step.how_to_resume(steps, ctx_soft_tokens)`: пауза с контекстом ≥ мягкой границы считается паузой по контексту (новая сессия с handoff, вторая такая — `replan`). У шагов мёртвого прогона контекста в `detail` нет — они не считаются.
   - `git.save_wip(repo, ref, untracked_before, base)`: WIP строится от `base`; `reap` передаёт `checkpoint` шага и предупреждает в логе, если HEAD сдвинулся.
   - `loop-settings.json`: allow — `Read(./**)`, `Edit(./**)`, `TodoWrite`; `Glob`/`Grep` убраны (в 2.1.296 их нет, а голое правило дало бы им чтение без ограничений, если вернутся). В deny — чтение `~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.config/gcloud`, `~/.kube`, `~/.gnupg`, `~/.docker/config.json`, `~/.netrc`, `~/.git-credentials`, `~/.pypirc`, `~/.npmrc`.
   - `recovery.session_groups` требует в команде токен с именем `claude`: `grep <id>` оператора не получит SIGTERM. Тест `reap` держит рядом такой процесс и проверяет, что он жив.
- Проба на реальном CLI (2.1.296, `haiku`, скрипт вне репозитория), находки — в плане, раздел 2, «Находки этапа 2а». Главное: `Read(./**)` не ограничивает Bash, Bash в sandbox читает весь `$HOME`, поэтому каталоги с ключами закрыты правилами deny (они попадают в sandbox); запись в `.git/hooks` и `.git/config` уже закрыта CLI и sandbox, флаги git — второй рубеж. Раздел 10 плана дополнен.
- Не чинили (решение ревью): «прилипающий» `guard.json` — на этапе 3 источник правды о готовности шага — ответ по схеме; `budget.save_limits` без блокировки — пока нет параллельных прогонов.

Чего нет:
- Фильтров `clean`/`smudge` и `textconv` из `.git/config` флаги git не отключают; сейчас их закрывает только запрет записи в `.git/config` (CLI и sandbox). Если в gates появится `git diff` с выводом содержимого — давать `--no-ext-diff --no-textconv`.
- Чтения через Read из `$TMPDIR` Bash (`/tmp/claude-<uid>`) нет — агент читает такие файлы через `cat`. Добавить `Read(//tmp/claude-*/**)`, если на реальных прогонах это будет мешать.
- Keychain macOS из Bash в sandbox (`security find-generic-password`) не проверяли.

Проверка:
```
uv run pytest -q            # 163 теста
uv run ruff check . && uv run ruff format --check .
uv run loop selfcheck       # ~20 с, guard на реальном CLI с новыми deny-правилами
```

Ревью этапа 2 (2026-10-11, после правок 2а) подтвердило формулы бюджета (перебор по сетке расхода и времени: `resume_at` всегда первая разрешённая секунда, иногда на секунду позже возможного), поиск сессии в `ps` на macOS (командная строка не обрезается до 60k символов и при `COLUMNS=80`), правила `Read(.env*)`/`Read(*.pem)` (по документации матчат на любой глубине). Нашло дыру в изоляции - проектные `.claude/settings*.json` и `.mcp.json` подхватываются в следующей сессии, это этап 2b в плане (раздел 14) - и пробелы для вызывающего кода, они записаны в плане во вводной к этапу 3.

## Этап 2b. Настройки проекта не принадлежат агенту — готово 2026-10-11

Ветка `guard-n-budget`, как и 2а; смержена в `main` (PR #1).

Сделано:
1. `agent.command`: всегда `--strict-mcp-config` — серверы только из `--mcp-config`, то есть никакие; `.mcp.json` репозитория не подключается.
2. `loop-settings.json`: в deny `Edit(.claude/**)` и `Edit(.mcp.json)`.
3. `src/agent_loop/protected.py`:
   - `PATHS`: `.claude/settings.json`, `.claude/settings.local.json`, `.claude/rules`, `.claude/hooks`, `.mcp.json`. `.claude/hooks` добавлен к списку плана: обычное место скриптов проектных hook'ов, они тоже идут вне sandbox.
   - `save(repo, run_dir)` — копия этих путей в `runs/<run>/protected/` при старте прогона (символические ссылки копируются ссылками). Каталог создаётся всегда: отсутствие файла в копии значит, что его не было.
   - `restore(repo, run_dir) -> list[str]` — сверка по сигнатурам (sha256 содержимого, цель ссылки, тип для fifo и прочих особых файлов — fifo не читается, иначе цикл повиснет); отличающиеся пути удаляются и, если были в копии, возвращаются из неё. Без копии — `FileNotFoundError`, иначе файлы человека сочлись бы новыми и удалились.
   - Копия от старта прогона, а не от чекпоинта шага: иначе правка агента в прерванной сессии стала бы эталоном для `--resume`. Если человек сам поменяет эти файлы посреди прогона, шаг тоже встанет — осознанно.
   - `save` отказывается работать (`ProjectHooksError`, копия не создаётся), если в `.claude/settings.json` или `.claude/settings.local.json` есть непустой `hooks`: hook человека идёт вне sandbox и исполнил бы файлы, которые пишет агент (решение 2026-10-11, план, раздел 3). Пустой `"hooks": {}` допустим.
4. `step.run` перед запуском сессии вызывает `protected.restore`: если что-то откатано — ошибка в лог со списком файлов, шаг `failed` с причиной `step.PROJECT_SETTINGS` (`project_settings`) и `detail["restored"]`, сессия не стартует. Перевод задачи в `needs_human` — забота вызывающего (этап 3). Вызывающий обязан сделать `protected.save` при старте прогона.
- Тесты: `tests/test_protected.py` — отказ от репозитория с hook'ами в каждом из двух файлов настроек, пустые `hooks` допустимы; откат новых настроек, локальных настроек, `.mcp.json`, скрипта hook'а, ссылки вместо файла, ссылки на каталог, fifo, каталога вместо файла, удалённого правила; посторонние файлы (`app.py`, `.claude/commands`) не трогаются; ссылка человека сохраняется и восстанавливается; без копии — ошибка. В `test_step` — сессия с hook'ом в `.claude/settings.json` репозитория не стартует (поддельный `claude` не вызван, файл удалён). `--strict-mcp-config` — в тесте аргументов адаптера.
- Находка: в `claude --help` (2.1.296) есть `--restricted` — игнорирует проектные настройки целиком и разрешает запись в настройки и git только человеку. Может заменить сверку хешей, нужен спайк (план, раздел 2, «Находки этапа 2b»).

Чего нет:
- Прогона на реальном CLI: спайк (подхватываются ли `.claude/settings.local.json` и `.mcp.json` при `--setting-sources project`, отключает ли их `--strict-mcp-config`) в этой сессии не запускали. `loop selfcheck` проверит, что CLI принимает `--strict-mcp-config`.
- Вызова `save` при старте прогона и вывода `ProjectHooksError` оператору — это команда `run` (этап 3).
- Hook'ов из плагинов, включённых проектными настройками (`enabledPlugins`), отказ не учитывает.
- Сверки после сессии: её сделают gates (3.1) той же функцией `protected.restore`.

Проверка:
```
uv run pytest -q            # 179 тестов
uv run ruff check . && uv run ruff format --check .
uv run loop selfcheck       # CLI принимает --strict-mcp-config
```

Ревью ветки `guard-n-budget` целиком (2026-10-11, перед мержем): критических проблем нет, 179 тестов стабильны на четырёх прогонах, `loop selfcheck` на 2.1.296 зелёный (CLI принимает `--strict-mcp-config`, guard остановил сессию по stop-файлу). Находки - в плане, раздел 14, шаг 3.0: незащищённые `.claude/skills`/`agents`/`commands` и обход deny через символическую ссылку (спайк), `protected.save` без `exist_ok`, `write_atomic` с коллизией временного файла внутри процесса, `killed` важнее готового `result` в `step._outcome`, формулировка про ветки 2а/2b в этом файле.

## Этап 3. Цикл реализации

### 3.0 Подготовка по ревью ветки `guard-n-budget` — готово 2026-10-11

Ветка этапа 3 — `implementation-loop`, от `main` после мержа PR #1; все шаги этапа идут в ней.

Сделано:
1. **Спайк на реальном CLI** (2.1.296, `haiku`, настройки цикла с guard; скрипт вне репозитория). Находки — в плане, раздел 2, «Находки шага 3.0». Коротко:
   - hook из frontmatter skill'а или команды не выполняется: `Skill` отклоняет `dontAsk`, с `Skill` в deny инструмента нет;
   - Bash в sandbox не пишет ни в один путь под `.claude/` и в `.mcp.json` — сверка `protected` теперь третья линия, а не вторая;
   - обхода deny через символическую ссылку нет: правила сверяются по реальному пути, `Write` в ссылку CLI отклоняет;
   - `--restricted` работает с нашим hook'ом, sandbox и `--resume`, игнорирует проектный hook, но **не подхватывает CLAUDE.md рабочего каталога**. Переход не делали: это решение (раздел 3 плана), см. «Дальше».
2. `protected.PATHS` дополнен `.claude/skills`, `.claude/agents`, `.claude/commands`; `Skill` — в deny `loop-settings.json`. `loop selfcheck` с новым deny зелёный.
3. `protected.save` — контракт в докстринге: один раз на прогон, повторный вызов — `FileExistsError`; `loop resume` новую копию не снимает (иначе эталоном стала бы правка агента из прерванной сессии).
4. `files.write_atomic`: во временном имени, кроме pid, `threading.get_ident()` — поток-наблюдатель (`timeout`) и обработчик SIGINT (`interrupt`) больше не пишут в один временный файл.
5. `step._outcome`: ответ по схеме — источник правды о шаге, если guard не отклонил агенту ни одного вызова (`denied == 0` и не `guard_error`). Тогда шаг `completed`, даже если stop-файл пришёл, пока агент отвечал (guard пропустил `StructuredOutput`, но записал `guard.json`), или сессию убили после ответа (`idle`, второй Ctrl+C). `Outcome.reason` у такого шага — просьба остановиться, пришедшая за это время: после `interrupt` вызывающий останавливает прогон, когда шаг доведён до коммита. Если guard хоть раз отклонил вызов — по-прежнему `paused` с его причиной: ответ агента в этом случае — отчёт о паузе.
6. progress.md: ветки 2а и 2b — `guard-n-budget`, смержена в `main`.

Тесты: новый `tests/test_files.py` (два потока пишут один файл); в `test_protected` — skill с hook'ом откатывается, повторный `save` падает (пример незащищённого файла — `.claude/output-styles`, `commands` теперь под защитой); в `test_step` — шаг с ответом `completed` с причиной `interrupt` в двух сценариях. Все новые тесты, кроме контракта `save`, падают на старом коде.

Проверка:
```
uv run pytest -q            # 184 теста
uv run ruff check . && uv run ruff format --check .
uv run loop selfcheck       # Skill в deny не ломает сессии
```

Предлагаемое сообщение коммита 3.0 (если ещё не закоммичено): `fix(step): apply guard-n-budget review findings before stage 3`.

### 3.1а Спайк `--restricted` — готово 2026-10-11, зелёный

Решение человека (обсуждение 2026-10-11): переходим на `--restricted`, если выигрыш подтвердится спайком. CLAUDE.md в большинстве его репозиториев нет, контекст проекта идёт через `context_file`, так что потеря нативной загрузки CLAUDE.md почти ничего не стоит. Выигрыш: проектные настройки игнорируются целиком, а не по списку путей; работа в репозиториях с проектными hook'ами; в сессии 4 инструмента вместо ~22; уходит `--setting-sources project`.

Сделано (кода в репозитории нет, только план):
- Спайк на реальном CLI (2.1.296, `haiku`, effort `low`): `guard.prepare` + `agent.Process` с подменённой `agent.command` — `--restricted --tools Bash,Read,Edit,Write` вместо `--setting-sources project`. Скрипт лежал в scratchpad сессии и не сохранится; восстанавливается по этому описанию: рабочий каталог — git-репозиторий с набором конфигов, `.env`, `.claude/skills`, `.claude/rules`, `.mcp.json` с сервером, пишущим маркер; агента просят выполнить список действий и ответить по схеме `{results: [{item, ok, output}]}`; сверка — `git status`, `permission_denials`, вызовы инструментов из потока.
- Находки — в плане, раздел 2, «Находки шага 3.1а». Коротко:
  - блокера нет: `Edit` правит `pyproject.toml`, `requirements*.txt`, линтеры, `Makefile`, `Dockerfile`, CI, `.gitignore`, `conftest.py`;
  - закрыты для `Edit` только конфиги менеджеров git-hook'ов и IDE: `.pre-commit-config.yaml`, `.husky/*`, `lefthook.yml`, `.devcontainer/*`, `.idea/*`, `.vscode/settings.json`, `.npmrc`; `allow` из нашего hook'а запрет не снимает;
  - Bash в sandbox их пишет (кроме `.vscode`, `.idea`) — в обеих схемах. Подложенный `.pre-commit-config.yaml`/`.husky`/`lefthook.yml` выполнит код при `git commit` человека: в план добавлен риск (раздел 10) и проверка gates (раздел 7, раздел 14 шаг 3);
  - `StructuredOutput` при `--json-schema` добавляется сам; `TodoWrite` в 2.1.296 нет вообще;
  - deny из `--settings` (`.env*`, `~/.ssh`, абсолютный путь), сеть наружу закрыта, localhost открыт, `git status`/`diff`/`log` работают, `git commit` отклонён;
  - игнорируются проектные skills, rules, CLAUDE.md, `.mcp.json`; без `--strict-mcp-config` подключаются коннекторы claude.ai — флаг остаётся.
- План: раздел 2 (находки), раздел 3 (строка «Переход на `--restricted`»), раздел 7 (строка «Hook'и человека»), раздел 10 (риск), раздел 14 (3.1а сделан, gates).

Проверка: тестов и кода нет; `uv run pytest -q` — по-прежнему 184 теста.

Предлагаемое сообщение коммита 3.1а: `docs(plan): record the --restricted spike and switch to it in 3.1b`.

### 3.1б Переход на `--restricted` — готово 2026-10-11

Сделано:
1. `agent.command`: `--restricted --tools Bash,Read,Edit,Write` (`agent.TOOLS`) вместо `--setting-sources project`; `--strict-mcp-config` остаётся. `Session.disallowed_tools` и `--disallowed-tools` убраны: второго применения нет, `Agent` в сессию не попадает и так.
2. `agent.project_instructions(cwd)`: `CLAUDE.md` и `.claude/CLAUDE.md` рабочего каталога с `@path`-импортами (пути от импортирующего файла, `~` разворачивается, глубина `IMPORT_DEPTH = 5`, в код-спанах и блоках ``` не ищутся, повторы и циклы отсекаются). Каждый файл — блоком `Contents of <путь>:`. `agent.Process` дописывает их после `system_files` в тот же `--append-system-prompt-file` для каждой сессии, в том числе `--resume`.
   - Импортированный файл читается, только если он закоммичен в HEAD (`git ls-tree`): CLAUDE.md агенту править можно, и `@.env` или `@~/.ssh/config` затянули бы в промпт следующей сессии файл, который ему читать нельзя. Индекс не годится: `git add` агенту доступен. CLAUDE.md-ссылка (обычно `CLAUDE.md -> AGENTS.md`) подчиняется тому же правилу. git вызывается, только если есть что проверять, — selfcheck вне git работает.
   - Хвостовая пунктуация (`@docs/x.md,`) не отрезается — импорт не найдётся; как это делает CLI, не проверяли.
3. `selfcheck`: CLAUDE.md рабочего каталога проверяется уже как наша передача; в `.claude/settings.json` каталога — hook'и `SessionStart` и `PreToolUse`, пишущие маркер; маркер после трёх сессий — проблема «`--restricted` no longer ignores them».
4. `protected`: отказ `ProjectHooksError` и `SETTINGS_FILES` убраны, вводная модуля переписана: сверка защищает следующую интерактивную сессию человека. `save`/`restore` и вызов в `step.run` не менялись.
5. `loop-settings.json`: из allow убран `TodoWrite`, из deny — `Skill`. `WebFetch`/`WebSearch` в deny оставлены: инструментов в сессии нет, но запрет страхует, если их добавят в `TOOLS`.
6. План: разделы 2, 3, 7 (субагенты), 8 (CLAUDE.md), 10, 14.

Тесты: в `test_agent` — аргументы `--restricted`/`--tools`, CLAUDE.md рабочего каталога в файле правил `Process`, импорты (закоммиченные — да; `.env`, незакоммиченный файл, `~/...`, несуществующий, адрес почты, код — нет; цикл), глубина, CLAUDE.md-ссылка на `.env` и на закоммиченный `AGENTS.md`. В `test_selfcheck` — проектный hook, выполненный подделкой `claude` (новая команда `project-hook` в `fakes.py`), даёт проблему. Тесты отказа `ProjectHooksError` удалены.

Проверка:
```
uv run pytest -q            # 184 теста
uv run ruff check . && uv run ruff format --check .
uv run loop selfcheck       # 2.1.296: зелёный; в init 5 инструментов (Bash, Edit, Read, StructuredOutput, Write), mcp_servers пуст
```

Предлагаемое сообщение коммита 3.1б: `feat(agent): run sessions with --restricted and pass the project CLAUDE.md`.

### 3.2 Профиль репозитория и lock — готово 2026-10-11

Сделано:
1. `config`: `Profile` — `commands`, `timeouts`, `env`, `deps_files`, `secret_patterns` из `loop.toml`, в `Config.repo` (без профиля — пустой `Profile()`). Неизвестный ключ верхнего уровня, команда не из `config.COMMANDS` (`install`, `fmt`, `lint`, `test`, `test_full`, `up`, `down`, `smoke`) в `[commands]` или `[timeouts]`, не строка в `[commands]`/`[env]`, не целое > 0 в `[timeouts]` (`true` тоже), не список строк в `deps_files`/`secret_patterns` — `ConfigError`. Команда с пустой строкой выбрасывается: шаг пропускается. Значения `[env]` — только строки (`PORT = 5433` без кавычек — ошибка).
2. Новый модуль `src/agent_loop/commands.py`:
   - `freeze(profile, run_dir, run_id)` — подстановка `{run_id}` (команды и `[env]`) и `{free_port}` (только `[env]`, каждый — свой порт: сокеты держатся до выбора всех), результат в `runs/<run>/profile.json`. Один раз при старте прогона; `frozen(run_dir)` читает его для `loop resume` и `reap`. `{changed_tests}` остаётся — его подставит gate. Правка `loop.toml` посреди прогона на него не влияет.
   - `run(profile, name, repo)` — команда через shell в корне репозитория, окружение процесса плюс `[env]`, stdout и stderr вместе; таймаут из `[timeouts]` или `DEFAULT_TIMEOUT` (10 мин). Своя группа процессов: по таймауту, Ctrl+C или SIGTERM оркестратора SIGKILL получает вся группа. Ненулевой код или таймаут — `CommandError` с хвостом вывода (20 строк).
   - `services(profile, repo)` — контекстный менеджер прогона: `up` на входе, `down` в `finally` (выход по SIGTERM/SIGHUP — `SystemExit`, см. `cli`, — тоже его проходит; упавший `up` тоже). `down(profile, repo)` ошибку только пишет в лог: запущенные сервисы не портят итог прогона.
3. `recovery`: `hold_repo(repo)` — lock рабочего каталога `<git-dir>/agent-loop.lock` через `hold` (`--absolute-git-dir`: у worktree свой lock). `reap` после паузы мёртвого прогона делает `down` его замороженного профиля, если профиль успели заморозить.
4. `agent.Session.env` — `[env]` профиля поверх `ENV_KEYS` в окружении `claude`.
5. План: раздел 9 (замороженный профиль, `up`/`down`, таймаут по умолчанию), раздел 13 (`profile.json`), раздел 14 (шаг 2 сделан).

Контракты для команды `run` (3.3–3.5): порядок старта — `hold_repo` (иначе отказ: в каталоге уже идёт прогон) → `create_run` → `hold(runs/<run>/lock)` → `commands.freeze` → `move_run(running)` → `with commands.services(...)`; `loop resume` — `hold_repo`, `hold`, `commands.frozen` вместо `freeze`. Сессиям передавать `env=profile.env`.

Тесты: в `test_config` — полный профиль, пустой профиль по умолчанию, 11 новых случаев в `test_invalid_profile`; новый `tests/test_commands.py` — подстановка и разные порты, `frozen` возвращает то же, окружение и каталог команды, хвост вывода, таймаут убивает дочерний процесс оболочки (тест падает, если убивать только оболочку), `down` после блока, при `SystemExit` и при упавшем `up`, упавший `down` только в логе; в `test_recovery` — lock в git-каталоге и отдельный у worktree, `reap` делает `down` только мёртвого прогона; в `test_agent` — переменная из `Session.env` доходит до `claude`.

Чего нет:
- Вызывающего кода: `freeze`/`services`/`hold_repo` вызовет команда `run`.
- `loop config` команды профиля не показывает — только путь к нему.
- `{free_port}` в `[commands]` не подставляется и не считается ошибкой — останется в команде как есть.
- Окружение gate-команд — всё окружение оркестратора плюс `[env]`; тесты агента идут вне sandbox. Решать вместе с gates (3.3), если нужно.

Проверка:
```
uv run pytest -q            # 205 тестов
uv run ruff check . && uv run ruff format --check .
```

Предлагаемое сообщение коммита 3.2: `feat(commands): load the repository profile, freeze it per run, lock the working directory`.

### 3.3а `gates`: инварианты репозитория — готово 2026-10-11

Решения человека (2026-10-11, перед шагом): шаг 3.3 разбит на 3.3а (инварианты без команд профиля) и 3.3б (команды); HEAD или ветка сдвинулись — всегда пауза, без отката; число тестов — необязательная команда `collect`; упавшие тесты — повтор команды `test`. Записано в плане, раздел 3.

Сделано:
1. `git`:
   - `branch` — текущая ветка (`HEAD` при detached);
   - `new_files(repo, base, untracked_before)` — файлы, добавленные шагом: новые неотслеживаемые плюс добавленные агентом в индекс (`git add`), минус неотслеживаемые до шага. `save_wip` берёт их же: раньше новый файл, добавленный агентом в индекс, в WIP не попадал;
   - `diff(repo, base, added, *options, paths=)` — дифф работы шага против чекпоинта через отдельный индекс (общий с `save_wip` `_work_index`), с `--no-ext-diff --no-textconv --no-renames`: драйверы diff и textconv из конфига и `.gitattributes` не запускаются.
2. Новый модуль `src/agent_loop/gates.py`, `check(repo, run_dir, branch, step, profile, settings) -> Verdict`. `Verdict.status` — `pass` или статус, в который переводить задачу: `fixing`, `paused`, `needs_human`, `replan`; плюс `reason`, `findings`, `secrets` (новые файлы, которые не коммитить), `lines`. Порядок:
   - `branch_moved` (ветка и HEAD против чекпоинта шага) → `paused(branch_changed)`, ничего не трогая: это может быть работа человека. Функция публичная — её же звать перед шагом;
   - `protected.restore` — до снятия диффа, чтобы откаченное не считалось работой шага;
   - секреты: новые файлы по `SECRET_PATTERNS` (`.env*`, `*.pem`, `*.key`, `*credentials*`) и `secret_patterns` профиля, по имени и по пути, без учёта регистра; из диффа и размера исключаются;
   - откаченные настройки CLI → `needs_human(project_settings)`; изменённые конфиги менеджеров git-hook'ов (`HOOK_CONFIGS`: `.pre-commit-config.y(a)ml`, `.husky/`, варианты `lefthook`) → `needs_human(hook_config)` с диффом в находке; файл в дереве остаётся;
   - размер по `--numstat` (бинарный файл — 0 строк): больше `diff_hard_lines` → `replan(diff_too_big)`, больше `diff_soft_lines` — предупреждение в лог;
   - ослабление тестов: тестовый файл — в каталоге `test`/`tests`/`__tests__` или с именем `test_*`, `*_test.*`, `*.test.*`, `*.spec.*`; по `-U0`-диффу всех тестовых файлов вместе считается прибавка строк со skip/xfail (pytest, unittest, Go, JS) и убыль строк с assert (`assert*`, `expect(`, `require.`, `t.Error*`/`t.Fatal*`) → `fixing(weakened_tests)` с файлами. Изменённый assert, перенесённый тест, новый тест — не находка; удалённый тестовый файл — находка.

Чего нет:
- Откат к чекпоинту не написан: после решения «всегда пауза» второго применения нет; понадобится — в 3.4.
- Секреты среди изменённых отслеживаемых файлов не ищутся — только среди новых (как в плане).
- «Если без файла-секрета не собирается — `needs_human`» не проверяется: тесты идут по дереву, где файл есть.
- Вызывающего кода нет: `check` и `branch_moved` позовёт цикл в 3.4.

Тесты: новый `tests/test_gates.py` — чистая работа с размером, секреты (в т. ч. шаблон профиля, неотслеживаемый до шага `.env` не в счёт), три вида сдвига ветки без отката настроек, откат настроек, три конфига git-hook'ов, границы размера, размер без файлов человека, четыре вида ослабления, четыре правки без ослабления. В `test_git` — `new_files` с файлом из индекса; тест «git цикла не запускает код репозитория» дополнен `diff.external`, textconv и командой драйвера (падает без `--no-ext-diff`/`--no-textconv`).

Проверка:
```
uv run pytest -q            # 228 тестов
uv run ruff check . && uv run ruff format --check .
```

Предлагаемое сообщение коммита 3.3а: `feat(gates): check the branch, protected files, hook configs, secrets, diff size and weakened tests`.

### 3.3б `gates`: команды профиля — готово 2026-10-11

Сделано:
1. `config.COMMANDS` — новая необязательная команда `collect` (между `lint` и `test`).
2. `commands.OUTPUT_TAIL_LINES` — 200 строк вместо 20: хвост вывода — это находка для fixer'а. Отдельного хвоста для лога не заводили: в лог идут только ошибки `up`/`down`.
3. `store`: миграция 4 — `tasks.tests` (число тестов перед первым шагом задачи), `Task.tests`, `Store.set_tests(task_id, count)`. Эталон на задаче, а не на шаге: после первого шага дерево уже не совпадает с чекпоинтом, а эталон нужен и после `loop resume`.
4. `gates`:
   - `check(..., tests_before)` — новый обязательный параметр: `Task.tests`; `None` — число тестов не сравнивается (и `collect` в gate не запускается);
   - `count_tests(profile, repo)` — `collect` профиля, первое целое в последней непустой строке; нет команды — `None`; нет числа — `ConfigError` (ошибка профиля, не работы). Её же цикл зовёт перед первым шагом задачи;
   - команды идут, только если инварианты дали `pass`. Порядок: `install`, если среди изменённых файлов (пути `numstat`, удалённые тоже) есть совпадающие с `deps_files` (`_matches`: по имени и по пути); упал — сразу `fixing(install_failed)`, остальное не запускается. Дальше `fmt`, `lint`, `collect`, `test` — все, чтобы fixer получил все падения сразу; `reason` — первое из `fmt_failed`, `lint_failed`, `collect_failed`, `weakened_tests` (меньше тестов, чем `tests_before`), `test_failed`, `flaky_tests`; находки — `str(CommandError)`: строка с кодом выхода и хвост вывода;
   - `{changed_tests}` подставляется во все команды: изменённые шагом тестовые модули — по имени (`TEST_NAME`: `test_*`, `*_test.*`, `*.test.*`, `*.spec.*`), существующие в дереве, через `shlex.quote`, через пробел; нет — пустая строка. `conftest.py`, фикстуры в `tests/` и удалённые модули не передаются: раннер счёл бы их ошибкой;
   - упавший `test` повторяется один раз. Упал снова — `test_failed` с выводом второго прогона. Прошёл — флакающий: если имя изменённого тестового модуля есть в выводе первого прогона — `fixing(flaky_tests)`, иначе `Verdict("pass", "flaky_tests", (вывод,))` — предупреждение для отдельной задачи в конце плана.
5. План: раздел 3 (решение про эталон и флакающий тест), раздел 9 (`collect` в примере профиля), раздел 14 (шаг 3 сделан).

Чего нет:
- `{changed_tests}` — изменения против чекпоинта шага, а не всей задачи: до коммита это одно и то же; после коммита задачи (ревью, этап 5) — только тесты правки ревью.
- `install` повторяется на каждом gate задачи, пока файлы зависимостей отличаются от чекпоинта (fixer тоже идёт от того же чекпоинта).
- Флакающий тест, не тронутый задачей, теряется, если в том же gate упала другая команда: в находках только падения.
- Вызывающего кода нет: `count_tests` + `set_tests` перед первым шагом и `check` после шага позовёт цикл в 3.4.
- **Gate-команды запускают код агента вне sandbox** (`pytest` исполнит `conftest.py` агента с сетью и `$HOME`, `install` — `setup.py`/build-скрипты из правки зависимостей). Так было задумано планом с самого начала, но это обходит всю защиту sandbox. Решение — шаг 3.3в, см. «Дальше».

Тесты: в `test_gates` — `install` по `deps_files` (и не запускается без изменений), упавший `install` останавливает gate, все упавшие команды в находках и повтор `test`, команды не идут при нарушенных инвариантах, `{changed_tests}` (пробел в имени, `conftest.py` и `.go`), удалённый модуль не передаётся, флакающий тест задачи и чужой, число тестов (4 вывода), без эталона `collect` не запускается, упавший `collect`, `count_tests` без команды и без числа. В `test_store` — `set_tests` и `tests` после миграции старой базы; в `test_config` — `collect` в полном профиле; в `test_commands` хвост проверяется на 300 строках.

Проверка:
```
uv run pytest -q            # 244 теста
uv run ruff check . && uv run ruff format --check .
```

Предлагаемое сообщение коммита 3.3б: `feat(gates): run install, fmt, lint, collect and test of the profile, rerun failed tests once`.

### 3.3в Sandbox для gate-команд — готово 2026-10-11

Спайк (скрипты в scratchpad, итог — план, раздел 2, «Находки шага 3.3в»): `sandbox-exec` работает, но набросок от `(allow default)` дырявый — `(local ip "localhost:*")` открывал всю сеть, `open -a` запускал приложения вне sandbox, `defaults write` писал чужие настройки через `cfprefsd`, `mv`/`ln` обходили запрет чтения внутри репозитория, запись в `.git/hooks` не была закрыта. Профиль от `(deny default)` по образцу `@anthropic-ai/sandbox-runtime` 0.0.79 (sandbox CLI) закрыл всё это; Postgres из docker на localhost, `ruff`, 240 из 244 тестов agent-loop на нём зелёные.

Решения человека (2026-10-11, после спайка; план, раздел 3): свой профиль от `(deny default)` без node/srt; `.env*` gate-командам открыт, значения маскируются и ищутся в диффе; окружение sandbox-команд — как у сессий.

Сделано:
1. `commands`:
   - `SANDBOXED` (`fmt`, `lint`, `collect`, `test`, `test_full`, `smoke`) идут через `sandbox-exec -p <профиль> /bin/sh -c <команда>`; окружение — `agent.ENV_KEYS` плюс `[env]`, `TMPDIR` — свой временный каталог на команду, удаляется после неё (иначе следующая сессия агента прочитала бы оставленное). `install`, `up`, `down` — как раньше: без обёртки, всё окружение.
   - `_sandbox(repo, tmp)` — `SANDBOX_BASE` (из srt: процессы, список mach-служб, sysctl, iokit; сеть — bind/inbound везде, outbound только `localhost`) плюс файлы: чтение всего, кроме `HOME_SECRETS` (как `Read(~/...)` в `loop-settings.json`, плюс `Library/Keychains`) и `REPO_SECRETS` (`*.pem`, `*.key` на любой глубине); запись — репозиторий, `tmp`, `/dev/null`, `/dev/stdout`, `/dev/stderr`, `/dev/fd/*`, кроме `OPERATOR_FILES` (`.git`, `protected.PATHS`, `protected.HOOK_CONFIGS`, `.vscode`, `.idea`, `.devcontainer`) и `REPO_SECRETS` + `.env*`. Имена — регулярками `[Cc][Ll]...` (`(?i)` seatbelt не понимает): на регистронезависимой ФС новый `.CLAUDE` — это `.claude`. Пути — строками JSON с `ensure_ascii=False` (`\uXXXX` seatbelt не читает: кириллица в пути ломала разрешение записи).
2. `protected.HOOK_CONFIGS` — список переехал из `gates` (нужен и sandbox, а `gates` импортирует `commands`).
3. `gates`:
   - `_env_secrets(repo)` — значения ключей с `KEY|TOKEN|SECRET|PASS|PWD|CREDENTIAL|AUTH|DSN|PRIVATE` в имени и длиной от 8 из всех `.env*` репозитория (`git ls-files --cached --others ':(glob,icase)**/.env*'`, игнорируемые тоже), `export` и кавычки снимаются;
   - после команд дифф снимается заново (команды могли записать прочитанное в работу): значение в добавленной строке — `needs_human(secret_in_diff)` с именами ключей; иначе находки идут через `_mask` — значение заменяется на `<DB_PASSWORD of .env>`;
   - `_is_hook_config` сравнивает без учёта регистра (`.PRE-COMMIT-CONFIG.YAML` раньше проходил мимо).
4. `selfcheck._check_gate_sandbox`: sandbox-команда должна создать файл в своём репозитории и не создать рядом; `sandbox-exec` нет — проблема в отчёте.
5. План: раздел 2 (находки), 3 (решение), 7 (строка «Значения из `.env*`»), 9 (окружение sandbox-команд, `uv run --no-sync --no-cache`, кэши через `$TMPDIR`), 10 (правила sandbox, остаток риска), 14 (шаг 3 (в) сделан).

Тесты: в `test_commands` — 11 отказов sandbox (запись рядом и в `~`, чтение `~/.ssh` и `*.pem`, `mv` секрета и `.env`, `.git/hooks`, `.CLAUDE/settings.local.json`, `.Pre-Commit-Config.yaml`, `.vscode`, соединение с `192.0.2.1`), каждый по «Operation not permitted»: без sandbox запись проходит, а сеть даёт таймаут; разрешённое (чтение `.env`, запись в репозиторий и `$TMPDIR`, удаление `$TMPDIR` после команды, соединение с localhost, окружение без `GITHUB_TOKEN`); `install` вне sandbox с полным окружением; `HOME_SECRETS` покрывает `Read(~/...)` из `loop-settings.json`. В `test_gates` — маскирование (ключ с `export` и кавычками, `.env.test` в подкаталоге, короткое значение и несекретный ключ не трогаются), `secret_in_diff`, `.PRE-COMMIT-CONFIG.YAML` в параметрах теста конфигов git-hook'ов; журнал вызовов команд (`calls`) переехал в репозиторий, в `.git/info/exclude`: вне репозитория sandbox писать не даёт. В `test_selfcheck` — запись рядом при выключенном sandbox попадает в список проблем.

Чего нет:
- Не macOS — `sandbox-exec` не найден, `FileNotFoundError` из `commands.run`. Отказ на старте прогона — в команде `run` (3.4–3.5): звать `selfcheck` или проверять `sandbox-exec` там.
- Тесты agent-loop, которые сами запускают `sandbox-exec` (`test_commands`, `test_gates`, `test_selfcheck`), под gate не пройдут: вложенный sandbox запрещён. Ещё 4 теста падают под sandbox из-за `ps` (setuid-бинарник) и `.env.example` (закрыт и сессиям). Цикл на самом agent-loop пока не пускать.
- Файлы, созданные gate-командами (отчёты, артефакты не из `.gitignore`), выглядят как новые файлы шага. Коммит кодом (3.5) должен брать список файлов с учётом этого.
- Значение `.env` может дойти до модели закодированным или через игнорируемый файл, который gate-команда оставит в репозитории (риск в разделе 10).
- `install` без sandbox по-прежнему исполняет build-скрипты; `docker-compose*.yml` и `[build-system]` → `needs_human` — кандидат, не сделано.

Проверка:
```
uv run pytest -q            # 261 тест
uv run ruff check . && uv run ruff format --check .
```

Предлагаемое сообщение коммита 3.3в: `feat(commands): run gate commands under sandbox-exec, mask .env values in findings`.

### 3.4а Промпт задачи, роли, схема ответа — готово 2026-10-11

Решения человека (2026-10-11, перед шагом; план, раздел 3): шаг 3.4 разбит на 3.4а (промпт) и 3.4б (цикл задачи); fixer — новая сессия с ролью `fixer.md` (карточка, дифф работы, находки gate), а не `--resume` сессии исполнителя.

Сделано:
1. Новый пакет `src/agent_loop/prompts/`:
   - файлы правил (английский): `session.md` — общие для исполнителя и fixer'а: что делает цикл после сессии (gate, проверка ослабления тестов, коммит, `install`), сети нет, файлы человека (`.git/`, `.claude/`, `.mcp.json`, конфиги git-hook'ов, `.idea`/`.vscode`/`.devcontainer`, `.npmrc`, секреты) — `Edit` отклоняется, обходить через Bash нельзя, сказать в ответе; CLAUDE.md вложенного каталога прочитать самому; отказ guard'а — записать handoff и закончить; смысл полей ответа. `implementer.md`, `fixer.md` — роли;
   - `AGENTS.md` — символическая ссылка на `AGENTS.md` в корне репозитория: один источник, в колесо (`uv build`) попадает содержимое;
   - `system_files(role, settings)` — `session.md`, роль, `AGENTS.md`, `context_file` (если задан) для `agent.Session.system_files`; CLAUDE.md репозитория к ним добавляет адаптер;
   - `ANSWER_SCHEMA` и `Answer`/`answer(structured_output)`: `status` (`done`, `too_big`, `needs_human`, `blocked`), `summary`, `commit_message` (≤ 72), `notes_for_next` — обязательные; `question`, `split` (список строк). Одна схема на обе роли;
   - `implementer(task, total, settings, previous, handoff=None, work=None)` — карточка (`goal`, `criteria`, `files`, `tests`, `decisions`; пустые списки — без раздела), размер (`diff_soft_lines`/`diff_hard_lines`), язык комментариев из `doc_lang`, сводка прошлых задач; при возобновлении в новой сессии (`work` не `None`) — handoff или указание идти по WIP-диффу, если handoff нет;
   - `fixer(task, total, settings, verdict, work)` — карточка, правила, дифф работы, `verdict.reason` и каждая находка в своём блоке;
   - `previous_tasks(repo, base, notes)` — `git diff --name-status` от HEAD старта прогона до HEAD и новые публичные имена Python-модулей по AST (функции, классы с методами, переменные, `type`; приватные и непарсящиеся файлы — без имён), плюс непустые `notes_for_next` прошлых задач парами (заголовок, заметки);
   - блоки кода с длинным забором, если в тексте есть свои ```` ``` ````.
2. План: раздел 3 (решение про fixer и разбиение), раздел 8 (`session.md` и роли), раздел 14 (шаг 4 (а) сделан).

Чего нет:
- `jsonschema` не добавлен: ответ проверяет сам CLI (`--json-schema` — «structured output validation», находки S3), `answer` только раскладывает его по полям.
- Формат карточки зафиксирован только докстрингом `prompts`: схему `plan.json` (этап 4) делать с этими пятью полями, все обязательные, списки могут быть пустыми.
- Сводка прошлых задач — только закоммиченное (до HEAD); имена — только Python.
- Вызывающего кода нет: всё позовёт цикл задачи в 3.4б.

Тесты: новый `tests/test_prompts.py` — файлы правил обеих ролей существуют и `AGENTS.md` совпадает с корневым, без `context_file` — три файла; поля `Answer` совпадают со схемой (и обязательные), `split` — кортеж; полный текст промпта исполнителя; первая задача без разделов прошлых задач и возобновления; возобновление с handoff и без, длинный забор вокруг диффа с ```` ``` ````; промпт fixer'а; сводка прошлых задач (изменённый, добавленный, удалённый файл, не-Python, непарсящийся `.py`, приватные имена и методы, пустые заметки) и пустая сводка на старте.

Проверка:
```
uv run pytest -q            # 271 тест
uv run ruff check . && uv run ruff format --check .
```

Предлагаемое сообщение коммита 3.4а: `feat(prompts): build the implementer and fixer prompts, add the role files and the answer schema`.

### 3.4б Цикл новой задачи — готово 2026-10-11

Решения человека (2026-10-11, перед шагом; план, раздел 3): шаг 3.4б разбит на 3.4б (новая задача) и 3.4в (продолжение остановленной); gate — шаг `kind="gate"` в `steps` с вердиктом в `detail`; остановка между сессиями оставляет задаче её статус, на паузу встаёт прогон, `paused` у задачи — только с приостановленным шагом.

Сделано:
1. Новый модуль `src/agent_loop/implement.py` (не `task`: имя затеняло бы переменные `task` вызывающего кода), `start(cfg, store, run, task, total, profile, previous) -> str | None` для задачи в `pending`:
   - эталон: `gates.count_tests` → `store.set_tests` (нет `collect` — не пишется), затем `implementing`;
   - чекпоинт и неотслеживаемые файлы снимаются один раз на старте задачи и общие для всех её шагов: работа задачи до коммита — работа против этого чекпоинта, кто бы из сессий её ни сделал;
   - сессия (`_session`): `guard.prepare` перед каждой, `agent.Session` с `model_implementer`/`effort_implementer` (у fixer'а те же — отдельных настроек нет), `prompts.system_files(role)`, `ANSWER_SCHEMA`, `env=profile.env`; шаг `implement` или `fix` в store с `session_id`, `log.scope(stage=kind)`, задача в логе — `seq/total`;
   - исход сессии (`_take`): `paused` → WIP в `git.wip_ref(run, seq)` через `save_wip` и `paused(<причина шага>)`; `failed` → `needs_human(project_settings)` или `needs_human(session_failed)`; ответа нет → `needs_human(no_answer)`; `too_big` → `replan(too_big)`, `needs_human` → `needs_human(question)`, `blocked` → `needs_human(blocked)`; `done` → `gating`. `split` и `question` лежат в `detail.answer` шага;
   - gate (`_gate`): шаг `gate` без сессии, `gates.check(..., tests_before=эталон)`, `asdict(verdict)` в `detail`; `paused` (ветка или HEAD сдвинулись) — шаг `paused`, WIP и `paused(branch_changed)`; `pass` — задача остаётся в `gating` для коммита (3.5); `needs_human`/`replan` — с причиной вердикта; `fixing` → `fixing(<причина>)` и fixer, но `weakened_tests` второй раз на задаче → `needs_human(weakened_tests)`, а после `fix_attempts` завершённых fixer'ов → `replan(fix_attempts)`;
   - fixer: `prompts.fixer` с вердиктом и диффом работы против чекпоинта задачи — новые файлы без `verdict.secrets` (содержимое файла-секрета в промпт не попадает);
   - Ctrl+C, пока агент отвечал (исход `completed` с причиной `interrupt`): ответ разбирается, задача переходит, куда он ведёт, и `start` возвращает `interrupt` до gate — задача остаётся, например, в `gating` без шага gate.
2. `config.load`: `home` — от `cwd`, если `LOOP_HOME` относительный (hook иначе искал бы `run_dir` от каталога сессии).
3. План: раздел 3 (решение про разбиение, шаг gate и остановку между сессиями), раздел 14 (шаг 4 (б) сделан, (в) записан).

Чего нет (всё — 3.4в, кроме оговорённого):
- Продолжения: `start` работает только с `pending`. Задача в `paused`, в `gating` после `interrupt` (gate ещё не было), в `implementing`/`fixing` с завершённым шагом (остановка между ответом и переходом) не продолжается.
- `.loop/handoff.md` не исключён из работы: handoff приостановленной сессии попадает в WIP и выглядит как новый файл задачи (в gate и в будущий коммит). Читать, удалять и не считать работой — в 3.4в; там же чистить устаревший handoff перед первой сессией задачи.
- Проверки бюджета перед сессией нет; проверки ветки и HEAD перед первой сессией задачи нет (`gates.branch_moved` — только в gate). Ветку при старте прогона сверит команда `run` (3.5).
- Повтор ослабления тестов узнаётся только по `verdict.reason`: убыль тестов по `collect` за упавшими `fmt`/`lint` — не причина вердикта и не считается.
- `agent.AgentError` из `step.run` и `ConfigError` из `count_tests`/gate не ловятся: задача остаётся в своём статусе, шаг сессии — `failed`, открытый шаг gate доуберёт `reap` как `died`. Что с этим делать, решит команда `run`.
- Коммит (3.5): задача после gate остаётся в `gating`; сообщение — `commit_message` из `detail.answer` последнего шага сессии, файлы-секреты — `secrets` из `detail` последнего шага gate, предупреждение `flaky_tests` — там же.

Тесты: новый `tests/test_implement.py` — сессия подменяется сценарием (действие с репозиторием и исход, шаг завершается в store, как это делает `step.run`), gate и git настоящие, профиль с `collect` и `test` (`grep` по `app.py`). Работа проходит gate (эталон, переходы, шаги, общий чекпоинт, поля `Session`, сводка прошлых задач в промпте); fixer получает находки и дифф с новым файлом, у всех шагов один чекпоинт; файл-секрет не попадает в промпт fixer'а (тест падает, если не фильтровать `secrets`); исчерпанные попытки → `replan(fix_attempts)`; ослабление дважды → `needs_human`; шесть исходов без `done` не идут в gate; пауза сохраняет WIP с новым файлом поверх чекпоинта; `interrupt` во время ответа — без gate; коммит в сессии → шаг gate `paused`, задача `paused(branch_changed)`. В `test_config` — относительный `LOOP_HOME`.

Проверка:
```
uv run pytest -q            # 286 тестов
uv run ruff check . && uv run ruff format --check .
```

Предлагаемое сообщение коммита 3.4б: `feat(implement): take a task through the implementer, gate and fixer`.

### 3.4в Продолжение остановленной задачи — готово 2026-10-11

Сделано:
1. `implement.resume(cfg, store, run, task, total, profile, previous)` рядом со `start`; оба возвращают `Stop | None`: `Stop("interrupt")` (Ctrl+C, пока агент отвечал) или `Stop("budget", resume_at)`, задача при этом остаётся в своём статусе. Общий цикл `_loop`: сессия → разбор её конца → gate → fixer. Продолжение идёт от последнего шага задачи:
   - шагов нет (прогон встал до первой сессии) — исполнитель с обычным промптом;
   - последний шаг `paused`, а задача ещё не `paused` (прогон встал между шагом и задачей) — сначала `_pause` (WIP и `paused`), дальше как ниже;
   - приостановленный gate (`died`, `branch_changed` после того, как человек вернул ветку) — gate заново;
   - приостановленная сессия — `step.how_to_resume`: `session` — `--resume` той же сессии с `prompts.RESUMED`; `handoff` — новая сессия: исполнитель с handoff и WIP-диффом, fixer — с вердиктом последнего gate из `detail`, диффом работы и handoff; `replan` — задача `paused → replan` с причиной `context_pauses` или `stall_pauses`;
   - завершённый gate при `gating` — вердикт применяется заново (`pass` — ждать коммита), при `fixing` — fixer;
   - завершённая сессия при `implementing`/`fixing` — её конец разбирается из `detail` шага (`failed` с `{"error": ...}` от `AgentError` → `needs_human(session_failed)`), при `gating` — gate.
2. Задача переходит в стадию следующего шага (`pending`/`paused` → `implementing`/`fixing`/`gating`) только после проверки бюджета, перед самим шагом: при `budget` задача в `pending` остаётся `pending`, в `paused` — `paused`.
3. Бюджет перед каждой сессией: `budget.load_limits` + `budget.check`; нет `limits.json` — сессия разрешена. В лог — сводка и время возобновления.
4. Handoff `.loop/handoff.md`: `.loop/` добавляется в `.git/info/exclude` (`git.exclude`, через `rev-parse --git-path`) при создании `_Task` — handoff не попадает ни в gate, ни в WIP, ни в будущий коммит. Перед каждой сессией файл удаляется (устаревший от прошлой задачи или уже переданный в промпт); для новой сессии после паузы по контексту он сначала читается.
5. `step.how_to_resume`: `ValueError`, если последний шаг не `paused`; паузы `timeout`/`idle`/`guard_error` считаются на задачу, третья (`STALL_PAUSES_TO_REPLAN = 3`) — `Resume("replan", reason="stall_pauses")`; у `Resume` новое поле `reason`.
6. `store.TASK_TRANSITIONS`: `paused → replan`.
7. `gates.secret_files(paths, profile)` — общий отбор файлов-секретов для `check` и для диффа в промпте (теперь и в промпте исполнителя после паузы).
8. `prompts.fixer(..., handoff=None)` — раздел «Resuming the fix» с handoff остановленного fixer'а (без handoff — обычный промпт fixer'а: работа уже в диффе); `prompts.RESUMED` — промпт `--resume`.
9. Счётчик эталона тестов при продолжении — из `task.tests`, чекпоинт и неотслеживаемые файлы — из первого шага задачи.

Решения шага (принял агент, можно пересмотреть): порог застреваний 3 (у контекста 2) — `idle` бывает от разового зависания CLI; `guard_error` тоже ведёт в `replan`, как сказано во вводной к этапу, хотя причина обычно не в размере задачи; fixer после паузы по контексту получает handoff — иначе записанный им handoff пропадал бы.

Чего нет:
- Ветка и HEAD перед продолжением не сверяются: это сделает команда `run` на старте прогона (3.5), иначе — gate после сессии.
- Рабочее дерево перед продолжением не сверяется с WIP-ref и не восстанавливается из него: считается, что работа осталась в дереве (пауза его не трогает).
- `resume` для `pending` не считает эталон тестов — для `pending` звать `start`. Открытый шаг (без `outcome`) не разбирается: перед `resume` прогона нужен `reap`.
- Убыль тестов по `collect` за упавшими `fmt`/`lint` по-прежнему не считается повтором ослабления (из 3.4б).

Тесты: `test_implement` — `--resume` приостановленной сессии (тот же `session_id`, промпт `RESUMED`, переходы); новая сессия после паузы по контексту с handoff, WIP-диффом и сводкой прошлых задач, handoff удалён; fixer после паузы по контексту с находками gate и handoff; приостановленный gate после возврата HEAD; gate после `interrupt`; бюджет кончился после gate → `Stop("budget")`, задача в `fixing`, продолжение даёт fixer; бюджета нет на старте → задача `pending`, шагов нет; неразобранный конец шага (ответ, ошибка CLI, пауза, вердикт gate); три застревания → `replan(stall_pauses)`; устаревший handoff удаляется перед первой сессией; WIP без handoff. `test_step` — застревания и `ValueError`, если последний шаг не приостановлен; `test_git` — `exclude`; `test_prompts` — handoff в промпте fixer'а.

Проверка:
```
uv run pytest -q            # 303 теста
uv run ruff check . && uv run ruff format --check .
```

Предлагаемое сообщение коммита 3.4в: `feat(implement): resume a stopped task, check the budget before each session`.

### 3.5а Коммит кодом — готово 2026-10-11

Решения человека (2026-10-11, перед шагом; план, раздел 3): шаг 3.5 разбит на 3.5а (коммит), 3.5б (проверки стиля), 3.5в (`run`/`resume`); сообщение, не прошедшее проверку, сразу заменяется шаблоном, без повторного запроса; до этапа 4 `loop run` берёт задачи из файла карточек.

Сделано:
1. `git.commit(repo, base, added, message)` — коммит работы (изменения отслеживаемых файлов и `added`) через временный индекс, как `diff`/`save_wip`: `write-tree` → `commit-tree -p base` → `update-ref HEAD <commit> <base>` (ветка сдвигается, только если она всё ещё на `base`, иначе `GitError`) → `reset --quiet` (реальный индекс — на коммит; файл, добавленный агентом в индекс, но не попавший в коммит, снова неотслеживаемый). Автор и подпись — по git config (`commit-tree` читает `commit.gpgSign`); hook'и (`reference-transaction`, `post-index-change`) не запускаются — `SAFE_CONFIG`.
2. `gates`:
   - `Verdict.artifacts` — новые файлы, появившиеся за время команд профиля (отчёты, кэши мимо `.gitignore`); в лог. Проверка значений `.env*` в диффе по-прежнему смотрит и их;
   - `commit_message(proposed, title)` — `type(module): gist` (`COMMIT_MESSAGE`, только печатный ASCII, одна строка, модуль обязателен), ≤ 72, без `co-authored-by`/`claude`/`anthropic`/`generated with` в любом регистре (`ATTRIBUTION`); иначе `feat: <title>`, обрезанный до 72, и предупреждение в лог.
3. `implement`:
   - `pass` в gate (и `pass` с предупреждением `flaky_tests`) → `_commit`: работы нет (пустой дифф против чекпоинта) → `needs_human(no_changes)` без шага; иначе шаг `commit` в store, сверка ветки и HEAD (`gates.branch_moved`: вердикт мог остаться от прошлого запуска) → при сдвиге шаг `paused`, WIP и `paused(branch_changed)`; сообщение — `commit_message` из ответа последней сессии (`implement`/`fix`), `detail` шага — `{"commit", "message"}`, задача → `committed`;
   - артефакты gate-команд добавляются к неотслеживаемым файлам задачи (`self.untracked`; при продолжении — из `detail` завершённых шагов gate): их нет ни в коммите, ни в диффе для fixer'а, ни в WIP, ни в следующем gate;
   - продолжение: приостановленный `commit` — как приостановленный gate, gate заново, затем коммит; завершённый `commit` при `gating` (прогон встал между шагом и переходом) → `committed`; `pass` в последнем gate при `gating` — коммит;
   - `_new_files()` — новые файлы задачи без секретов, общие для `_work` и `_commit`.
4. План: раздел 3 (решение к шагу 3.5), раздел 7 (строка «Сообщение коммита», абзац про коммит), раздел 14 (шаг 5 разбит, (а) сделан).

Решения шага (принял агент, можно пересмотреть): ответ `done` без изменений — `needs_human(no_changes)`, а не пустой коммит и не `committed` без коммита; коммит через `commit-tree` + `update-ref` со сверкой старого значения, а не `git commit` в реальном индексе — в коммит идёт ровно то, что проверял gate; подпись — по конфигу пользователя (если подпись спрашивает пароль или Touch ID, коммит ждёт человека).

Чего нет:
- Задача после коммита остаётся в `committed`: ревью — этап 5, перевод в `done` до него решит команда `run` (3.5в).
- Прогон, умерший между `update-ref` и `finish_step` шага `commit`: `reap` закончит шаг как `paused(died)`, продолжение запустит gate, а тот увидит сдвинутый HEAD (коммит уже есть) → `paused(branch_changed)`. Окно — миллисекунды; разбирает человек.
- Если работа не собирается без файла-секрета или артефакта, gate этого не видит: тесты идут по дереву, где файлы есть (как и в 3.3а).
- Чистоту отслеживаемых файлов на старте прогона никто не проверяет: правки оператора до старта стали бы работой первой задачи. Проверять в `run` (3.5в).
- В выводе упавших тестов pytest видны `--- Logging error ---`: обработчик из `log.setup` в `test_cli` остаётся на закрытом stderr захвата pytest. Было до шага, на результат тестов не влияет.

Тесты: `test_git` — коммит берёт работу и оставляет файлы вне её в дереве (секрет, добавленный в индекс, снова неотслеживаемый), не сдвигает ветку, ушедшую с `base`; `git.commit` в тесте «git цикла не запускает код репозитория». `test_gates` — артефакты команд; восемь сообщений (верные, без модуля, кириллица, `Co-Authored-By`, `generated with`, `Claude`, длиннее 72) и обрезка длинного заголовка. `test_implement` — работа после gate закоммичена (родитель — чекпоинт, сообщение, `detail` шага, чистый `git status`); ожидания `gating` после прохода gate сменились на `committed` и шаг `commit`; новый файл исполнителя в коммите; файл-секрет не в коммите и остаётся неотслеживаемым; отчёт `test`-команды не в промпте fixer'а и не в коммите; плохое сообщение → `feat: Fix x`; `done` без изменений → `needs_human(no_changes)`; ветка сдвинулась между gate и коммитом → `commit` `paused`, после возврата — gate и коммит; неразобранный конец шага `commit`.

Проверка:
```
uv run pytest -q            # 320 тестов
uv run ruff check . && uv run ruff format --check .
```

Предлагаемое сообщение коммита 3.5а: `feat(implement): commit the work that passes the gate, check the message`.

## Дальше

### Шаг 3.5б и после

3.5б — детерминированные проверки стиля в gate (план, раздел 7, строка «Стиль»): по добавленным строкам Python-диффа — декоративные разделители `# ---`, markdown в докстрингах, ссылки на номера задач/решений/шагов плана; AST — глобальная переменная модуля, использованная в нём меньше 3 раз. Нарушение → `fixing(style)` с находками. Учесть, что в самом agent-loop скомпилированные регулярки — глобалы с одним использованием (`gates.SKIP`, `gates.COMMIT_MESSAGE`): решить, считать ли константы-регулярки и `UPPER_CASE` нарушением, иначе проверка не пройдёт на коде, который правило писало.

3.5в — команды `run` и `resume` по контрактам из 3.2: `loop run tasks.json` — файл карточек (`title`, `goal`, `criteria`, `files`, `tests`, `decisions`, проверка полей), прогон `create_run` → `approved` → `running`; на старте `reap`, сверка ветки и HEAD, чистота отслеживаемых файлов, отказ без `sandbox-exec`; задачи по очереди через `implement.start` (`pending`) и `implement.resume` (остальные незавершённые); `previous` — `prompts.previous_tasks` от HEAD старта прогона и `notes_for_next` закоммиченных задач; `committed` → `done` до этапа 5 (или оставить); `Stop("budget", resume_at)` — прогон в `paused(budget)` до `resume_at`, `Stop("interrupt")` — прогон в `paused(interrupt)`; задача в `needs_human`/`replan` останавливает прогон; `AgentError` из `step.run` и `ConfigError` из `count_tests`/gate — решить, что с ними делать.

Из 3.3в: профили рабочих репозиториев писать с `uv run --no-sync --no-cache`. Открыто к обсуждению: `needs_human` на изменение `docker-compose*.yml` и `[build-system]` (остаток риска `install`/`up`, раздел 10).

Все шаги этапа 3 — коммиты в ветке `implementation-loop`.
