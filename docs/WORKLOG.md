# Журнал работ

Что менялось в слое `bu_mcp` и в `browser_use/browser/` — по датам, с тем, что
проверено, и с тем, что осталось сломанным. Не пересказ коммитов: сюда попадает
причина правки, способ проверки и тупики, в которые не стоит ходить второй раз.

Контракты модулей — [`CONTRACTS.md`](CONTRACTS.md), журнал и макросы —
[`JOURNAL_CONTRACT.md`](JOURNAL_CONTRACT.md), обзор слоя — [`BU_MCP.md`](BU_MCP.md).

## 2026-09-13 — bu_eval → benchmark, удалены мёртвые доки/Docker, найдены 2 бага переноса

Переименование: `bu_eval/` → `benchmark/`, `examples/eval/` → `examples/benchmark/`.
Механическая замена `\bbu_eval\b` → `benchmark` во всех `.py`/`.md` (env-переменные
`BU_EVAL_*` не тронуты — публичный конфиг, как раньше `BU_MCP_*` при переезде
`bu_mcp`).

Удалены как мёртвые (не читались ни кодом, ни CI, ни README/CLAUDE.md —
проверено `grep`): `AGENTS.md`, `BETA_AGENT_INTEGRATION_FEATURES.md`,
`CLOUD.md` (апстримные доки под контрибьютинг/Rust-агента/облако — не про
этот форк), `Dockerfile`, `Dockerfile.fast`, `docker/`, `.dockerignore`
(мы гоняем локальный Chrome через CDP, а не контейнеризируем браузер; CI под
это уже был отключён раньше — `docker.yml.disabled`).

**После переноса `bench doctor`/`benchmark selftest` поймали 2 реальных бага,
оставшихся от более раннего разбиения `server.py` на подмодули и от
сегодняшнего переезда `bu_mcp` → `browser_use/mcp/` — раньше их никто не
гонял:**

1. `NOOP_MARKERS`, `NEW_TAB_CLAIM_RE`, `NEW_TAB_NOTE_RE` переехали в
   `server_shared.py` при разбиении `server.py`, но не были реэкспортированы
   обратно из `server.py` (в отличие от `JOURNAL_FIELDS`, который сделали
   правильно). `benchmark/upstream.py` их так и ждёт с `browser_use.mcp.server`
   — падало `ImportError`. Починено реэкспортом.
2. `browser_use/mcp/bench.py` и `browser_use/mcp/smoke.py` считали корень
   репозитория как `Path(__file__).resolve().parent` — это было верно, когда
   файлы лежали прямо в `bu_mcp/` (один уровень от корня), но после переезда в
   `browser_use/mcp/` (два уровня) `REPO`/`ROOT` стали указывать на
   `browser_use/`, а не на корень. `bench.py` из-за этого пытался запустить
   несуществующий `browser_use/.venv/bin/python` — `FileNotFoundError`.
   `smoke.py` та же ошибка маскировалась editable-инсталлом (`browser_use`
   всё равно резолвился без верного `PYTHONPATH`), поэтому 85/1 при прошлой
   проверке был не врал, но `ROOT` там тоже был битым — поправлено на
   `.parents[1]`/`.parents[2]` в обоих файлах.

Заодно проверка `check_dead_wait_knobs` в `benchmark/upstream.py` теперь
исключает `browser_use/mcp/` из сканирования (это наш код с 2026-09-13, не
апстрим — её же докстрока упоминает имена мёртвых полей апстрима как прозу,
что раньше давало ложный `СЛОМ`).

**Проверено:** `benchmark doctor` — все допущения снова OK, кроме
`viewport-override при headless` (не регрессия — воспроизводится и на чистом
коде до всех сегодняшних правок, чисто рантайм/окружение). `benchmark selftest`
— 11/11 OK. Полный `tests/ci` (1152 теста) — на чистом коде и на коде с
сегодняшними правками ОДИНАКОВЫЙ результат: 1113 passed / 34 skipped
(2 теста в `test_beta_agent.py` нестабильны при полном xdist-прогоне
независимо от наших правок — по отдельности оба проходят; воспроизведено на
чистом HEAD тоже, так что это не регрессия).

## 2026-09-13 — удалён мёртвый /skills/, починена регрессия в его тесте

Корневой `/skills/` (Claude-Skill-формат: `browser-use/SKILL.md`,
`open-source/`, `cloud/`, `qa/`, `remote-browser/`, `x402/`) — это контент под
штатный `browser-use skill install`, который мы уже вычистили из README.
Ничем в коде не читался (проверено `grep` по всем `.py`), удалён целиком.

Важно не путать с `browser_use/skills/` (библиотечный модуль, `SkillService`) —
это отдельная фича, выполнение скиллов через Browser Use Cloud API, реально
используется в `Agent.__init__` (`skills=`/`skill_ids=`), `browser_use/cli.py`,
`browser_use/mcp/cli_mcp.py`. Его не трогали — удаление сломало бы импорт
самого класса `Agent`.

Попутно нашли и почини регрессию, которую пропустили раньше: правка README
(строка `run \`browser-use skill install\` to register the skill` была убрана
вместе с секцией Quickstart) уже ломала
`tests/ci/test_browser_use_skill_install_docs.py::test_docs_install_browser_use_skill_from_package_alias`,
просто никто не гонял `tests/ci` целиком после того коммита. Плюс 2 теста в
том же файле читали контент удалённого `/skills/` напрямую
(`test_cloud_v4_reference_scopes_workspace_file_listing`,
`test_remote_browser_skill_uses_current_cli`). Убрали все три, оставили 2
теста поведения `browser-use cli skill install` (они не про README/`/skills/`,
а про реальный код `browser_use/skills/browser_use.py` — им ничего не
угрожало). `uv run ruff check` + `pytest` по файлу — 2/2 зелёных.

`.github/workflows/install-script.yml` триггерится на push при изменении
`skills/browser-use/SKILL.md` и гоняет `scripts/sync_browser_harness_skill.py
--check`, который бы теперь падал (файла нет) — отключили тем же способом,
что раньше `docker.yml` (`.disabled`).

## 2026-09-13 — bu_mcp перенесён в browser_use/mcp/

`bu_mcp/` физически переехал в `browser_use/mcp/`, заменив штатный MCP-сервер
апстрима файлом на файл: `git rm browser_use/mcp/server.py` + `git mv
bu_mcp/server.py browser_use/mcp/server.py`, остальные модули (`server_shared.py`,
`domain_gate.py`, `cdp_session.py`, `registry_bridge.py`, `noop.py`, `delta.py`,
`journal.py`, `macro.py`, `state.py`, `resolve.py`, `waiting.py`, `downloads.py`,
`uploads.py`, `bench.py`, `bench_results.json`, `smoke.py`, `actions/`) — рядом,
без префикса `bu_mcp.`. Папка `bu_mcp/` больше не существует. Раньше слой жил
СНАРУЖИ библиотеки и её код не трогал вовсе; теперь `browser_use/mcp/server.py`
и соседи в этой директории — наш код на месте апстримного, со всеми вытекающими
последствиями для будущих обновлений апстрима (конфликт почти гарантирован
именно в `browser_use/mcp/`, в отличие от `benchmark/`/`scripts/`, которые
остались снаружи).

`browser_use/mcp/__init__.py`: ленивый экспорт `BrowserUseServer` заменён на
`BuMcpServer` — других имён `BrowserUseServer` в кодовой базе не осталось.
`browser_use/cli.py` править не пришлось: `_run_mcp_stdio_server('browser_use.mcp.server')`
уже делает `importlib.import_module(...).main()`, а у нашего `server.py`
сигнатура `main()` совпадает — `browser-use --mcp` теперь запускает наш код
без правок диспетчера.

Все реальные пути поправлены: статические импорты (`from bu_mcp.X import` во
всех модулях `browser_use/mcp/` и в `benchmark/{selftest,backends,upstream}.py`),
ленивые `importlib.import_module('bu_mcp.X')` (`server_shared.bu_mcp_module`,
`macro.py`, `journal.py`), строковые аргументы подпроцесса (`smoke.py`:
`args=['-m', 'bu_mcp.server']` x2; `bench.py`: `argv=[...,'-m','bu_mcp.server']`),
и критично — ключи `sys.modules['bu_mcp.journal']` в тестах отказоустойчивости
журнала в `smoke.py` (иначе подмена модуля переставала перехватывать реальный
`importlib.import_module('browser_use.mcp.journal')` внутри `journal.write()`,
и тесты тихо проверяли бы не то). Логгеры (`logging.getLogger('bu_mcp.server')`)
и вступительные докстроки тоже поправлены. НЕ тронуты намеренно: `BU_MCP_*`
переменные окружения (публичный конфиг, путь кода на них не влияет), ключ
`"bu-mcp"` в `~/.claude.json`/`mcp__bu-mcp__*` имена тулов (это имя MCP-сервера,
а не модуля), бренд-упоминания "bu_mcp" в прозе бенчмарк-отчётов
(`browser_use/mcp/bench.py`) и в `benchmark/*` — там это название нашего слоя,
не путь к коду.

Удалены 2 CI-теста апстрима, привязанные к внутренностям штатного
`BrowserUseServer` (`_execute_tool`, `_retry_with_browser_use_agent`, тулы
`browser_get_state`/`retry_with_browser_use_agent`), которых у `BuMcpServer`
нет: `tests/ci/test_mcp_tool_annotations.py` и
`tests/ci/security/test_mcp_allowed_domains.py`. **Важно:** второй — это
регрессионный тест на реальную уязвимость **GHSA-vfcm-843v-w6v3** (обход
allowlist доменов через `retry_with_browser_use_agent` с
`allowed_domains=[]` по умолчанию вместо `None`). Удалили по прямому решению
пользователя, БЕЗ проверки, что `domain_gate.py` защищён от эквивалентной
дыры на новой форме API — это осознанно принятый риск, а не забытая
проверка. `tests/ci/test_mcp_client_error_result.py` не трогали — он тестирует
`browser_use.mcp.client.MCPClient`, к `BrowserUseServer`/`BuMcpServer`
отношения не имеет.

Обновлено вне репозитория: `~/.claude.json` → `mcpServers.bu-mcp.args`
(`["-m", "bu_mcp.server"]` → `["-m", "browser_use.mcp.server"]`), бэкап снят
перед правкой. Остальные ключи (`command`, `env.PYTHONPATH`, сам ключ
`"bu-mcp"`) не тронуты. После переноса живому MCP-подключению этой сессии
нужен перезапуск (`/mcp`) — как и при переезде пути репозитория раньше.

**Побочный эффект: A/B-бенчмарк сломан.** `browser_use/mcp/bench.py` сравнивал
`STOCK` (`python -m browser_use.mcp`, тулы `browser_get_state`/...) с `OURS`
(`python -m browser_use.mcp.server`, тулы `browser_state`/...). После переноса
оба запускают один и тот же код — стокового `BrowserUseServer` в репозитории
больше нет физически. `STOCK`-плечо теперь падает на `Unknown tool:
browser_get_state` вместо честного сравнения. Не чинили: пришлось бы тащить
старый `server.py` из истории git в отдельный путь только ради бенчмарка, а
это отдельное решение. `bench_results.json` остаётся как протокол одного
прошлого прогона, актуальным больше не является. Если бенчмарк снова понадобится
— можно `git show <коммит до переноса>:bu_mcp/server.py` или сравнить с
апстримным `browser_use/mcp/server.py` из отдельного чекаута.

## 2026-09-13 — server.py разбит на подмодули

`bu_mcp/server.py` (3544 строки, один класс `BuMcpServer` на ~65 методов) резал
на части: доменный гейт, CDP-сессия, мост к реестру `Tools()`, классификация
noop, дельта до/после действия — каждый в свой файл (`domain_gate.py`,
`cdp_session.py`, `registry_bridge.py`, `noop.py`, `delta.py`) как
mixin-классы, собранные в `BuMcpServer` множественным наследованием. Тулы по
группам действий — в `bu_mcp/actions/` (`navigate.py`, `interact.py`,
`capture.py`, `scroll.py`, `switch.py`, `files.py`, `macros.py`). Общие
константы, JS-сниппеты и схемы тулов — в `server_shared.py`. `server.py` стал
тонкой сборкой на 321 строку.

Логика journal/macro-тулов, которая была встроена в `server.py` вперемешку с
остальным, уехала в уже существующие `bu_mcp/journal.py` и `bu_mcp/macro.py`.
Методы там не трогали `self`, поэтому стали обычными функциями модуля, а не
методами-обёртками. `journal.write()` нарочно оставлен с ленивым
`importlib.import_module('bu_mcp.journal')` внутри самого модуля — на прямой
подмене `sys.modules['bu_mcp.journal']` стоят тесты отказоустойчивости
журнала, и обычный вызов сломал бы эту перехватываемость.

Три места чуть не сломались молча (fail-soft маскировал бы регрессию):
`macro.py` читал JS-сниппет через `getattr(server, '_DELTA_PROBE_JS', None)` —
источник переехал в `server_shared.DELTA_PROBE_JS`; `smoke.py` доставал
приватные методы `_journal_open/_write/...` через `getattr(BuMcpServer, ...)` —
переключил на прямые импорты из новых модулей; `server.py` перестал
экспортировать `JOURNAL_FIELDS`, который `smoke.py` тоже читает через
`getattr` — вернули реэкспортом.

Проверено: `ruff check`/`format` чисто. `pyright bu_mcp/server.py` (реальная
точка входа, где всё собрано) — 0 ошибок; `pyright` на отдельных файлах
миксинов даёт 109 `reportAttributeAccessIssue` — ожидаемый побочный эффект
паттерна (каждый файл типизируется в изоляции от `self`, который на самом деле
собран из всех миксинов) — не правили, ради этой метрики проект отдельные
файлы не проверяет. `bu_mcp/smoke.py` до и после — оба раза упирается в один и
тот же открытый дефект (см. следующий пункт, таймаут `Page.captureScreenshot`):
после рефакторинга 78 PASS / 1 FAIL, все pure contract-тесты прошли,
поведение перенесённой логики не изменилось.

## 2026-09-13 — репозиторий переехал в `~/browser-agent`

Папка называлась `~/browser-use` по имени апстрима, а пушим мы в
`github.com/draiqw/browser-agent`. Переименовали, чтобы совпадало.

Что пришлось поправить вместе с этим: абсолютные пути в `.venv`
(`bin/*`, `.pth`, метаданные dist-info), `~/.claude.json` (команда и
`PYTHONPATH` MCP-сервера `bu-mcp`, запись проекта), определения агентов
`haiku-browser-use` и `desktop-web`, `bu_mcp/BENCH.md` и `bu_mcp/bench.py`.

`bu_mcp/bench_results.json` оставлен как есть: там записан путь того прогона,
это протокол измерения, а не инструкция.

После переименования MCP-серверу нужен перезапуск (`/mcp`) — работающий процесс
держит старый inode и о переезде не знает.

## 2026-09-12 — скачивание, похожее имя, сторож фокуса

### Кейс целиком: фото → промпт → скачанный результат

Проверено на живом `chatgpt.com`, не на макете: фото из `bu_mcp/uploads/`
прикладывается, промпт отправляется, картинка генерируется, `Save` в лайтбоксе
кладёт PNG (789–817 КБ, 1586×992) в `bu_mcp/downloads/`. Записанный макрос
`chatgpt-photo-download` повторяет это **без модели в цикле**: 12 шагов из 12,
три успешных прогона, 50–76 с.

Что для этого понадобилось починить:

* `downloads.py` — постоянная папка `bu_mcp/downloads` (`BU_MCP_DOWNLOAD_DIR`)
  вместо временного каталога со случайным именем. Путь ставится и серверу, и
  CLI повтора: без `downloads_path` в профиле CLI повтор скачивал в никуда;
* `checkpoint(download=true|".png")` — ожидание файла. Точка отсчёта берётся
  **перед** действием (`downloads.mark_baseline()`), иначе загрузка успевает
  закончиться раньше проверки и снимок «на момент проверки» показывает новый
  файл как лежавший всегда;
* чекпоинт читает подписи элементов (`aria-label`/`alt`/`title`), а не только
  текст: на ChatGPT «файл приложен» выражено только подписью, в `innerText`
  этого нет;
* `resolve.py` — ступень `similar_name` в конце лестницы: имя совпало на 80% по
  словам, фразы от четырёх слов, единственный кандидат. Нужна там, где имя
  элемента порождено содержимым и каждый прогон другое (описание
  сгенерированной картинки — на нём падал шаг 8). Первый порог был мягче и
  сломал 5 проверок smoke; затянут до 0.8 / четырёх слов, после чего smoke
  чистый. Обычное переименование контрола («macro button» → «macro button v2»)
  под ступень не подпадает и по-прежнему валит повтор.

Headless для ChatGPT закрыт: Cloudflare не пускает, страница виснет на
«Just a moment…». Поэтому окно есть, и отсюда вся возня с фокусом ниже.

### Фокус: три подхода, третий выключен

Жалоба была простая — машину альт-табает во время прогона. Разбор по шагам:

1. `preserve_frontmost` возвращала фокус «предыдущему приложению», а если
   впереди уже был наш Chrome, отдавала фокус ему же, и окно залипало впереди.
   Чиним: помним последнее **чужое** фронтовое приложение;
2. окно Chrome выходит вперёд не в момент CDP-вызова, а спустя мгновение после
   него — обёртка успевала «вернуть» фокус до кражи и считала, что всё хорошо.
   Замерено: окно выходило вперёд через ~10 с после старта и держало фокус 8
   секунд. Чиним: три догоняющие проверки (0.4/0.7/1.2 с) фоном;
3. догоняющие проверки всё равно пропускали кражи на 7 и 2 секунды во время
   smoke. Чиним: `start_focus_guard()` — один процесс `osascript` на всю
   сессию. Крадёт ноль раз за прогон.

И третий пришлось выключить по умолчанию: он ломает `Page.captureScreenshot`.
Chrome рисует кадры только для видимого окна, сторож не даёт окну быть впереди,
снимок висит до таймаута в 60 с. Пауза сторожа на время снимка (SIGSTOP/SIGCONT,
`paused_focus_guard`) не помогла — проверено прогоном, smoke стал хуже (94/12).
`fromSurface=false` Chrome отклоняет: «Unable to capture screenshot». Итог:
`BU_FOCUS_GUARD=1` включает сторожа вручную, по умолчанию остаётся обёртка.

Поллинг фокуса на стороне Python пробовать не нужно: запуск `osascript` стоит
~150 мс, то есть проверка медленнее самой кражи. Потому сторож и сделан одним
долгоживущим циклом внутри AppleScript.

### Отказ без живого CDP

`server.py` проверяет `/json/version` перед созданием сессии. Без этого
browser-use молча поднимает **свой** Chromium из кеша playwright, и агент
работает не в том браузере, думая, что всё в порядке.

### Замок профиля Chrome

Диалог «Something went wrong when opening your profile» — это протухший
`SingletonLock` от мёртвого процесса (в нашем случае pid 11539) плюс SIGKILL во
время записи профиля. `scripts/chrome-automation.sh`: `clean_stale_lock()`
убирает `SingletonLock`/`SingletonCookie`/`SingletonSocket`, если владелец
мёртв, а отсрочка на выход поднята с 5 до 15 с. После чистого старта диалога
нет.

Профиль автоматизации оставлен один — `~/chrome-automation` (`default`,
порт 9222), авторизация в ChatGPT в нём сохранена; профили `-personal` и
`-work` удалены.

## 2026-09-11 — правки по итогам ревью пятью агентами

Независимое ревью всего слоя. Восемь подтверждённых дефектов:

* `macro._opaque_segment`: сегмент пути считался id-подобным слишком охотно, и
  слаг `iphone-15-pro-max` совпадал с `samsung-galaxy-s23-ultra` — повтор шёл по
  чужой странице. Теперь нужен машинный признак: смешанный регистр или доля
  цифр ≥ 0.3. Проверено: слаги дают «different», uuid/nanoid по-прежнему
  «template»;
* `macro.auto_start` на том же шаблоне пути с другим id молчал — теперь пишет
  расхождение в отчёт;
* `journal`: склейка вводов в одно поле шла попарно, а нужна по всей цепочке
  (три ввода схлопываются в один);
* `journal.mark('stop', <чужое имя>)` молча закрывал открытую запись — теперь
  ставит `mismatch`, сервер отвечает ошибкой;
* `server`: `Runtime.releaseObject` после пробы `files.length` — утекал
  objectId; allowlist вложений считается только для инструментов, которым он
  нужен; неподтверждённая загрузка помечается `verified=false`, а не считается
  успехом (было fail-open);
* `session_manager`: эмуляция фокуса в своём `try` (её сбой не отменял
  мониторинг) и снимается при отключении — чужой браузер остаётся как был;
* `macro` CLI: битый JSON макроса — внятная ошибка, не traceback;
* `smoke`: проверка уборки вкладок смотрит на утечку, а не на факт открытия.

## Открытая проблема

**Полный smoke не доходит до конца на этой машине.** `browser_screenshot`
упирается в 60-секундный таймаут `Page.captureScreenshot`, прогон падает на
`[10b]`: `browser_state` возвращает отказ «CDP недоступен». Автоматизационный
Chrome заклинивает посреди прогона — процесс жив, порт 9222 слушает,
`/json/version` молчит, краш-репорта нет. Тот же smoke дважды проходил 165/0 в
тот же день до правок фокуса. Причина не найдена.
