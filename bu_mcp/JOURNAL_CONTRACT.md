# Журнал действий и макросы — контракт

Цель: каждое действие пишется в журнал так, чтобы из него можно было собрать
повторяемый сценарий, выполняемый **без модели в цикле**.

Опора: `resolve.py` уже умеет переидентификацию (backendNodeId → xpath →
accessible name → уникальный атрибут) с гардом по смене URL и по несовпадению
accessible name, и `describe_handle` возвращает составной хендл, который
принимается обратно как `hint`. Повтор строится на этом, а не на индексах.

Хранилище: `~/.config/bu-mcp/journals/` (JSONL, по файлу на сессию),
макросы — `~/.config/bu-mcp/macros/<name>.json`.

## bu_mcp/journal.py

    def record(entry: dict) -> None
    def current_path() -> Path
    def read(path: Path | None = None, *, limit: int | None = None) -> list[dict]
    def to_macro(entries: list[dict], *, name: str) -> dict

Запись на каждое действие, меняющее состояние: `ts`, `tool`, `params`,
`handle` (полный выхлоп `describe_handle` на момент действия), `url_before`,
`url_after`, `delta`, `outcome` (`ok` / `noop` / `error`), `error`.
Запись не должна ломать действие: любая ошибка журнала гасится и логируется.

`to_macro` схлопывает журнал в сценарий: выкидывает наблюдения (`browser_state`,
`browser_screenshot`, `find_elements`), оставляет шаги, меняющие состояние,
и параметризует введённый текст в именованные переменные.

## bu_mcp/macro.py

    class StepFailed(Exception): ...
    async def run(session, macro: dict, *, vars: dict | None = None,
                  strict: bool = True) -> dict

Каждый шаг: резолв по сохранённому `hint` через `resolve_index`, затем действие,
затем сверка дельты с записанной. `strict=True` (по умолчанию) — любое
расхождение останавливает сценарий: fail closed, как и весь остальной слой.
`strict=False` — продолжать, копя список расхождений.

Возврат: `{'ok': bool, 'steps': [...], 'failed_at': int | None, 'vars': {...}}`.
