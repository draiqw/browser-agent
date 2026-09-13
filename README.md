# browser-agent — доработанная версия browser-use

Это форк [browser-use/browser-use](https://github.com/browser-use/browser-use) (MIT,
© Gregor Zunic) со своим MCP-сервером, `browser_use/mcp/server.py` (класс
`BuMcpServer`), который напрямую ЗАМЕНЯЕТ штатный `BrowserUseServer` апстрима —
не живёт рядом, а физически лежит на его месте, вместе с подмодулями
(`domain_gate.py`, `cdp_session.py`, `registry_bridge.py`, `noop.py`, `delta.py`,
`journal.py`, `macro.py`, `state.py`, `resolve.py`, `waiting.py`, `downloads.py`,
`uploads.py`, `actions/`). Это значит, что `browser_use/mcp/` при обновлении
апстрима будет конфликтовать почти гарантированно — в отличие от `bu_eval/` и
`scripts/`, которые снаружи и не патчены. Плюс ещё 5 файлов библиотеки точечно
тронуты отдельно (`browser_use/actor/page.py`, `browser_use/browser/profile.py`,
`session.py`, `session_manager.py`, `watchdogs/screenshot_watchdog.py`) — фиксы
фокуса и скриншотов для фоновых вкладок со скрытым окном, подробности в
[`docs/WORKLOG.md`](docs/WORKLOG.md).

**Зачем.** Штатный MCP-сервер отдаёт клиенту 5 страничных примитивов и плоский JSON
состояния. Разбор кода и бенчмарк на 16 живых сайтах вскрыли ряд проблем, которые
этот слой и чинит.

| | оригинал | этот форк |
|---|---|---|
| инструментов наружу | 5 | **27** (13 своих + динамический мост к реестру `Tools()`) |
| формат состояния | плоский JSON `{index, tag, text}` | дерево с иерархией, `role`, `aria-*`, shadow-маркерами |
| стоимость наблюдения | базовая | **0.76x** по корпусу, при том же числе элементов |
| латентность состояния | базовая | **0.52x** — не снимает и не выбрасывает скриншот |
| действие по мёртвому хендлу | «Clicked element N», ложный успех **16/16** | переидентификация либо жёсткий отказ |
| прочие ответы-нооп | успех без `error` в 6 действиях | 8 путей закрыты контрактной проверкой |
| скролл | «Scrolled 479px» независимо от результата | проверка по факту `scrollY` |
| ожидание навигации | отсутствует | лестница готовности + `loaderId`, 48/48 распознано |
| `hover` | действия нет в реестре | `browser_hover` через движение мыши CDP |
| последствия действия | не сообщаются | дельта URL/вкладок/элементов, флаг `no_effect` |
| `data:`/`blob:`/`file:` мимо allowlist | проходят ([#4763](https://github.com/browser-use/browser-use/issues/4763), [#5099](https://github.com/browser-use/browser-use/issues/5099)) | блокируются, deny by default |
| повторяемые сценарии | нет | журнал действий + `macro_record`/`macro_run`: обучение один раз, повтор без модели, чинится с шага N |
| загрузка файлов | нет | `upload_file` из папки вложений, allowlist по инструментам |
| фоновая вкладка (окно скрыто) | не принимает ввод | сторож фокуса эмуляцией, ввод проходит |

Общая карта всего форка (что где лежит и зачем) — [`docs/OVERVIEW.md`](docs/OVERVIEW.md).
Подробности, замеры и известные ограничения MCP-слоя — [`docs/BU_MCP.md`](docs/BU_MCP.md).
Методика и полные результаты бенчмарка — [`docs/BENCH.md`](docs/BENCH.md),
сырые данные в `browser_use/mcp/bench_results.json`. Что менялось и почему, с
проверками и тупиками — [`docs/WORKLOG.md`](docs/WORKLOG.md).

```bash
scripts/chrome-automation.sh          # Chrome с CDP на 9222, окно скрыто
scripts/chrome-automation.sh login    # показать окно, чтобы залогиниться руками
scripts/chrome-automation.sh status   # что работает и в каком режиме
scripts/chrome-automation.sh list     # все профили: порт, состояние, каталог
browser-use --mcp                                    # то же самое, штатной командой CLI
PYTHONPATH=. python -m browser_use.mcp.server         # MCP-сервер напрямую, транспорт stdio
PYTHONPATH=. python browser_use/mcp/smoke.py          # проверки на живом браузере
PYTHONPATH=. python -m browser_use.mcp.macro run ИМЯ  # повтор макроса без модели
uv run python examples/mcp/connect_and_browse.py       # минимальный свой MCP-клиент, для примера
```

Полный список подкоманд `chrome-automation.sh` (`show`/`hide`/`install`/`stop`)
и деталей запуска — в [`docs/BU_MCP.md`](docs/BU_MCP.md#запуск).

**Известная проблема.** На этой машине полный прогон `smoke.py` не всегда
доходит до конца: `browser_screenshot` может упереться в таймаут CDP, если
Chrome-автоматизация подвисает посреди прогона. Причина не найдена, см.
[`docs/WORKLOG.md`](docs/WORKLOG.md#открытая-проблема).

**Что НЕ проверено:** что агент с этим слоем решает реальные задачи лучше или
дешевле. Все измерения сделаны без модели в цикле и характеризуют сервер, а не
агента.

## Авторство и лицензия

Наш код (`browser_use/mcp/`, `bu_eval/`, `scripts/`, плюс точечные правки в
`browser_use/actor/`, `browser_use/browser/` — см. выше) — © 2026 Roman Akramov
([@draiqw](https://github.com/draiqw)), MIT.

Всё остальное — browser-use, © 2024 Gregor Zunic, MIT, и авторское уведомление
сохранено в [`LICENSE`](LICENSE) как того требует лицензия. Условия у обеих
частей одни и те же, так что практической разницы для пользователя нет —
разделение нужно, чтобы было видно, кто что писал.

Родственный проект — [computer-agent](https://github.com/draiqw/computer-agent):
та же идея, но работа делается файлами и шеллом, а не страницами.

Ниже — оригинальный README browser-use.

---

<!-- mcp-name: com.browser-use/browser-use -->
<picture>
  <source media="(prefers-color-scheme: light)" srcset="https://github.com/user-attachments/assets/2ccdb752-22fb-41c7-8948-857fc1ad7e24">
  <source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/774a46d5-27a0-490c-b7d0-e65fcbbfa358">
  <img alt="Shows a black Browser Use Logo in light color mode and a white one in dark color mode." src="https://github.com/user-attachments/assets/2ccdb752-22fb-41c7-8948-857fc1ad7e24"  width="full">
</picture>

<div align="center">
    <picture>
    <source media="(prefers-color-scheme: light)" srcset="https://github.com/user-attachments/assets/9955dda9-ede3-4971-8ee0-91cbc3850125">
    <source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/6797d09b-8ac3-4cb9-ba07-b289e080765a">
    <img alt="The AI browser agent." src="https://github.com/user-attachments/assets/9955dda9-ede3-4971-8ee0-91cbc3850125"  width="400">
    </picture>
</div>

<div align="center">
<a href="https://cloud.browser-use.com?utm_source=github&utm_medium=readme-badge-downloads"><img src="https://media.browser-use.tools/badges/package" height="48" alt="Browser-Use Package Download Statistics"></a>
</div>

---

<div align="center">
<a href="#what-can-browser-use-do"><img src="https://media.browser-use.tools/badges/demos" alt="Demos"></a>
<img width="16" height="1" alt="">
<a href="https://docs.browser-use.com"><img src="https://media.browser-use.tools/badges/docs" alt="Docs"></a>
<img width="16" height="1" alt="">
<a href="https://browser-use.com/posts"><img src="https://media.browser-use.tools/badges/blog" alt="Blog"></a>
<img width="16" height="1" alt="">
<a href="https://browsermerch.com"><img src="https://media.browser-use.tools/badges/merch" alt="Merch"></a>
<img width="100" height="1" alt="">
<a href="https://github.com/browser-use/browser-use"><img src="https://media.browser-use.tools/badges/github" alt="Github Stars"></a>
<img width="4" height="1" alt="">
<a href="https://x.com/intent/user?screen_name=browser_use"><img src="https://media.browser-use.tools/badges/twitter" alt="Twitter"></a>
<img width="4" height="1" alt="">
<a href="https://link.browser-use.com/discord"><img src="https://media.browser-use.tools/badges/discord" alt="Discord"></a>
<img width="4" height="1" alt="">
<a href="https://cloud.browser-use.com?utm_source=github&utm_medium=readme-badge-cloud"><img src="https://media.browser-use.tools/badges/cloud" height="48" alt="Browser-Use Cloud"></a>
</div>

</br>

# What can Browser Use do?

Browser Use lets an AI agent use a web browser the same way humans do — it opens pages, clicks buttons, types, and fills in forms. You describe the task, and it completes it. For example, you can have it:


### 📋 Fill Forms
#### Task: "Fill in this job application with my resume and information."

![Job Application Demo](https://github.com/user-attachments/assets/57611d8e-0474-4de6-84b7-37a0c0cd27e7)

[Example code ↗](https://github.com/browser-use/browser-use/blob/main/examples/use-cases/apply_to_job.py)


### 🍎 Extract data
#### Task: "Extract structured data about my followers and export it as a CSV."

https://github.com/user-attachments/assets/485fd3ec-61b9-4afc-9e86-ee9b85acb592

[Browser Use Cloud Docs ↗](https://docs.browser-use.com/cloud/quickstart)


<br/>

# Open Source vs Cloud

We benchmark Browser Use across 100 real-world browser tasks. Full benchmark is open source: **[browser-use/benchmark](https://github.com/browser-use/benchmark)**.

Browser Use is also **#1 on the [Odysseys leaderboard](https://odysseysbench.com/leaderboard)** with an 87.4% average, ahead of computer-use agents from OpenAI, Anthropic, Google, and Microsoft. Odysseys measures the agent's performance on 200 long-horizon web tasks.

**Use the Open-Source Agent**
- Free, and runs on your own machine
- Deep code-level integration and control: pick your LLM, customize the agent's behavior
- We recommend pairing it with our [cloud browsers](https://docs.browser-use.com/open-source/customize/browser/remote) for leading stealth, proxy rotation, and scaling

**Use the [Fully-Hosted Cloud Agent](https://cloud.browser-use.com?utm_source=github&utm_medium=readme-hosted-agent) (recommended)**
- Much more powerful agent for complex tasks (see plot above)
- Easiest way to start and scale
- Best stealth with proxy rotation and captcha solving
- 1000+ integrations (Gmail, Slack, Notion, and more)
- Persistent filesystem and memory
- Rerunnable scripts fetch live data, even when sites change ([guide](https://docs.browser-use.com/cloud/agent/scripts))

```sh
curl -X POST https://api.browser-use.com/api/v4/runs \
  -H "X-Browser-Use-API-Key: $BROWSER_USE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"task": "Your task"}'
```

<br/>

<br/>

# FAQ

<details>
<summary><b>What's the best model to use?</b></summary>

We optimized **ChatBrowserUse()** specifically for browser automation tasks. On avg it completes tasks 3-5x faster than other models with SOTA accuracy.

For pricing and other LLM providers, see our [supported models documentation](https://docs.browser-use.com/supported-models).
</details>

<details>
<summary><b>Can I use Claude / GPT / Gemini through ChatBrowserUse?</b></summary>

Yes. `ChatBrowserUse` accepts provider-prefixed model ids, so a single `BROWSER_USE_API_KEY` reaches all of them — no separate OpenAI/Anthropic/Google keys required:

```python
from browser_use import Agent, ChatBrowserUse

llm = ChatBrowserUse(model='anthropic/claude-sonnet-4-6')  # or 'openai/gpt-5.5', 'google/gemini-3-pro'
agent = Agent(task='...', llm=llm)
```

For the best speed and cost we still recommend the default `bu-*` models.
</details>

<details>
<summary><b>Should I use the Browser Use system prompt with the open-source preview model?</b></summary>

Yes. If you use `ChatBrowserUse(model='browser-use/bu-30b-a3b-preview')` with a normal `Agent(...)`, Browser Use still sends its default agent system prompt for you.

You do **not** need to add a separate custom "Browser Use system message" just because you switched to the open-source preview model. Only use `extend_system_message` or `override_system_message` when you intentionally want to customize the default behavior for your task.

If you want the best default speed/accuracy, we still recommend the newer hosted `bu-*` models. If you want the open-source preview model, the setup stays the same apart from the `model=` value.
</details>

<details>
<summary><b>Can I use custom tools with the agent?</b></summary>

Yes! You can add custom tools to extend the agent's capabilities:

```python
from browser_use import Tools

tools = Tools()

@tools.action(description='Description of what this tool does.')
def custom_tool(param: str) -> str:
    return f"Result: {param}"

agent = Agent(
    task="Your task",
    llm=llm,
    browser=browser,
    tools=tools,
)
```

</details>

<details>
<summary><b>Can I use this for free?</b></summary>

Yes! Browser-Use is open source and free to use. You only need to choose an LLM provider (like OpenAI, Google, ChatBrowserUse, or run local models with Ollama).
</details>

<details>
<summary><b>Terms of Service</b></summary>

This open-source library is licensed under the MIT License. For Browser Use services & data policy, see our [Terms of Service](https://browser-use.com/legal/terms-of-service) and [Privacy Policy](https://browser-use.com/privacy/).
</details>

<details>
<summary><b>How do I handle authentication?</b></summary>

Check out our authentication examples:
- [Using real browser profiles](https://github.com/browser-use/browser-use/blob/main/examples/browser/real_browser.py) - Reuse your existing Chrome profile with saved logins
- If you want to use temporary accounts with inbox, choose AgentMail
- To sync your auth profile with a remote browser, install `profile-use` for your platform from the [official releases](https://github.com/browser-use/profile-use-releases/releases/latest), then follow the [profile sync guide](https://github.com/browser-use/browser-harness/blob/main/interaction-skills/profile-sync.md).

These examples show how to maintain sessions and handle authentication seamlessly.
</details>

<details>
<summary><b>How do I solve CAPTCHAs?</b></summary>

For CAPTCHA handling, you need better browser fingerprinting and proxies. Use [Browser Use Cloud](https://cloud.browser-use.com?utm_source=github&utm_medium=readme-faq-captcha) which provides stealth browsers designed to avoid detection and CAPTCHA challenges.
</details>

<details>
<summary><b>How do I go into production?</b></summary>

Chrome can consume a lot of memory, and running many agents in parallel can be tricky to manage.

For production use cases, use our [Browser Use Cloud API](https://cloud.browser-use.com?utm_source=github&utm_medium=readme-faq-production) which handles:
- Scalable browser infrastructure
- Memory management
- Proxy rotation
- Stealth browser fingerprinting
- High-performance parallel execution
</details>

<br/>

## Citation

If you use Browser Use in your research or project, please cite:

```bibtex
@software{browser_use2024,
  author = {Müller, Magnus and Žunič, Gregor},
  title = {Browser Use: Enable AI to control your browser},
  year = {2024},
  publisher = {GitHub},
  url = {https://github.com/browser-use/browser-use}
}
```

<br/>

<div align="center">

**Tell your computer what to do, and it gets it done.**

<img src="https://github.com/user-attachments/assets/06fa3078-8461-4560-b434-445510c1766f" width="400"/>

[![Twitter Follow](https://img.shields.io/twitter/follow/Magnus?style=social)](https://x.com/intent/user?screen_name=mamagnus00)
&emsp;&emsp;&emsp;
[![Twitter Follow](https://img.shields.io/twitter/follow/Gregor?style=social)](https://x.com/intent/user?screen_name=gregpr07)

</div>

<div align="center"> Made with ❤️ in Zurich and San Francisco </div>
