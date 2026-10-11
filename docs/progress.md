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

## Дальше: переход на `--restricted` (обсуждение 2026-10-11)

Решение человека: переходим на `--restricted`, если выигрыш подтвердится спайком. CLAUDE.md в большинстве его репозиториев нет, контекст проекта идёт через `context_file`, так что потеря нативной загрузки CLAUDE.md почти ничего не стоит.

Зачем (текущая схема уже держит — спайк 3.0, — выигрыш в простоте и запасе):
- Проектные настройки игнорируются целиком, а не по нашему списку путей (`settings*.json`, `hooks`, `skills`, `agents`, `commands`, `.mcp.json`), который уже дважды дополняли по ревью (2b, 3.0). Закрываются и плагины из `enabledPlugins` проектных настроек.
- Можно работать в репозиториях с проектными hook'ами: отказ `ProjectHooksError` уходит.
- В сессии только инструменты из `--tools`; сейчас их ~22 (`Workflow`, `Cron*`, `RemoteTrigger`, `SendMessage` и т. п.): `dontAsk` их отклоняет, но описания едят контекст, а агент пробует их вызывать.
- Уходит `--setting-sources project`.

### Шаг 3.1а. Спайк `--restricted` (без кода в репозитории)

Скрипт спайка 3.0 был в scratchpad прошлой сессии и не сохранился; основа — `guard.prepare` + `agent.command` с добавленными флагами, запуск `claude` с окружением `agent.ENV_KEYS`, разбор `init` (`tools`), `tool_use`/`tool_result`, `permission_denials`. Модель `haiku`, effort `low`. Флаги: `--restricted --tools <список>` к нашему `--settings` и `--strict-mcp-config`.

Проверить:
1. **Блокер, если подтвердится**: по `--help`, запись в «settings, git and tool-configuration files» в этом режиме разрешает только человек или обработчик разрешений. Может ли агент через `Edit`/`Write` править `pyproject.toml`, `requirements.txt`, `ruff.toml`, `setup.cfg`, `.pre-commit-config.yaml`, `Makefile`, `.gitignore`, `Dockerfile`, `.github/workflows/*.yml`. Если `pyproject.toml` и подобные закрыты — остаёмся на текущей схеме (агент вносит зависимости правкой `pyproject.toml`, раздел 3 плана).
2. `--json-schema`: приходит ли `structured_output`, нужно ли называть `StructuredOutput` в `--tools`; то же для `TodoWrite`.
3. Правила из `--settings` в этом режиме: deny на `.env` и `~/.ssh` (и для `Read`, и для Bash в sandbox), сеть наружу из Bash закрыта, localhost открыт, `git status`/`git diff`/`git log` из Bash работают, `git commit` отклонён.
4. Что ещё игнорируется: проектные `.claude/skills` (виден ли skill в `init.skills`), `.claude/rules`, `.mcp.json` без `--strict-mcp-config`.

Уже проверено в 3.0 (план, раздел 2): hook из `--settings` работает, sandbox действует, `--resume` работает, hook из `.claude/settings.json` репозитория не выполняется, CLAUDE.md рабочего каталога **не** подхватывается.

Итог спайка — в план: находки в раздел 2, решение в раздел 3 (строка «Проектные hook'и» и «Изоляция сессий цикла»), раздел 10.

### Шаг 3.1б. Переход (если спайк зелёный), ~200–300 строк

- `agent.command`: `--restricted --tools <список>` вместо `--setting-sources project`; `--strict-mcp-config` остаётся. Список инструментов — из спайка (`Bash,Read,Edit,Write` и, возможно, `StructuredOutput`, `TodoWrite`); `--disallowed-tools Agent` становится лишним.
- CLAUDE.md репозитория передаётся сами: `CLAUDE.md` и `.claude/CLAUDE.md` из корня, если есть, добавляются к файлам правил (адаптер уже склеивает их в один `--append-system-prompt-file`). `@path`-импорты разворачиваются сами, только для файлов внутри репозитория (иначе `@~/.ssh/config` затянет секрет в промпт), с ограничением глубины. CLAUDE.md вложенных каталогов и `.claude/rules` с `paths:` не повторяем — в промпте исполнителя (3.3) строка «если в каталоге, где работаешь, есть CLAUDE.md, прочитай его».
- `selfcheck`: первая сессия по-прежнему проверяет слово из CLAUDE.md рабочего каталога — теперь это проверка нашей передачи; добавить проверку, что проектный hook не выполнился.
- `protected`: убрать отказ `ProjectHooksError` (и тесты на него). `protected.restore` **оставить**: он защищает уже не сессии цикла, а следующую интерактивную сессию человека в этом репозитории — подложенный hook в `.claude/settings.local.json` сработал бы у него вне sandbox, а этот файл обычно в gitignore и в дифф не попадает.
- `loop-settings.json`: пересмотреть deny, ставшие лишними (`Skill`, если инструмента нет в `--tools`; `Edit(.claude/**)`, `Edit(.mcp.json)` оставить — это защита человека, см. выше).
- План: раздел 3 (решение), раздел 10, раздел 14 (нумерация этапа 3).

Если спайк красный — записать находки и причину отказа в план, 3.1б пропустить.

### Потом

Бывший шаг 3.1 сдвигается: 3.2 — профиль репозитория (`loop.toml`: `[commands]`, `[timeouts]`, `[env]`, `deps_files`, `secret_patterns`, проверка неизвестных ключей), lock рабочего каталога `.git/agent-loop.lock` через `recovery.hold`, `down` профиля в финализаторе и в `reap`; 3.3 — `gates` (`install` по изменению зависимостей, команды с таймаутами, перезапуск упавших тестов, инварианты ветки/HEAD, числа тестов, размера диффа, секретов, `protected.restore`). Дальше по плану: сборка промпта и цикл fixer, коммит кодом. Учесть пробелы из вводной к этапу 3 в плане (handoff после жёсткой остановки, счётчик пауз `timeout`/`guard_error`, `how_to_resume` только после `paused`, абсолютный `LOOP_HOME`).

Все шаги этапа 3 — коммиты в ветке `implementation-loop`.
