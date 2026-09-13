# Оценка агента с LLM в цикле (benchmark)

Это не про `examples/` в смысле питоновских сниппетов для копирования — `benchmark/`
уже сам по себе CLI, дублировать его логику здесь смысла нет. Он гоняет
конкретные задачи через разные бэкенды (`bu-mcp` — наш MCP-слой, `browser-use` —
стоковый `Agent`) и разные LLM, считает цену и результат.

**Прогоны стоят денег** — нужен ключ провайдера в `.env`. Матрица печатает свой
размер до старта; `--dry-run` показывает ячейки и не тратит ни цента.

```bash
python -m benchmark doctor      # состояние допущений об апстриме и bu_mcp
python -m benchmark selftest    # проверить сам харнесс, без LLM и без денег
python -m benchmark models      # какие провайдеры готовы к работе
python -m benchmark tasks       # список задач, профилей и бэкендов
python -m benchmark run -t clickgate -m openai:gpt-5-mini -b bu-mcp
python -m benchmark run -t clickgate -m openai:gpt-5-mini -b bu-mcp -b browser-use -r 3
```

Браузер нужен уже работающий и headless: `scripts/chrome-automation.sh` без флагов.

Задачи — в `benchmark/tasks/` (`cbr.py`, `hn.py`, `clickgate.py`), профили и бэкенды —
в `benchmark/profiles.py` / `benchmark/backends.py`.
