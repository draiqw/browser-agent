"""Журнал действий и сборка макросов.

Зачем это вообще. Модель в цикле — самая дорогая часть агента и единственная
недетерминированная. Если один раз записать, ЧТО и НАД ЧЕМ было сделано, то
второй прогон того же сценария можно выполнить без инференса вовсе. Playwright
MCP решает это тем, что каждый вызов возвращает эквивалентный код; Stagehand —
парой ``observe()`` -> ``Action`` -> ``act(action)``, где второй шаг модель уже
не зовёт. Наш вариант ближе к Stagehand: в журнал ложится составной хендл
элемента (``resolve.describe_handle``), и повтор идёт через переидентификацию,
а не через координаты и не через индексы.

Почему НЕ индексы. Индекс — это номер строки в конкретном снимке страницы. Он
не переживает ни перезагрузку, ни пересборку DOM-карты: после reload индекс 12
почти наверняка существует и почти наверняка указывает на ДРУГОЙ элемент. Запись
индекса в макрос — это гарантированный тихий неверный клик, то есть ровно то,
против чего написан весь остальной слой. Поэтому ``to_macro`` индексы вычищает,
оставляя их только как справочное поле ``recorded_index``, которым никто не
действует.

Главный принцип записи: журнал — это лог, а не критический путь. Любое
исключение внутри ``record``/``capture`` гасится и уходит в логгер. Действие,
которое удалось, не имеет права провалиться из-за того, что мы не смогли о нём
написать.

Хранилище
---------
``~/.config/bu-mcp/journals/<session>.jsonl`` — по файлу на процесс сервера,
одна строка на действие, дописывание в конец. JSONL выбран не из любви к
формату: файл дописывается атомарно одной строкой, его можно читать во время
записи, и повреждение одной строки не уносит остальные. JSON-массив пришлось бы
переписывать целиком на каждое действие.

``~/.config/bu-mcp/macros/<name>.json`` — макросы, уже человекочитаемым JSON с
отступами: их читают и правят руками, а пишут раз.

Корень переопределяется через ``BU_MCP_HOME``; запись целиком выключается
через ``BU_MCP_JOURNAL=0``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
	'record',
	'current_path',
	'read',
	'to_macro',
	'capture',
	'enabled',
	'save_macro',
	'load_macro',
	'macro_path',
	'list_macros',
	'home',
	'reset',
	'MACRO_VERSION',
	'OBSERVATIONS',
	'STATE_CHANGING',
]

MACRO_VERSION = 1

# --------------------------------------------------------------------------- #
# Классификация инструментов
# --------------------------------------------------------------------------- #

#: Наблюдения. В журнал их писать незачем (они ничего не меняют), а в макросе
#: они прямо вредны: это следы РАЗВЕДКИ, а не намерения. Агент зовёт
#: ``browser_state`` десять раз просто потому, что забыл, что видел. Повторять
#: это без модели бессмысленно — читать выхлоп некому.
OBSERVATIONS = frozenset(
	{
		'browser_state',
		'browser_screenshot',
		'screenshot',
		'find_elements',
		'search_page',
		'find_text',
		'dropdown_options',
		'extract',
		'read_file',
		'save_as_pdf',
		'evaluate',
		'wait',
	}
)

#: Действия, меняющие состояние. Именно они пишутся в журнал и попадают в макрос.
STATE_CHANGING = frozenset(
	{
		'browser_navigate',
		'navigate',
		'browser_click',
		'click',
		'browser_type',
		'input',
		'browser_hover',
		'hover',
		'select_dropdown',
		'send_keys',
		'scroll',
		'go_back',
		'search',
		'upload_file',
		'switch',
		'close',
	}
)

#: Инструменты, действующие над элементом: у них в params есть ``index``, и без
#: хендла шаг невоспроизводим.
ELEMENT_TOOLS = frozenset(
	{
		'browser_click',
		'click',
		'browser_type',
		'input',
		'browser_hover',
		'hover',
		'select_dropdown',
		'dropdown_options',
		'upload_file',
	}
)

#: Жизненный цикл вкладок. В макрос НЕ идёт — см. ``_DROP_REASONS['tab']``.
TAB_TOOLS = frozenset({'switch', 'close'})

_DROP_REASONS = {
	'observation': 'observation: nothing changed on the page, and there is no model to read the output',
	'error': 'the action failed at record time (fail-closed layer: nothing happened), so there is nothing to replay',
	'no_effect': 'delta reported no_effect: the page did not move, so this was a probe, not a step',
	'superseded': 'superseded by a later input into the same field (the agent retyped it)',
	'scroll_mechanics': 'scroll that only brought an element into view; click already does scrollIntoViewIfNeeded, '
	'and scroll offsets do not carry across viewports',
	'tab': 'tab lifecycle is not reproducible: the recorded tab_id belongs to that run only, and replaying a close '
	'would target whatever tab happens to hold that id now — possibly one that is not ours',
}

# --------------------------------------------------------------------------- #
# Пути
# --------------------------------------------------------------------------- #

_SESSION_ID: str | None = None
_SEQ = 0


def home() -> Path:
	"""Корень хранилища. ``BU_MCP_HOME`` перекрывает ``~/.config/bu-mcp``."""
	raw = os.getenv('BU_MCP_HOME')
	return Path(raw).expanduser() if raw else Path.home() / '.config' / 'bu-mcp'


def enabled() -> bool:
	"""Пишем ли мы вообще. ``BU_MCP_JOURNAL=0`` полностью выключает запись."""
	return os.getenv('BU_MCP_JOURNAL', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def _session_id() -> str:
	"""Идентификатор сессии = имя файла журнала.

	Время старта плюс pid: сортируется лексикографически по времени, читается
	глазами и не конфликтует при двух серверах на одной машине. uuid7 здесь дал
	бы то же свойство сортировки, но нечитаемое имя файла — журнал смотрят
	руками чаще, чем программой.
	"""
	global _SESSION_ID
	if _SESSION_ID is None:
		_SESSION_ID = f'{datetime.now().strftime("%Y%m%dT%H%M%S")}-{os.getpid()}'
	return _SESSION_ID


def current_path() -> Path:
	"""Файл журнала текущей сессии. Каталог создаётся при первом обращении."""
	path = home() / 'journals' / f'{_session_id()}.jsonl'
	try:
		path.parent.mkdir(parents=True, exist_ok=True)
	except Exception as exc:  # noqa: BLE001
		logger.debug('journal: cannot create %s: %r', path.parent, exc)
	return path


def macro_path(name: str) -> Path:
	return home() / 'macros' / f'{_safe_name(name)}.json'


def reset(*, session_id: str | None = None) -> None:
	"""Начать новый журнал. Нужно тестам и «запиши сценарий заново»."""
	global _SESSION_ID, _SEQ
	_SESSION_ID = session_id
	_SEQ = 0


def _safe_name(name: str) -> str:
	cleaned = re.sub(r'[^A-Za-z0-9._-]+', '-', (name or 'macro').strip()).strip('-.')
	return cleaned or 'macro'


# --------------------------------------------------------------------------- #
# Снимок контекста перед действием
# --------------------------------------------------------------------------- #

_CONTEXT_JS = """(() => {
  const d = document.documentElement;
  return {
    url: location.href,
    title: document.title || '',
    vw: Math.round(window.innerWidth || (d && d.clientWidth) || 0),
    vh: Math.round(window.innerHeight || (d && d.clientHeight) || 0),
    dpr: window.devicePixelRatio || 1,
  };
})()"""


async def capture(session: Any, index: int | None = None) -> dict[str, Any]:
	"""Всё, что надо снять ДО действия: составной хендл и контекст страницы.

	Вызывать один раз перед действием, результат класть в ``entry`` целиком.
	Никогда не бросает: при любом сбое возвращает то, что успело собраться, в
	худшем случае — пустой dict. Пропуск полей делает шаг менее воспроизводимым,
	но не ломает действие.

	Почему ``title`` и ``viewport``, которых нет в контракте.

	``viewport`` — потому что адаптивная вёрстка меняет НАБОР элементов, а не
	только их расположение. Сценарий, записанный на 1440px, при повторе в 375px
	упадёт на «кнопки нет», хотя кнопка есть — она уехала в гамбургер-меню.
	Без записанного вьюпорта оператор видит только «элемент не найден» и не имеет
	ни одного способа догадаться, почему. Это два целых числа и один
	``Runtime.evaluate``, который мы всё равно делаем ради url/title.

	``title`` — потому что URL не идентифицирует состояние SPA: `/app` до и после
	открытия модалки один и тот же. Заголовок — самый дешёвый человекочитаемый
	признак «а та ли это вообще страница», и он же годится как мягкая проверка
	предусловия при повторе. Ни то, ни другое не стоит отдельного CDP-вызова:
	оба поля приходят из того же evaluate, что и url.
	"""
	out: dict[str, Any] = {}
	if not enabled():
		return out

	if index is not None:
		try:
			from bu_mcp import resolve as _resolve

			out['handle'] = await _resolve.describe_handle(session, int(index))
		except Exception as exc:  # noqa: BLE001
			logger.debug('journal: describe_handle(%s) failed: %r', index, exc)

	try:
		cdp = await session.get_or_create_cdp_session(session.agent_focus_target_id, focus=False)
		res = await cdp.cdp_client.send.Runtime.evaluate(
			params={'expression': _CONTEXT_JS, 'returnByValue': True},
			session_id=cdp.session_id,
		)
		value = (res or {}).get('result', {}).get('value')
		if isinstance(value, dict):
			out['url_before'] = value.get('url')
			page: dict[str, Any] = {'title': value.get('title') or ''}
			vw, vh = int(value.get('vw') or 0), int(value.get('vh') or 0)
			if vw and vh:
				page['viewport'] = {'width': vw, 'height': vh}
			dpr = value.get('dpr')
			if dpr and float(dpr) != 1.0:
				page['dpr'] = round(float(dpr), 2)
			out['page'] = page
	except Exception as exc:  # noqa: BLE001
		logger.debug('journal: page context probe failed: %r', exc)

	return out


# --------------------------------------------------------------------------- #
# Запись
# --------------------------------------------------------------------------- #


def record(entry: dict[str, Any]) -> None:
	"""Дописать одну запись в журнал текущей сессии.

	Контрактные поля: ``tool``, ``params``, ``handle``, ``url_before``,
	``url_after``, ``delta``, ``outcome`` (``ok`` / ``noop`` / ``error``),
	``error``; плюс наши ``page`` (заголовок и вьюпорт, см. ``capture``) и любые
	дополнительные ключи вызывающего (``cost_ms``, ``resolved_index``,
	``handle_error``, ``tab`` — сервер их проставляет сам). Лишние ключи
	сохраняются как есть: журнал — это протокол, а не схема, и выбрасывать из
	него то, чего мы не ожидали, значит терять данные ровно там, где они и
	нужны — при разборе непонятного прогона.

	``ts`` ставится, только если его не проставил вызывающий (у него момент
	начала действия, у нас — момент записи, и первый точнее). ``seq`` — наш,
	сквозной по файлу.

	Не бросает НИКОГДА. Любая ошибка — в debug-лог. Это единственное требование,
	которое здесь важнее корректности: журнал висит на пути каждого действия, и
	падение сериализации не имеет права превращать удавшийся клик в ошибку.
	"""
	global _SEQ
	try:
		if not enabled():
			return
		tool = str(entry.get('tool') or '').strip()
		if not tool:
			return
		if tool in OBSERVATIONS:
			# Наблюдения не пишем совсем: они не меняют состояние, а объём журнала
			# определяют именно они (browser_state — самый частый вызов агента).
			return

		_SEQ += 1
		payload: dict[str, Any] = {
			'ts': round(float(entry.get('ts') or time.time()), 3),
			'seq': _SEQ,
			'tool': tool,
			'outcome': entry.get('outcome') or 'ok',
		}
		for key, value in entry.items():
			if key in ('ts', 'seq', 'tool', 'outcome'):
				continue
			if value in (None, {}, ''):
				continue
			payload[key] = value

		line = json.dumps(_sanitize(payload), ensure_ascii=False, separators=(',', ':'))
		path = current_path()
		with path.open('a', encoding='utf-8') as fh:
			fh.write(line + '\n')
	except Exception as exc:  # noqa: BLE001
		logger.debug('journal: record failed: %r', exc)


def _sanitize(value: Any, _depth: int = 0) -> Any:
	"""Привести к JSON-совместимому виду. Незнакомые объекты -> repr, без падения."""
	if _depth > 8:
		return '...'
	if value is None or isinstance(value, (bool, int, float, str)):
		return value
	if isinstance(value, dict):
		return {str(k): _sanitize(v, _depth + 1) for k, v in value.items()}
	if isinstance(value, (list, tuple, set)):
		return [_sanitize(v, _depth + 1) for v in value]
	if isinstance(value, Path):
		return str(value)
	try:
		return repr(value)[:500]
	except Exception:  # noqa: BLE001
		return '<unrepresentable>'


def read(path: Path | None = None, *, limit: int | None = None) -> list[dict[str, Any]]:
	"""Прочитать журнал. Битые строки пропускаются, не роняя чтение.

	``limit`` — последние N записей (а не первые): интересна всегда хвостовая
	часть, «что агент только что делал».
	"""
	target = Path(path) if path is not None else current_path()
	out: list[dict[str, Any]] = []
	try:
		with target.open('r', encoding='utf-8') as fh:
			for line in fh:
				line = line.strip()
				if not line:
					continue
				try:
					item = json.loads(line)
				except Exception:  # noqa: BLE001
					continue
				if isinstance(item, dict):
					out.append(item)
	except FileNotFoundError:
		return []
	except Exception as exc:  # noqa: BLE001
		logger.debug('journal: read %s failed: %r', target, exc)
		return out
	if limit is not None and limit >= 0:
		return out[-limit:]
	return out


# --------------------------------------------------------------------------- #
# Параметризация введённого текста
# --------------------------------------------------------------------------- #

#: Признаки поля, в которое нельзя записывать значение НИ В КАКОМ ВИДЕ.
#: Проверяется по имени/подписи/атрибутам, а не по значению: угадывать секрет по
#: содержимому — значит гарантированно ошибиться в обе стороны.
_SECRET_NAME_RE = re.compile(
	r'(?i)(pass(word|wd|phrase)?|pwd|secret|token|api[\s_-]*key|apikey|auth|credential|'
	r'otp|2fa|mfa|one[\s_-]*time|verification[\s_-]*code|security[\s_-]*code|'
	r'cvv|cvc|card[\s_-]*(number|no)|ccnum|iban|ssn|pin\b|seed[\s_-]*phrase|private[\s_-]*key)'
)

#: Признаки секрета в САМОМ значении. Узкий список: только то, что ни при каких
#: обстоятельствах не должно лежать в файле на диске.
_SECRET_VALUE_RE = re.compile(
	r'(?x)'
	r'^ (?: \d[ -]?){13,19} $'  # номер карты
	r'| ^ (?: sk|pk|rk ) [-_] (?: live|test|proj ) [-_] \S{8,} $'  # stripe-подобные ключи
	r'| ^ gh[pousr]_[A-Za-z0-9]{20,} $'  # github token
	r'| ^ xox[baprs]-[A-Za-z0-9-]{10,} $'  # slack token
	r'| ^ ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\. '  # JWT
	r'| AKIA[0-9A-Z]{16}'  # aws key id
)

_STOPWORDS = frozenset({'the', 'a', 'an', 'your', 'my', 'enter', 'type', 'input', 'field', 'please'})


def _slug(text: str, *, limit: int = 32) -> str:
	"""Человекочитаемое имя переменной из произвольной подписи."""
	text = unicodedata.normalize('NFKD', str(text or ''))
	text = text.encode('ascii', 'ignore').decode('ascii')
	words = [w for w in re.split(r'[^A-Za-z0-9]+', text) if w]
	words = [w.lower() for w in words if w.lower() not in _STOPWORDS] or [w.lower() for w in words]
	if not words:
		return ''
	slug = '_'.join(words)[:limit].strip('_')
	if slug and slug[0].isdigit():
		slug = 'f_' + slug
	return slug


def _field_label(handle: dict[str, Any] | None) -> tuple[str, str]:
	"""Из чего выводить имя переменной: (подпись, откуда взяли).

	    Порядок осмысленности, а не доступности:

	1. ``aria-label`` — это подпись, которую автор страницы написал ДЛЯ людей;
	2. accessible name — вычисленное браузером имя. Для ``<input>`` алгоритм
	   AccName сам обходит ``<label for>``, ``aria-labelledby``, ``title`` и в
	   последнюю очередь ``placeholder``, то есть «ближайшая подпись» и
	   «placeholder» из ТЗ приезжают сюда бесплатно, без отдельного CDP-запроса;
	3. ``name``/``data-testid``/``id`` — машинные, но стабильные и всё-таки
	   что-то говорящие («search_query» лучше, чем «var_3»);
	4. тег — последняя линия обороны, даст ``input_1``.
	"""
	handle = handle or {}
	attrs = handle.get('attributes') or {}
	for key, source in (
		('aria-label', 'aria-label'),
		('data-testid', 'data-testid'),
		('data-test-id', 'data-test-id'),
		('data-qa', 'data-qa'),
	):
		if attrs.get(key):
			return str(attrs[key]), source
	if handle.get('accessible_name'):
		return str(handle['accessible_name']), 'accessible name (label / aria-labelledby / placeholder)'
	for key in ('name', 'id'):
		if attrs.get(key):
			return str(attrs[key]), key
	return str(handle.get('tag') or 'value'), 'tag'


def _is_secret(label: str, handle: dict[str, Any] | None, value: str) -> str | None:
	"""Секрет ли это. Возвращает причину или ``None``."""
	handle = handle or {}
	attrs = handle.get('attributes') or {}
	haystack = ' '.join(
		str(x)
		for x in (
			label,
			handle.get('accessible_name') or '',
			handle.get('role') or '',
			attrs.get('name') or '',
			attrs.get('id') or '',
			attrs.get('aria-label') or '',
			attrs.get('data-testid') or '',
		)
	)
	if _SECRET_NAME_RE.search(haystack):
		return 'field name/label looks like a credential'
	if value and _SECRET_VALUE_RE.search(value.strip()):
		return 'value matches a known secret format'
	return None


class _VarPool:
	"""Именованные переменные макроса с разрешением коллизий."""

	def __init__(self) -> None:
		self.vars: dict[str, dict[str, Any]] = {}

	def add(self, base: str, value: str, *, source: str, secret: bool, note: str | None = None) -> str:
		name = _slug(base) or 'value'
		# Одинаковое имя + одинаковое значение = та же переменная (агент ввёл
		# одно и то же в два одинаково подписанных поля — это одна сущность).
		existing = self.vars.get(name)
		if existing is not None and not (existing.get('value') == value and existing.get('secret') == secret):
			n = 2
			while f'{name}_{n}' in self.vars:
				n += 1
			name = f'{name}_{n}'
		entry: dict[str, Any] = {'source': source, 'secret': secret}
		if secret:
			entry['value'] = None
			entry['required'] = True
			entry['note'] = note or 'not recorded on purpose; supply it via run(vars=...)'
		else:
			entry['value'] = value
		self.vars[name] = entry
		return name


# --------------------------------------------------------------------------- #
# Схлопывание журнала в сценарий
# --------------------------------------------------------------------------- #


def _handle_key(handle: dict[str, Any] | None) -> tuple | None:
	"""Ключ «это тот же самый элемент» для склейки соседних шагов."""
	if not handle:
		return None
	key = (
		handle.get('target_id'),
		handle.get('frame_id'),
		handle.get('xpath'),
		handle.get('backend_node_id'),
	)
	return key if any(key) else None


def _delta_flag(entry: dict[str, Any], key: str) -> Any:
	delta = entry.get('delta')
	return delta.get(key) if isinstance(delta, dict) else None


def _expectation(entry: dict[str, Any]) -> dict[str, Any]:
	"""Что должно случиться при повторе, в терминах, сравнимых МЕЖДУ ПРОГОНАМИ.

	Записанная дельта содержит числа, которые между прогонами не совпадут в
	принципе: ``digest``, ``nodes``, ``rendered`` зависят от рекламы, времени
	суток и ширины окна. Сравнивать их «до/после» бессмысленно. Сравнимо ровно
	то, что является ФОРМОЙ последствия:

	* было ли вообще значимое изменение (``changed``);
	* сменился ли URL и на какой (это и есть пример из ТЗ: при записи клик вёл
	  на другую страницу, при повторе не ведёт — расхождение, а не успех);
	* изменилось ли число вкладок, появился ли диалог, сменился ли заголовок.

	Времена в ожидание НЕ идут: ``cost_ms``/``settle_ms`` — свойство машины и
	сети, а не сценария.
	"""
	delta = entry.get('delta') if isinstance(entry.get('delta'), dict) else {}
	fields = delta.get('fields') if isinstance(delta.get('fields'), dict) else {}
	url_before, url_after = entry.get('url_before'), entry.get('url_after')

	expect: dict[str, Any] = {}
	changed = delta.get('changed')
	if changed is not None:
		expect['changed'] = bool(changed)
	if delta.get('status'):
		expect['status'] = delta['status']

	url_changed = bool(url_before and url_after and url_before != url_after) or ('url' in fields)
	expect['url_changed'] = url_changed
	if url_changed and url_after:
		expect['url_after'] = url_after

	if 'tabs' in fields:
		try:
			before_n, after_n = fields['tabs']
			expect['tabs_delta'] = int(after_n) - int(before_n)
		except Exception:  # noqa: BLE001
			expect['tabs_delta'] = None
	if 'dialogs' in fields:
		expect['dialogs_changed'] = True
	if 'title' in fields:
		expect['title_changed'] = True
	return expect


def _params_for_macro(entry: dict[str, Any]) -> dict[str, Any]:
	"""Параметры без ``index`` и без служебных таймаутов записи."""
	params = dict(entry.get('params') or {})
	params.pop('index', None)
	# Таймауты — свойство прогона, а не сценария: на другой машине они другие.
	# ``macro.run`` назначает свои.
	params.pop('timeout', None)
	params.pop('hydrate', None)
	return params


def to_macro(entries: list[dict[str, Any]], *, name: str) -> dict[str, Any]:
	"""Схлопнуть журнал в повторяемый сценарий.

	Это не фильтрация по списку имён. Вопрос, на который отвечает каждое
	правило, один: «здесь было НАМЕРЕНИЕ или РАЗВЕДКА?». Разведка — всё, что
	агент делал, чтобы посмотреть; намерение — то, ради чего он это делал.

	Правила, в порядке применения:

	1. **Наблюдения выкидываются.** ``browser_state``, ``browser_screenshot``,
	   ``find_elements`` и родня не меняют страницу, а их выхлоп при повторе
	   некому читать. (Они и в журнал-то не пишутся — ``record`` их отсекает.)

	2. **Неудавшиеся действия выкидываются.** Слой fail-closed: если
	   ``outcome == 'error'``, значит НИЧЕГО не произошло — протухший хендл,
	   неоднозначность, отказ резолва. Промах агента не является шагом сценария.

	3. **Шаги с ``delta.no_effect`` выкидываются.** Дельта у нас чувствительная
	   (геометрия + число узлов + digest, куда входят ``value``, ``checked``,
	   ``selectedIndex``, ``aria-expanded``), поэтому «отрапортовано успешно, но
	   не изменилось ничего» — это почти наверняка клик в мимо: съеденный
	   оверлеем, по неактивной кнопке, по уже выбранному пункту. Повторять его
	   нечего, а вот в strict-режиме он бы каждый раз давал ложное расхождение.
	   Исключения: ``browser_navigate`` (переход на страницу, где ты уже стоишь,
	   ничего не меняет, но задаёт стартовое состояние сценария) и ``scroll``
	   (у него дельта пуста ПО ПОСТРОЕНИЮ — ``scroll`` в списке информационных
	   признаков, см. правило 5).

	4. **Подряд идущие вводы в одно поле склеиваются в последний.** Агент печатает,
	   смотрит, стирает, печатает заново — намерением было конечное содержимое
	   поля, а не траектория к нему. Склейка идёт по хендлу (тот же
	   target/frame/xpath/backendNodeId), и только если ПОСЛЕДНИЙ ввод сделан с
	   ``clear=True``: он затирает поле, значит всё, что было до него, роли не
	   играет. Если последний ввод дописывающий (``clear=False``), склеивать
	   нельзя — текст накопительный, и мы оставляем всю цепочку как есть.

	5. **Скролл — механика, кроме двух случаев.** Прокрутка ради того, чтобы
	   элемент попал во вьюпорт, воспроизводить не нужно и вредно: клик и так
	   делает ``scrollIntoViewIfNeeded``, а смещение в пикселях на другом
	   вьюпорте означает другое место страницы. Скролл остаётся, если он
	   ИЗМЕНИЛ страницу (``delta.changed`` — сработала бесконечная лента или
	   ленивая загрузка: следующие шаги зависят от контента, которого без него
	   нет) или если он последний в записи (записью закончили на прокрутке —
	   значит она и была целью).

	6. **Переключение и закрытие вкладок не попадают в макрос.**
	   ``tab_id`` — это последние символы ``target_id`` конкретного прогона.
	   При повторе такого таргета либо нет, либо, что хуже, он есть и
	   принадлежит чужой вкладке. Закрыть чужую вкладку хуже, чем не закрыть
	   свою, поэтому шаг выбрасывается, а в ``issues`` пишется, что сценарий был
	   многовкладочный.

	7. **Индексы вычищаются.** В шаге остаётся ``hint`` (полный
	   ``describe_handle``) и справочный ``recorded_index``, которым никто не
	   действует. Если у шага над элементом хендла нет — сценарий помечается
	   ``incomplete``, а шаг остаётся с явным ``unreplayable``: молча выкинуть
	   шаг из сценария нельзя, это изменило бы его смысл.

	Параметризация. Введённый текст становится именованной переменной; имя
	выводится из подписи поля (``aria-label`` -> accessible name, куда AccName
	уже включил ``<label>`` и ``placeholder`` -> ``name``/``id`` -> тег).
	Значение сохраняется как значение по умолчанию — КРОМЕ полей, похожих на
	секрет: там пишется только имя переменной, ``value: null`` и
	``required: true``, и ``macro.run`` без явно переданного значения такой
	сценарий не запустит. URL первой навигации тоже параметризуется
	(``start_url``): подменить точку входа — самая частая правка макроса.
	"""
	entries = [e for e in (entries or []) if isinstance(e, dict)]
	pool = _VarPool()
	dropped: list[dict[str, Any]] = []
	issues: list[str] = []

	def drop(entry: dict[str, Any], reason_key: str, extra: str = '') -> None:
		dropped.append(
			{
				'seq': entry.get('seq'),
				'tool': entry.get('tool'),
				'why': _DROP_REASONS.get(reason_key, reason_key) + (f' ({extra})' if extra else ''),
			}
		)

	# --- проход 1: отсев ---------------------------------------------------- #
	kept: list[dict[str, Any]] = []
	for entry in entries:
		tool = str(entry.get('tool') or '')
		if not tool or tool in OBSERVATIONS:
			drop(entry, 'observation')
			continue
		if (entry.get('outcome') or 'ok') == 'error':
			drop(entry, 'error', str(entry.get('error') or '')[:120])
			continue
		if tool in TAB_TOOLS:
			drop(entry, 'tab')
			issues.append(
				f'step {entry.get("seq")}: `{tool}` dropped — the recording used more than one tab; '
				f'the replayed macro stays in the tab it starts in'
			)
			continue
		kept.append(entry)

	# --- проход 2: скролл ---------------------------------------------------- #
	pruned: list[dict[str, Any]] = []
	for i, entry in enumerate(kept):
		if str(entry.get('tool')) != 'scroll':
			pruned.append(entry)
			continue
		is_last = i == len(kept) - 1
		if _delta_flag(entry, 'changed') is True or is_last:
			pruned.append(entry)
		else:
			drop(entry, 'scroll_mechanics')

	# --- проход 3: no_effect ------------------------------------------------- #
	survivors: list[dict[str, Any]] = []
	for entry in pruned:
		tool = str(entry.get('tool'))
		if tool in ('browser_navigate', 'navigate', 'scroll'):
			survivors.append(entry)
			continue
		if _delta_flag(entry, 'no_effect') is True or (entry.get('outcome') == 'noop'):
			drop(entry, 'no_effect')
			continue
		survivors.append(entry)

	# --- проход 4: склейка вводов в одно поле -------------------------------- #
	collapsed: list[dict[str, Any]] = []
	for entry in survivors:
		tool = str(entry.get('tool'))
		if tool not in ('browser_type', 'input') or not collapsed:
			collapsed.append(entry)
			continue
		prev = collapsed[-1]
		same_tool = str(prev.get('tool')) in ('browser_type', 'input')
		same_field = _handle_key(prev.get('handle')) is not None and _handle_key(prev.get('handle')) == _handle_key(
			entry.get('handle')
		)
		clears = bool((entry.get('params') or {}).get('clear', True))
		if same_tool and same_field and clears:
			drop(prev, 'superseded')
			collapsed[-1] = entry
		else:
			collapsed.append(entry)

	# --- проход 5: сборка шагов ---------------------------------------------- #
	steps: list[dict[str, Any]] = []
	incomplete = False
	seen_navigate = False

	for entry in collapsed:
		tool = str(entry.get('tool'))
		handle = entry.get('handle') if isinstance(entry.get('handle'), dict) else None
		params = _params_for_macro(entry)

		if tool in ('browser_type', 'input'):
			raw = params.get('text')
			text = '' if raw is None else str(raw)
			label, source = _field_label(handle)
			secret = _is_secret(label, handle, text)
			var = pool.add(
				label,
				text,
				source=source,
				secret=bool(secret),
				note=f'{secret}; not recorded' if secret else None,
			)
			params['text'] = {'$var': var}

		nav_url: str | None = None
		if tool in ('browser_navigate', 'navigate'):
			url = params.get('url')
			if isinstance(url, str) and url:
				nav_url = url
				if not seen_navigate:
					seen_navigate = True
					var = pool.add('start_url', url, source='entry point of the recording', secret=False)
					params['url'] = {'$var': var}

		step: dict[str, Any] = {
			'n': len(steps) + 1,
			'seq': entry.get('seq'),
			'tool': tool,
			'params': params,
			'expect': _expectation(entry),
		}
		if nav_url:
			# Литеральный адрес дублируется в шаг СПЕЦИАЛЬНО. После параметризации
			# ``params['url']`` — это ``{'$var': 'start_url'}``, и любой обход
			# макроса в поисках URL (например, доменный гейт в server.py, который
			# обязан проверить макрос ДО первого шага) увидел бы там словарь и
			# прошёл мимо. Сам шаг исполняется по переменной, не по этому полю.
			step['url'] = nav_url
		if entry.get('url_before'):
			step['url_before'] = entry['url_before']
		if isinstance(entry.get('page'), dict):
			step['page'] = entry['page']

		if tool in ELEMENT_TOOLS:
			if handle:
				step['hint'] = {k: v for k, v in handle.items() if k != 'reachable'}
				step['recorded_index'] = handle.get('index', (entry.get('params') or {}).get('index'))
			else:
				incomplete = True
				step['unreplayable'] = (
					'no element handle was recorded for this step, and replaying it by the recorded index '
					'would click whatever now sits at that index'
				)
				issues.append(f'step {step["n"]} (`{tool}`): no handle recorded, macro cannot be replayed as is')
		steps.append(step)

	first = next((e for e in collapsed if isinstance(e.get('page'), dict) or e.get('url_before')), None)
	recorded_on: dict[str, Any] = {}
	if first:
		recorded_on['url'] = first.get('url_before')
		page = first.get('page') or {}
		if page.get('title'):
			recorded_on['title'] = page['title']
		if page.get('viewport'):
			recorded_on['viewport'] = page['viewport']

	return {
		'name': _safe_name(name),
		'version': MACRO_VERSION,
		'created': datetime.now(timezone.utc).isoformat(timespec='seconds'),
		'source': {'entries': len(entries), 'kept': len(steps), 'dropped': len(dropped)},
		'recorded_on': recorded_on,
		'vars': pool.vars,
		'steps': steps,
		'dropped': dropped,
		'issues': issues,
		'incomplete': incomplete,
	}


# --------------------------------------------------------------------------- #
# Макросы на диске
# --------------------------------------------------------------------------- #


def save_macro(macro: dict[str, Any], *, path: Path | None = None) -> Path:
	"""Записать макрос. С отступами: его читают и правят руками."""
	target = Path(path) if path is not None else macro_path(str(macro.get('name') or 'macro'))
	target.parent.mkdir(parents=True, exist_ok=True)
	tmp = target.with_suffix(target.suffix + '.tmp')
	tmp.write_text(json.dumps(_sanitize(macro), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
	tmp.replace(target)  # атомарная замена: недописанный макрос никто не прочитает
	return target


def load_macro(name: str, *, path: Path | None = None) -> dict[str, Any]:
	target = Path(path) if path is not None else macro_path(name)
	return json.loads(target.read_text(encoding='utf-8'))


def list_macros() -> list[str]:
	directory = home() / 'macros'
	if not directory.is_dir():
		return []
	return sorted(p.stem for p in directory.glob('*.json'))


def journals() -> list[Path]:
	directory = home() / 'journals'
	if not directory.is_dir():
		return []
	return sorted(directory.glob('*.jsonl'))


# --------------------------------------------------------------------------- #
# Самопроверка
# --------------------------------------------------------------------------- #

if __name__ == '__main__':
	import asyncio
	import sys
	import tempfile
	import threading
	from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

	CDP_URL = os.getenv('BU_MCP_CDP_URL', 'http://127.0.0.1:9222')

	PAGE_HTML = (
		'<!doctype html><meta charset=utf-8><title>bu-mcp journal selfcheck</title>'
		'<form><label for=q>Search query</label>'
		'<input id=q name=q placeholder="what are you looking for"><button>Go</button></form>'
	)

	def serve(html: str) -> tuple[ThreadingHTTPServer, str]:
		"""Локальный http-сервер: нужен настоящий URL, который переживает reload."""

		class Handler(BaseHTTPRequestHandler):
			def do_GET(self):  # noqa: N802
				body = html.encode()
				self.send_response(200)
				self.send_header('Content-Type', 'text/html; charset=utf-8')
				self.send_header('Content-Length', str(len(body)))
				self.send_header('Cache-Control', 'no-store')
				self.end_headers()
				self.wfile.write(body)

			def log_message(self, *args):  # noqa: A003
				pass

		httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
		threading.Thread(target=httpd.serve_forever, daemon=True).start()
		return httpd, f'http://127.0.0.1:{httpd.server_address[1]}/page.html'

	def head(text: str) -> None:
		print(f'\n{"=" * 74}\n{text}\n{"=" * 74}')

	def ok(text: str) -> None:
		print(f'  PASS  {text}')

	def fail(text: str) -> None:
		print(f'  FAIL  {text}')

	async def main() -> int:
		from browser_use.browser import BrowserProfile, BrowserSession
		from browser_use.browser.events import CloseTabEvent
		from bu_mcp import resolve as resolve_mod

		failures = 0
		os.environ['BU_MCP_HOME'] = tempfile.mkdtemp(prefix='bu-mcp-journal-')
		reset(session_id='selfcheck')
		print(f'storage: {home()}')

		httpd, base = serve(PAGE_HTML)
		print(f'test page: {base}')

		session = BrowserSession(browser_profile=BrowserProfile(cdp_url=CDP_URL, is_local=True))
		await session.start()
		foreign = {t.target_id for t in await session.get_tabs()}
		await session.navigate_to(base, new_tab=True)
		my_tab = session.agent_focus_target_id
		# `navigate_to(new_tab=True)` переиспользует уже открытый about:blank, и
		# тогда «своя» вкладка на самом деле чужая. Работать в ней можно, закрывать
		# её в конце — нельзя.
		owns_tab = my_tab not in foreign
		print(f'own tab: {my_tab} (наша: {owns_tab})')

		try:
			head('1. record: наблюдения не пишутся, действия пишутся, ошибка не всплывает')
			record({'tool': 'browser_state', 'params': {}})
			record({'tool': 'browser_click', 'params': {'index': 5}, 'outcome': 'ok'})
			# Заведомо несериализуемое значение внутри записи.
			record({'tool': 'browser_click', 'params': {'index': object()}, 'outcome': 'ok'})
			record({})  # без tool
			entries = read()
			print(f'  в журнале {len(entries)} записей: {[e["tool"] for e in entries]}')
			if len(entries) == 2 and all(e['tool'] == 'browser_click' for e in entries):
				ok('browser_state отфильтрован, несериализуемый params не уронил запись')
			else:
				failures += 1
				fail(f'ожидали 2 записи browser_click, получили {entries}')

			head('2. capture: хендл + title + viewport одним заходом')
			await session.get_browser_state_summary(include_screenshot=False, cached=False)
			resolve_mod.snapshot_handles(session)
			smap = session._cached_selector_map or {}
			field = next(
				(i for i, n in smap.items() if (n.node_name or '').lower() == 'input'),
				None,
			)
			assert field is not None, f'не нашли <input> среди {len(smap)} элементов'
			ctx = await capture(session, field)
			print(f'  url_before={ctx.get("url_before")}')
			print(f'  page={ctx.get("page")}')
			print(f'  handle={ {k: ctx["handle"][k] for k in ("tag", "xpath", "accessible_name")} }')
			if ctx.get('page', {}).get('viewport') and ctx.get('handle', {}).get('xpath'):
				ok('capture отдал вьюпорт, заголовок и составной хендл')
			else:
				failures += 1
				fail(f'capture отдал неполный контекст: {ctx}')

			head('3. capture на мёртвой сессии молчит, а не бросает')
			broken = await capture(object(), 1)
			print(f'  capture(<не сессия>) -> {broken}')
			ok('исключение погашено') if broken == {} else fail('вернулось что-то неожиданное')

			head('4. to_macro: схлопывание')
			reset(session_id='selfcheck-2')
			h_field = dict(ctx['handle'])
			h_pwd = dict(h_field, xpath='/html/body/form/input[2]', accessible_name='Password', attributes={'name': 'password'})
			h_btn = dict(h_field, tag='button', xpath='/html/body/form/button', accessible_name='Submit')
			page = ctx.get('page')

			def w(tool, **kw):
				kw.setdefault('page', page)
				kw.setdefault('url_before', base)
				record({'tool': tool, **kw})

			w('browser_navigate', params={'url': base}, url_after=base, delta={'changed': True, 'status': 'changed'})
			w('browser_state', params={})
			w(
				'browser_type',
				params={'index': 1, 'text': 'cha', 'clear': True},
				handle=h_field,
				delta={'changed': True, 'status': 'changed'},
			)
			w(
				'browser_type',
				params={'index': 1, 'text': '', 'clear': True},
				handle=h_field,
				delta={'changed': True, 'status': 'changed'},
			)
			w(
				'browser_type',
				params={'index': 1, 'text': 'chair', 'clear': True},
				handle=h_field,
				delta={'changed': True, 'status': 'changed'},
			)
			w(
				'browser_type',
				params={'index': 2, 'text': 'hunter2', 'clear': True},
				handle=h_pwd,
				delta={'changed': True, 'status': 'changed'},
			)
			w('scroll', params={'down': True, 'pages': 1}, delta={'changed': False, 'status': 'no-change'})
			w(
				'browser_click',
				params={'index': 9},
				handle=h_btn,
				delta={'changed': False, 'status': 'no-change', 'no_effect': True},
			)
			w(
				'browser_click',
				params={'index': 9},
				handle=h_btn,
				url_after=base + '?sent=1',
				delta={'changed': True, 'status': 'changed', 'fields': {'url': [base, base + '?sent=1']}},
			)
			w('browser_click', params={'index': 9}, handle=h_btn, outcome='error', error='STALE ELEMENT HANDLE [9]')
			w('close', params={'tab_id': 'AB12'})

			macro = to_macro(read(), name='selfcheck demo')
			print(json.dumps(macro, ensure_ascii=False, indent=2)[:2400])

			tools_kept = [s['tool'] for s in macro['steps']]
			checks = [
				('наблюдение выкинуто', 'browser_state' not in tools_kept),
				('ошибочный шаг выкинут', sum(1 for s in macro['steps'] if s['tool'] == 'browser_click') == 1),
				(
					'no_effect клик выкинут',
					all(s['expect'].get('changed') is not False for s in macro['steps'] if s['tool'] == 'browser_click'),
				),
				('три ввода склеены в один', sum(1 for t in tools_kept if t == 'browser_type') == 2),
				(
					'склеился именно последний',
					macro['vars'].get('search_field', macro['vars'].get(next(iter(macro['vars'])))) is not None,
				),
				('скролл-механика выкинута', 'scroll' not in tools_kept),
				('close выкинут', 'close' not in tools_kept),
				('индексов в params нет', all('index' not in s['params'] for s in macro['steps'])),
				('хендлы на месте', all('hint' in s for s in macro['steps'] if s['tool'] in ELEMENT_TOOLS)),
				('url-переход записан в expect', any(s['expect'].get('url_changed') for s in macro['steps'])),
			]
			secret_vars = [n for n, v in macro['vars'].items() if v.get('secret')]
			checks.append(('пароль стал секретной переменной', bool(secret_vars)))
			checks.append(
				(
					'пароля нет в макросе ни в каком виде',
					'hunter2' not in json.dumps(macro, ensure_ascii=False),
				)
			)
			checks.append(('текст поля параметризован', any('chair' == v.get('value') for v in macro['vars'].values())))
			checks.append(('start_url параметризован', 'start_url' in macro['vars']))
			for label, good in checks:
				if good:
					ok(label)
				else:
					failures += 1
					fail(label)

			head('5. save/load макроса')
			path = save_macro(macro)
			again = load_macro(macro['name'])
			print(f'  {path}')
			if again == json.loads(json.dumps(_sanitize(macro))):
				ok('макрос сохраняется и читается без потерь')
			else:
				failures += 1
				fail('round-trip макроса не сошёлся')

		finally:
			head('cleanup')
			try:
				# Строго по СВОЕМУ id и только если вкладка действительно наша:
				# `navigate_to(new_tab=True)` в browser-use переиспользует уже
				# открытый about:blank, и тогда «своя» вкладка на самом деле чужая.
				if my_tab and owns_tab:
					await session.event_bus.dispatch(CloseTabEvent(target_id=my_tab))
					print(f'  своя вкладка {my_tab} закрыта')
				elif my_tab:
					print(f'  вкладка {my_tab} была чужой (переиспользована) — НЕ закрываем')
			except Exception as exc:  # noqa: BLE001
				print(f'  вкладку закрыть не удалось: {exc}')
			left = {t.target_id for t in await session.get_tabs()}
			print(f'  чужих вкладок было {len(foreign)}, осталось {len(left & foreign)}')
			await session.stop()
			httpd.shutdown()

		head(f'ИТОГ: {"всё зелено" if failures == 0 else f"{failures} провал(ов)"}')
		return 1 if failures else 0

	sys.exit(asyncio.run(main()))
