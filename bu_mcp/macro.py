"""Повтор записанного сценария без модели в цикле.

Каждый шаг макроса — это тройка «резолв по сохранённому хендлу -> действие ->
сверка последствий». Ни один из трёх элементов не опциональный, и порядок
именно такой.

**Резолв.** Индекс из записи НЕ используется как идентичность — ни на секунду.
``resolve_index`` умеет короткий путь «индекс есть в текущей карте и узел жив»,
и на нём при повторе можно очень красиво кликнуть не туда: после перезагрузки
индекс 12 существует почти всегда, просто принадлежит другому элементу. Поэтому
здесь резолв всегда идёт по составному хендлу (``hint``) через сентинельный
индекс, то есть через лестницу переидентификации целиком: backendNodeId ->
xpath (с гардом по URL и по accessible name) -> accessible name -> уникальный
атрибут. Найденный узел дополнительно сверяется с хендлом по тегу, роли и
имени: лестница может закончиться на ступени, где совпадение слабее, чем нам бы
хотелось, и лишняя проверка стоит один сравниваемый словарь.

**Сверка.** Мало кликнуть — надо убедиться, что получилось то же, что и при
записи. В журнале лежит дельта момента записи; если тогда клик менял URL, а
сейчас не меняет, то это не успех, а расхождение, и молчать о нём нельзя. При
этом сравнивается ФОРМА последствия, а не числа: ``digest``, ``nodes``,
``rendered`` между прогонами не совпадут никогда (реклама, время суток, ширина
окна), а «URL сменился на такой-то», «открылась вкладка», «появился диалог»,
«страница вообще отреагировала» — совпадут.

**strict=True по умолчанию.** Любое расхождение ранга ``stop`` останавливает
сценарий на этом шаге. Формулировка, которая этого стоит: *a wrong cached click
is worse than a slow click*. Весь остальной слой выбран fail closed, и повтор —
самое опасное место из всех: тут вообще нет модели, которая заметила бы, что
страница выглядит не так.

**Ожидания — из ``waiting``, не паузы.** Перед каждым шагом
``wait_for_page_ready``; если шаг при записи вызывал навигацию — после действия
``wait_after_navigation`` с baseline, снятым ДО действия. Записанные тайминги в
макрос не попадают и здесь не используются: на другой машине и другой сети они
другие. Единственная реальная пауза в файле — 120 мс между перепробами пустой
дельты, ровно как в ``server._delta_end``: «сразу ничего не изменилось» бывает у
нормального клика, который дёрнул fetch.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ['StepFailed', 'run', 'probe', 'delta', 'DEFAULT_STEP_TIMEOUT', 'DEFAULT_SETTLE_TIMEOUT']

DEFAULT_STEP_TIMEOUT = 15.0
DEFAULT_SETTLE_TIMEOUT = 8.0
DEFAULT_NAV_TIMEOUT = 10.0

#: Столько раз перепроверяем «дельта пуста, а ожидали изменение», с такой паузой.
#: Те же числа, что в ``server.DELTA_RECHECKS`` и по той же причине: эскалация
#: включается только на подозрительной ветке.
RECHECKS = 2
RECHECK_DELAY = 0.12

#: Сентинельный индекс: заведомо отсутствует в любой карте, поэтому короткий путь
#: ``resolve_index`` («индекс жив — берём его») гарантированно не срабатывает и
#: элемент ищется ТОЛЬКО по хендлу. См. модульный докстринг.
_SENTINEL_INDEX = -1

#: Признаки, изменение которых значит «страница отреагировала». Зеркало
#: ``server.DELTA_SIGNIFICANT``: ``scroll`` и ``active`` сюда не входят —
#: и то и другое меняется от механики действия, а не от реакции страницы.
_SIGNIFICANT = ('url', 'title', 'nodes', 'rendered', 'interactive', 'doc', 'dialogs', 'digest', 'tabs')

#: Точки перелома адаптивной вёрстки. Не «магические числа с потолка»: это
#: дефолтные брейкпоинты Tailwind/Bootstrap, вокруг которых и написана
#: подавляющая часть медиазапросов в вебе. Пересечение любой из них между
#: записью и повтором означает, что страница могла собрать ДРУГОЙ набор
#: элементов (навигация уехала в гамбургер, таблица стала карточками).
_BREAKPOINTS = (480, 640, 768, 1024, 1280, 1536)

#: Имя инструмента -> имя действия в реестре browser-use.
_REGISTRY_ALIAS = {
	'browser_navigate': 'navigate',
	'browser_click': 'click',
	'browser_type': 'input',
	'browser_hover': 'hover',
}

#: Действия, которым нужен разрешённый элемент.
_NEEDS_ELEMENT = frozenset({'click', 'input', 'hover', 'select_dropdown', 'upload_file'})

#: Единственный по-настоящему опасный «тихий нооп» апстрима на пути повтора:
#: узел исчез между резолвом и действием, а browser-use вернул это без ``error``.
#: Полная таблица живёт в ``server.NOOP_MARKERS`` и используется, если сервер
#: импортируется; этот регексп — минимум, который работает и без него.
_STALE_INDEX_RE = re.compile(r'Element index \d+ not available - page may have changed')


class StepFailed(Exception):
	"""Шаг сценария не выполнился или выполнился не так, как записано.

	Несёт достаточно, чтобы понять, где именно всё встало: номер шага, сам шаг и
	список расхождений. ``run`` по умолчанию её ЛОВИТ и возвращает в конверте —
	контракт обещает dict, а не исключение; пробросить наружу можно через
	``raise_on_failure=True``.
	"""

	def __init__(
		self,
		message: str,
		*,
		n: int | None = None,
		tool: str | None = None,
		discrepancies: list[dict[str, Any]] | None = None,
	) -> None:
		super().__init__(message)
		self.n = n
		self.tool = tool
		self.discrepancies = discrepancies or []


# --------------------------------------------------------------------------- #
# Проба страницы
# --------------------------------------------------------------------------- #

#: Запасная проба на случай, если ``bu_mcp.server`` не импортируется (его правят
#: параллельно, и он тянет за собой mcp). Считает те же признаки, что и
#: серверная, но короче: нам от неё нужен только БУЛЕВ ответ «изменилось ли
#: что-нибудь», а не сопоставимые между прогонами числа.
_LOCAL_PROBE_JS = """(() => {
  const LIMIT = 20000;
  const se = document.scrollingElement || document.documentElement || document.body;
  const nodes = document.getElementsByTagName('*');
  const total = nodes.length;
  const n = Math.min(total, LIMIT);
  const INTER = { A: 1, BUTTON: 1, INPUT: 1, SELECT: 1, TEXTAREA: 1, SUMMARY: 1, DETAILS: 1 };
  let digest = 0, rendered = 0, interactive = 0;
  for (let i = 0; i < n; i++) {
    const el = nodes[i];
    const w = el.offsetWidth | 0, h = el.offsetHeight | 0;
    const shown = (w || h || el.getClientRects().length) ? 1 : 0;
    if (shown) rendered++;
    const tag = el.tagName;
    if (INTER[tag] === 1) interactive++;
    let v = tag.length * 131 + tag.charCodeAt(0);
    if (shown) v += (el.offsetLeft | 0) * 3 + (el.offsetTop | 0) * 5 + w * 11 + h * 13;
    const cn = el.className;
    if (typeof cn === 'string') v += cn.length * 17;
    if (typeof el.value === 'string') {
      const s = el.value;
      v += s.length * 19 + (s.charCodeAt(0) | 0) * 3 + (s.charCodeAt(s.length - 1) | 0) * 7;
    }
    if (typeof el.selectedIndex === 'number') v += (el.selectedIndex + 2) * 43;
    if (el.checked) v += 23;
    if (el.disabled) v += 29;
    if (el.open) v += 31;
    const ae = el.getAttribute('aria-expanded');
    if (ae) v += ae === 'true' ? 37 : 41;
    digest = (digest + (i + 1) * (v | 0)) % 2147483647;
  }
  return {
    url: location.href,
    title: document.title || '',
    nodes: total,
    rendered: rendered,
    interactive: interactive,
    doc: (se ? Math.round(se.scrollHeight) : 0) + 'x' + (se ? Math.round(se.scrollWidth) : 0),
    dialogs: document.querySelectorAll('dialog[open],[role=dialog],[role=alertdialog]').length,
    digest: digest,
    vw: Math.round(window.innerWidth || 0),
    vh: Math.round(window.innerHeight || 0),
  };
})()"""


def _probe_source() -> tuple[str, str]:
	"""JS пробы. Предпочитаем серверную — одна логика на запись и на повтор."""
	try:
		server = importlib.import_module('bu_mcp.server')
		js = getattr(server, '_DELTA_PROBE_JS', None)
		if isinstance(js, str) and js.strip():
			return js, 'server'
	except Exception as exc:  # noqa: BLE001
		logger.debug('macro: bu_mcp.server unavailable, using the local probe: %r', exc)
	return _LOCAL_PROBE_JS, 'local'


async def _evaluate(session: Any, expression: str) -> Any:
	cdp = await session.get_or_create_cdp_session(session.agent_focus_target_id, focus=False)
	res = await cdp.cdp_client.send.Runtime.evaluate(
		params={'expression': expression, 'returnByValue': True, 'awaitPromise': True},
		session_id=cdp.session_id,
	)
	if 'exceptionDetails' in res:
		raise RuntimeError(str(res['exceptionDetails'])[:300])
	return res.get('result', {}).get('value')


async def probe(session: Any) -> dict[str, Any]:
	"""Один снимок признаков страницы + число вкладок. Fail-open: ``{'ok': False}``."""
	expression, source = _probe_source()
	out: dict[str, Any] = {'ok': False, 'probe': source}
	try:
		value = await asyncio.wait_for(_evaluate(session, expression), timeout=3.0)
		if isinstance(value, dict):
			out.update(value)
			out['ok'] = True
	except Exception as exc:  # noqa: BLE001
		logger.debug('macro: page probe failed: %r', exc)
	try:
		out['tabs'] = len(await session.get_tabs())
	except Exception:  # noqa: BLE001
		pass
	return out


def delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
	"""Дельта в формате ``server._delta_verdict``: ``changed`` / ``status`` / ``fields``.

	Нужна не только сверке, но и записи: тест, который записывает сценарий без
	MCP-сервера, обязан класть в журнал дельту той же формы, иначе ``to_macro``
	соберёт неверные ожидания.
	"""
	if not (before.get('ok') and after.get('ok')):
		return {'changed': None, 'status': 'unavailable'}
	fields: dict[str, Any] = {}
	changed = False
	for key in _SIGNIFICANT:
		if key not in before or key not in after or before[key] == after[key]:
			continue
		fields[key] = 'changed' if key == 'digest' else [before[key], after[key]]
		changed = True
	out: dict[str, Any] = {'changed': changed, 'status': 'changed' if changed else 'no-change'}
	if fields:
		out['fields'] = fields
	if not changed:
		out['no_effect'] = True
	return out


def _consequence(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
	"""Наблюдённое последствие в тех же терминах, что ``journal._expectation``."""
	d = delta(before, after)
	out: dict[str, Any] = {'changed': d.get('changed'), 'status': d.get('status')}
	if before.get('ok') and after.get('ok'):
		out['url_changed'] = before.get('url') != after.get('url')
		out['url_after'] = after.get('url')
		out['title_changed'] = before.get('title') != after.get('title')
		out['dialogs_changed'] = before.get('dialogs') != after.get('dialogs')
	if before.get('tabs') is not None and after.get('tabs') is not None:
		out['tabs_delta'] = int(after['tabs']) - int(before['tabs'])
	return out


# --------------------------------------------------------------------------- #
# Сверка ожидания с наблюдением
# --------------------------------------------------------------------------- #


def _same_page(a: str | None, b: str | None) -> str:
	"""``same`` / ``query`` / ``different``: насколько два URL — одна страница."""
	if a == b:
		return 'same'
	if not a or not b:
		return 'different'
	try:
		from urllib.parse import urlsplit

		pa, pb = urlsplit(a), urlsplit(b)
	except Exception:  # noqa: BLE001
		return 'different'
	if (pa.scheme, pa.netloc, pa.path.rstrip('/')) == (pb.scheme, pb.netloc, pb.path.rstrip('/')):
		return 'query'
	return 'different'


def _compare(expect: dict[str, Any], observed: dict[str, Any]) -> list[dict[str, Any]]:
	"""Расхождения между записанным и наблюдённым последствием.

	Асимметрия здесь намеренная и её стоит проговорить.

	*Пропавшее* последствие — расхождение ранга ``stop``: при записи клик вёл на
	другую страницу / открывал вкладку / вообще что-то менял, а сейчас нет. Это
	значит, что шаг НЕ сработал, и продолжать сценарий бессмысленно: дальше он
	будет искать элементы страницы, до которой не доехал.

	*Лишнее* изменение — ранга ``note``. Проба чувствительна до геометрии
	каждого узла, и на живой странице она видит вращающийся баннер, доехавший
	шрифт и подгруженную аватарку. Считать это провалом шага — значит сделать
	strict-режим неприменимым к реальному вебу. Единственное исключение —
	НЕЗАПЛАНИРОВАННАЯ навигация: если при записи клик со страницы не уводил, а
	сейчас увёл, то мы уже не там, где думаем, и это ранг ``stop``.
	"""
	out: list[dict[str, Any]] = []

	def add(field: str, expected: Any, got: Any, severity: str, why: str) -> None:
		out.append({'field': field, 'expected': expected, 'observed': got, 'severity': severity, 'why': why})

	if observed.get('status') == 'unavailable':
		add(
			'probe',
			'readable page state',
			'unavailable',
			'stop',
			'could not read the page before/after the action (CDP probe failed), so nothing was verified; '
			'refusing to call an unverified step a success',
		)
		return out

	want_changed = expect.get('changed')
	got_changed = observed.get('changed')
	if want_changed is True and got_changed is False:
		add(
			'changed',
			True,
			False,
			'stop',
			'when this step was recorded the page reacted; on replay nothing measurably changed '
			'(same URL, tab count, element counts, layout and form state) — the action was most likely '
			'swallowed by an overlay, a disabled control or a handler that returned early',
		)
	elif want_changed is False and got_changed is True:
		add('changed', False, True, 'note', 'the page changed more than it did at record time (ads, lazy content, animations)')

	want_nav = bool(expect.get('url_changed'))
	got_nav = bool(observed.get('url_changed'))
	if want_nav and not got_nav:
		add(
			'url_changed',
			True,
			False,
			'stop',
			f'recorded: this step navigated to {expect.get("url_after")!r}; on replay the URL did not change '
			f'(still {observed.get("url_after")!r})',
		)
	elif got_nav and not want_nav:
		add(
			'url_changed',
			False,
			True,
			'stop',
			f'this step did not navigate at record time, but on replay it left the page for '
			f'{observed.get("url_after")!r} — the rest of the macro would run on the wrong document',
		)
	elif want_nav and got_nav and expect.get('url_after'):
		verdict = _same_page(expect.get('url_after'), observed.get('url_after'))
		if verdict == 'different':
			add(
				'url_after',
				expect.get('url_after'),
				observed.get('url_after'),
				'stop',
				'the step navigated somewhere else than at record time',
			)
		elif verdict == 'query':
			add(
				'url_after',
				expect.get('url_after'),
				observed.get('url_after'),
				'note',
				'same page, different query string or fragment',
			)

	want_tabs = expect.get('tabs_delta')
	got_tabs = observed.get('tabs_delta')
	if want_tabs is not None and got_tabs is not None and int(want_tabs) != int(got_tabs):
		# Ранг note, а не stop, и это не мягкотелость. Число вкладок — свойство
		# ВСЕГО браузера, а не страницы: пользователь открыл письмо в соседнем
		# окне, расширение подняло свою вкладку — и записанная дельта уже врёт.
		# Проверено на живом Chrome с чужими вкладками: ложное срабатывание
		# ловится на первом же прогоне. Настоящий «клик открывал вкладку, а
		# теперь не открывает» всё равно будет пойман — следующим шагом, который
		# не найдёт своего элемента в текущей вкладке (макрос по построению
		# остаётся в той вкладке, в которой стартовал).
		add(
			'tabs_delta',
			want_tabs,
			got_tabs,
			'note',
			'the recorded step changed the number of open tabs and the replay did not — but tab count is a '
			'property of the whole browser, not of this page, so a foreign tab opening during either run '
			'produces exactly this difference',
		)

	if expect.get('dialogs_changed') and observed.get('dialogs_changed') is False:
		add('dialogs_changed', True, False, 'stop', 'a dialog appeared or closed at record time; on replay it did not')

	if expect.get('title_changed') and observed.get('title_changed') is False:
		add('title_changed', True, False, 'note', 'the document title changed at record time but not on replay')

	return out


# --------------------------------------------------------------------------- #
# Переменные
# --------------------------------------------------------------------------- #


def _materialize(value: Any, values: dict[str, Any]) -> Any:
	"""Подставить значения переменных в ``{'$var': 'name'}`` на любой глубине."""
	if isinstance(value, dict):
		if set(value) == {'$var'}:
			name = str(value['$var'])
			if name not in values:
				raise KeyError(name)
			return values[name]
		return {k: _materialize(v, values) for k, v in value.items()}
	if isinstance(value, list):
		return [_materialize(v, values) for v in value]
	return value


def _resolve_vars(macro: dict[str, Any], override: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
	declared = macro.get('vars') or {}
	override = dict(override or {})
	values: dict[str, Any] = {}
	missing: list[str] = []
	for name, spec in declared.items():
		spec = spec if isinstance(spec, dict) else {'value': spec}
		if name in override:
			values[name] = override.pop(name)
		elif spec.get('value') is not None:
			values[name] = spec['value']
		elif spec.get('required') or spec.get('secret'):
			missing.append(name)
		else:
			values[name] = spec.get('value')
	# Переменные, которых в макросе нет, но которые передали, — не ошибка:
	# макрос могли править руками.
	values.update(override)
	return values, missing


def _mask(macro: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
	declared = macro.get('vars') or {}
	out = {}
	for name, value in values.items():
		spec = declared.get(name)
		secret = isinstance(spec, dict) and spec.get('secret')
		out[name] = '***' if secret else value
	return out


# --------------------------------------------------------------------------- #
# Резолв шага
# --------------------------------------------------------------------------- #


def _identity_mismatch(node: Any, hint: dict[str, Any]) -> str | None:
	"""Тот ли это элемент. Возвращает описание расхождения или ``None``.

	Лестница переидентификации сама по себе может закончиться на ступени, где
	совпадение слабее записанного: например, ``backend_node_id`` ступень тег не
	проверяет вовсе. Проверка стоит одно сравнение словарей, а цена ошибки —
	клик не туда, поэтому она безусловная.
	"""
	tag = (hint.get('tag') or '').lower()
	got_tag = (getattr(node, 'node_name', '') or '').lower()
	if tag and got_tag and tag != got_tag:
		return f'expected <{tag}>, resolved to <{got_tag}>'

	ax = getattr(node, 'ax_node', None)
	want_name = (hint.get('accessible_name') or '').strip()
	got_name = ((ax.name if ax else None) or '').strip()
	if want_name and got_name and want_name != got_name:
		return f'expected accessible name {want_name!r}, resolved element is named {got_name!r}'
	if want_name and not got_name:
		return f'expected accessible name {want_name!r}, resolved element has no accessible name'

	want_role = (hint.get('role') or '').strip()
	got_role = ((ax.role if ax else None) or '').strip()
	if want_role and got_role and want_role != got_role:
		return f'expected role {want_role!r}, resolved element has role {got_role!r}'
	return None


async def _resolve_step(
	session: Any,
	hint: dict[str, Any],
	*,
	deadline: float,
	settle_timeout: float,
) -> tuple[Any, int, dict[str, Any]]:
	"""Найти элемент шага по хендлу. Ждём появления, но не угадываем.

	Опрос до ``deadline`` включён только для ``StaleHandleError``: «элемента ещё
	нет» — нормальное состояние страницы, которая дорисовывается, и ровно это
	Playwright называет auto-waiting. ``AmbiguousHandleError`` не повторяется
	вовсе: неоднозначность сама не рассосётся, а ждать, пока один из двух
	одинаковых элементов исчезнет, — это и есть угадывание.
	"""
	resolve_mod = importlib.import_module('bu_mcp.resolve')
	waiting_mod = importlib.import_module('bu_mcp.waiting')
	attempts = 0
	last: Exception | None = None

	while True:
		attempts += 1
		try:
			node = await resolve_mod.resolve_index(session, _SENTINEL_INDEX, hint=hint)
		except resolve_mod.AmbiguousHandleError as exc:
			raise StepFailed(
				f'AMBIGUOUS ELEMENT: the recorded handle matches more than one element on the page now. '
				f'{_scrub(exc)} Nothing was done — acting on the wrong element is worse than not acting.'
			) from exc
		except resolve_mod.StaleHandleError as exc:
			last = exc
			if time.monotonic() >= deadline:
				raise StepFailed(
					f'ELEMENT NOT FOUND after {attempts} attempt(s) within the step budget. {_scrub(exc)} '
					f'Recorded as <{hint.get("tag")}> {hint.get("accessible_name")!r} at {hint.get("xpath")} '
					f'on {hint.get("url")}.'
				) from exc
			await waiting_mod.wait_for_page_ready(session, timeout=min(settle_timeout, max(0.5, deadline - time.monotonic())))
			# Шаг опроса растёт: каждая попытка стоит полной пересборки DOM-карты
			# (``resolve._refresh``), и долбить её двадцать раз подряд — чистый
			# налог. Первые попытки частые (элемент обычно появляется сразу),
			# дальше реже.
			await asyncio.sleep(min(1.0, 0.15 * (1.6 ** (attempts - 1))))
			continue
		except Exception as exc:  # noqa: BLE001
			raise StepFailed(f'cannot resolve the recorded element: {type(exc).__name__}: {exc}') from exc

		mismatch = _identity_mismatch(node, hint)
		if mismatch is not None:
			raise StepFailed(
				f'WRONG ELEMENT: re-identification returned an element that does not match the recorded '
				f'handle ({mismatch}). Refusing to act on it. Recorded: <{hint.get("tag")}> '
				f'{hint.get("accessible_name")!r} at {hint.get("xpath")}.'
			)

		live_index = session.get_selector_index(node)
		info: dict[str, Any] = {'attempts': attempts}
		try:
			resolution = dict(resolve_mod.last_resolution(session) or {})
			# Сентинель наружу не показываем: в телеметрии должен стоять индекс
			# ЗАПИСИ, иначе читателю нечего с ним соотнести.
			resolution['index'] = hint.get('index')
			info['resolution'] = resolution
		except Exception:  # noqa: BLE001
			pass
		if last is not None:
			info['waited'] = True
		return node, live_index, info


def _scrub(exc: Exception) -> str:
	"""Убрать из сообщения resolve сентинельный индекс: наружу он смысла не несёт."""
	return re.sub(r'handle \[-1\]', 'handle', str(exc))


# --------------------------------------------------------------------------- #
# Выполнение действия
# --------------------------------------------------------------------------- #

_TOOLS: Any = None
_FS: Any = None


def _tools() -> tuple[Any, Any]:
	global _TOOLS, _FS
	if _TOOLS is None:
		import tempfile
		from pathlib import Path

		from browser_use.filesystem.file_system import FileSystem
		from browser_use.tools.service import Tools

		_TOOLS = Tools()
		_FS = FileSystem(base_dir=Path(tempfile.mkdtemp(prefix='bu-mcp-macro-')))
	return _TOOLS, _FS


def _result_text(name: str, result: Any) -> str:
	"""Текст результата; невыполненное действие — исключение, а не «успех».

	Тот же принцип, что в ``server._action_result_text``: browser-use в шести
	местах возвращает «не получилось» без ``error``. Если сервер импортируется,
	берём его полную таблицу маркеров; если нет — остаётся один регексп на
	«индекс протух», единственный, который реально встречается на пути повтора.
	"""
	error = getattr(result, 'error', None)
	if error:
		raise StepFailed(f'{name} failed: {error}')
	parts = [p for p in (getattr(result, 'extracted_content', None), getattr(result, 'long_term_memory', None)) if p]
	joined = '\n'.join(parts)

	marker = None
	try:
		server = importlib.import_module('bu_mcp.server')
		marker = server.BuMcpServer._classify_noop(name, joined)
	except Exception:  # noqa: BLE001
		if _STALE_INDEX_RE.search(joined):
			marker = 'stale-index'
	if marker is not None:
		code = getattr(marker, 'code', marker)
		raise StepFailed(f'{name} did NOT run ({code}); browser-use reported it as a normal result: {joined[:300]}')
	return parts[0] if parts else f'{name}: ok'


async def _hover(session: Any, node: Any) -> str:
	"""Физическое наведение курсора: единственный способ включить CSS ``:hover``.

	Сжатая версия ``server._hover_point`` + диспатча: та же геометрия (CDP-сессия
	ФРЕЙМА узла, ``scrollIntoViewIfNeeded``, ``get_element_coordinates``) и тот же
	отказ зажимать точку во вьюпорт. Синтетический ``MouseEvent`` тут не годится:
	он не двигает указатель браузера и ``:hover`` не включает.
	"""
	cdp = await session.cdp_client_for_node(node)
	sid = cdp.session_id
	try:
		await cdp.cdp_client.send.DOM.scrollIntoViewIfNeeded(params={'backendNodeId': node.backend_node_id}, session_id=sid)
		await asyncio.sleep(0.05)
	except Exception:  # noqa: BLE001
		pass

	metrics = await cdp.cdp_client.send.Page.getLayoutMetrics(session_id=sid)
	vw = float(metrics['layoutViewport']['clientWidth'])
	vh = float(metrics['layoutViewport']['clientHeight'])
	rect = await session.get_element_coordinates(node.backend_node_id, cdp)
	if rect is None:
		raise StepFailed('hover: the element has no geometry (display:none, detached or zero-sized)')
	x, y, w, h = float(rect.x), float(rect.y), float(rect.width), float(rect.height)
	x0, y0, x1, y1 = max(0.0, x), max(0.0, y), min(vw, x + w), min(vh, y + h)
	if x1 - x0 < 1.0 or y1 - y0 < 1.0:
		raise StepFailed(
			f'hover: the element sits at ({x:g}, {y:g}) {w:g}x{h:g} px, entirely outside the '
			f'{vw:g}x{vh:g} viewport. Refusing to clamp the pointer to an arbitrary visible pixel.'
		)
	cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
	for px, py in ((cx, cy), (min(x1 - 0.5, cx + 1.0), min(y1 - 0.5, cy + 1.0))):
		await cdp.cdp_client.send.Input.dispatchMouseEvent(
			params={'type': 'mouseMoved', 'x': px, 'y': py, 'button': 'none', 'buttons': 0, 'clickCount': 0},
			session_id=sid,
		)
		await asyncio.sleep(0.04)
	return f'hovered at {cx:g},{cy:g}'


async def _ensure_target(session: Any, home: Any) -> str | None:
	"""Держать макрос в той вкладке, в которой он стартовал.

	browser-use при отсоединении цели молча переключает ``agent_focus`` на
	соседнюю вкладку (issue #5529 — тот же механизм, что сервер разбирает в
	``_reconcile_new_tab``). Для повтора это худший из возможных сюрпризов: все
	последующие резолвы, клики и пробы уезжают на ЧУЖУЮ страницу, а по логам
	всё выглядит нормально. Поймано на живом Chrome: во время прогона соседний
	процесс закрыл свою вкладку, фокус переехал на example.com, и шаг «клик по
	кнопке» отчитался бы об успехе на совершенно другом документе.

	Поэтому: дрейф фокуса чинится (возвращаемся в свою вкладку) и записывается в
	warnings; исчезновение своей вкладки — жёсткий отказ. Макрос своих вкладок не
	открывает и чужих не закрывает, поэтому «вернуться домой» здесь всегда
	правильное действие.
	"""
	current = session.agent_focus_target_id
	if not home or current == home:
		return None
	try:
		ids = {str(t.target_id) for t in await session.get_tabs()}
	except Exception:  # noqa: BLE001
		ids = set()
	if str(home) not in ids:
		raise StepFailed(
			f'the tab this macro started in ({home}) is gone and the browser moved focus to {current}. '
			f'Refusing to keep replaying in a tab the macro never opened.'
		)
	await session.get_or_create_cdp_session(home, focus=True)
	return f'browser focus drifted to tab {current} and was moved back to {home}'


async def _act(session: Any, tool: str, params: dict[str, Any], node: Any, live_index: int | None) -> str:
	"""Выполнить одно действие макроса."""
	name = _REGISTRY_ALIAS.get(tool, tool)
	if name == 'hover':
		if node is None:
			raise StepFailed('hover without a resolved element')
		return await _hover(session, node)

	tools, fs = _tools()
	payload = dict(params)
	if name in _NEEDS_ELEMENT:
		if live_index is None:
			raise StepFailed(f'{name} without a resolved element')
		payload['index'] = live_index
	elif name == 'scroll' and live_index is not None:
		payload['index'] = live_index
	elif name == 'scroll':
		payload.pop('index', None)

	try:
		result = await tools.registry.execute_action(name, payload, browser_session=session, file_system=fs)
	except StepFailed:
		raise
	except Exception as exc:  # noqa: BLE001
		raise StepFailed(f'{name} raised {type(exc).__name__}: {exc}') from exc
	return _result_text(name, result)


# --------------------------------------------------------------------------- #
# Предусловия
# --------------------------------------------------------------------------- #


def _viewport_verdict(recorded: dict[str, Any] | None, current: dict[str, Any] | None) -> dict[str, Any] | None:
	"""Сравнить вьюпорт записи и повтора.

	Почему это вообще проверяется. Адаптивная вёрстка меняет НАБОР элементов, а
	не только их расположение: на 375px навигация уезжает в гамбургер, таблица
	превращается в карточки, половина кнопок физически не рендерится. Сценарий,
	записанный на 1440px, в таком окне упадёт на «элемента нет», и без этой
	проверки оператор не получит ни одного намёка, почему.

	Почему это не всегда ``stop``. Разница в пару десятков пикселей (полоса
	прокрутки, панель расширений) не меняет ничего, и падать на ней значило бы
	сделать макросы неприменимыми. Останавливаемся только когда между записью и
	повтором пересечена хотя бы одна стандартная точка перелома — то есть когда
	у страницы был реальный повод собрать другой DOM.
	"""
	if not recorded or not current:
		return None
	rw, cw = int(recorded.get('width') or 0), int(current.get('width') or 0)
	rh, ch = int(recorded.get('height') or 0), int(current.get('height') or 0)
	if not rw or not cw:
		return None
	crossed = [b for b in _BREAKPOINTS if (rw < b) != (cw < b)]
	if crossed:
		return {
			'field': 'viewport',
			'expected': f'{rw}x{rh}',
			'observed': f'{cw}x{ch}',
			'severity': 'stop',
			'why': (
				f'the macro was recorded at {rw}px wide and is being replayed at {cw}px, crossing the '
				f'responsive breakpoint(s) {crossed}. Media queries at those widths change which elements '
				f'exist at all, so recorded handles may be missing for layout reasons rather than page '
				f'state. Resize the window, or pass strict=False if you know the page is not responsive.'
			),
		}
	if (rw, rh) != (cw, ch):
		return {
			'field': 'viewport',
			'expected': f'{rw}x{rh}',
			'observed': f'{cw}x{ch}',
			'severity': 'note',
			'why': 'viewport differs from the recording, but no responsive breakpoint is crossed',
		}
	return None


# --------------------------------------------------------------------------- #
# Прогон
# --------------------------------------------------------------------------- #


async def run(
	session: Any,
	macro: dict[str, Any],
	*,
	vars: dict[str, Any] | None = None,  # noqa: A002 — имя из контракта
	strict: bool = True,
	raise_on_failure: bool = False,
	step_timeout: float = DEFAULT_STEP_TIMEOUT,
	settle_timeout: float = DEFAULT_SETTLE_TIMEOUT,
) -> dict[str, Any]:
	"""Прогнать макрос. Без модели, без индексов, без записанных таймингов.

	Args:
		session: живой ``BrowserSession``. Вкладку выбирает вызывающий: макрос
			своих вкладок не открывает и чужих не закрывает.
		macro: словарь из ``journal.to_macro`` (или прочитанный
			``journal.load_macro``).
		vars: значения переменных. Обязательны для тех, у кого
			``required``/``secret`` — секреты в макросе не хранятся.
		strict: ``True`` (по умолчанию) — первое расхождение ранга ``stop``
			останавливает сценарий. ``False`` — идём дальше, копя расхождения,
			но шаг, который не удалось выполнить физически, всё равно
			пропускается (выполнять его нечем).
		raise_on_failure: пробросить ``StepFailed`` наружу вместо конверта.
			По умолчанию выключено: контракт обещает вернуть dict.

	Returns:
		``{'ok', 'steps', 'failed_at', 'vars', 'discrepancies', 'warnings',
		'elapsed', 'probe'}``. ``failed_at`` — номер шага (нумерация с 1);
		``0`` означает «упало на предусловии, до первого шага»; ``None`` —
		не упало.
	"""
	waiting_mod = importlib.import_module('bu_mcp.waiting')
	started = time.perf_counter()
	steps_in = list(macro.get('steps') or [])
	report: dict[str, Any] = {
		'ok': False,
		'name': macro.get('name'),
		'steps': [],
		'failed_at': None,
		'vars': {},
		'discrepancies': [],
		'warnings': [],
		'probe': _probe_source()[1],
	}

	def finish() -> dict[str, Any]:
		report['elapsed'] = round(time.perf_counter() - started, 3)
		return report

	def stop(n: int, message: str, discrepancies: list[dict[str, Any]] | None = None) -> dict[str, Any]:
		report['ok'] = False
		report['failed_at'] = n
		report['error'] = message
		if discrepancies:
			report['discrepancies'].extend(discrepancies)
		if raise_on_failure:
			raise StepFailed(message, n=n, discrepancies=discrepancies)
		return finish()

	if not steps_in:
		report['ok'] = True
		return finish()

	# --- предусловие 1: переменные ------------------------------------------ #
	values, missing = _resolve_vars(macro, vars)
	report['vars'] = _mask(macro, values)
	if missing:
		return stop(
			0,
			f'macro {macro.get("name")!r} needs values for {missing}: these are recorded as secrets, so the '
			f'macro deliberately does not carry them. Pass them as run(vars={{...}}).',
		)

	# --- предусловие 2: невоспроизводимые шаги ------------------------------ #
	broken = [s for s in steps_in if s.get('unreplayable')]
	if broken and strict:
		first = broken[0]
		return stop(
			int(first.get('n') or 0),
			f'macro is incomplete: step {first.get("n")} (`{first.get("tool")}`) has no recorded element '
			f'handle ({first.get("unreplayable")}). Refusing to run it by index.',
		)
	if broken:
		report['warnings'].append(f'{len(broken)} step(s) have no handle and will be skipped')

	# --- предусловие 3: вьюпорт --------------------------------------------- #
	# Вкладка, в которой макрос стартовал. Всё, что дальше, должно происходить
	# именно в ней — см. ``_ensure_target``.
	home_target = getattr(session, 'agent_focus_target_id', None)
	current_probe = await probe(session)
	recorded_viewport = (macro.get('recorded_on') or {}).get('viewport')
	current_viewport = {'width': current_probe.get('vw'), 'height': current_probe.get('vh')} if current_probe.get('vw') else None
	if current_viewport is None:
		try:
			raw = await _evaluate(session, '({vw: innerWidth, vh: innerHeight})')
			current_viewport = {'width': raw.get('vw'), 'height': raw.get('vh')}
		except Exception:  # noqa: BLE001
			current_viewport = None
	verdict = _viewport_verdict(recorded_viewport, current_viewport)
	if verdict is None and not recorded_viewport:
		report['warnings'].append('no viewport recorded with this macro: responsive mismatches cannot be detected')
	elif verdict is not None:
		report['discrepancies'].append(dict(verdict, step=0))
		if verdict['severity'] == 'stop' and strict:
			return stop(0, verdict['why'])
		report['warnings'].append(verdict['why'])

	# --- шаги ---------------------------------------------------------------- #
	for step in steps_in:
		n = int(step.get('n') or (len(report['steps']) + 1))
		tool = str(step.get('tool') or '')
		expect = step.get('expect') or {}
		record: dict[str, Any] = {'n': n, 'tool': tool, 'status': 'ok'}
		step_started = time.perf_counter()

		if step.get('unreplayable'):
			record.update(status='skipped', error=step['unreplayable'])
			report['steps'].append(record)
			continue

		try:
			params = _materialize(step.get('params') or {}, values)
		except KeyError as exc:
			record.update(status='failed', error=f'missing variable {exc}')
			report['steps'].append(record)
			return stop(n, f'step {n}: missing variable {exc}')

		deadline = time.monotonic() + step_timeout
		try:
			drift = await _ensure_target(session, home_target)
			if drift:
				report['warnings'].append(f'step {n}: {drift}')
			# Ожидание — не пауза: ждём, пока страница успокоится сама.
			await waiting_mod.wait_for_page_ready(session, timeout=settle_timeout)

			node = live_index = None
			if step.get('hint'):
				node, live_index, info = await _resolve_step(
					session, step['hint'], deadline=deadline, settle_timeout=settle_timeout
				)
				record['resolved_index'] = live_index
				record.update(info)

			before = await probe(session)
			baseline = await waiting_mod.navigation_baseline(session) if expect.get('url_changed') else None

			record['action'] = await _act(session, tool, params, node, live_index)

			# Действие могло увести фокус (апстрим переключается на новую вкладку
			# сам). Сверку делаем на СВОЕЙ странице, иначе она сравнит наш «до» с
			# чужим «после» и объявит расхождением всё подряд.
			drift = await _ensure_target(session, home_target)
			if drift:
				record['focus_restored'] = drift
				report['warnings'].append(f'step {n}: {drift} (after the action)')

			if baseline is not None:
				record['waiting'] = await waiting_mod.wait_after_navigation(
					session, timeout=DEFAULT_NAV_TIMEOUT, baseline=baseline
				)
			else:
				record['waiting'] = await waiting_mod.wait_for_page_ready(session, timeout=settle_timeout)

			after = await probe(session)
			observed = _consequence(before, after)
			# Та же эскалация, что у сервера: пустая дельта перепроверяется, потому
			# что клик мог дёрнуть fetch и перерисоваться через сотню миллисекунд.
			probes = 1
			while expect.get('changed') and observed.get('changed') is False and probes <= RECHECKS:
				await asyncio.sleep(RECHECK_DELAY)
				after = await probe(session)
				observed = _consequence(before, after)
				probes += 1
			record['probes'] = probes
			record['expect'] = expect
			record['observed'] = observed

			discrepancies = _compare(expect, observed)
			for item in discrepancies:
				item['step'] = n
			record['discrepancies'] = discrepancies
			report['discrepancies'].extend(discrepancies)

			blocking = [d for d in discrepancies if d['severity'] == 'stop']
			if blocking:
				record['status'] = 'mismatch'
				record['elapsed'] = round(time.perf_counter() - step_started, 3)
				report['steps'].append(record)
				message = f'step {n} (`{tool}`) did not reproduce: ' + '; '.join(f'{d["field"]}: {d["why"]}' for d in blocking)
				if strict:
					# Расхождения уже в report['discrepancies'] — второй раз не кладём.
					return stop(n, message)
				report['warnings'].append(message)
				continue

		except StepFailed as exc:
			record.update(status='failed', error=str(exc))
			record['elapsed'] = round(time.perf_counter() - step_started, 3)
			report['steps'].append(record)
			message = f'step {n} (`{tool}`): {exc}'
			if strict:
				return stop(n, message, exc.discrepancies)
			report['warnings'].append(message)
			continue
		except Exception as exc:  # noqa: BLE001
			record.update(status='failed', error=f'{type(exc).__name__}: {exc}')
			record['elapsed'] = round(time.perf_counter() - step_started, 3)
			report['steps'].append(record)
			message = f'step {n} (`{tool}`): {type(exc).__name__}: {exc}'
			if strict:
				return stop(n, message)
			report['warnings'].append(message)
			continue

		record['elapsed'] = round(time.perf_counter() - step_started, 3)
		report['steps'].append(record)

	failed = [s for s in report['steps'] if s['status'] in ('failed', 'mismatch', 'skipped')]
	report['ok'] = not failed
	report['failed_at'] = int(failed[0]['n']) if failed else None
	if failed:
		# Дошли до конца только в нестрогом режиме: сводка вместо одного текста
		# ошибки, потому что провалов могло быть несколько.
		report['error'] = f'{len(failed)} of {len(report["steps"])} step(s) did not reproduce: ' + '; '.join(
			f'[{s["n"]}] {s["tool"]} {s["status"]}' for s in failed
		)
	return finish()


# --------------------------------------------------------------------------- #
# Самопроверка на живом Chrome
# --------------------------------------------------------------------------- #

if __name__ == '__main__':
	import json
	import os
	import sys
	import tempfile
	import threading
	from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

	CDP_URL = os.getenv('BU_MCP_CDP_URL', 'http://127.0.0.1:9222')

	FORM_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>bu-mcp macro selfcheck</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:2rem auto}
label{display:block;margin:.6rem 0 .2rem}input,select,button{font-size:1rem;padding:.3rem}
#result{margin-top:1.4rem;min-height:2rem}</style></head>
<body>
<h1>Order form</h1>
<form id="form" onsubmit="return false">
  <label for="item">Item name</label>
  <input id="item" name="item" placeholder="what to order">
  <label for="size">Size</label>
  <select id="size" name="size">
    <option value="">choose a size</option>
    <option>Small</option><option>Medium</option><option>Large</option>
  </select>
  <label for="secret">Password</label>
  <input id="secret" name="password" type="password">
  <button type="button" id="add">Add item</button>
</form>
<div id="result"></div>
<script>
document.getElementById('add').addEventListener('click', function () {
  if (window.__inert) { return; }          // «обработчик вернулся рано»
  var item = document.getElementById('item').value;
  var size = document.getElementById('size').value;
  document.getElementById('result').innerHTML =
    '<p id="receipt">Added: ' + item + ' (' + size + ')</p>';
});
</script>
</body></html>
"""

	def serve(html: str) -> tuple[ThreadingHTTPServer, str]:
		"""Настоящий http-сервер: нужен URL, который переживает перезагрузку.

		``data:``-страница здесь не годится — её нельзя перезагрузить так, чтобы
		получился новый документ с тем же URL, а именно это и надо проверить.
		"""

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
		return httpd, f'http://127.0.0.1:{httpd.server_address[1]}/order.html'

	def head(text: str) -> None:
		print(f'\n{"=" * 74}\n{text}\n{"=" * 74}')

	def ok(text: str) -> None:
		print(f'  PASS  {text}')

	def fail(text: str) -> None:
		print(f'  FAIL  {text}')

	async def main() -> int:  # noqa: C901
		from browser_use.browser import BrowserProfile, BrowserSession
		from browser_use.browser.events import CloseTabEvent
		from bu_mcp import journal as journal_mod
		from bu_mcp import resolve as resolve_mod

		failures = 0
		os.environ['BU_MCP_HOME'] = tempfile.mkdtemp(prefix='bu-mcp-macro-')
		journal_mod.reset(session_id='macro-selfcheck')
		httpd, base = serve(FORM_HTML)
		print(f'storage:   {journal_mod.home()}')
		print(f'test page: {base}')

		session = BrowserSession(browser_profile=BrowserProfile(cdp_url=CDP_URL, is_local=True))
		await session.start()
		foreign = {t.target_id for t in await session.get_tabs()}
		await session.navigate_to(base, new_tab=True)
		my_tab = session.agent_focus_target_id
		# См. выше: browser-use переиспользует свободный about:blank.
		owns_tab = my_tab not in foreign
		print(f'own tab:   {my_tab}  (наша: {owns_tab}, чужих вкладок: {len(foreign)})')

		waiting_mod = importlib.import_module('bu_mcp.waiting')

		async def pin() -> None:
			"""Прибить фокус к СВОЕЙ вкладке.

			В этом Chrome живут чужие вкладки, и browser-use при любом отсоединении
			цели молча переезжает на соседнюю. Тест, который этого не проверяет,
			рано или поздно кликает в чужую страницу — один раз в ходе разработки
			именно так и произошло: клик уехал на example.com и увёл сессию на
			iana.org.
			"""
			if session.agent_focus_target_id != my_tab:
				print(f'  !! фокус уехал на {session.agent_focus_target_id}, возвращаем на {my_tab}')
				try:
					await session.get_or_create_cdp_session(my_tab, focus=True)
				except Exception as exc:  # noqa: BLE001
					raise AssertionError(
						f'своя вкладка {my_tab} исчезла ({exc}); в этом Chrome работает кто-то ещё — '
						f'запусти самопроверку на отдельном порту через BU_MCP_CDP_URL'
					) from exc

		async def js(expr: str):
			await pin()
			return await _evaluate(session, expr)

		async def where() -> str:
			return await js('location.href')

		async def index_of(el_id: str) -> int:
			"""Индекс элемента с таким id в свежей карте — как его получил бы агент."""
			await pin()
			await session.get_browser_state_summary(include_screenshot=False, cached=False)
			resolve_mod.snapshot_handles(session)
			smap = session._cached_selector_map or {}
			hits = [i for i, n in smap.items() if (n.attributes or {}).get('id') == el_id]
			assert len(hits) == 1, f'ожидали ровно один #{el_id} в карте из {len(smap)}, нашли {hits}'
			return hits[0]

		async def reload() -> None:
			"""Настоящая перезагрузка: новый документ на том же URL.

			``navigate_to`` на тот же адрес browser-use может свернуть в no-op, и
			тогда главный тест ничего не доказывает — backendNodeId остаются
			живыми. ``Page.reload`` гарантированно строит документ заново.
			"""
			await pin()
			baseline = await waiting_mod.navigation_baseline(session)
			cdp = await session.get_or_create_cdp_session(my_tab, focus=True)
			await cdp.cdp_client.send.Page.reload(params={'ignoreCache': True}, session_id=cdp.session_id)
			await waiting_mod.wait_after_navigation(session, timeout=10.0, baseline=baseline)
			await session.get_browser_state_summary(include_screenshot=False, cached=False)

		async def do(tool: str, el_id: str | None, params: dict) -> None:
			"""Выполнить действие и записать его в журнал — как это делает сервер."""
			index = await index_of(el_id) if el_id else None
			ctx = await journal_mod.capture(session, index)
			node = live = None
			if index is not None:
				node = await resolve_mod.resolve_index(session, index)
				live = session.get_selector_index(node)
			before = await probe(session)
			text = await _act(session, tool, params, node, live)
			# То же, что делает ``run``: действие могло увести фокус на новую
			# вкладку, и тогда «после» снялось бы с чужой страницы.
			await pin()
			await waiting_mod.wait_for_page_ready(session, timeout=5.0)
			after = await probe(session)
			entry = {
				'tool': tool,
				'params': dict(params, **({'index': index} if index is not None else {})),
				'delta': delta(before, after),
				'url_after': after.get('url'),
				'action': text,
			}
			entry.update(ctx)
			journal_mod.record(entry)
			print(f'    записан {tool:<16} -> {(entry["delta"].get("status"))}, url={entry["url_after"]}')

		def show(result: dict) -> None:
			for st in result['steps']:
				res = st.get('resolution') or {}
				print(
					f'    [{st["n"]}] {st["tool"]:<16} {st["status"]:<9} '
					f'index {res.get("index")}->{res.get("resolved_index")} level={res.get("level")} '
					f'changed={(st.get("observed") or {}).get("changed")}'
				)
			if result.get('error'):
				print(f'    error: {result["error"][:300]}')

		try:
			# =============================================================== #
			head('1. ЗАПИСЬ: ввод -> выбор в селекте -> ввод пароля -> клик -> результат')
			print(f'  стартовый url: {await where()}')
			await do('browser_type', 'item', {'text': 'Sunset chair', 'clear': True})
			await do('select_dropdown', 'size', {'text': 'Large'})
			await do('browser_type', 'secret', {'text': 'hunter2', 'clear': True})
			await do('browser_click', 'add', {})
			receipt = await js("(() => { const e = document.getElementById('receipt'); return e && e.textContent; })()")
			print(f'  на странице: {receipt!r}')
			if receipt == 'Added: Sunset chair (Large)':
				ok('сценарий записан и при записи он реально сработал')
			else:
				failures += 1
				fail(f'запись не дала результата: {receipt!r}')

			entries = journal_mod.read()
			macro = journal_mod.to_macro(entries, name='order-form')
			print(f'  журнал: {len(entries)} записей -> макрос: {len(macro["steps"])} шагов')
			print(f'  переменные: {json.dumps(macro["vars"], ensure_ascii=False)}')
			if 'hunter2' in json.dumps(macro, ensure_ascii=False):
				failures += 1
				fail('ПАРОЛЬ УТЁК В МАКРОС')
			else:
				ok('пароля в макросе нет ни в каком виде')

			# =============================================================== #
			head('2. ГЛАВНЫЙ ТЕСТ: перезагрузка страницы -> все индексы и backendNodeId мертвы')
			old_handles = [(s['hint']['backend_node_id'], s['hint']['index']) for s in macro['steps'] if s.get('hint')]
			print(f'  записанные (backendNodeId, index): {old_handles}')
			await reload()  # тот же URL, НОВЫЙ документ
			after_reload = await js(
				"(() => ({receipt: !!document.getElementById('receipt'), "
				"item: document.getElementById('item').value, "
				"size: document.getElementById('size').value}))()"
			)
			print(f'  после перезагрузки: {after_reload}')

			# Доказательство, что старые хендлы действительно протухли.
			await session.get_browser_state_summary(include_screenshot=False, cached=False)
			live_backends = {n.backend_node_id for n in (session._cached_selector_map or {}).values()}
			stale = [b for b, _ in old_handles if b not in live_backends]
			print(f'  из {len(old_handles)} записанных backendNodeId живы 0, протухли {len(stale)}')
			if len(stale) == len(old_handles):
				ok('все записанные backendNodeId недействительны — повтор пойдёт только через xpath/имя')
			else:
				failures += 1
				fail('часть backendNodeId пережила перезагрузку, тест не доказывает того, что должен')

			result = await run(session, macro, vars={'password': 'hunter2'}, strict=True)
			print(f'  run -> ok={result["ok"]} failed_at={result["failed_at"]}')
			show(result)
			receipt = await js("(() => { const e = document.getElementById('receipt'); return e && e.textContent; })()")
			print(f'  на странице после повтора: {receipt!r}')

			levels = {(s.get('resolution') or {}).get('level') for s in result['steps'] if s.get('resolution')}
			if result['ok'] and receipt == 'Added: Sunset chair (Large)':
				ok(f'макрос отработал целиком через переидентификацию (ступени: {sorted(levels - {None})})')
			else:
				failures += 1
				fail(f'макрос не доехал: ok={result["ok"]} receipt={receipt!r}')
			if levels and levels <= {'xpath', 'accessible_name', 'attribute'}:
				ok('ни один шаг не разрешился по протухшему индексу — только по хендлу')
			else:
				failures += 1
				fail(f'подозрительные ступени резолва: {levels}')

			# =============================================================== #
			head('3. НЕГАТИВ A: кнопка исчезла -> strict останавливается на нужном шаге')
			await reload()
			await js("(() => { document.getElementById('add').remove(); return 1; })()")
			print('  кнопка #add удалена из документа')
			result = await run(session, macro, vars={'password': 'hunter2'}, strict=True)
			click_step = next(st['n'] for st in macro['steps'] if st['tool'] == 'browser_click')
			print(f'  run -> ok={result["ok"]} failed_at={result["failed_at"]} (шаг клика = {click_step})')
			show(result)
			receipt = await js("(() => !!document.getElementById('receipt'))()")
			item = await js("(() => document.getElementById('item').value)()")
			print(f'  на странице: receipt={receipt}, поле item={item!r}')
			if not result['ok'] and result['failed_at'] == click_step and 'NOT FOUND' in (result.get('error') or ''):
				ok('остановились ровно на шаге клика, с объяснением «элемент не найден»')
			else:
				failures += 1
				fail(f'ожидали остановку на шаге {click_step}: {result["failed_at"]} / {result.get("error")}')
			if not receipt and item == 'Sunset chair':
				ok('предыдущие шаги выполнены, ни во что постороннее не кликнули')
			else:
				failures += 1
				fail(f'побочный эффект: receipt={receipt} item={item!r}')

			# =============================================================== #
			head('4. НЕГАТИВ B: кнопка стала неоднозначной -> отказ вместо угадывания')
			await reload()
			await js(
				"(() => { const f = document.getElementById('form'); const b = document.getElementById('add'); "
				"b.removeAttribute('id'); "
				"const a = document.createElement('div'), c = document.createElement('div'); "
				'a.appendChild(b.cloneNode(true)); c.appendChild(b.cloneNode(true)); b.remove(); '
				'f.appendChild(a); f.appendChild(c); return 1; })()'
			)
			count = await js("(() => document.querySelectorAll('button').length)()")
			print(f'  кнопка продублирована: «Add item» на странице {count} шт., обе на новых xpath, id снят')
			result = await run(session, macro, vars={'password': 'hunter2'}, strict=True)
			print(f'  run -> ok={result["ok"]} failed_at={result["failed_at"]}')
			show(result)
			receipt = await js("(() => !!document.getElementById('receipt'))()")
			if (
				not result['ok']
				and result['failed_at'] == click_step
				and 'AMBIGUOUS' in (result.get('error') or '')
				and not receipt
			):
				ok('на неоднозначности отказ, ни одна из двух кнопок не нажата')
			else:
				failures += 1
				fail(f'ожидали AMBIGUOUS без клика: {result.get("error")} receipt={receipt}')

			# =============================================================== #
			head('5. НЕГАТИВ C: кнопка на месте, но обработчик молчит -> ловится СВЕРКОЙ ДЕЛЬТЫ')
			await reload()
			await js('(() => { window.__inert = true; return 1; })()')
			print('  обработчик кнопки теперь возвращается сразу (страница не отреагирует)')
			result = await run(session, macro, vars={'password': 'hunter2'}, strict=True)
			print(f'  run -> ok={result["ok"]} failed_at={result["failed_at"]}')
			show(result)
			blocking = [d for d in result['discrepancies'] if d['severity'] == 'stop']
			if not result['ok'] and result['failed_at'] == click_step and blocking and blocking[0]['field'] == 'changed':
				ok('элемент найден и нажат, но записанного последствия нет — расхождение, а не успех')
			else:
				failures += 1
				fail(f'сверка дельты не сработала: {result["failed_at"]} {blocking}')

			# =============================================================== #
			head('6. strict=False на той же поломке: идём дальше и копим расхождения')
			result = await run(session, macro, vars={'password': 'hunter2'}, strict=False)
			done = [s for s in result['steps'] if s['status'] == 'ok']
			print(f'  run -> ok={result["ok"]} шагов всего {len(result["steps"])}, из них ok {len(done)}')
			print(f'  расхождений: {len(result["discrepancies"])}')
			if not result['ok'] and len(result['steps']) == len(macro['steps']) and result['discrepancies']:
				ok('прошли весь сценарий, вернули список расхождений')
			else:
				failures += 1
				fail(f'strict=False повёл себя не так: {result}')

			# =============================================================== #
			head('7. Секрет без значения: сценарий не стартует вовсе')
			result = await run(session, macro, strict=True)
			print(f'  run(без vars) -> ok={result["ok"]} failed_at={result["failed_at"]}')
			print(f'  error: {result.get("error")}')
			if not result['ok'] and result['failed_at'] == 0 and 'password' in (result.get('error') or ''):
				ok('без переданного пароля не сделано ни одного шага')
			else:
				failures += 1
				fail('макрос стартовал без обязательной переменной')

			# =============================================================== #
			head('8. Вьюпорт: пересечение брейкпоинта останавливает до первого шага')
			narrow = dict(macro, recorded_on=dict(macro['recorded_on'], viewport={'width': 375, 'height': 800}))
			result = await run(session, narrow, vars={'password': 'hunter2'}, strict=True)
			print(f'  run -> failed_at={result["failed_at"]}')
			print(f'  error: {(result.get("error") or "")[:300]}')
			if not result['ok'] and result['failed_at'] == 0 and 'breakpoint' in (result.get('error') or ''):
				ok('несовпадение вьюпорта поймано на предусловии, а не через «элемента нет»')
			else:
				failures += 1
				fail(f'вьюпорт не проверен: {result.get("error")}')

			# =============================================================== #
			head('9. Шаг навигации: start_url + wait_after_navigation при повторе')
			journal_mod.reset(session_id='macro-selfcheck-nav')
			other = base + '?start=1'
			await session.navigate_to(other)
			await do('browser_navigate', None, {'url': base})
			await do('browser_click', 'add', {})
			nav_macro = journal_mod.to_macro(journal_mod.read(), name='nav-macro')
			print(
				f'  макрос: {[st["tool"] for st in nav_macro["steps"]]}, '
				f'url первого шага -> {nav_macro["steps"][0]["params"]["url"]}'
			)
			await session.navigate_to(other)
			print(f'  ушли на {await where()}')
			result = await run(session, nav_macro, strict=True)
			print(f'  run -> ok={result["ok"]} failed_at={result["failed_at"]}')
			show(result)
			navigated = (result['steps'][0].get('waiting') or {}).get('navigated')
			receipt = await js("(() => { const e = document.getElementById('receipt'); return e && e.textContent; })()")
			print(f'  url после повтора: {await where()}, receipt={receipt!r}, navigated={navigated}')
			if result['ok'] and navigated and receipt and (await where()) == base:
				ok('навигация воспроизведена, переход подтверждён wait_after_navigation, клик доехал')
			else:
				failures += 1
				fail(f'навигационный макрос не отработал: ok={result["ok"]} navigated={navigated} receipt={receipt!r}')
			if 'start_url' in nav_macro['vars']:
				ok('точка входа вынесена в переменную start_url')
			else:
				failures += 1
				fail(f'start_url не параметризован: {list(nav_macro["vars"])}')

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
