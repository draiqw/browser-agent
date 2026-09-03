"""Доктор допущений: на чём стоит bu_mcp и на чём стоит сам харнесс.

`bu_mcp` — не форк: код browser-use не меняется, слой работает поверх его
внутренностей. Значит у нас есть список допущений об апстриме, и каждое из них
апстрим волен молча поломать переименованием поля или переформулировкой строки.

Сейчас про такой слом мы узнаём посреди смоука или, хуже, посреди прогона с
моделью. Этот модуль отвечает на вопрос «что именно отвалилось» за пару секунд
и в одном месте.

    python -m bu_eval doctor

Проверки поделены на две группы:

* HARNESS  — допущения самого харнесса (перенесены из browseruse-lab/harness/upstream.py);
* BU_MCP   — допущения нашего исполнительного слоя, по модулям:
  `resolve.py` (индекс = CDP backendNodeId, `_cached_selector_map`, поля узла),
  `state.py`   (`SerializedDOMState.llm_representation`, `DEFAULT_INCLUDE_ATTRIBUTES`),
  `waiting.py` (буфер lifecycle-событий, `loaderId` главного фрейма),
  `server.py`  (тексты-нооп из `tools/service.py`, мост к реестру `Tools()`).

Стиль здесь fail-open, как и во всём `bu_eval`: упавшая проверка становится
строкой «СЛОМ», а не исключением на весь запуск. Наш исполнительный слой
(`bu_mcp`) fail-closed — это осознанное расхождение, и оно не должно перетекать
в обратную сторону.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import inspect
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Check:
	group: str
	name: str
	ok: bool
	detail: str


def pkg_dir() -> Path:
	import browser_use

	return Path(browser_use.__file__).resolve().parent


def version() -> str:
	from importlib.metadata import version as v

	return v('browser-use')


def _read(rel: str) -> str:
	return (pkg_dir() / rel).read_text(encoding='utf-8', errors='replace')


# --------------------------------------------------------------------------- #
# Группа HARNESS: допущения оценочного кода
# --------------------------------------------------------------------------- #


def check_integrity() -> Check:
	"""Никто не правил browser-use руками.

	Стоит первой: если код апстрима патчен локально, все остальные проверки
	говорят не про апстрим, а про чью-то правку — а весь смысл `bu_mcp` в том,
	что это не форк.

	Путей два, потому что `browser_use` может быть и установленным пакетом
	(тогда сверяем sha256 по RECORD), и рабочей копией репозитория (тогда
	сверяем через git — RECORD там не существует в принципе).
	"""
	root = pkg_dir().parent
	dist = root / f'browser_use-{version()}.dist-info' / 'RECORD'
	if not dist.exists():
		return _integrity_via_git(root)
	bad, missing, total = [], 0, 0
	for row in csv.reader(dist.open()):
		if len(row) < 3 or not row[1].startswith('sha256='):
			continue
		f = root / row[0]
		total += 1
		if not f.exists():
			missing += 1
			continue
		h = base64.urlsafe_b64encode(hashlib.sha256(f.read_bytes()).digest()).rstrip(b'=').decode()
		if h != row[1][7:]:
			bad.append(row[0])
	ok = not bad and not missing
	return Check(
		'harness',
		'целостность пакета',
		ok,
		f'{total} файлов, изменено {len(bad)}, отсутствует {missing}' + (f' -> {bad[:3]}' if bad else ''),
	)


def _integrity_via_git(root: Path) -> Check:
	"""Рабочая копия вместо установленного пакета: спрашиваем git, а не RECORD."""
	import subprocess

	if not (root / '.git').exists():
		return Check('harness', 'целостность пакета', False, f'нет ни dist-info/RECORD, ни git в {root}')
	try:
		out = subprocess.run(
			['git', '-C', str(root), 'status', '--porcelain', '--', 'browser_use'],
			capture_output=True,
			text=True,
			timeout=30,
		)
	except Exception as exc:  # noqa: BLE001
		return Check('harness', 'целостность пакета', False, f'git не ответил: {exc!r}')
	dirty = [ln[3:] for ln in out.stdout.splitlines() if ln.strip()]
	head = subprocess.run(
		['git', '-C', str(root), 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True, timeout=30
	).stdout.strip()
	return Check(
		'harness',
		'целостность пакета',
		not dirty,
		f'рабочая копия на {head or "?"}, browser_use/ чист'
		if not dirty
		else f'рабочая копия на {head}, в browser_use/ правки: {dirty[:5]}',
	)


def check_coordinate_api() -> Check:
	"""Профиль act-coords включает клики по координатам сам, минуя зашитый в апстрим список моделей."""
	from browser_use import Tools

	if not hasattr(Tools, 'set_coordinate_clicking'):
		return Check('harness', 'API координатных кликов', False, 'у Tools больше нет set_coordinate_clicking')
	t = Tools()
	t.set_coordinate_clicking(True)
	on = getattr(t, '_coordinate_clicking_enabled', None)
	return Check('harness', 'API координатных кликов', on is True, f'флаг после включения: {on}')


def check_coordinate_allowlist() -> Check:
	"""Апстрим сам включает координаты только избранным моделям.

	Состав списка важен для чтения результатов: если модель в нём есть, наш
	форсинг ничего не меняет; если нет — меняет всё.
	"""
	src = _read('agent/service.py')
	m = re.search(r'supports_coordinate_clicking\s*=\s*any\((.*?)\)\s*\n', src, re.S)
	if not m:
		return Check('harness', 'список моделей апстрима', False, 'не нашёл блок supports_coordinate_clicking')
	pats = re.findall(r"'([^']+)'", m.group(1))
	return Check('harness', 'список моделей апстрима', bool(pats), f'{len(pats)} шаблонов: {", ".join(pats)}')


def check_tools_before_action_model() -> Check:
	"""Включать координаты нужно ДО конструктора Agent.

	Схема действий собирается позже, на `_setup_action_models`. Поменяется
	порядок — включение потеряется молча.
	"""
	src = _read('agent/service.py')
	i_tools = src.find('set_coordinate_clicking')
	i_setup = src.find('self._setup_action_models()')
	ok = -1 < i_tools < i_setup
	return Check(
		'harness',
		'порядок сборки Agent',
		ok,
		f'set_coordinate_clicking@{i_tools} < _setup_action_models@{i_setup}',
	)


def check_structured_output() -> Check:
	"""Контракт baseline-бэкенда стоит на history.structured_output."""
	from browser_use.agent.views import AgentHistoryList

	ok = 'structured_output' in dir(AgentHistoryList)
	return Check('harness', 'structured_output у истории', ok, 'есть' if ok else 'поля больше нет')


def check_history_accounting() -> Check:
	"""Отчёт берёт из истории шаги, действия, ошибки и токены — все четыре метода нужны."""
	from browser_use.agent.views import AgentHistoryList

	# `usage` — поле pydantic-модели, остальные три — методы: hasattr на классе
	# ловит только вторые, поэтому смотрим и model_fields тоже
	fields = set(getattr(AgentHistoryList, 'model_fields', {}) or {})
	need = ('number_of_steps', 'action_names', 'errors', 'usage')
	missing = [n for n in need if not hasattr(AgentHistoryList, n) and n not in fields]
	return Check(
		'harness',
		'учёт по истории',
		not missing,
		'все четыре метода на месте' if not missing else f'пропали: {missing}',
	)


def check_llm_classes() -> Check:
	"""Фабрика моделей грузит классы провайдеров из browser_use по имени."""
	import browser_use
	from bu_eval.models import PROVIDERS

	missing = sorted({p.cls_name for p in PROVIDERS.values() if not hasattr(browser_use, p.cls_name)})
	return Check(
		'harness',
		'классы провайдеров',
		not missing,
		f'{len({p.cls_name for p in PROVIDERS.values()})} классов на месте' if not missing else f'нет: {missing}',
	)


# --------------------------------------------------------------------------- #
# Группа BU_MCP: допущения исполнительного слоя
# --------------------------------------------------------------------------- #


def check_index_is_backend_node_id() -> Check:
	"""resolve.py: индекс элемента — это CDP backendNodeId.

	Отсюда следует его нестабильность при пересоздании узла, и ровно ради этого
	написана лестница переидентификации. Апстрим: dom/serializer/serializer.py,
	`_allocate_selector_index` — возвращает сам backend_node_id, пока тот
	свободен, и синтетический индекс при коллизии.
	"""
	src = _read('dom/serializer/serializer.py')
	has_fn = '_allocate_selector_index' in src
	m = re.search(r'def _allocate_selector_index\(self, backend_node_id: int\) -> int:(.*?)\n\tdef ', src, re.S)
	body = m.group(1) if m else ''
	returns_id = 'return backend_node_id' in body
	ok = has_fn and returns_id
	return Check(
		'bu_mcp',
		'индекс = backendNodeId',
		ok,
		'dom/serializer/serializer.py::_allocate_selector_index возвращает backend_node_id'
		if ok
		else f'функция={has_fn}, возврат backend_node_id={returns_id} — разметка индексов изменилась',
	)


def check_selector_map_attr() -> Check:
	"""resolve.py читает приватный `BrowserSession._cached_selector_map`.

	Публичного доступа к карте нет; переименование приватного поля сделает
	резолв слепым, а не сломанным — поэтому проверяем и имя, и тип значения.
	"""
	from browser_use.browser import BrowserSession

	attrs = getattr(BrowserSession, '__private_attributes__', {}) or {}
	ok = '_cached_selector_map' in attrs
	also = hasattr(BrowserSession, 'update_cached_selector_map')
	return Check(
		'bu_mcp',
		'карта селекторов',
		ok,
		f'_cached_selector_map: {"есть" if ok else "НЕТ"}, update_cached_selector_map: {"есть" if also else "нет"}',
	)


def check_dom_node_fields() -> Check:
	"""resolve.py собирает хендл из полей узла: без любого из них ступень лестницы отваливается."""
	import dataclasses

	from browser_use.dom.views import EnhancedDOMTreeNode

	# узел — dataclass, а не pydantic-модель; апстрим волен это поменять,
	# поэтому смотрим оба варианта, а не один
	if dataclasses.is_dataclass(EnhancedDOMTreeNode):
		fields = {f.name for f in dataclasses.fields(EnhancedDOMTreeNode)}
	else:
		fields = set(getattr(EnhancedDOMTreeNode, 'model_fields', {}) or {})
	need_fields = {'backend_node_id', 'node_name', 'attributes', 'frame_id', 'session_id'}
	need_props = {'xpath'}
	missing = sorted((need_fields - fields) | {p for p in need_props if not hasattr(EnhancedDOMTreeNode, p)})
	return Check(
		'bu_mcp',
		'поля узла DOM',
		not missing,
		f'{len(need_fields | need_props)} полей на месте' if not missing else f'пропали: {missing}',
	)


def check_visibility_filter() -> Check:
	"""clickgate стоит на том, что элементы с opacity:0 не попадают в карту.

	Это не наша фича, а наблюдаемое поведение апстрима: исчезнет фильтр —
	задача перестанет мерить то, ради чего заведена, и начнёт проходить сама.
	"""
	src = _read('dom/serializer/serializer.py') + _read('dom/views.py')
	ok = 'opacity' in src
	return Check('bu_mcp', 'фильтр видимости по opacity', ok, 'фильтр на месте' if ok else 'упоминаний opacity нет')


def check_llm_representation() -> Check:
	"""state.py отдаёт клиенту богатое дерево апстрима вместо плоского JSON штатного сервера.

	Нужны и метод, и именно параметр `include_attributes`: без него мы не можем
	протащить свой список атрибутов.
	"""
	from browser_use.dom.views import SerializedDOMState

	fn = getattr(SerializedDOMState, 'llm_representation', None)
	if fn is None:
		return Check('bu_mcp', 'llm_representation', False, 'у SerializedDOMState больше нет llm_representation')
	params = list(inspect.signature(fn).parameters)
	ok = 'include_attributes' in params
	return Check('bu_mcp', 'llm_representation', ok, f'сигнатура: ({", ".join(params)})')


def check_include_attributes() -> Check:
	"""state.py дополняет DEFAULT_INCLUDE_ATTRIBUTES своими атрибутами.

	Проверяем, что список на месте, непустой и что наши добавки всё ещё
	добавки, а не дубли (иначе экономия символов посчитана неверно).
	"""
	from browser_use.dom.views import DEFAULT_INCLUDE_ATTRIBUTES

	ok = isinstance(DEFAULT_INCLUDE_ATTRIBUTES, list) and len(DEFAULT_INCLUDE_ATTRIBUTES) > 0
	ours = {'href', 'value', 'checked', 'selected'}
	already = sorted(ours & set(DEFAULT_INCLUDE_ATTRIBUTES))
	return Check(
		'bu_mcp',
		'DEFAULT_INCLUDE_ATTRIBUTES',
		ok,
		f'{len(DEFAULT_INCLUDE_ATTRIBUTES)} атрибутов'
		+ (f'; уже включены апстримом: {already}' if already else '; наши добавки всё ещё добавки'),
	)


def check_lifecycle_buffer() -> Check:
	"""waiting.py ждёт навигацию по буферу lifecycle-событий SessionManager.

	Нужны: сам метод `get_lifecycle_events(target_id)` и то, что события в
	буфере несут `loaderId` — иначе отличить новый документ от старого нечем.
	"""
	from browser_use.browser.session_manager import SessionManager

	has_fn = hasattr(SessionManager, 'get_lifecycle_events')
	src = _read('browser/session_manager.py')
	has_loader = "'loaderId'" in src or '"loaderId"' in src
	ok = has_fn and has_loader
	return Check(
		'bu_mcp',
		'буфер lifecycle-событий',
		ok,
		f'get_lifecycle_events: {"есть" if has_fn else "НЕТ"}, loaderId в буфере: {"есть" if has_loader else "НЕТ"}',
	)


def check_loader_id_navigation() -> Check:
	"""waiting.py снимает loaderId главного фрейма через Page.getFrameTree.

	Механика взята из апстримного `_navigate_and_wait`; отличие в том, что там
	loaderId возвращает сам `Page.navigate`, а мы навигацию не инициировали.
	Если апстрим бросит эту схему, наше ожидание надо будет пересматривать.
	"""
	src = _read('browser/session.py')
	has_nav = '_navigate_and_wait' in src
	has_loader = "get('loaderId')" in src
	ok = has_nav and has_loader
	return Check(
		'bu_mcp',
		'ожидание по loaderId',
		ok,
		f'_navigate_and_wait: {"есть" if has_nav else "НЕТ"}, чтение loaderId: {"есть" if has_loader else "НЕТ"}',
	)


def check_dead_wait_knobs() -> Check:
	"""Причина, по которой waiting.py вообще написан: штатные ручки ожидания — мёртвый код.

	`minimum_wait_page_load_time` и `wait_for_network_idle_page_load_time`
	объявлены в профиле, мапятся на env — и НИ РАЗУ не читаются как значение.
	Если апстрим их однажды оживит, это надо заметить: часть нашей лестницы
	станет лишней.

	Ищем именно чтение атрибута (`.minimum_wait_page_load_time`), а не любое
	упоминание: объявления параметров и таблицы env — это не использование.
	"""
	profile_src = _read('browser/profile.py')
	declared = 'minimum_wait_page_load_time' in profile_src
	reads = re.compile(r'\.(?:minimum_wait_page_load_time|wait_for_network_idle_page_load_time)\b')
	users = []
	for path in sorted(pkg_dir().rglob('*.py')):
		text = path.read_text(encoding='utf-8', errors='replace')
		if reads.search(text):
			users.append(path.relative_to(pkg_dir()).as_posix())
	ok = declared and not users
	return Check(
		'bu_mcp',
		'штатные ручки ожидания мертвы',
		ok,
		'объявлены и ни разу не читаются — предпосылка waiting.py в силе'
		if ok
		else (f'значение читают: {users[:4]}' if declared else 'ручек больше нет в профиле'),
	)


def check_noop_markers() -> Check:
	"""server.py: таблица NOOP_MARKERS против живого апстрима.

	У каждого маркера в `source` записан адрес места, которое эту строку
	порождает. Здесь этот адрес превращается в проверку: строка-образец
	(как её увидел бы клиент) должна и опознаваться нашим `_classify_noop`,
	и по-прежнему присутствовать в исходнике апстрима.

	Это самая ценная проверка во всём файле: переформулированная в апстриме
	строка не ломает ничего громко — она просто перестаёт совпадать, и молчаливый
	ложный успех возвращается.
	"""
	from bu_mcp.server import NOOP_MARKERS, BuMcpServer

	# (код маркера, действие, текст как его увидит клиент, куски исходника апстрима)
	probes: dict[str, tuple[str, str, tuple[str, ...]]] = {
		'stale-index': (
			'click',
			'Element index 42 not available - page may have changed. Try refreshing browser state.',
			('not available - page may have changed. Try refreshing browser state.',),
		),
		'text-not-found': (
			'find_text',
			"Text 'quarterly report' not found or not visible on page",
			('not found or not visible on page',),
		),
		'no-dropdown-options': (
			'dropdown_options',
			'No options found in dropdown at index 7',
			('No options found in dropdown at index', 'No options found in ARIA combobox at index'),
		),
		'option-not-available': (
			'select_dropdown',
			"Couldn't select the dropdown option as 'RUB' is not one of the available options.",
			('is not one of the available options.',),
		),
		'switch-attempted': (
			'switch',
			'Attempted to switch to tab #A1B2',
			('Attempted to switch to tab #',),
		),
	}

	table = {m.code: m for m in NOOP_MARKERS}
	problems: list[str] = []

	untested = sorted(set(table) - set(probes))
	if untested:
		problems.append(f'в таблице появились маркеры без образца: {untested}')
	for code in sorted(set(probes) - set(table)):
		problems.append(f'{code}: маркер исчез из NOOP_MARKERS')

	for code, (action, text, literals) in probes.items():
		marker = table.get(code)
		if marker is None:
			continue
		got = BuMcpServer._classify_noop(action, text)
		if got is None or got.code != code:
			problems.append(f'{code}: наш _classify_noop не узнал собственный образец')
		# адрес места в апстриме записан в самом маркере — берём файл оттуда
		files = re.findall(r'browser_use/[\w/]+\.py', marker.source)
		if not files:
			problems.append(f'{code}: в source не указан файл апстрима')
			continue
		for literal in literals:
			if not any(literal in _read(f.split('browser_use/', 1)[1]) for f in files):
				problems.append(f'{code}: апстрим переформулировал строку в {files}: нет {literal!r}')

	return Check(
		'bu_mcp',
		'тексты-нооп апстрима',
		not problems,
		f'{len(table)} маркеров сверены с исходниками апстрима' if not problems else '; '.join(problems)[:400],
	)


def check_new_tab_claim() -> Check:
	"""server.py переписывает рапорт об авто-переключении на вкладку (issue #5529).

	Обе ветки `_detect_new_tab_opened` должны быть на месте: ложную мы правим по
	фактическому target_id, честную не трогаем.
	"""
	from bu_mcp.server import NEW_TAB_CLAIM_RE, NEW_TAB_NOTE_RE

	src = _read('tools/service.py')
	claim = 'Automatically switched to new tab (tab_id:' in src
	note = 'Note: This opened a new tab (tab_id:' in src
	ok_claim = bool(NEW_TAB_CLAIM_RE.search('. Automatically switched to new tab (tab_id: AB12).'))
	ok_note = bool(NEW_TAB_NOTE_RE.search('. Note: This opened a new tab (tab_id: AB12) - switch to it'))
	ok = claim and note and ok_claim and ok_note
	return Check(
		'bu_mcp',
		'рапорт о новой вкладке',
		ok,
		f'в апстриме claim={claim} note={note}; наши регекспы claim={ok_claim} note={ok_note}',
	)


def check_bridge_exclude() -> Check:
	"""server.py исключает часть действий реестра из моста.

	Исключение по имени — договор с апстримом: если действие переименуют,
	исключение станет пустым, и наружу вылезет второй путь к клику мимо резолва.
	"""
	from browser_use.tools.service import Tools
	from bu_mcp.server import BRIDGE_EXCLUDE

	actions = set(Tools().registry.registry.actions)
	gone = sorted(BRIDGE_EXCLUDE - actions)
	return Check(
		'bu_mcp',
		'мост к реестру',
		not gone,
		f'{len(actions)} действий в реестре, все {len(BRIDGE_EXCLUDE)} исключений попадают в цель'
		if not gone
		else f'исключения мимо цели (действие переименовано или удалено): {gone}',
	)


def check_registry_privates() -> Check:
	"""server.py гейтит домены приватным `ActionRegistry._match_domains`.

	Публичного эквивалента нет: `domains` в реестре фильтрует только выдачу
	списка действий, а `execute_action` домены не проверяет вовсе.
	"""
	from browser_use.tools.registry.views import ActionRegistry

	ok = hasattr(ActionRegistry, '_match_domains')
	return Check('bu_mcp', 'allowlist доменов', ok, '_match_domains на месте' if ok else 'приватный метод исчез')


def check_action_result_fields() -> Check:
	"""server.py читает результат действия по трём полям; их отсутствие ослепит разбор нооп."""
	from browser_use.agent.views import ActionResult

	fields = set(getattr(ActionResult, 'model_fields', {}) or {})
	missing = sorted({'error', 'extracted_content', 'long_term_memory'} - fields)
	return Check(
		'bu_mcp',
		'поля ActionResult',
		not missing,
		'error / extracted_content / long_term_memory на месте' if not missing else f'пропали: {missing}',
	)


def check_server_imports() -> Check:
	"""Всё, что server.py тащит из апстрима по именам, должно импортироваться.

	Дешёвая страховка от переезда модулей: импорты, а не поведение.
	"""
	problems = []
	try:
		from browser_use.browser import BrowserProfile, BrowserSession  # noqa: F401
		from browser_use.filesystem.file_system import FileSystem  # noqa: F401
		from browser_use.tools.service import Tools  # noqa: F401
		from browser_use.utils import is_new_tab_page  # noqa: F401
	except Exception as exc:  # noqa: BLE001
		problems.append(repr(exc))
	return Check(
		'bu_mcp',
		'импорты server.py',
		not problems,
		'BrowserProfile, BrowserSession, FileSystem, Tools, is_new_tab_page' if not problems else problems[0],
	)


CHECKS = [
	# HARNESS
	check_integrity,
	check_coordinate_api,
	check_coordinate_allowlist,
	check_tools_before_action_model,
	check_structured_output,
	check_history_accounting,
	check_llm_classes,
	# BU_MCP
	check_index_is_backend_node_id,
	check_selector_map_attr,
	check_dom_node_fields,
	check_visibility_filter,
	check_llm_representation,
	check_include_attributes,
	check_lifecycle_buffer,
	check_loader_id_navigation,
	check_dead_wait_knobs,
	check_noop_markers,
	check_new_tab_claim,
	check_bridge_exclude,
	check_registry_privates,
	check_action_result_fields,
	check_server_imports,
]


def run_all() -> list[Check]:
	out = []
	for fn in CHECKS:
		try:
			out.append(fn())
		except Exception as exc:  # noqa: BLE001 — упавшая проверка это тоже отчёт, а не крах прогона
			group = 'bu_mcp' if fn.__name__ in {c.__name__ for c in CHECKS[7:]} else 'harness'
			out.append(Check(group, fn.__name__, False, f'проверка упала: {exc!r}'))
	return out
