"""Проверки без LLM и без денег: то, что должно работать всегда.

Отвечает на вопрос «я сломал харнесс своей правкой или нет» за пару секунд,
не потратив ни одного токена. Единственная проверка, которая трогает браузер, —
`t_mcp_tools`: она поднимает наш MCP-сервер по stdio и спрашивает список
инструментов. Сессия при этом не стартует (список строится из реестра), так что
чужие вкладки не задеваются.
"""

from __future__ import annotations

import re

from bu_eval.upstream import Check


def t_never_steals_focus() -> Check:
	"""Автоматизация не имеет права забрать фокус у того, кто за машиной.

	Проверка появилась после происшествия: харнесс поднял Chrome с окном, и
	каждая открытая вкладка забирала фокус у владельца. Тогда это чинилось
	запретом окна вообще. Запрет пришлось снять: headless=new не проходит
	Cloudflare (на chatgpt.com проверка виснет на "Verifying..." навсегда), а
	настоящий Chrome с настоящим профилем проходит. Поэтому инвариант теперь
	формулируется по существу, а не через отсутствие окна:

	* `bu_eval` умеет только ПОДКЛЮЧАТЬСЯ к уже работающему Chrome — ветки
	  запуска своего браузера здесь нет и не должно появиться;
	* окно, если оно есть, стартует без активации и сразу прячется, а фокус
	  после запуска возвращается тому, кто был впереди;
	* `Target.createTarget` обёрнут в `preserve_frontmost`: это единственный
	  вызов CDP, после которого macOS выносит Chrome вперёд (0 краж фокуса на
	  50 навигаций в текущей вкладке против 100% на создании новой);
	* режим живого процесса СВЕРЯЕТСЯ перед выходом. Через молчаливый выход
	  «раз 9222 живой, всё хорошо» в прошлый раз и выживал чужой экземпляр.
	"""
	from pathlib import Path

	from bu_eval.backends import attached_profile

	p = attached_profile()
	bad = []
	if p.keep_alive is not True:
		bad.append(f'keep_alive={p.keep_alive} — выход из сессии сбросит чужой Chrome')
	if p.viewport is not None or p.no_viewport is not True:
		bad.append(f'viewport={p.viewport} no_viewport={p.no_viewport} — поедет геометрия чужих вкладок')
	if not p.cdp_url:
		bad.append('нет cdp_url — browser-use поднимет браузер сам')

	root = Path(__file__).resolve().parent
	for src in sorted(root.rglob('*.py')):
		text = src.read_text(encoding='utf-8')
		rel = src.relative_to(root).as_posix()
		if rel == 'selftest.py':
			continue
		if 'Browser(' in text or 'headless=headless' in text:
			bad.append(f'{rel}: вернулась ветка запуска своего браузера')

	guard = root.parent / 'browser_use' / 'browser' / 'session.py'
	if not guard.exists():
		bad.append('browser_use/browser/session.py пропал')
	else:
		g = guard.read_text(encoding='utf-8')
		# Флаг должен ПЕРЕЗАПИСЫВАТЬСЯ: апстрим передаёт background=False явно,
		# и setdefault его не трогал — вкладка продолжала забирать фокус.
		if "updated['background'] = True" not in g:
			bad.append('session.py: background не форсируется (setdefault не перебьёт явный False)')
		if 'async def preserve_frontmost' not in g:
			bad.append('session.py: предохранитель фокуса пропал')
		# Свой Chrome от чужого отличается ТОЛЬКО по pid: оба зовутся
		# «Google Chrome», и разбор по имени однажды уже прятал у владельца
		# его собственный браузер.
		if '_automation_chrome_pid' not in g:
			bad.append('session.py: наш Chrome больше не опознаётся по pid')
		if 'name of f is "Google Chrome"' in g:
			bad.append('session.py: вернулось сравнение по имени — заденет чужой Chrome')
		created = g.count('Target.createTarget(')
		guarded = g.count('async with preserve_frontmost():')
		if guarded < created:
			bad.append(f'session.py: createTarget без предохранителя ({guarded} из {created})')

	# Питон — не единственная дверь. Окно в прошлый раз пришло из шелл-скрипта.
	launcher = root.parent / 'scripts' / 'chrome-automation.sh'
	if not launcher.exists():
		bad.append('scripts/chrome-automation.sh пропал')
	else:
		sh = launcher.read_text(encoding='utf-8')
		if 'hide_app' not in sh:
			bad.append('chrome-automation.sh: окно больше не прячется после запуска')
		# Смотрим на КОМАНДЫ, а не на текст: про `open -a` в шапке скрипта
		# написано специально, и упоминание не должно ронять проверку.
		code = [ln for ln in sh.splitlines() if not ln.lstrip().startswith('#')]
		if any(re.search(r'(^|[;&|]\s*)open\s+-', ln) for ln in code):
			bad.append('chrome-automation.sh: вернулся запуск через `open` — LaunchServices активирует ЧУЖОЙ Chrome')
		if not any('hide_app' in ln for ln in code):
			bad.append('chrome-automation.sh: hide_app не вызывается')
		if 'frontmost_app' not in sh:
			bad.append('chrome-automation.sh: фокус после запуска не возвращается')
		# Сторож на время запуска: без него Chrome стоит впереди ~0.6 с, пока
		# скрипт ждёт готовности CDP. Это была последняя кража фокуса в замерах.
		if 'Сторож на время запуска' not in sh:
			bad.append('chrome-automation.sh: пропал сторож фокуса на время запуска')
		if 'running_mode' not in sh:
			bad.append('chrome-automation.sh: режим живого процесса не сверяется')
		if 'уже работает на $PORT' in sh and 'перезапускаю' not in sh:
			bad.append('chrome-automation.sh: молчаливый выход без перезапуска — так выживал чужой режим')
		# Окно показывается специально ровно в одном месте — в `login`: там и
		# только там hide_app вызывается БЕЗ pid'а, куда вернуть фокус.
		if 'login_back=$(frontmost_app)' not in sh:
			bad.append('chrome-automation.sh: login не запоминает фокус ДО показа окна')
		if 'hide_app "$(browser_pids | head -1)" "$login_back"' not in sh:
			bad.append('chrome-automation.sh: видимый вход (login) потерялся или не прячет окно')

	return Check(
		'selftest',
		'браузер не забирает фокус',
		not bad,
		'; '.join(bad) if bad else f'только подключение к {p.cdp_url}, окно спрятано, фокус возвращается',
	)


def t_model_factory() -> Check:
	"""Фабрика моделей ставит лимит ответа туда, где он у провайдера называется по-своему,
	и не подсовывает классу параметров, которых у него нет.

	Настоящий ключ здесь не нужен и не должен быть нужен: selftest по своему
	контракту не ходит в сеть и не тратит денег, а проверяется тут раскладка
	параметров по полям класса, то есть чистая конструкция. `make_model` при
	этом требует ключ просто как гейт, поэтому на время проверки подставляется
	заглушка — иначе тест падал у всех, у кого рядом нет .env.
	"""
	import os

	from bu_eval.models import make_model

	placeholder = os.getenv('OPENAI_API_KEY') is None
	if placeholder:
		os.environ['OPENAI_API_KEY'] = 'selftest-placeholder-not-a-real-key'
	try:
		o = make_model('openai:gpt-5-mini', max_output_tokens=32000)
	finally:
		if placeholder:
			os.environ.pop('OPENAI_API_KEY', None)

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
	t_never_steals_focus,
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
		except Exception as exc:
			out.append(Check('selftest', fn.__name__, False, f'упало: {exc!r}'))
	return out
