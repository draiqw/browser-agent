"""Бэкенд — то, чем крутится задача. Их три, и в этом весь смысл пакета.

* `browser-use` — ванильный апстримный `Agent`. Перенесён из browseruse-lab
  почти без правок и оставлен намеренно: это база сравнения. Он меряет апстрим,
  а не нас.
* `bu-mcp` — модель ходит в браузер ТОЛЬКО через наш MCP-сервер по stdio,
  ровно как это делал бы любой клиент. Цикл «модель ↔ инструменты» — в
  `bu_eval.loop`, транспорт — клиент из `bu_mcp.bench` (переиспользован, а не
  написан заново).
* `scripted` — та же труба, но вместо модели записанный сценарий задачи.
  Отвечает на вопрос «а решается ли задача через наш набор инструментов
  ВООБЩЕ», отделяя предел слоя от предела модели, и стоит ноль долларов.

Одни и те же задачи, одни и те же имена профилей, один и тот же отчёт. Разница
между строками — это и есть ответ на вопрос, ради которого пакет написан:
сколько шагов, сколько секунд, сколько долларов стоит слой.

Правила обращения с чужим Chrome (нарушать нельзя, ими уже терялись чужие
вкладки): свою вкладку сервер получает навигацией с `new_tab=True` на
уникальный маркер, её target_id запоминается, закрывается в конце ТОЛЬКО он.
Множество чужих вкладок снимается до старта и сверяется после.
"""

from __future__ import annotations

import asyncio
import os
import time
import urllib.request
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Protocol

from pydantic import BaseModel

from bu_eval.models import PROVIDERS, make_model, missing_key, parse_spec
from bu_eval.pricing import cost_of, is_free
from bu_eval.profiles import Profile
from bu_eval.task import Task

# Апстрим по умолчанию шлёт анонимную телеметрию. Для работы с финансовыми
# страницами это лишнее — выключаем, если пользователь явно не включил обратно.
os.environ.setdefault('ANONYMIZED_TELEMETRY', 'false')


@dataclass
class RunReport:
	task: str
	model: str
	profile: str
	backend: str
	ok: bool = False  # агент вернул данные по схеме
	verified: bool = False  # данные прошли проверку
	problems: list[str] = field(default_factory=list)
	attempts: int = 0
	steps: int = 0
	seconds: float = 0.0
	tok_in: int = 0  # только свежие входные: кэшированные учтены отдельно и стоят дешевле
	tok_cached: int = 0
	tok_out: int = 0
	cost: float | None = None
	actions: list[str] = field(default_factory=list)
	errors: list[str] = field(default_factory=list)
	summary: str = ''
	stopped: str = ''  # чем кончился цикл: done / max_steps / no_tool_call / error
	tools_offered: int = 0  # сколько инструментов отдал сервер
	tools_allowed: int = 0  # сколько из них оставил профиль
	data: BaseModel | None = None

	@property
	def tokens(self) -> int:
		return self.tok_in + self.tok_cached + self.tok_out

	def as_dict(self, with_data: bool = True) -> dict:
		d = asdict(self)
		d.pop('data', None)
		d['tokens'] = self.tokens
		if with_data and self.data is not None:
			d['data'] = self.data.model_dump(mode='json')
		return d

	def price(self, model: str) -> None:
		_, bare = parse_spec(model)
		self.cost = 0.0 if is_free(model) else cost_of(bare, self.tok_in, self.tok_cached, self.tok_out)


class Backend(Protocol):
	name: str

	async def run(self, task: Task, model: str, profile: Profile, max_steps: int, headless: bool) -> RunReport: ...


# --------------------------------------------------------------------------- #
# База сравнения: ванильный browser-use
# --------------------------------------------------------------------------- #


class BrowserUseBackend:
	"""Апстримный `Agent` со своим браузером. Наш слой здесь не участвует вовсе."""

	name = 'browser-use'

	async def run(self, task: Task, model: str, profile: Profile, max_steps: int, headless: bool = True) -> RunReport:
		from browser_use import Agent, Browser

		rep = RunReport(task=task.name, model=model, profile=profile.name, backend=self.name)
		started = time.time()
		browser = Browser(headless=headless)
		try:
			agent = Agent(
				task=task.prompt,
				llm=make_model(model),
				browser=browser,
				output_model_schema=task.schema,
				tools=profile.build_tools(),
				**profile.agent_kwargs(),
			)
			history = await agent.run(max_steps=max_steps)
			rep.steps = history.number_of_steps()
			rep.actions = [a for a in history.action_names() if a]
			rep.errors = [str(e) for e in history.errors() if e]
			if history.usage:
				u = history.usage
				# у апстрима total_prompt_tokens включает кэшированные — вычитаем,
				# иначе кэш будет посчитан по полной цене
				rep.tok_cached = u.total_prompt_cached_tokens
				rep.tok_in = u.total_prompt_tokens - rep.tok_cached
				rep.tok_out = u.total_completion_tokens
			rep.data = history.structured_output
			rep.ok = rep.data is not None
			rep.stopped = 'done' if rep.ok else 'max_steps'
		except Exception as exc:  # noqa: BLE001 — упавший прогон это строка отчёта, а не крах матрицы
			rep.errors.append(repr(exc))
			rep.stopped = 'error'
		finally:
			await browser.kill()

		rep.seconds = time.time() - started
		rep.price(model)
		return rep


# --------------------------------------------------------------------------- #
# Наш слой: модель через bu_mcp по stdio
# --------------------------------------------------------------------------- #


class BuMcpBackend:
	"""Модель видит браузер только через инструменты `bu_mcp.server`.

	Сервер поднимается как отдельный процесс с транспортом stdio — тем же путём,
	которым его запустил бы любой MCP-клиент. Никаких внутрипроцессных
	сокращений: если инструмент не выведен наружу, модель им не воспользуется.
	"""

	name = 'bu-mcp'

	async def run(self, task: Task, model: str, profile: Profile, max_steps: int, headless: bool = True) -> RunReport:
		# Импорт здесь, а не наверху: bu_mcp.bench тянет websockets и browser_use,
		# а базовому бэкенду он не нужен вовсе.
		from bu_eval.loop import ToolSpec, done_spec, run_anthropic, run_openai

		rep = RunReport(task=task.name, model=model, profile=profile.name, backend=self.name)
		provider, bare = parse_spec(model)
		started = time.time()

		api = PROVIDERS[provider].api
		if not api:
			rep.errors.append(f'провайдер {provider} не умеет tool-use в этом харнессе; есть openai и anthropic')
			rep.stopped = 'error'
			rep.seconds = time.time() - started
			return rep
		need = missing_key(provider)
		if need:
			rep.errors.append(f'провайдер {provider}: нет {need}')
			rep.stopped = 'error'
			rep.seconds = time.time() - started
			return rep
		if not profile.supports_mcp:
			rep.errors.append(f'профиль {profile.name} не поддержан бэкендом bu-mcp')
			rep.stopped = 'error'
			rep.seconds = time.time() - started
			return rep

		try:
			async with mcp_session(profile, rep) as (client, by_name, allowed):
				specs = [
					ToolSpec(name=n, description=by_name[n].get('description') or n, schema=by_name[n]['inputSchema'])
					for n in allowed
				]
				specs.append(done_spec(task.schema))

				async def call(name: str, args: dict):
					return await client.call(name, args)

				runner = run_openai if api == 'openai' else run_anthropic
				base_url = os.getenv(f'{provider.upper()}_BASE_URL') or None
				tr = await runner(bare, task.prompt, specs, call, task.schema, max_steps, base_url=base_url)

			rep.steps = tr.steps
			rep.actions = tr.calls
			rep.errors += tr.errors
			rep.tok_in, rep.tok_cached, rep.tok_out = tr.tok_in, tr.tok_cached, tr.tok_out
			rep.summary = tr.summary
			rep.stopped = tr.stopped
			rep.data = tr.data
			rep.ok = tr.data is not None
		except Exception as exc:  # noqa: BLE001
			rep.errors.append(repr(exc))
			rep.stopped = rep.stopped or 'error'

		rep.seconds = time.time() - started
		rep.price(model)
		return rep


class ScriptedBackend:
	"""Та же труба, но решения принимает не модель, а записанный сценарий задачи.

	Отделяет способности слоя от способностей модели и стоит ноль долларов:
	если `scripted` задачу не решает, модель тем более не решит, и провал модели
	нечего списывать на модель. Плюс это регрессия, которую можно гонять в CI —
	ключей она не требует.
	"""

	name = 'scripted'

	async def run(self, task: Task, model: str, profile: Profile, max_steps: int, headless: bool = True) -> RunReport:
		rep = RunReport(task=task.name, model='—', profile=profile.name, backend=self.name)
		started = time.time()
		if task.script is None:
			rep.errors.append(f'у задачи {task.name} нет записанного сценария')
			rep.stopped = 'error'
			rep.seconds = time.time() - started
			return rep
		if not profile.supports_mcp:
			rep.errors.append(f'профиль {profile.name} не поддержан бэкендом bu-mcp')
			rep.stopped = 'error'
			rep.seconds = time.time() - started
			return rep

		try:
			async with mcp_session(profile, rep) as (client, _by_name, allowed):
				allowed_set = set(allowed)

				async def call(name: str, args: dict):
					# сценарий обязан жить внутри профиля, иначе он мерит не то,
					# что мерит модель в той же ячейке матрицы
					if name not in allowed_set:
						raise KeyError(f'инструмент {name} вне профиля {profile.name}')
					rep.steps += 1
					rep.actions.append(name)
					res = await client.call(name, args)
					if res['is_error']:
						rep.errors.append(f'{name}: {res["text"][:200]}')
					return res

				rep.data = await task.script(call)
			rep.ok = rep.data is not None
			rep.stopped = 'done' if rep.ok else 'no_result'
		except Exception as exc:  # noqa: BLE001
			rep.errors.append(repr(exc))
			rep.stopped = 'error'

		rep.seconds = time.time() - started
		rep.cost = 0.0  # модели в цикле нет, платить нечем
		return rep


@asynccontextmanager
async def mcp_session(profile: Profile, rep: RunReport):
	"""Поднять `bu_mcp.server` по stdio, привязать свою вкладку, отдать набор инструментов профиля.

	Общее место обоих MCP-бэкендов: один и тот же сервер, одна и та же
	дисциплина вкладок, один и тот же фильтр инструментов. Разница между
	бэкендами должна быть ровно в том, кто принимает решения.
	"""
	from bu_mcp.bench import CDP_URL, OURS, McpClient, cdp_pages

	foreign = {t['id']: (t.get('url') or '') for t in cdp_pages()}
	client = McpClient(OURS)
	own_tab: str | None = None
	reused = False
	try:
		await client.start()
		own_tab, reused = await _bind_own_tab(client, foreign)
		# публичного списка инструментов у клиента из bench нет — берём тем же
		# JSON-RPC, которым он ходит за всем остальным
		listed = await client._request('tools/list', {}, timeout=60.0)
		by_name = {t['name']: t for t in listed.get('tools', [])}
		allowed = profile.filter_mcp_tools(list(by_name))
		rep.tools_offered, rep.tools_allowed = len(by_name), len(allowed)
		yield client, by_name, allowed
	finally:
		await client.kill()
		await _release_own_tab(CDP_URL, own_tab, reused, foreign, rep)


#: Пустые страницы, в которые browser-use навигирует ВМЕСТО открытия новой
#: вкладки. Из-за этого `new_tab=True` может приземлиться на уже существующий
#: пустой таб — не чужую работу, но и не нашу собственность.
_BLANK_URLS = ('', 'about:blank', 'chrome://newtab/', 'chrome://new-tab-page/', 'chrome://new-tab-page-third-party/')


async def _bind_own_tab(client, foreign: dict[str, str]) -> tuple[str, bool]:
	"""Привязать сервер к вкладке и вернуть `(target_id, переиспользована ли чужая пустая)`.

	Механика `bu_mcp.bench.Bound.bind` с двумя отличиями.

	Первое: маркер локальный, чтобы офлайновые задачи (clickgate) не требовали
	сети даже на привязке.

	Второе: `new_tab=True` — просьба, а не гарантия. browser-use навигирует в
	уже открытую пустую вкладку вместо создания новой, и такой таб окажется в
	снимке «чужих». Отличаем два случая по URL из снимка: пустую вкладку
	забираем взаймы и в конце возвращаем пустой, а приземление на живую чужую
	страницу — это авария, прогон прерывается. Так уже терялась чужая вкладка,
	и повторять не будем.
	"""
	from bu_eval.fixtures import base_url
	from bu_mcp.bench import McpError, cdp_pages

	token = f'bueval-{uuid.uuid4().hex[:8]}'
	marker = f'{base_url()}/#{token}'
	res = await client.call('browser_navigate', {'url': marker, 'new_tab': True}, timeout=120.0)
	if res['is_error']:
		raise McpError(f'не смог завести свою вкладку: {res["text"][:400]}')
	for _ in range(20):
		for t in cdp_pages():
			if token not in (t.get('url') or ''):
				continue
			tid = t['id']
			if tid not in foreign:
				return tid, False
			if foreign[tid] in _BLANK_URLS:
				return tid, True
			raise McpError(f'маркер приземлился на ЖИВУЮ чужую вкладку {tid} ({foreign[tid][:80]}), прерываю')
		await asyncio.sleep(0.5)
	raise McpError(f'своя вкладка с маркером {token} не нашлась')


async def _release_own_tab(cdp_url: str, own_tab: str | None, reused: bool, foreign: dict[str, str], rep: RunReport) -> None:
	"""Вернуть вкладку как было и сверить, что чужие на месте.

	Свою — закрыть по её target_id и только по нему. Взятую взаймы пустую —
	не закрывать (её открывали не мы), а вернуть на about:blank.
	"""
	from bu_mcp.bench import cdp_eval, cdp_pages

	if own_tab and not reused:
		try:
			urllib.request.urlopen(f'{cdp_url}/json/close/{own_tab}', timeout=10).read()  # noqa: ASYNC210 — уборка, вне измеряемого пути
		except Exception as exc:  # noqa: BLE001
			rep.errors.append(f'не закрылась своя вкладка {own_tab[:8]}: {exc!r}')
	elif own_tab and reused:
		try:
			await cdp_eval(own_tab, "window.location.replace('about:blank')")
		except Exception as exc:  # noqa: BLE001
			rep.errors.append(f'не вернул взятую взаймы вкладку {own_tab[:8]} на about:blank: {exc!r}')
	try:
		still = {t['id'] for t in cdp_pages()}
	except Exception:  # noqa: BLE001
		return
	lost = [tid[:8] for tid in foreign if tid not in still]
	if lost:
		rep.errors.append(f'ВНИМАНИЕ: пропали чужие вкладки {lost}')


BACKENDS: dict[str, Backend] = {b.name: b for b in [BrowserUseBackend(), BuMcpBackend(), ScriptedBackend()]}
