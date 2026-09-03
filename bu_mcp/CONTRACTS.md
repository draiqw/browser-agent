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

`browser_click` / `browser_type` / `browser_screenshot` — тоже компактный JSON.

## bu_mcp/resolve.py
    class StaleHandleError(Exception): ...
    async def resolve_index(session, index: int)

Возвращает узел или бросает `StaleHandleError` с человекочитаемым текстом.
