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

Лестница fail-open. Возвращает `{'ready': bool, 'stages': [...], 'elapsed': float}`.
Ни одна стадия не бросает исключение наружу.

## bu_mcp/resolve.py
    class StaleHandleError(Exception): ...
    async def resolve_index(session, index: int)

Возвращает узел или бросает `StaleHandleError` с человекочитаемым текстом.
