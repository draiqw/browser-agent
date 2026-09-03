"""Проверки без LLM и без денег: то, что должно работать всегда.

Отвечает на вопрос «я сломал харнесс своей правкой или нет» за пару секунд,
не потратив ни одного токена. Единственная проверка, которая трогает браузер, —
`t_mcp_tools`: она поднимает наш MCP-сервер по stdio и спрашивает список
инструментов. Сессия при этом не стартует (список строится из реестра), так что
чужие вкладки не задеваются.
"""

from __future__ import annotations

from bu_eval.upstream import Check


def t_model_factory() -> Check:
	"""Фабрика моделей ставит лимит ответа туда, где он у провайдера называется по-своему,
	и не подсовывает классу параметров, которых у него нет."""
	from bu_eval.models import make_model

	o = make_model('openai:gpt-5-mini', max_output_tokens=32000)
	ok_openai = getattr(o, 'max_completion_tokens', None) == 32000 and o.temperature == 0.0
	lo = make_model('ollama:qwen3:8b', max_output_tokens=32000)  # у ChatOllama нет ни того, ни другого
	ok_ollama = lo.model == 'qwen3:8b' and not hasattr(lo, 'temperature')
	return Check(
		'selftest',
		'фабрика моделей',
		ok_openai and ok_ollama,
		f'openai лимит={getattr(o, "max_completion_tokens", None)}, ollama создан без лишних полей',
	)


def t_profiles() -> Check:
	"""Профили собирают разные наборы действий, координаты включаются только там, где заявлено."""
	from bu_eval.profiles import PROFILES

	rows, bad = [], []
	for name, p in PROFILES.items():
		tools = p.build_tools()
		acts = set(tools.registry.registry.actions)
		coord = bool(getattr(tools, '_coordinate_clicking_enabled', False))
		if coord != p.coordinates:
			bad.append(f'{name}: координаты {coord} != {p.coordinates}')
		if p.coordinates and 'coordinate_x' not in tools.registry.registry.actions['click'].param_model.model_fields:
			bad.append(f'{name}: координаты включены, но у click нет coordinate_x')
		if name in ('act', 'act-coords') and 'evaluate' in acts:
			bad.append(f'{name}: evaluate должен быть выключен')
		rows.append(f'{name}={len(acts)}')
	return Check('selftest', 'профили', not bad, '; '.join(bad) if bad else 'действий: ' + ', '.join(rows))


def t_mcp_profiles() -> Check:
	"""Наборы MCP-инструментов не пусты, вложены как заявлено и не разъезжаются с сервером."""
	from bu_eval.profiles import MCP_ACT, MCP_ALL, MCP_READ, PROFILES

	bad = []
	if not set(MCP_READ) < set(MCP_ACT):
		bad.append('act больше не надмножество extract')
	if 'evaluate' in MCP_ACT:
		bad.append('evaluate попал в act — профиль перестанет мерить интерфейс')
	if PROFILES['act-coords'].supports_mcp:
		bad.append('act-coords помечен доступным, хотя координатных кликов у нас нет')
	for name in ('extract', 'act', 'act-js', 'raw'):
		p = PROFILES[name]
		if not p.supports_mcp:
			bad.append(f'{name}: профиль потерял набор MCP-инструментов')
	sizes = ', '.join(
		f'{n}={"все" if PROFILES[n].mcp == MCP_ALL else len(PROFILES[n].mcp or ())}' for n in ('extract', 'act', 'act-js', 'raw')
	)
	return Check('selftest', 'профили MCP', not bad, '; '.join(bad) if bad else f'инструментов: {sizes}')


def t_mcp_tools() -> Check:
	"""Сервер отдаёт инструменты, и каждый профиль на них отображается без остатка.

	Именно здесь ловится расхождение «профиль назвал инструмент, которого нет»:
	без этой проверки профиль `act` однажды молча выродится в `extract`.
	"""
	import asyncio

	from bu_eval.profiles import PROFILES
	from bu_mcp.bench import OURS, McpClient

	async def ask() -> list[str]:
		client = McpClient(OURS)
		try:
			await client.start()
			res = await client._request('tools/list', {}, timeout=60.0)
			return [t['name'] for t in res.get('tools', [])]
		finally:
			await client.kill()

	offered = asyncio.run(ask())
	bad = []
	sizes = []
	for name, p in PROFILES.items():
		if not p.supports_mcp:
			continue
		try:
			sizes.append(f'{name}={len(p.filter_mcp_tools(offered))}')
		except ValueError as exc:
			bad.append(str(exc))
	return Check(
		'selftest',
		'инструменты сервера',
		not bad,
		'; '.join(bad) if bad else f'сервер отдал {len(offered)}; профили: {", ".join(sizes)}',
	)


def t_done_schema() -> Check:
	"""Синтетический `done` собирает параметры из схемы задачи и не теряет вложенные модели."""
	import json

	from bu_eval.loop import done_spec
	from bu_eval.tasks.hn import Front

	spec = done_spec(Front)
	body = json.dumps(spec.schema)
	has_defs = '$defs' in spec.schema
	refs_ok = '#/$defs/Story' not in body or has_defs
	ok = spec.name == 'done' and 'result' in spec.schema['properties'] and refs_ok
	return Check(
		'selftest',
		'схема done',
		ok,
		f'$defs подняты в корень: {has_defs}, required={spec.schema.get("required")}',
	)


def t_tasks() -> Check:
	"""У каждой задачи есть схема и проверка, и проверка ловит заведомо неверные данные."""
	from bu_eval.task import all_tasks
	from bu_eval.tasks.clickgate import Gate, verify

	bad = []
	for name, t in all_tasks().items():
		if not t.schema or not callable(t.verify):
			bad.append(f'{name}: нет схемы или проверки')
	# проверка обязана ругаться на подделку
	if not verify(Gate(code='GATE-0000', enabled=['Депозит'])):
		bad.append('clickgate: проверка пропустила неверный код')
	if verify(Gate(code='GATE-8190', enabled=['Специальный счёт 40802', 'Депозит'])):
		bad.append('clickgate: проверка забраковала верный ответ')
	return Check(
		'selftest',
		'задачи',
		not bad,
		'; '.join(bad) if bad else f'{len(all_tasks())} задач, проверки различают верное и неверное',
	)


def t_fixture() -> Check:
	"""Офлайновая фикстура генерируется и отдаётся по http."""
	import urllib.request

	from bu_eval.tasks.clickgate import setup

	url = setup()
	body = urllib.request.urlopen(url, timeout=5).read().decode()
	ok = 'opacity:0' in body.replace(' ', '') and body.count('type="checkbox"') == 4
	return Check('selftest', 'фикстура clickgate', ok, f'{url}, {len(body)} байт, 4 скрытых чекбокса')


def t_pricing() -> Check:
	"""Цена считается по реестру и отличает известную модель от неизвестной."""
	from bu_eval.pricing import cost_of

	known = cost_of('gpt-5-mini', 1_000_000, 0, 0)
	unknown = cost_of('несуществующая-модель-xyz', 1000, 0, 100)
	ok = known and known > 0 and unknown is None
	return Check('selftest', 'подсчёт цены', bool(ok), f'gpt-5-mini 1M входных = ${known:.4f}, неизвестная = {unknown}')


def t_report() -> Check:
	"""Отчёт собирается из пустого и из заполненного прогона, не падая."""
	from bu_eval.backends import RunReport
	from bu_eval.report import line, table

	r = RunReport(
		task='t',
		model='m',
		profile='p',
		backend='b',
		ok=True,
		verified=True,
		steps=3,
		seconds=10.0,
		tok_in=100,
		tok_out=50,
		cost=0.01,
	)
	ok = 'OK' in line(r) and 'сошлось' in table([r])
	return Check('selftest', 'отчёты', ok, 'строка и таблица собираются')


def t_matrix() -> Check:
	"""Матрица разворачивается по бэкендам: одна задача на двух бэкендах — две ячейки."""
	from bu_eval.runner import Matrix

	mx = Matrix(tasks=['clickgate'], models=['openai:gpt-5-mini'], profiles=['act'], backends=['browser-use', 'bu-mcp'])
	cells = mx.cells()
	ok = len(cells) == 2 and {c[3] for c in cells} == {'browser-use', 'bu-mcp'}
	return Check('selftest', 'матрица', ok, f'{len(cells)} ячейки: {[c[3] for c in cells]}')


CHECKS = [
	t_model_factory,
	t_profiles,
	t_mcp_profiles,
	t_mcp_tools,
	t_done_schema,
	t_tasks,
	t_fixture,
	t_pricing,
	t_report,
	t_matrix,
]


def run_all() -> list[Check]:
	out = []
	for fn in CHECKS:
		try:
			out.append(fn())
		except Exception as exc:  # noqa: BLE001
			out.append(Check('selftest', fn.__name__, False, f'упало: {exc!r}'))
	return out
