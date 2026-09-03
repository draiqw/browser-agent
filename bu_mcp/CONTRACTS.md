# Контракты модулей

Общее: венв `~/browser-use/.venv/bin/python`, browser-use установлен editable из `~/browser-use`.
Chrome для автоматизации поднят headless на `http://127.0.0.1:9222`
(если не отвечает — `~/bu-lab/chrome-automation.sh`).
Репозиторий `~/browser-use` — только чтение, не менять.

## bu_mcp/state.py
    async def serialize_state(session, *, max_chars: int = 40000) -> dict

`session` — живой `browser_use.browser.BrowserSession`.
Возвращает dict: `url`, `title`, `tabs`, `viewport`, `scroll`, `tree` (str), `truncated` (bool).
`tree` — компактное текстовое дерево, НЕ плоский JSON.

## bu_mcp/waiting.py
    async def wait_for_page_ready(session, *, timeout: float = 8.0) -> dict
    async def navigation_baseline(session) -> dict
    async def wait_after_navigation(session, *, timeout: float = 10.0, baseline: dict | None = None) -> dict

Лестница fail-open. `wait_for_page_ready` возвращает
`{'ready': bool, 'stages': [...], 'elapsed': float}`.
Ни одна стадия не бросает исключение наружу.

`navigation_baseline` — снимок `{'target_id', 'loader_id', 'url'}` ТЕКУЩЕГО документа,
снимать ДО действия и передавать в `wait_after_navigation(baseline=...)`. Без него
baseline снимается уже после действия, то есть с нового документа, и смены `loaderId`
не видно вовсе. `wait_after_navigation` возвращает то же плюс `navigated: bool`, `url`.

## bu_mcp/server.py (контракт по проводу)

`browser_navigate` -> один текстовый блок, компактный JSON:
`{'action', 'url', 'waiting': {'ready', 'navigated', 'hydrated', 'elapsed', 'stages': [...]}}`.
Стадии: `navigation_start`, `lifecycle_load`, затем `hydration.*` — отдельная стадия
«дать SPA дорисоваться» со своим бюджетом (`hydrate`, по умолчанию 3 с, `0` выключает).
`ready` = сошлись ВСЕ стадии.

`browser_state` -> ДВА текстовых блока:

1. однострочный компактный JSON-заголовок:
   `url`, `title`, `elements` (int), `viewport` (`"800x479"`), `scroll` (`"x,y"`),
   `truncated`, `tabs` (`[{'tab_id', 'url', 'current'?}]`, `tab_id` — последние 4 символа
   target_id, ровно то, что принимают `switch`/`close`), плюс `page` (если отличается от
   вьюпорта) и `href_map` (если не пуст);
2. само дерево, СЫРЫМ текстом.

Дерево уехало из JSON-поля намеренно: внутри строки каждый `\n` и `\t` стоил два
символа. Клиент, который парсит ответ как один JSON, сломается — блоков два.

`browser_click` -> один текстовый блок, компактный JSON:
`{'action', 'requested_index', 'resolved_index', 'resolution'?, 'tab', 'url', 'waiting', 'delta'}`.
`tab` = `{'focus_before', 'focus_after', 'opened': [...], 'switched', 'claim', 'closed'?, 'claimed'?}` —
сверка апстримного рапорта об авто-переключении на новую вкладку с фактическим
`agent_focus_target_id` (issue #5529). `claim` ∈ `verified | false | mismatch | silent-open |
silent-switch | upstream-note | none`.

`browser_type` -> `{'action', 'requested_index', 'resolved_index', 'resolution'?, 'url', 'waiting', 'delta'}`.

`browser_hover` -> `{'action', 'requested_index', 'resolved_index', 'resolution'?, 'point' ("x,y"),
'rect', 'viewport', 'hit': {'hit', 'self'}, 'url', 'waiting', 'delta', 'clipped'?}`.
Физическое `Input.dispatchMouseEvent(mouseMoved)` в точку внутри элемента — единственный
способ включить CSS `:hover` (действия `hover` в реестре browser-use нет, issue #4964;
синтетический `MouseEvent` из `evaluate` не двигает курсор и `:hover` не активирует).
`hit.self` — попала ли точка в сам элемент (или его потомка/предка), то есть не перекрыт ли он.
Точку вне вьюпорта НЕ зажимаем во вьюпорт (в отличие от апстримного клика) — это `isError`.

`browser_screenshot` -> компактный JSON первым блоком + `ImageContent` вторым.

`scroll` -> компактный JSON (НЕ голый текст):
`{'upstream_report', 'action', 'scrolled': bool|None, 'status': 'scrolled'|'at-end'|'unverified',
'scope': 'page'|'element', 'delta': {'y','x'}, 'position': {'y','x','max_y','max_x'}, 'at_end'?}`.
Прокрутка не сдвинулась при наличии запаса -> `isError`.

`switch` -> компактный JSON: `{'action', 'upstream_report', 'tab': {'focus_before','focus_after'}, 'url'}`.
Фактический фокус не совпал с запрошенным `tab_id` -> `isError`.

`select_dropdown` / `send_keys` -> компактный JSON `{'action', 'url', 'delta'}`
(схема входа остаётся реестровой, меняется только конверт ответа).

### `delta` — расписка о последствиях (issues #5137, #4758)

Есть у всех действий, меняющих состояние: `browser_click`, `browser_type`, `browser_hover`,
`select_dropdown`, `send_keys`.

    {'changed': bool|None, 'status': 'changed'|'no-change'|'unavailable',
     'cost_ms': float, 'probes': int, 'settle_ms'?: int,
     'fields'?: {'<признак>': [до, после]},  # 'digest' печатается как строка 'changed'
     'truncated'?: true, 'no_effect'?: true, 'note'?: str}

Признаки снимаются одним `Runtime.evaluate` до и одним после: `url`, `title`, `nodes`,
`rendered`, `interactive`, `doc` (`"HxW"`), `dialogs`, `digest`, плюс `tabs` из списка целей и
информационные `scroll`, `active`. На вердикт `changed` НЕ влияют `scroll` и `active`: оба
меняются от самой механики действия (`scrollIntoViewIfNeeded` перед кликом, фокус после
`mousePressed`), а не от реакции страницы. Исключение — `send_keys`, где перевод фокуса и
есть весь результат.

Дельта НИКОГДА не поднимает ошибку: «ничего не изменилось» — законный исход клика по
неактивной кнопке. Но если действие рапортует успех при пустой дельте, ставится
`no_effect: true` + `note`. Пустая дельта дополнительно перепроверяется до двух раз с паузой
120 мс (`settle_ms`) — эскалация включается только на этой ветке.

Замеренная цена (headless Chrome, 2026-09-03): `cost_ms` 2.2 мс на example.com, 5.0 мс на
arxiv.org, 21 мс на en.wikipedia.org/wiki/Main_Page (844 интерактивных элемента); в ответе
167–208 символов, когда дельта непустая, и 455 символов вместе с `note`, когда сработал
`no_effect`. Для сравнения: один `browser_state` на той же википедии — 40 307 символов.

### Профиль браузера: headless и вьюпорт

`BrowserProfile` строится в `BuMcpServer._profile()` с ЯВНЫМ `headless`
(`BU_MCP_HEADLESS`, по умолчанию `1`, значения `0/false/no/off` дают окно).

Важно, чем это НЕ является. Мы всегда подключаемся к уже работающему Chrome по
`cdp_url`, а на этом пути `headless` не влияет ни на что: режим окна определился
при запуске браузера, и `on_BrowserStartEvent` до ветки запуска не доходит вовсе
(`if not self.cdp_url`). Считать это защитой от выскакивающего окна нельзя.
Значение имеет ровно один путь — если `BU_MCP_CDP_URL` пуст и browser-use
поднимает браузер сам: там без явного флага `headless` остаётся `None`, и
`detect_display_configuration` выводит его из наличия дисплея (на машине с
экраном — окно). `~/.config/browseruse/config.json` наш путь не читает вообще:
`BrowserProfile` конструируется напрямую, а `load_browser_use_config` живёт в
CLI и штатном MCP-сервере.

Побочка, которую пришлось снимать. `headless=True` в валидаторе тянет
`viewport = screen`, `no_viewport = False`, а с ними browser-use шлёт
`Emulation.setDeviceMetricsOverride` на каждую вкладку, которую создаёт ИЛИ на
которую переводит фокус (`on_TabCreatedEvent` / `on_AgentFocusChangedEvent`) —
включая ЧУЖУЮ, на которую он переключается сам, когда наша отсоединяется.
Поэтому при подключении (`cdp_url` задан) геометрия возвращается к
`viewport=None`, `no_viewport=True`: чужой вьюпорт мы не трогаем. Для ветки
запуска viewport остаётся штатным.

### Журнал действий и макросы (JOURNAL_CONTRACT.md)

Каждое действие, МЕНЯЮЩЕЕ состояние, пишется в журнал: `browser_click`,
`browser_type`, `browser_hover`, `browser_navigate`, `select_dropdown`,
`send_keys`, `scroll` (константа `JOURNALED_TOOLS`). Наблюдения не пишутся —
их всё равно выкидывает `to_macro`.

Запись (`journal.record`) идёт в `finally`, поэтому фиксируется ЛЮБОЙ исход,
включая отказ и отмену. Поля — `JOURNAL_FIELDS`: `ts`, `tool`, `params`,
`handle`, `url_before`, `url_after`, `delta`, `outcome`, `error`; плюс наши
`cost_ms`, `resolved_index`, `handle_error`. `outcome` ∈ `ok | noop | error`;
`noop` ставится и на `NOOP_MARKERS`, и на `delta.no_effect`.

Журнал — наблюдатель, а не участник: отсутствующий модуль и упавший
`journal.record` гасятся в лог и НЕ ломают действие (проверено тестом).

Цена. `handle` — полный `resolve.describe_handle` по УЖЕ разрешённому индексу,
снятый ДО действия; `url_before` берётся из уже снятой пробы дельты (для
`browser_navigate` — из `navigation_baseline`, для `scroll` — из
`get_current_page_url`, который читается из session_manager, медиана 0.000 мс).
Замерено на headless Chrome 2026-09-03: `describe_handle` 0.78 / 0.94 / 2.94 мс
(min/med/max), дозапись строки JSONL 0.040 / 0.063 / 0.395 мс, запись целиком
~587 байт. Сквозным прогоном: медиана `cost_ms` 0.90 мс против медианы
`delta.cost_ms` 1.80 мс на тех же вызовах — журнал дешевле дельты, к которой
пристёгнут.

`journal_list` -> компактный JSON:
`{'action', 'path', 'total', 'returned', 'entries': [...]}`.
У каждой записи есть АБСОЛЮТНАЯ позиция `i` — её и принимает
`macro_save(include=[...])`. По умолчанию строки — выжимка
(`ts`, `tool`, `params`, `outcome`, `url`, короткий `handle`, `delta.status`,
`cost_ms`); `full=true` отдаёт записи дословно.

`macro_save` -> `{'action', 'name', 'file', 'steps' (int), 'tools': [...], 'vars': {...}}`.
Схлопывает выбранные записи через `journal.to_macro` и кладёт файл через
`journal.save_macro` (путь считает `journal.home()`, а не сервер). Имя
проверяется белым списком `MACRO_NAME_RE` — не подошло, значит `ToolError`, а
не тихая замена символов: имя едет в путь файла. Макрос без шагов — тоже
`ToolError`, а не пустой успех.

`macro_list` -> без имени `{'action', 'dir', 'macros': [{'name','file','steps','tools','vars'}]}`;
с именем — `{'action', 'name', 'file', 'macro': {...}}` целиком.

`macro_run` -> `{'action', 'name', 'strict', 'file', ...выхлоп macro.run}`
(`ok`, `steps`, `failed_at`, `vars`, `discrepancies`, ...).
`strict` по умолчанию `true`. **`ok == False` — это `ToolError` в ОБОИХ режимах**:
`strict` решает только, останавливаться ли на первом расхождении или дойти до
конца, собрав их все, и НЕ влияет на видимость провала. Полный отчёт едет
внутри текста ошибки. Перед первым шагом макрос дополнительно гейтится по
allowlist по всем URL, зашитым в его шаги (`_macro_urls`): шаги исполняет
`macro.py` напрямую, мимо `_check_domain_gate`, и без этой проверки сохранённый
`navigate` был бы дырой в доменной политике.

## bu_mcp/resolve.py
    class StaleHandleError(Exception): ...
    async def resolve_index(session, index: int)

Возвращает узел или бросает `StaleHandleError` с человекочитаемым текстом.
