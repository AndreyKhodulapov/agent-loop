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

### 3.5б Проверки стиля в gate — готово 2026-10-12

Решение человека (2026-10-12, перед шагом; план, раздел 3): правило о глобалах применяется буквально, константы `UPPER_CASE` и скомпилированные регулярки тоже считаются.

Сделано:
1. `gates._style` — после команд профиля (`fmt` мог поправить работу), по добавленным строкам изменённых `.py` из диффа `-U0` против чекпоинта (секреты и артефакты команд не смотрятся):
   - комментарии (`tokenize`): декоративный разделитель — 3+ подряд из `-=*#~_`;
   - комментарии и докстринги (строки исходника в диапазоне докстринга модуля, класса, функции): markdown-выделение `**x**`/`*x*` (`*args`, `**kwargs`, `a * b`, `*.py` не находки), в докстрингах ещё заголовки `#`;
   - ссылки на план, задачи и решения: `plan.md`/`progress.md`, `§N`, `ADR-N`, слово (`task`, `step`, `stage`, `phase`, `decision`, `ticket` и русские `задача`, `шаг`, `этап`, `решение`, `раздел`, `пункт`) с `#N`/`№N`, номером через точку (`3.5`) или с буквой (`3б`); голое число не находка (`step 2`, `retry the task 3 times`);
   - AST: новое имя уровня модуля (`Assign`/`AnnAssign` со значением в `tree.body`, строка присваивания добавлена), у которого в модуле меньше 3 чтений (`ast.Name` в `Load`). Не новое — имя уровня модуля в версии чекпоинта любого изменённого (в том числе удалённого) модуля: перенос глобала — не работа задачи. Не нарушение — dunder-имена, `logger`, `pytestmark`, `pytest_plugins` и имена, найденные целым словом в других файлах репозитория (`git.words_in`): это API модуля.
   Файл, который не разбирается (`SyntaxError`), пропускается — это поймают lint и тесты; старая версия, которая не разбирается, не даёт имён.
2. Вердикт: находки стиля — `path:line: ...`, по строкам. Команды прошли → `fixing(style)` (флакающий тест, не тронутый задачей, fixer'у не передаётся); команды упали → причина команды, находки стиля — после их вывода, чтобы fixer получил всё за один заход. Повтор стиля особо не считается: работает общий лимит `fix_attempts`.
3. `git.words_in(repo, words, outside)` — `git grep --untracked -I -o -h -w -F` по отслеживаемым и неигнорируемым неотслеживаемым файлам, кроме `outside` (`:(exclude,literal)`); код выхода 1 — ничего не найдено. Номера строк и столбцы из конфига (`grep.lineNumber`, `grep.column`) выключены флагами.
4. План: раздел 3 (решение 2026-10-12), раздел 7 (строка «Стиль»), раздел 14 (шаг 5(б) сделан).

Решения шага (принял агент, можно пересмотреть): разделители ищутся только в комментариях — подчёркивания секций в докстрингах numpy-стиля (`----------`) не находка; ссылки — только с номером через точку, буквой или `#`, иначе ловились бы обычные фразы; имя из старой версии любого изменённого модуля не новое — переименование или перенос модуля (`--no-renames`) иначе превращал бы чужие глобалы в находки задачи; стиль идёт вместе с упавшими командами, а не отдельным заходом — сессии fixer'а дорогие, лимит попыток 3.

Чего нет:
- Эмодзи, восклицания и длинное тире (AGENTS.md) не проверяются: в плане их нет, ложных срабатываний больше.
- Путь, который git берёт в кавычки (кавычки, обратный слэш, управляющие символы, не-ASCII при `core.quotePath`), стилем не проверяется. В `_weakened` путь с пробелом остаётся с табом на конце в тексте находки (было до шага).
- Только Python: модули других языков проверками стиля не покрыты.
- Имя, найденное как слово в любом другом файле (в том числе в документации), считается API: проверка мягче, чем правило.

Тесты: `test_gates` — девять нарушений (разделитель `# ---` и `#####`, заголовок в докстринге, выделение в докстринге и комментарии, `step 3.5b`, `plan.md`, `решение #12`, глобал с двумя чтениями); без находок — модуль с `logger`, `__all__`, `*args`/`**kwargs`, `step 2 of 3` и глобалом с тремя чтениями, изменённое значение старого глобала, перенос глобала из удалённого модуля, глобал, импортированный другим файлом, модуль с `SyntaxError`, разделитель в `.txt`; старые нарушения в недобавленных строках не находка; стиль вместе с упавшим `lint`; стиль без находки о флакающем тесте. В двух старых тестах содержимое новых модулей сменилось с неиспользуемых глобалов на `print(...)` (сами проверки не менялись). `test_git` — `words_in` (неотслеживаемые файлы, игнорируемые, целые слова, без совпадений); `words_in` в тесте «git цикла не запускает код репозитория». Проверка стиля на диффе самого шага находок не дала.

Проверка:
```
uv run pytest -q            # 339 тестов
uv run ruff check . && uv run ruff format --check .
```

Предлагаемое сообщение коммита 3.5б: `feat(gates): check the style of the added Python lines`.

### 3.5в Команды `run` и `resume` — готово 2026-10-12

Решения человека (2026-10-12, перед шагом; план, раздел 3): нет места в лимитах — прогон ждёт в процессе и продолжает сам; задача после коммита остаётся `committed`; `AgentError` — `needs_human(session_failed)`; без зелёного selfcheck на установленной версии CLI прогон не стартует.

Сделано:
1. Новый модуль `src/agent_loop/runner.py`:
   - `load_tasks(path)` — файл карточек: JSON-список объектов ровно с ключами `title`, `goal` (непустые строки), `criteria`, `files`, `tests`, `decisions` (списки строк); иначе `RunError` с номером задачи, недостающими и лишними ключами;
   - `start(cfg, tasks_file)` — `selfcheck.require` → `hold_repo` (занят — отказ) → ветка (detached HEAD — отказ) → чистота отслеживаемых файлов (`status --porcelain --untracked-files=no`, есть изменения — отказ с их списком) → `create_run(repo, branch, base=HEAD)` и задачи → `hold(runs/<run>/lock)` → `commands.freeze` → `protected.save` → `approved` → `running` → `install` профиля, если есть → `with commands.services` → задачи;
   - `resume(cfg, run_id=None)` — без id берётся последний `paused` прогон этого репозитория; отказ, если прогона нет, он из другого репозитория или не `paused`, если задача ждёт в `needs_human`/`replan`, если ветка не та или HEAD не там, где прогон его оставил (последний завершённый шаг `commit` или `base`). Профиль — `commands.frozen`, `protected` не переснимается, `install` не повторяется;
   - `_work` — первая задача не в `committed`/`done`/`dropped`: `pending` → `implement.start`, остальные → `implement.resume`; `previous` — `prompts.previous_tasks` от `run.base` и `notes_for_next` закоммиченных задач (`implement.last_answer`). Нет таких задач — прогон `done`. Дальше по итогу задачи:
     - `Stop("budget", resume_at)` → прогон `paused(budget)`, `time.sleep` до `resume_at`, `running`, та же задача снова; Ctrl+C во время ожидания — выход, прогон остаётся `paused(budget)`;
     - `Stop("interrupt")`, задача `paused(interrupt)` или `paused(branch_changed)` → прогон `paused(<причина>)`;
     - задача `needs_human`/`replan` → прогон `paused(needs_human|replan)`;
     - другие паузы задачи (`context`, `budget` внутри сессии, `timeout`, `idle`, `guard_error`) — сразу `implement.resume`: сессия продолжается или задача уходит в `replan` по счётчикам `step`.
2. `store`: миграция 5 — `runs.base` (HEAD на старте прогона: от него `previous_tasks`, проверка HEAD при `resume`, позже — финальное ревью ветки); `create_run(repo, branch, base)`; `Run.base` — `None` у прогона, созданного до колонки.
3. `implement`: `AgentError` из сессии (шаг уже `failed` с `{"error": ...}`) → лог и `_take` → `needs_human(session_failed)`; `last_answer(steps)` — ответ последней сессии, общий для коммита и заметок прогона.
4. `selfcheck.require(home)` — `AgentError`, если `selfcheck.json` нет или в нём другая версия, чем `claude --version`.
5. `cli`: `loop run <tasks.json>` и `loop resume [run_id]` вместо заглушек; код выхода 0 — прогон `done`, 1 — пауза или отказ (`RunError`, `AgentError`, `ConfigError`, `CommandError` — одной строкой в лог), 130 — Ctrl+C. После команды всегда `reap`: прогон, остановленный ошибкой или Ctrl+C вне сессии, ставится на паузу как мёртвый (`paused(died)`, WIP, открытые шаги, `down`). Перед `run`/`resume` — `reap`, как у остальных команд прогонов.
6. План: раздел 3 (решение 2026-10-12), раздел 14 (шаг 5(в) сделан).

Решения шага (принял агент, можно пересмотреть): модуль `runner` (не в `cli`: слой команд только разбирает аргументы); ошибки посреди прогона (`ConfigError` из `collect`, упавшие `install`/`up`, `KeyboardInterrupt` в gate) не разбираются отдельно — прогон остаётся `running`, и `reap` после команды делает с ним то же, что с мёртвым: всё нужное для продолжения он уже умеет; `base` — колонкой в `runs`, а не из чекпоинта первого шага: понадобится ревью ветки на этапе 5; паузы `context`/`timeout`/`idle`/`guard_error` прогон не останавливают — счётчики `how_to_resume` отправят застрявшую задачу в `replan`.

Чего нет:
- `ConfigError` из `collect` повторится после `loop resume`: профиль заморожен, правка `loop.toml` на прогон не действует — начинать новый прогон (или править `runs/<run>/profile.json`).
- `install`, упавший на старте, `loop resume` не повторяет.
- На пути ошибки `down` выполняется дважды: `commands.services` и `reap`.
- Прогон с задачей в `needs_human`/`replan` продолжить нечем: `loop answer` и планировщик — этап 4.
- Прогон, умерший между `update-ref` и концом шага `commit` (из 3.5а): `resume` откажет — HEAD на коммите, которого нет в store. Разбирает человек.
- `loop status` по-прежнему заглушка: id прогона — в логе старта и паузы.
- Реальный прогон на рабочем репозитории не делался (S1b — на первом прогоне).

Тесты: новый `tests/test_runner.py` — задачи по очереди до `done` (переходы прогона, `base`, карточка, сводка и заметки прошлой задачи в `previous`, `install`/`up`/`down`, копия `protected` и `profile.json`); три отказа на старте (изменённый отслеживаемый файл, detached HEAD, занятый каталог); десять сломанных файлов карточек; `needs_human`/`replan` ставят прогон на паузу, `resume` отказывает; ожидание лимитов и продолжение; Ctrl+C во время ожидания и `resume`; три остановки оператором и `resume`; пауза по контексту продолжается сразу; `resume` при чужом коммите и другой ветке отказывает, после возврата — продолжает; `resume` без прогона и с неизвестным id. `test_cli` — `loop run` с настоящим `implement` и gate (сессия подменена) коммитит работу; `ConfigError` посреди прогона — код 1 и `paused(died)`. `test_implement` — `AgentError` → `needs_human(session_failed)`; `test_selfcheck` — `require`; `test_store` — `base` нового прогона и `None` после миграции старой базы. Тесты заглушек `run`/`resume` ушли вместе с заглушками. Проверка стиля из gate на диффе шага находок не дала.

Проверка:
```
uv run pytest -q            # 365 тестов
uv run ruff check . && uv run ruff format --check .
```

Предлагаемое сообщение коммита 3.5в: `feat(runner): run and resume the tasks of a cards file on the current branch`.

## Дальше

Решение человека (2026-10-12; план, раздел 14, этап 3а): до этапа 4 — ревью ветки, пробный прогон на реальном репозитории, затем мерж. Все правки — коммиты в `implementation-loop`.

### 3а.1 Ревью ветки `implementation-loop` — готово 2026-10-12

Ревью всей ветки против `main` (`git diff main...HEAD`: 38 файлов, +4968/−162) одним проходом в основном контексте. Тесты (365), `ruff check`, `ruff format --check` зелёные. Проверено и чисто: ссылок на план и задачи в коде нет, markdown в комментариях нет, слоистость `cli → runner → implement → {gates, step, prompts} → {commands, git, agent, guard, protected, budget} → {config, store, files}` без циклов, проглоченных исключений нет, тесты не ослаблены. `commit.gpgsign` не задан ни здесь, ни в `api-service-foodtech`. Не подтвердились гипотезы: `_context_full` на паузах gate, `how_to_resume` на шагах `died`, `recovery._pause` для открытых gate/commit-шагов, `_outcome` при ответе во время stop, счётчики попыток fixer'а.

Находки по убыванию серьёзности (строки по состоянию на `29b60bc`):
1. Критично. `gates.py:446`, `:452` — `_env_secrets` читает и отслеживаемые `.env*`. На `api-service-foodtech` даёт `{'postgres': 'POSTGRES_PASSWORD of .env.example'}` (значение ровно 8 символов), а `postgres` уже есть в README, `app/core/config.py`, `tests/conftest.py`: первая задача со строкой `postgres` в диффе встанет в `needs_human(secret_in_diff)`, `loop resume` откажет, `loop answer` ещё нет. `_mask` при этом портит вывод fixer'у: `postgresql+asyncpg://` → `<POSTGRES_PASSWORD of .env.example>ql+asyncpg://`.
2. Высокая. `gates.py:44`, `:97`, `:99` — новые файлы под `SECRET_PATTERNS` (`*credentials*`, `.env*` ловят `tests/test_credentials.py`, `app/credentials.py`, `.env.example`) молча выпадают из коммита: warning в логе и поле `secrets` в detail gate-шага, fixer и оператор не узнают. Для следующей задачи файл уже в `untracked_before` и не закоммитится никогда. План (раздел 7) обещал `needs_human`, если без файла не собирается.
3. Средняя. `gates.py:179` — запасное сообщение коммита `feat: {title}` не проверяется теми же правилами: русский заголовок карточки даст русское сообщение без модуля (R17).
4. Средняя. `cli.py:116` — `_work` ловит четыре типа; `git.GitError` (например, `commit-tree` без `user.name`), `StoreError`, `FileNotFoundError` из `protected.restore` выпадают traceback'ом (R10). `reap` при этом прогон доубирает.
5. Низкая. `recovery.py:84`, `commands.py:284` — на пути ошибки `down` выполняется дважды (`commands.services`, затем `reap` из `cli._work`): второй `docker rm -f` даёт в лог ложное `cannot stop the services`.
6. Низкая. `prompts/__init__.py:68` — `prompts/AGENTS.md` симлинк `../../../AGENTS.md`, работает только из checkout; при сборке пакета сломается.
7. Критерий глобальных переменных: собственный gate ветки (`gates._module_style` на диффе `main...HEAD`) нашёл 15 новых имён уровня модуля с 1–2 использованиями без ссылок из других файлов: `agent.py` `PROJECT_INSTRUCTIONS`, `IMPORT`, `CODE`; `gates.py` `DIFF_TOO_BIG`, `TEST_DIRS`, `SKIP`, `ASSERT`, `SECRET_KEY`; `implement.py` `NO_ANSWER`, `SESSION_FAILED`, `FIX_ATTEMPTS`, `NO_CHANGES`, `ANSWERS`; `selfcheck.py` `PROJECT_HOOK_MARKER`; `step.py` `STALL_REASONS`. В `main` та же манера (`IDLE_MARGIN`, `CTX_LOG_STEP`), regex на уровне модуля — идиома языка.
8. Дублирование тестов: `tests/test_implement.py::test_commit_message_that_breaks_the_rules_is_made_of_the_title` повторяет случай `Co-Authored-By` из параметризации `tests/test_gates.py` с тем же именем. `test_cli::test_run_commits_the_work_of_its_tasks` пересекается со счастливым путём `test_runner`, допустимо как smoke CLI.
9. Докстринги у тривиальных приватных помощников: `implement.py` `_Session`, `_work`, `_verdict`; `runner.py` `_wait`, `_head`.

Вывод: блокер испытания — находка 1, воспроизводится на репозитории испытания без запуска агента; находку 2 закрыть до запуска (задача 1 про `ADMIN_TOKEN` с заметной вероятностью создаст файл под `*credentials*` или `.env*`). Находки 3–6 прогону не мешают, 7–9 — на решение человека.

### 3а.1б Правки по ревью — готово 2026-10-12

Решения человека (2026-10-12), по рекомендациям агента: находки сгруппированы по причине — секреты в gate (1, 2), правила, которые цикл требует от агента, но сам не соблюдает (3, 7–9), путь ошибки прогона (4, 5), упаковка (6). Четыре коммита в `implementation-loop`.

Находка 6 не подтвердилась: `uv build --wheel` кладёт в wheel содержимое `AGENTS.md` (2664 байта), а не симлинк. Сборку wheel из sdist не проверяли: симлинк в sdist может её не пережить — проверить, когда понадобится сборка пакета; правок нет.

#### Коммит 1. Секреты в gate (находки 1, 2) — готово 2026-10-12

1. `gates._env_secrets(repo, checkpoint)`: значения, которые уже есть в файлах чекпоинта, отбрасываются — их не маскируют и не ищут в диффе. Это закрывает и `.env.example`, и локальную копию `.env` с тем же паролем по умолчанию (фильтр «только неотслеживаемые `.env*`» второй случай не ловил). Значение отслеживаемого `.env*`, изменённое в рабочем дереве, по-прежнему секрет. Поиск — новый `git.strings_in(repo, strings, rev)`: один `git grep -F -o` по дереву, подстрока внутри слова тоже считается. `words_in` и `strings_in` делят `git._grep`. На `api-service-foodtech` `_env_secrets` теперь пуст.
2. Новый файл работы под `SECRET_PATTERNS`/`secret_patterns` — `needs_human(secret_file)` в начале gate, вместе с проверками настроек CLI и конфигов hook'ов, до команд профиля: команды прошли бы с файлом в рабочем дереве, а коммит без него мог не собраться. Находка — список файлов, они же в `Verdict.secrets`. `implement._new_files` по-прежнему держит их вне промптов и коммита.
3. `SECRET_PATTERNS`: `*credentials*` заменён на `*credentials`, `*credentials*.json`, `*credentials*.yaml`, `*credentials*.yml` — `test_credentials.py`, `credentials.py` больше не секреты; `.git-credentials`, `aws/credentials`, `credentials.json` — секреты.
4. План, раздел 7: строки «Секреты в коммите» и «Значения из `.env*`».

Тесты: `test_gates` — `test_new_files_like_secrets_need_a_human` вместо `..._are_left_out` (плюс `credentials` без расширения и `credentials.py`, который не секрет; команды не запускались), новый `test_values_the_repository_has_are_no_secrets` (`postgres` из закоммиченного `.env.example` и `.env` в диффе и в выводе — `fixing`, без маски; `API_TOKEN` из `.env` маскируется); в `test_values_from_env_are_masked_for_the_fixer` `conf/.env.test` теперь файл оператора до шага (раньше он был новым файлом шага и молча выпадал). `test_git` — `strings_in`. `test_implement` — `test_secrets_stay_out_of_the_prompts_and_stop_the_task` вместо `..._of_the_fixer_and_the_commit`: секрет из паузы не попадает в промпт новой сессии, задача — `needs_human(secret_file)`, коммита нет, файл на месте.

Находка по ходу: `_style` принимает `*credentials*` в обратных кавычках комментария за markdown-выделение. Комментарий переписан; regex выделения не пропускает код в обратных кавычках — в коммит 4.

Проверка:
```
uv run pytest -q            # 367 тестов
uv run ruff check . && uv run ruff format --check .
```

Предлагаемое сообщение: `fix(gates): stop on new secret files, ignore env values the repository already has`.

#### Коммит 2. Путь ошибки прогона (находки 4, 5) — готово 2026-10-12

Отступление от плана коммита: «`down` только при нормальном выходе из блока, остальное — `reap`» не годится. SIGTERM/SIGHUP во время ожидания лимитов (`runner._wait`) приходит, когда прогон уже `paused(budget)`; `reap` берёт только `running` — контейнер остался бы поднятым. Сделано иначе:
1. `commands.services(profile, repo, run_dir)`: метка `runs/<run>/services` создаётся до `up` и удаляется после `down` в `finally` (`down` по-прежнему при любом выходе). `recovery.reap` зовёт `down` только при метке и удаляет её; раньше условием был замороженный профиль. Метка остаётся только у процесса, умершего без `finally` (`kill -9`, перезагрузка). Ошибка посреди прогона: `down` из `finally`, `reap` ставит прогон на паузу без второго `down`. План, раздел 9 (строка про `up`/`down`), и докстринг `recovery` обновлены.
2. `cli._work`: `git.GitError`, `StoreError`, `FileNotFoundError` (копии защищённых файлов нет, `protected.restore`) — одной строкой в лог и код 1, как остальные; `reap` прогон доубирает.

Тесты: `test_cli::test_run_stopped_by_an_error_is_paused_as_a_dead_one` параметризован по `ConfigError`, `GitError`, `StoreError`, `FileNotFoundError` и идёт с профилем `up`/`down`: лог — `up\ndown\n` (на старом коде — `up\ndown\ndown\n` и traceback для трёх новых типов). `test_recovery::test_live_run_is_left_alone_and_dead_one_stops_its_services` параметризован по метке: без неё `down` не зовётся; метка после `reap` удалена. `test_commands`: метка есть внутри блока и нет после.

Проверка:
```
uv run pytest -q            # 371 тест
uv run ruff check . && uv run ruff format --check .
```

Предлагаемое сообщение: `fix(runner): stop the services once when a run fails, log git and store errors in one line`.

#### Коммит 3. Сообщение коммита (находка 3) — готово 2026-10-12

Запасное `feat: {title}` не проходило собственный `COMMIT_MESSAGE` никогда (нет `(module)`). Сделано:
1. `gates.commit_message_ok(message) -> bool` вместо `gates.commit_message(proposed, title)`: правила те же, шаблона нет.
2. `implement._commit`: после проверки «работы нет» и до шага `commit` — проверка сообщения последней сессии. Плохое — `_ask_message`: `_Session` того же вида (`implement`/`fix`) с `session_id` последней сессии (`--resume`) и промптом `prompts.commit_message(proposed)` (плохое сообщение в ограде, правила, «файлы не менять»). Сессия идёт через `_loop` как любая: бюджет, `_enter` (`gating → implementing/fixing`), `_take`, затем gate заново — сессия с инструментами могла тронуть файлы, коммитить непроверенное нельзя. Сессия, у которой уже есть завершённый шаг (её уже переспрашивали), снова с плохим сообщением — `needs_human(commit_message)`.
3. `_after_gate` возвращает следующую сессию (`fixer` или переспрос) вместо вердикта; `_loop` и `resume` берут её. Остановка между gate и переспросом безопасна: `resume` с последним шагом `gate` (pass) снова идёт в `_commit` и переспрашивает.
4. Попытки fixer'а — число разных `session_id` завершённых шагов `fix`: переспрошенный fixer не съедает попытку.
5. План: раздел 3 (решение 2026-10-12), раздел 7 (строка «Сообщение коммита»).

Тесты: `test_gates::test_commit_message_keeps_the_rules` (параметризация `bool`, граница 72 символа, пустое). `test_implement`: дубль `Co-Authored-By` (находка 8) заменён на `..._goes_back_to_its_session` (тот же `session_id`, `resume`, плохое сообщение в промпте, шаги `implement, gate, implement, gate, commit`), `..._again_goes_to_a_human` (коммита нет), `test_fixer_asked_for_another_message_has_not_used_up_a_fix` (на подсчёте шагов — `replan`).

Известный остаток: переспрошенная сессия, вставшая на паузу с полным контекстом, продолжится по handoff; для fixer'а это промпт с находками последнего gate, а он `pass`. Редко (нужен полный контекст на однострочном ответе), правок нет.

Проверка:
```
uv run pytest -q            # 374 теста
uv run ruff check . && uv run ruff format --check .
```

Предлагаемое сообщение: `fix(implement): ask the session again for a commit message that breaks the rules`.

#### Коммит 4. Код цикла против `AGENTS.md` (находки 7–9) — готово 2026-10-12

1. Имён оказалось больше 15. `git.words_in` считает ссылкой любое упоминание в другом файле, документацию тоже: при ревью 11 имён прятались за `docs/progress.md` и `docs/plan.md`, а после записи находок в progress спрятались и сами 15. `_style` на `git diff main...HEAD` без `docs/` дал 25 имён в `src` и одно в тестах. Исправлены все:
   - коды причин (`diff_too_big`, `no_answer`, `session_failed`, `fix_attempts`, `no_changes`) — литералами на месте;
   - regex, кортежи и словари — локальными в своих функциях: `agent.project_instructions` (`instructions`, `imports`, `code`), `agent.command` (`--tools`), `commands._sandbox` (`operator_files`, `repo_secrets`, профиль Seatbelt — в возвращаемую f-строку), `config` (`known` в разборе профиля), `gates.commit_message_ok`, `secret_files`, `_env_secrets`, `_weakened`, `_is_test`, `implement._Task._take` (`ANSWERS`), `step.how_to_resume` (`stall_reasons`, порог 3), `selfcheck.run`; таймаут команды по умолчанию — `10 * 60` на месте;
   - `TEST_NAME` нужен двум функциям — `gates._is_test_module(name)` вместо глобала;
   - `tests/test_step.py`: `ANSWER` поднят к константам и использован в третьем месте, где тот же ответ собирался заново.
   Имена из `main` (`CONTEXT_PAUSES_TO_REPLAN`, `IDLE_MARGIN` и др.) не тронуты.
2. Докстринги `_Session`, `_work`, `_verdict` (`implement`), `_wait`, `_head` (`runner`) убраны.
3. `_style`: код в обратных кавычках вырезается перед поиском markdown-выделения. Тест — строка `` `*credentials*`, `*.pem` и `*.key` `` в комментарии `write_orders` (`test_style_keeps_to_new_breaks_in_python`); на старом коде падает.

Открыто: упоминание имени в документации (README, docs) освобождает его от правила трёх использований. Для агента в продуктовом репозитории это редкий обход; сузить `words_in` до файлов кода — решить после испытания.

Проверка:
```
uv run pytest -q            # 374 теста
uv run ruff check . && uv run ruff format --check .
# _style на git diff main...HEAD (со скриптом из scratchpad, docs/ исключён из words_in): находок нет
```

Предлагаемое сообщение: `refactor(loop): keep the loop's own code to the rules it checks in the agent's work`.

Для подготовки испытания (3а.2) учесть: агент не может править `.env.example` (`Read(.env*)` запрещён, `Edit` без чтения не пройдёт, sandbox закрывает запись в `.env*` из Bash) — в карточке задачи 1 сказать, где документировать `ADMIN_TOKEN` (README). Новый файл `.env*` от агента (`Write` его не запрещает) теперь останавливает задачу в `needs_human(secret_file)`.

### 3а.2 Пробный прогон на `api-service-foodtech` — готово 2026-10-16

Репозиторий: `~/PET_PROJECTS/api-service-foodtech` — Python 3.14, uv, FastAPI, SQLAlchemy async, Alembic, слои router → service → repository → DB, `ruff`, тесты на настоящем Postgres. Прогон ведёт агент: запускает `loop run` в фоне, следит за логом и `loop.db`, сам делает проверки и разбор. Ветку, профиль, файл контекста и карточки показывает человеку до запуска; `loop run` — только после его «да».

Что известно о репозитории (2026-10-12):
- тесты поднимают Postgres через testcontainers (под gate не работает — нужен docker socket), но `tests/conftest.py` берёт `TEST_DATABASE_URL`, если он задан: базу поднимает `up` профиля, тесты ходят на localhost (sandbox это разрешает);
- `docker-compose.yml` не годится для `up`: фиксированные `container_name` столкнутся со стеком человека — `docker run`/`docker rm`;
- CLAUDE.md в репозитории нет — нужен `context_file`; `.env` нет, `tech_task.md` в `.gitignore`;
- Docker Desktop на момент записи не был запущен — запустить перед прогоном;
- `main` не проходит `ruff format --check`: 12 файлов (`app/main.py`, `app/core/pagination.py`, `app/repositories/dish.py`, `app/repositories/order.py`, `app/services/order.py`, обе миграции, `tests/test_app.py`, `test_body_limit.py`, `test_dishes.py`, `test_migrations.py`, `test_orders.py`). Gate запускает `fmt` по всему репозиторию, а коммит задачи забирает все изменённые отслеживаемые файлы (`git add --update`): первый коммит цикла унёс бы с собой переформатирование этих 12 файлов, `Verdict.lines` их бы не посчитал (ревью 2026-10-12, находка 1).

Находка до прогона: каждый `{free_port}` в `[env]` получает свой порт, а один порт нужен в двух местах (URL тестов и `up`). Обход в профиле — порт только в `TEST_DATABASE_URL`, `up` достаёт его shell-подстановкой. Решить после прогона, нужна ли именованная подстановка порта.

Шаги:
1. Подготовка (агент):
   - человек форматирует `main` foodtech до создания ветки прогона: `uv run ruff format .`, коммит `style: format with ruff`; агент проверяет `ruff format --check .` зелёный, иначе прогон не стартует. Так цикл работает в чистом поле, а поведение «`fmt` трогает файлы вне работы» остаётся видимым, не замаскированным `fmt = ""`;
   - `~/.agent-loop/.env` (его ещё нет): `LOOP_KNOWLEDGE_DIR=~/andrey_personal_projects/MY-KNOWLEDGE-DATABASE/USEFUL_JOB_NOTES/agent_runs`;
   - профиль `agent_runs/api-service-foodtech/loop.toml` (черновик ниже) и рядом `CLAUDE.md` проекта: слои, тесты и `ruff` через `uv run --no-sync --no-cache` (кэш uv в `$HOME` в sandbox сессии закрыт), `TEST_DATABASE_URL` уже в окружении, комментарии на английском;
   - ручная проверка команд профиля: `install`, `up`, `collect` и `test` под sandbox gate (`commands.run`), `down`, контейнер удалён; `loop config` показывает профиль; `loop selfcheck` зелёный.
2. Карточки `tasks.json` (пишет агент, утверждает человек), задача из TODO «divide dishes api to admin (auth) and public parts» плюс «api versioning»:
   1. Авторизация администратора: настройка `ADMIN_TOKEN`, зависимость с Bearer-токеном (сравнение за постоянное время), 401 без токена или с чужим, тесты; без новых пакетов.
   2. Разделение dishes: публичное чтение остаётся, создание и удаление — в `/admin/dishes` под авторизацией из задачи 1; тесты и README. Проверяет сводку и `notes_for_next` между задачами и проверку ослабления тестов на переносе тестов.
   3. Версионирование: все маршруты, кроме `/health`, под `/api/v1`; тесты и README.
3. Запуск: человек дал «да» → ветка `admin-dishes` от чистого `main` → `loop run tasks.json`. Проверяется по ходу:
   - проход gate и коммит: сообщение, автор, состав файлов, без `.loop/` и артефактов команд;
   - однократный Ctrl+C (`kill -INT` процессу цикла) посреди сессии задачи 2 → handoff, `paused(interrupt)`, `loop resume` → `--resume`;
   - fixer — только если случится сам;
   - S1b: сколько `rate_limit_event` за сессию (`runs/<id>/step-*.jsonl`), строки бюджета в логе;
   - в конце: контейнер удалён, осиротевших `claude` нет, `git status` чистый.
4. Остановка: при первой ошибке самого цикла (не агента) — стоп, находка, исправление шагом в `implementation-loop`, затем `loop resume` или новый прогон.
5. Разбор (агент вместо ревью этапа 5): коммиты против карточек и `AGENTS.md`; по шагам из `loop.db` — ходы, стоимость, контекст, попытки fixer'а, паузы; находки — в план (раздел 2) и сюда. При успехе человек мержит `admin-dishes` в продукт.

Подготовка (шаг 1) — готово 2026-10-16: `main` foodtech отформатирован (`33803ec`), `admin-dishes` на нём; `~/.agent-loop/.env`, `agent_runs/api-service-foodtech/{loop.toml,CLAUDE.md,tasks.json}` написаны (профиль — черновик ниже без изменений). Ручная проверка через `commands.freeze`/`run`/`services`: `install`, `up`, `collect` (99 тестов), `test` (99 passed, 23 с), `fmt`, `lint` под sandbox, `down` — контейнер и метка удалены, `git status` чистый. `loop config` находит профиль, `loop selfcheck` зелёный на `claude` 2.1.301. Мелочь: `VIRTUAL_ENV` активированного venv agent-loop уходит в несандбоксные команды (`install`), uv пишет предупреждение — на работу не влияет.

Прогон `21273d58` (2026-10-16, 19:38–19:47) — `done`: три коммита на `admin-dishes` (`a403cd9`, `8b7403b`, `8dbfbc7`; 137, 365, 265 строк по gate), каждая задача с первого раза, тестов 99 → 126. Коммиты от git-автора репозитория, без co-author, без `.loop/` и артефактов; `git status` чистый, контейнер удалён, осиротевших `claude` нет. Код по карточкам и `AGENTS.md` замечаний не дал. Находки и S1b — в плане, раздел 2 («Находки пробного прогона 3а.2»).

Не проверено: Ctrl+C посреди сессии и `loop resume` — задача 2 шла 4,5 мин, прерывание не успел. Нужен отдельный прогон.

После прогона: в профиле `TEST_DATABASE_URL` с `?ssl=disable` (126 тестов под sandbox, `up` порт по-прежнему достаёт). Второй прогон — правка «по ревью заказчика», `tasks-review.json`, одна карточка на той же ветке: версии API пакетами (`app/api/routers/v1/` со своим роутером под `/api/v1`, `health` вне версий). На нём — Ctrl+C посреди сессии и `loop resume`.

Прогон `72871039` (2026-10-16, 20:04–20:06) — `done`: коммит `ae40c0b`, три модуля переехали в `routers/v1/` как переименования (similarity 100%), `v1/__init__.py` собирает роутер под `/api/v1`, 126 тестов без правок. `kill -INT` на 25-й секунде сессии пришёл во время последнего вызова агента (тесты); guard срабатывает на следующем вызове, а им был ответ `done` — агент остановки не увидел. Цикл: `stopped by the operator after the answer of the session` → `paused(interrupt)`, сервисы остановлены, работа в дереве; `loop resume` → сервисы, gate, коммит. Ветка «сессия на паузе → `--resume`» не проверена: прерывать надо в первые секунды сессии, на задаче, которая идёт минуты. Мелочь: остановка оператором по `run` даёт код выхода 1, как ошибка; 130 — только для второго Ctrl+C.

Третий прогон, до этапа 4 — задача из TODO «personal page of user with auth», `tasks-users.json`, 4 карточки: аккаунты и регистрация (argon2, миграция `users`), вход по JWT и текущий пользователь (`token_version` для отзыва), профиль и смена пароля (`/me`), заказы пользователя и история (`orders.user_id` nullable, `/me/orders`). Решения человека: PyJWT и argon2-cffi добавляет он сам до запуска (в сессии uv пакеты не ставит), связь заказа с пользователем необязательная, `/me` — профиль, смена пароля, история. Проверка: Ctrl+C в первые ~10 с сессии задачи 2 → пауза посреди работы → `loop resume` с `--resume`.

Риск: все роли на Opus с effort `high`, лимиты общие с сессией агента, ведущей прогон; при нехватке цикл сам встанет на ожидание.

По просьбе человека перед запуском в карточку 2 добавлены `docker-compose.yml` и `.env.example` (`JWT_SECRET`, `JWT_EXPIRE_MINUTES`), в карточку 4 — удаление пункта из `TODO.md`. Ошибка агента, готовившего карточки: что агент не может править `.env.example`, было записано в конце 3а.1б, Коммит 4.

Прогон `584816d9` (2026-10-16, 20:38–21:10) — `done` после 10 прерываний и 9 `loop resume`: коммиты `ad9d073`, `cac526e`, `d0409a1`, `03e2723` (373, 505, 323 строки по gate у первых трёх), тестов 126 → 224, fixer по одному разу на задачах 1–3 (все — правило трёх использований). Коммиты от git-автора, без co-author, без `.loop/`; `git status` чистый, контейнер удалён, осиротевших `claude` и pytest нет. Стоимость $4.82 по сессиям (сумма `cost_usd` шагов — $6.70, находка 3), 5-часовой лимит 3% → 15%.

Прерывания (`kill -INT` процессу цикла, скрипты в scratchpad сессии):

| Где | Итог |
|---|---|
| Задача 1, сессия, 120 с | handoff, `paused(interrupt)`, 29 с на остановку; `--resume` той же сессии |
| Возобновлённая сессия, 2 с | снова handoff, `paused(interrupt)` |
| `up` при `resume` | `down`, код 130, `paused(died)`, контейнер удалён |
| Gate задачи 1, внутри `pytest` | gate `paused(died)`, WIP-ref; при `resume` gate заново, сирот нет |
| Коммит задачи 2, после `update-ref` | находка 1 |
| Задача 3, новая сессия, 5 с | агент ответил `blocked`, цикл верно счёл это паузой `interrupt` |
| Задача 3, двойной Ctrl+C | сессия убита за 1 с, без handoff; `--resume` убитой посреди вызова сессии работает |
| Сразу после коммита задачи 3 | попал в `git show` сборки промпта задачи 4, состояние целое |
| Задача 4, тройной Ctrl+C через 0,2 с | просьба → terminate → kill за 1 с, `paused(interrupt)` |
| Конец сессии задачи 4 | попал в `git add` временного индекса gate; `.git` без `index.lock` и временных индексов |

Не поймано: Ctrl+C во время самого `git commit-tree`/`update-ref` первого коммита — окно в миллисекунды, сторож с опросом 20 мс его пропустил. Находка 1 редкая, но реальная.

После прогона: в `.env.example` foodtech добавлены `JWT_SECRET=` и `JWT_EXPIRE_MINUTES=60` (`e8f28d9`), затем мерж `user-profile`.

Черновик профиля (проверить руками на шаге 1):
```toml
doc_lang = "en"
context_file = "CLAUDE.md"   # рядом с loop.toml
deps_files = ["pyproject.toml", "uv.lock"]

[commands]
install = "uv sync --frozen"
fmt     = "uv run --no-sync --no-cache ruff format ."
lint    = "uv run --no-sync --no-cache ruff check ."
collect = "uv run --no-sync --no-cache pytest --collect-only -q"
test    = "uv run --no-sync --no-cache pytest -q"
up      = "port=${TEST_DATABASE_URL##*:}; port=${port%%/*}; docker run -d --rm --name loop-{run_id}-db -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=restaurant_test -p 127.0.0.1:$port:5432 postgres:16-alpine && until docker exec loop-{run_id}-db pg_isready -h 127.0.0.1 -U postgres; do sleep 1; done"
down    = "docker rm -f loop-{run_id}-db"

[timeouts]
up = 120

[env]
TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:{free_port}/restaurant_test"
```

### 3а.2б Правки по третьему прогону — готово 2026-10-16

Находки прогона `584816d9` по убыванию серьёзности:
1. Серьёзно. Остановка между `update-ref HEAD` в `git.commit` и `store.finish_step` в `implement._commit` (SIGINT, `kill -9`, сбой) оставляет коммит задачи в git, а шаг `commit` — открытым. `recovery._pause` видит «HEAD has moved since task 2 started», пишет WIP поверх checkpoint и ставит шаг `paused(died)`; `runner.resume` сравнивает HEAD с последним завершённым коммитом из store и отказывает: `the run has left user-profile at ad9d0730e2e5, now user-profile is at ebd85b4bd1eb: put them back`. Оператор должен сам понять, что `ebd85b4` — коммит цикла, и сделать `git reset ad9d073`; после этого `resume` прогнал gate и закоммитил то же дерево (`cac526e`, дифф с `ebd85b4` пуст). Остановка между `update-ref` и `git reset` оставила бы ещё и индекс от старого HEAD.
2. Задача 2 закоммичена как `done` с невыполненным критерием: `.env.example` закрыт для агента (`Read(.env*)` в `loop-settings.json`, запись `.env*` в sandbox), агент написал об этом только в сводке, а статус `needs_human` в схеме ответа есть. Причина в цикле — роль исполнителя не говорит, что делать с критерием, который ограничения цикла не дают выполнить; причина в карточке — см. 3а.2.
3. `cost_usd` шага — `total_cost_usd` результата CLI, а на `--resume` он накопительный за всю сессию: `modelUsage.outputTokens` шагов 13, 14, 15 одной сессии — 14822, 18211, 25806, а `usage.output_tokens` и `num_turns` — за вызов (14822, 3389, 7595). Сумма по шагам завышает стоимость (на прогоне $6.70 вместо $4.82), строка `completed in N turns, $X` в логе — тоже. Бюджет от `cost_usd` не зависит, только разбор прогонов.
4. Правило трёх использований (`gates._module_style`) срабатывает на псевдонимах типов: `UserName`, `Password` (`Annotated[...]`, по 2 использования в `app/schemas/user.py`). Fixer обошёл его хуже, чем было: `Password` унёс в `app/schemas/types.py` (карточка просила общий тип в `user.py`), `UserName` развернул в два одинаковых `NonEmptyStr = Field(max_length=255)`. Псевдоним типа — не глобальное состояние, а правило `AGENTS.md` о глобалах.
5. Мелочи:
   - а) перед `--resume` `implement.py:237` удаляет `.loop/handoff.md` (так задумано: старый handoff не должен сойти за новый), а `prompts.RESUMED` об этом молчит: агент в шаге 14 пытался дописать прежний handoff через `Edit`, получил «файла нет» и написал заново;
   - б) Ctrl+C вне сессии (`up`, gate, коммит, между задачами) — `KeyboardInterrupt` → `cli._work` → `_reap` → `recovery._pause`: причина `died`, в логе «the orchestrator of this run is gone», хотя оператор жив и сам остановил прогон; код выхода 130. Ctrl+C в сессии — `paused(interrupt)` и код 1. Одна остановка оператора — две причины и два кода;
   - в) `refs/loop/wip/584816d9/1..4` остались после `done`: WIP-ref закоммиченной задачи никто не удаляет, они копятся в продуктовом репозитории.

Решение человека (2026-10-16): находки 1–5 разложить на правки. Декомпозиция агента: пять коммитов в `implementation-loop` (правки этапа 3а — там же, решение 2026-10-12), порядок — по серьёзности, каждый коммит самостоятелен. Человек принял рекомендации агента, включая п. 1 коммита 4 (2026-10-16).

#### Коммит 1. Коммит задачи, прерванный на полпути (находки 1, 5в) — готово 2026-10-16

Сделано:
- `recovery._finish_commit`: у открытого шага `commit` HEAD с родителем-checkpoint и сообщением из `implement.last_answer` — коммит цикла: `git reset --quiet`, WIP-ref задачи удаляется, шаг `completed` с `{"commit", "message"}`, задача `committed`, WIP не пишется. Иначе прежний путь: WIP поверх checkpoint, `paused(died)`.
- `git.delete_ref` (`update-ref -d`; ref, которого нет, git ошибкой не считает). `implement._commit` удаляет WIP-ref сразу после `git.commit`, до `finish_step`: остановка между ними попадает в `_finish_commit`, который удаляет его ещё раз.
- План, раздел 3: строка решения 2026-10-16 (в разделе 9 про доуборку только `down`, поэтому решение — в таблице рядом с «Уборка после аварии»).
- Тесты: `test_recovery` — открытый `commit` с коммитом цикла и с чужим коммитом (параметризация); `test_cli` — `KeyboardInterrupt` из `git reset` внутри `git.commit`, код 130, `loop resume` коммитит вторую задачу; `test_implement` — после паузы и коммита WIP-ref нет. Без правки в `src` три новых проверки падают. 377 тестов, `ruff check`, `ruff format --check` зелёные.

Не делалось: блокировка SIGINT вокруг коммита (п. 2). Причина паузы после Ctrl+C внутри коммита по-прежнему `died` — это коммит 2.


1. `recovery._pause`: открытый шаг `commit`, у которого HEAD ветки прогона не равен checkpoint, родитель HEAD — checkpoint и сообщение HEAD равно `commit_message` последнего ответа задачи (`implement.last_answer`), — коммит цикла. Тогда `git reset --quiet` (индекс к HEAD, рабочее дерево не трогать), шаг `completed` с `{"commit", "message"}` как в `_commit`, задача `gating → committed`, WIP не пишется. Иначе — как сейчас: чужой HEAD, WIP поверх checkpoint, `paused(died)`.
2. Блокировка SIGINT вокруг коммита не нужна: `kill -9` и сбой оставляют то же окно, а п. 1 закрывает все три.
3. WIP-ref задачи (`git.wip_ref(run, seq)`) удаляется, когда задача закоммичена: в `_commit` после `finish_step` и в п. 1. Новая функция `git.delete_ref` только если в `git.py` нет подходящей; WIP-ref, которого нет, — не ошибка (задача без пауз).
4. План: раздел 9 (доуборка мёртвых прогонов) — строка про открытый шаг `commit`.

Тесты: `test_recovery` — открытый `commit` с коммитом цикла на HEAD: шаг `completed`, задача `committed`, индекс чистый, WIP-ref нет; с чужим коммитом на HEAD (родитель checkpoint, другое сообщение) — как сейчас. `test_implement` или `test_cli` — `KeyboardInterrupt` из `git reset` внутри `git.commit` (monkeypatch): после `_reap` задача `committed`, `resume` идёт к следующей задаче без отказа «put them back». `test_implement` — WIP-ref задачи, вставшей на паузу и затем закоммиченной, удалён.

#### Коммит 2. Остановка оператором вне сессии (находка 5б) — готово 2026-10-16

Сделано:
- `recovery.pause(store, run, reason)` — бывший `_pause`, причина параметром; «the orchestrator of this run is gone» пишет только `reap`, он же передаёт `died`.
- `runner.start`/`resume` возвращают причину паузы прогона (`None` — все задачи прошли) вместо `bool`. `KeyboardInterrupt` в `install`, `up`, gate, коммите, между задачами ловится в `runner` (`_interrupted`) и ставит свой прогон на паузу `recovery.pause(..., interrupt)` под его же lock; `down` к этому моменту уже отработал в `commands.services`. Ctrl+C в `down`, когда прогон уже на паузе или `done`, прогон не трогает.
- Ctrl+C во время ожидания лимитов (`runner._wait`): прогон остаётся `paused(budget)` — `loop resume` ждёт именно лимитов, — но возвращается `interrupt`. Отступление от п. 2 («источник — причина паузы прогона»): здесь код 130 даёт путь, а не причина.
- `cli._work`: `None` → 0, `interrupt` → 130, иначе 1. `except KeyboardInterrupt` в `cli` остаётся для Ctrl+C вне `runner` (до паузы прогона, во время самой паузы): такой прогон по-прежнему доуберёт `_reap` как `died`.
- П. 3 проверен: `step.how_to_resume` и `runner._work` `died` и `interrupt` не различают. У шагов gate/commit `implement._resume_paused` возвращается до `how_to_resume`; `how_to_resume` смотрит только на `timeout`/`idle`/`guard_error` и контекст (у шагов из `recovery.pause` `context_tokens` нет в обоих случаях); `runner._work` смотрит на причину задачи только после `go_on` в живом прогоне.
- План, раздел 3: строка решения 2026-10-16.
- Тесты: `test_cli` — Ctrl+C в `up`, в gate и после ответа сессии (параметризация): код 130, прогон `paused(interrupt)`, у gate — шаг `paused(interrupt)`, в логе нет «is gone», `loop resume` коммитит задачу. Без правки в `src` падают все три. `test_runner` — ожидания `True`/`False` заменены причиной паузы (`None`, `status`, `reason`, `interrupt`). 380 тестов, `ruff check`, `ruff format --check`, `gates._style` по диффу — зелёные.

План коммита:

1. `cli._work`, `except KeyboardInterrupt`: прогон этого процесса ставится на паузу с причиной `interrupt`, не `died`, и без «the orchestrator of this run is gone». Способ — параметр причины у `recovery.reap`/`_pause` или отдельная функция в `recovery` для своего прогона; выбрать по коду, без второго пути сохранения WIP. Шаги, открытые при Ctrl+C, — `paused(interrupt)`.
2. Код выхода остановки оператором — 130 везде: и `paused(interrupt)` после сессии (сейчас 1, мелочь второго прогона 3а.2), и `KeyboardInterrupt` вне сессии. Источник — причина паузы прогона, а не путь, которым он встал.
3. Проверить, что `step.how_to_resume` и `runner._work` не различают `died` и `interrupt` у шагов gate/commit; если различают — описать в коммите.

Тесты: `test_cli` — `KeyboardInterrupt` из `up` и из gate: прогон `paused(interrupt)`, код 130, в логе нет «is gone»; остановка после ответа сессии — код 130. Существующий `test_run_stopped_by_an_error_is_paused_as_a_dead_one` не меняется: ошибка — по-прежнему `died`.

#### Коммит 3. Стоимость шага на `--resume` (находка 3) — готово 2026-10-16

Сделано:
- `step.run`: `cost_usd` шага — `total_cost_usd` результата минус последний `session_cost_usd` шагов той же сессии (`step._cost_before`, `Store.session_steps`); `session_cost_usd` — сам `total_cost_usd`. Строка `completed in N turns, $X` — стоимость шага. Шаги без итога (убитые, из `recovery.pause`, упавшие с ошибкой CLI) пропускаются: стоимость убитого шага уходит в следующий шаг сессии.
- Комментарий у `agent.Result.cost_usd`: на `--resume` — стоимость всей сессии.
- План: раздел 2 (S1) — какие поля `result` накопительные на `--resume`; раздел 3 — строка решения 2026-10-16.
- Тесты: `test_step` — шаг сессии с итогом 0.5, убитый шаг той же сессии, шаг другой сессии (0.5), `--resume` с итогом 0.8: стоимости 0.5, —, 0.5, 0.3, в логе `$0.30`. Без правки в `src` тест падает.
- Попутно: фикстура `stderr_lines` в `test_step` вызывала `log.setup()` до тела теста, вопреки своему комментарию, и обработчик писал в закрытый поток capsys. `test_events_feed_the_guard_and_the_store` проходил на тексте `--- Logging error ---` (строка `Message: 'ctx 18k/150k · 5h …'`). `log.setup()` перенесён в тела тестов, как в `test_log`.
- 381 тест, `ruff check`, `ruff format --check`, `gates._style` по диффу — зелёные. Один полный прогон уронил `test_tool_results_keep_the_stream_alive` (паузы 0,6 с при таймауте простоя 1,2 с под нагрузкой); отдельно, с правкой и без, и при повторе полного набора — зелёный. Не трогал: тест о таймингах, правка его не касается.

План коммита:

1. `step.run_step`: `cost_usd` шага — `total_cost_usd` минус `cost_usd`-итог прошлых шагов той же сессии из store; накопительное значение — в `detail` как `session_cost_usd`, чтобы разность можно было пересчитать. Строка `completed in N turns, $X` — стоимость шага.
2. Шаг, убитый без результата (`detail` без `cost_usd`), разность не ломает: следующий шаг сессии вычитает последний известный итог. Стоимость убитого шага теряется (CLI её не отдал) и уходит в следующий шаг сессии — так и записать в комментарии.
3. Старые строки `loop.db` не мигрировать: в разборе прогонов до `584816d9` включительно брать максимум `cost_usd` по сессии.

Тесты: `test_step` — две итерации одной сессии с `total_cost_usd` 0.5 и 0.8: у шагов 0.5 и 0.3, `session_cost_usd` 0.5 и 0.8; убитый шаг между ними не меняет итог.

#### Коммит 4. Промпты: невыполнимый критерий и handoff после `--resume` (находки 2, 5а) — готово 2026-10-16

Сделано:
- `prompts.ANSWER_SCHEMA` и `Answer`: необязательное `operator_actions` (список строк, по умолчанию пустой кортеж, как `split`). `session.md`: при закрытом файле — доделать остальное и назвать правку в `operator_actions`, это не повод для `needs_human`; описание поля в списке ответа с примером `add JWT_SECRET= to .env.example`.
- `implement._commit`: после `committed` каждое действие — `WARNING` «the task needs this from the operator: …». `runner._report_operator_actions`: в конце прогона после «all N tasks are through» один `WARNING` со списком `task N: …` по закоммиченным задачам; без действий строки нет. `recovery._finish_commit` действий не пишет — их покажет список в конце прогона.
- `prompts.RESUMED`: handoff паузы удалён, при новой остановке писать заново целиком.
- План: раздел 3 — строка решения 2026-10-16; раздел 8 — поле в схеме ответа; этап 4 — п. 3 (карточка не просит правок закрытых файлов).
- Тесты: `test_prompts` — ответ с `operator_actions`; `test_implement` — новый `test_actions_for_the_operator_are_reported_after_the_commit` (задача `committed`, после строки `committed` ровно два `WARNING` с действиями); `test_runner` — `commits()` принимает действия, счастливый путь проверяет список после «all 2 tasks are through». caplog берёт INFO только с `caplog.at_level(logging.INFO, logger="agent_loop")`. Текст `RESUMED` отдельным тестом не проверяется: проверка константы на слово — шум, подключение `RESUMED` к возобновлённой сессии уже проверяет `test_paused_session_goes_on_with_its_context`.

План коммита:

1. Агент сделал ровно то, что велит `prompts/session.md`: «if the task needs such a change, say so in your answer» — и ответ ушёл в `summary`, которую цикл не читает. Рекомендация агента: необязательное поле ответа `operator_actions` (список строк: что оператор должен сделать руками после задачи) в `prompts.ANSWER_SCHEMA`; `session.md` направляет туда вместо «say so in your answer»; цикл пишет каждое действие строкой `WARNING` после коммита и ещё раз списком в конце прогона (`all N tasks are through`). Задача коммитится: строка в `.env.example` не стоит остановки прогона. Отклонено — `needs_human` на такой критерий: проще (одна фраза в `session.md`), но до `loop answer` (этап 4) прогон встаёт насовсем. Решение человека (2026-10-16): `operator_actions`, по рекомендации агента.
2. `prompts.RESUMED`: прежний `.loop/handoff.md` удалён; если сессию снова остановят, handoff пишется заново целиком.
3. Для этапа 4 (планировщик): карточка не просит правок `.env*` и других файлов, закрытых для агента, — их делает оператор после задачи. Записать в план, раздел планирования; в коде этапа 3 правок нет.

Тесты: `test_prompts` — схема с `operator_actions`, `RESUMED`; `test_implement` — ответ с `operator_actions`: задача `committed`, действия в логе после коммита; `test_runner` — список в конце прогона.

#### Коммит 5. Псевдонимы типов и правило трёх использований (находка 4) — готово 2026-10-16

Сделано:
- `gates._module_names` (им пользуются и `rare` в `_module_style`, и `before` в `_style`): пропускает `X = Annotated[...]` (имя `Annotated` само по себе или через модуль: `typing.Annotated`, `t.Annotated` — `gates._typing_name`), `X: TypeAlias = ...`; `type X = ...` — `ast.TypeAlias`, он и раньше не был `Assign`. Другие подписки и объединения остаются под правилом: `Rows = list[dict]` с двумя использованиями — находка, случай закреплён тестом.
- План, раздел 3: строка решения 2026-10-16.
- Тесты: `test_gates` — в `test_style_keeps_to_new_breaks_in_python` четыре псевдонима (`type_alias(line)`), в `test_style_breaks_go_back_to_the_executor` подписка `list[dict]`. Без правки в `src` падают три случая (`Annotated`, `typing.Annotated`, `TypeAlias`).

План коммита:

1. `gates._module_style`: имена, чьё значение — псевдоним типа, не считаются глобалами: `X = Annotated[...]`, `X: TypeAlias = ...`, `type X = ...` (`ast.TypeAlias`). Подписки вроде `Literal[...]` и объединения `A | B` — только вместе с `Annotated`, если тесты на `api-service-foodtech` покажут нужду; без нужды не расширять.
2. Константы остаются под правилом: `_EMAIL_PATTERN = r"..."` с одним использованием (находка задачи 1) — по-прежнему находка.

Тесты: `test_gates` — параметризация существующего теста правила: `Password = Annotated[str, ...]` с двумя использованиями — нет находки; строковая константа с двумя — находка.

#### Коммит 6. Gate с `pass` в логе (находка первого прогона 3а.2) — готово 2026-10-16

`gates.check` пишет `INFO` «passed with N changed lines» перед возвратом вердикта `pass` (и при `pass` с предупреждением о flaky-тестах). Тест: `test_clean_work_passes_with_its_size` проверяет строку через caplog. План, раздел 2: находка помечена закрытой.

### 3а.2в Ревью ветки перед мержем — готово 2026-10-16

Ревью `git diff main...implementation-loop` (38 файлов, +5776/−185, 29 коммитов) в отдельной сессии: критических проблем нет, 387 тестов и ruff зелёные, ссылок на план и маркеров AI-кода в коде нет, слоистость и переходы статусов сходятся. Восемь находок средней и низкой тяжести; решение человека (2026-10-16): 1 и 2 исправить в ветке, 3–8 — долг шагом 0 этапа 4 (план, раздел 14).

#### Коммит 7. Правки по ревью перед мержем (находки 1, 2) — готово 2026-10-16

1. `gates.check`: `numstat` и `lines` брались до команд профиля, а `git.commit` через `add --update` забирает все изменения отслеживаемых файлов. Файл, который `fmt` переформатировал мимо работы задачи, попадал в коммит, не входил в размер диффа и не проходил проверку стиля. Теперь после команд `numstat` и `lines` пересчитываются, `_style` и строка `passed with N changed lines` идут по ним. Проверка размера до команд не повторяется: замечание о коммите чужого переформатирования остаётся в задаче «`fmt`/`lint` на неотформатированных репозиториях» (3а.3).
2. `implement._loop`: ответ сессии с `interrupt` и статусом не `done` (`blocked`, `needs_human`, `too_big`) уводил задачу в `needs_human`/`replan`, а прогон вставал с причиной `interrupt` — на `loop resume` отказ «task N waits in needs_human» был верным, причина паузы прогона и предупреждение в логе — нет. Теперь `_take` решает первым: задача ушла ждать — цикл возвращает `None`, и `runner` ставит прогон на паузу по статусу задачи; `Stop(INTERRUPT)` — только когда работа идёт к gate.
3. Тесты: `test_gates` — новый `test_files_the_commands_change_are_the_work` (`fmt` дописывает разделитель в `old.py`, которого задача не трогала: `lines == 3`, находка стиля `old.py:2`); `test_implement` — `test_interrupt_while_answering_stops_before_the_gate` параметризован ответом `blocked` с `interrupt` (`None`, задача `needs_human(blocked)`). Без правок в `src` оба новых случая падают.

### 3а.3 Мерж и дальше — мерж готов 2026-10-16

`implementation-loop` смержена в `main` (PR #2, `10e2836`). Этап 4 — ветка `planner`. Этап 4 (планирование) — в новой ветке, имя спросить у человека; вход `loop run` из файла карточек заменит утверждённый план, там же `loop answer` для `needs_human` и перепланирование для `replan`. Go-репозиторий — отдельным прогоном позже.

Открыто к обсуждению: `needs_human` на изменение `docker-compose*.yml` и `[build-system]` (остаток риска `install`/`up`, раздел 10).

Задача после испытания: `fmt`/`lint` на неотформатированных репозиториях (cms). Сейчас gate запускает `fmt` по всему репозиторию, коммит забирает всё изменённое — в неотформатированном репозитории первый коммит уносит чужое переформатирование. Три режима задаются профилем, без режима в самом цикле:
- выключено — команды нет (`fmt` не задан);
- весь репозиторий — `ruff format .`, требует отформатированного `main`;
- только файлы работы — новая подстановка `{changed_files}` рядом с `{changed_tests}`: `ruff format {changed_files}`, `ruff check {changed_files}`.

Что сделать: заполнять `{changed_files}` существующими изменёнными файлами работы в `gates._run_commands`; команду с пустой подстановкой не запускать (`ruff format` без путей форматирует весь каталог). Ruff сам пропускает переданные явно не-Python файлы (проверено 2026-10-16: `README.md`, `pyproject.toml` не тронуты, код 0). Ограничения режима: тронутый файл форматируется целиком, а `lint` находит старые нарушения в тронутых файлах — для командного репозитория, где это нежелательно, остаётся «выключено». Проверка только изменённых строк (`ruff format --range` — один файл за вызов, разбор вывода `lint` по ханкам) — сложнее, не делать без нужды. Дополнительно обсудить: изменённые командами отслеживаемые файлы вне работы — находка gate, а не молчаливая часть коммита.

## Этап 4. Планирование

Ветка `planner` от `main` после мержа PR #2. Все шаги этапа — коммиты в ней.

### 4.0 Долг по ревью `implementation-loop` — готово 2026-10-16

Находки 3–8 ревью перед мержем (план, раздел 14, этап 4, шаг 0). Один коммит.
1. `runner.resume`: прогон с `base = NULL` (создан до колонки) — `RunError` «has no record of the HEAD it started from, start a new run». Тип `Run.base` остаётся `str | None`: старые строки не мигрируются, тип говорит правду о таблице.
2. `prompts/AGENTS.md` — симлинк: без правок, решить при первой упаковке (wheel уже проверен в 3а.1б).
3. `cli._work`: `FileNotFoundError` убран из списка. Отсутствие файлов прогона ожидаемо только в `runner.resume` (оператор удалил `runs/<run>` руками; `loop gc` их не удаляет): там проверка `profile.json` и копии `protected` до `commands.frozen` и `RunError` с именем пропавшего. `protected.restore` по-прежнему бросает `FileNotFoundError` — теперь это ошибка программы с трейсбеком.
4. `gates._weakened`: проверка — `assert` словом или `assert\w*(` (`assert_called_once()`, `self.assertEqual(`, `assert_that(`); `assertion`/`asserted` в комментарии не считаются. Раньше комментарий со словом `assertion` на месте удалённого `assert` гасил находку (−1 + 1 = 0).
5. `commands._sandbox`, спайк на реальном `sandbox-exec` 2026-10-16: пути с пробелом, табом, `()[]` и обратной косой чертой — запись в репозиторий есть, `.git/hooks`, `*.pem`, `../` закрыты; `re.escape` даёт `\ `, seatbelt читает верно. Путь с `"` ломает разбор профиля: любая gate-команда падает с невнятной ошибкой — закрыто, а не открыто; правок нет. Фикстура `sandboxed` теперь кладёт репозиторий в `my repo`: все тесты sandbox идут по пути с пробелом.
6. `test_cli`: `test_run_commits_the_work_of_its_tasks` → `test_run_through_all_its_tasks_exits_with_zero`, проверяет только код выхода и один прогон `done`; коммит проверяет `test_implement`.

Тесты: `test_runner::test_resume_refuses_a_run_it_cannot_check` (параметры: `base = NULL`, удалённая копия `protected`, удалённый `profile.json`); `test_gates::test_weakened_tests_go_back_to_the_executor` — случай «`assert` заменён комментарием `# no assertion yet`»; из `test_cli::test_run_stopped_by_an_error_is_paused_as_a_dead_one` убран параметр `FileNotFoundError`.

Предлагаемое сообщение коммита: `fix(runner): refuse to resume a run without its base or files, narrow the removed assert check`.

Дальше — шаг 4.1: схема `plan.json`, роль планировщика, `loop plan` (интерактивный и `--from task.md` с `QUESTIONS.md` и `loop answer`), валидация на выходе. Перед стартом шага предложить человеку разбиение на коммиты.
