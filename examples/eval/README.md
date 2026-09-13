# Оценка агента с LLM в цикле (bu_eval)

Это не про `examples/` в смысле питоновских сниппетов для копирования — `bu_eval/`
уже сам по себе CLI, дублировать его логику здесь смысла нет. Он гоняет
конкретные задачи через разные бэкенды (`bu-mcp` — наш MCP-слой, `browser-use` —
стоковый `Agent`) и разные LLM, считает цену и результат.

**Прогоны стоят денег** — нужен ключ провайдера в `.env`. Матрица печатает свой
размер до старта; `--dry-run` показывает ячейки и не тратит ни цента.

```bash
python -m bu_eval doctor      # состояние допущений об апстриме и bu_mcp
python -m bu_eval selftest    # проверить сам харнесс, без LLM и без денег
python -m bu_eval models      # какие провайдеры готовы к работе
python -m bu_eval tasks       # список задач, профилей и бэкендов
python -m bu_eval run -t clickgate -m openai:gpt-5-mini -b bu-mcp
python -m bu_eval run -t clickgate -m openai:gpt-5-mini -b bu-mcp -b browser-use -r 3
```

Браузер нужен уже работающий и headless: `scripts/chrome-automation.sh` без флагов.

Задачи — в `bu_eval/tasks/` (`cbr.py`, `hn.py`, `clickgate.py`), профили и бэкенды —
в `bu_eval/profiles.py` / `bu_eval/backends.py`.
