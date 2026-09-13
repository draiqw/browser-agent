# Наш MCP-сервер (browser_use.mcp.server)

Это не про штатный `browser-use --mcp` из апстрима — в этом форке он заменён
собственным сервером, `BuMcpServer` (`browser_use/mcp/server.py`). Разница с тем,
что было раньше: 27 инструментов вместо 5, дерево DOM вместо плоского JSON,
честные отчёты о провале вместо молчаливого успеха, журнал действий и макросы.
Подробности и таблица сравнения — [`../../docs/BU_MCP.md`](../../docs/BU_MCP.md).

`connect_and_browse.py` показывает минимальный MCP-клиент на Python: без Claude
Code, без какого-либо агента с LLM в цикле — просто протокол (`list_tools`,
`call_tool`). Обычно этот сервер используют иначе: как MCP-tools у внешнего
агента (Claude Code, Codex и т.п.), а не через самописный клиент — но именно
поэтому полезно один раз увидеть, что происходит под капотом.

## Запуск

Нужен уже поднятый Chrome с открытым CDP:

```bash
scripts/chrome-automation.sh
uv run python examples/mcp/connect_and_browse.py
```

## Что ещё посмотреть

- `python -m browser_use.mcp.server` — сам сервер, транспорт stdio (это и есть
  то, что запускает `connect_and_browse.py` подпроцессом).
- `python browser_use/mcp/smoke.py` — куда более полный прогон (165+ проверок,
  включая контрактные тесты без браузера) — по сути образец того, как писать
  клиента, если нужно не демо, а реальная проверка поведения.
- `python -m browser_use.mcp.macro run ИМЯ` — повтор записанного макроса без
  модели в цикле; как его записать — [`../../docs/JOURNAL_CONTRACT.md`](../../docs/JOURNAL_CONTRACT.md).
