"""``browser_hover`` / ``browser_click`` / ``browser_type``: ``InteractActionsMixin``.

Вынесено из server.py при разбиении на подмодули (docs/WORKLOG.md).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import mcp.types as types

from browser_use.browser import BrowserSession
from browser_use.mcp.server_shared import HOVER_HIT_JS, HOVER_MOVE_GAP, ToolError, bu_mcp_module


class InteractActionsMixin:
	# --- browser_hover ------------------------------------------------------ #

	async def _hover_point(self, session: BrowserSession, node: Any, index: int) -> dict[str, Any]:
		"""Куда физически везти курсор. Некуда — ЖЁСТКАЯ ошибка, а не «куда-нибудь».

		Геометрия берётся тем же путём, что у клика в
		``browser_use/browser/watchdogs/default_action_watchdog.py``:
		CDP-сессия ФРЕЙМА узла (``cdp_client_for_node`` — для кросс-доменного
		iframe координаты фрейм-локальные и в корневую сессию их слать нельзя),
		``DOM.scrollIntoViewIfNeeded``, затем ``get_element_coordinates``
		(getContentQuads -> getBoxModel -> getBoundingClientRect).

		Где мы РАСХОДИМСЯ с апстримом, намеренно:

		* апстрим при пустой геометрии падает в ``element.click()`` из JS. Для
		  наведения такой фолбэк бессмысленен: синтетическое событие не двигает
		  внутреннюю позицию мыши, ``:hover`` не включается — это и есть тот самый
		  тихий ложный успех, ради отсутствия которого написан весь сервер;
		* апстрим при точке вне вьюпорта делает
		  ``center = max(0, min(viewport - 1, center))`` — молча зажимает и кликает
		  по СЛУЧАЙНОМУ видимому пикселю, который к элементу отношения не имеет.
		  Здесь вместо зажима считается ПЕРЕСЕЧЕНИЕ прямоугольника элемента с
		  вьюпортом, и точка берётся в его центре: она по построению лежит и
		  внутри элемента, и внутри вьюпорта. Пересечение пустое — ошибка.
		"""
		try:
			cdp_session = await session.cdp_client_for_node(node)
		except Exception as exc:
			raise ToolError(f'hover on [{index}] failed: no CDP session for the element frame ({exc}).') from exc
		session_id = cdp_session.session_id
		backend_node_id = node.backend_node_id

		metrics = await cdp_session.cdp_client.send.Page.getLayoutMetrics(session_id=session_id)
		vw = float(metrics['layoutViewport']['clientWidth'])
		vh = float(metrics['layoutViewport']['clientHeight'])

		scrolled = True
		try:
			await cdp_session.cdp_client.send.DOM.scrollIntoViewIfNeeded(
				params={'backendNodeId': backend_node_id}, session_id=session_id
			)
			await asyncio.sleep(0.05)
		except Exception:
			scrolled = False

		rect = await session.get_element_coordinates(backend_node_id, cdp_session)
		if rect is None:
			raise ToolError(
				f'Cannot hover [{index}]: the element has NO geometry (getContentQuads, getBoxModel and '
				f'getBoundingClientRect all came back empty), so there is no point to move the pointer to. '
				f'It is display:none, detached, or zero-sized. Nothing was hovered. Note that a JavaScript '
				f'fallback would not help: a synthetic MouseEvent does not move the browser pointer and '
				f'does not trigger CSS :hover.'
			)

		x, y, w, h = float(rect.x), float(rect.y), float(rect.width), float(rect.height)
		if w <= 0 or h <= 0:
			raise ToolError(
				f'Cannot hover [{index}]: the element measures {w:g}x{h:g} px. There is nothing to point at. Nothing was hovered.'
			)

		vx0, vy0 = max(0.0, x), max(0.0, y)
		vx1, vy1 = min(vw, x + w), min(vh, y + h)
		if vx1 - vx0 < 1.0 or vy1 - vy0 < 1.0:
			raise ToolError(
				f'Cannot hover [{index}]: the element sits at ({x:g}, {y:g}) {w:g}x{h:g} px, entirely '
				f'outside the {vw:g}x{vh:g} viewport'
				+ ('' if scrolled else ' (scrollIntoViewIfNeeded failed too)')
				+ '. Nothing was hovered. Refusing to clamp the pointer back into the viewport: that is '
				'what browser-use does for clicks, and it means pointing at an arbitrary visible pixel that '
				'belongs to some other element. Scroll the element into view first, or resize the viewport.'
			)

		point: dict[str, Any] = {'x': (vx0 + vx1) / 2.0, 'y': (vy0 + vy1) / 2.0}
		# Второй mouseMoved — движение ВНУТРИ элемента, на пиксель в сторону, но не
		# за пределы видимой части.
		point['x2'] = min(vx1 - 0.5, point['x'] + 1.0)
		point['y2'] = min(vy1 - 0.5, point['y'] + 1.0)
		point['viewport'] = f'{vw:g}x{vh:g}'
		point['rect'] = f'{x:g},{y:g} {w:g}x{h:g}'
		point['clipped'] = bool(x < 0 or y < 0 or x + w > vw or y + h > vh)
		return {'cdp': cdp_session, 'session_id': session_id, 'backend_node_id': backend_node_id, 'point': point}

	async def _tool_browser_hover(self, args: dict[str, Any]) -> Sequence[types.ContentBlock]:
		"""Физическое наведение курсора на элемент.

		Issue #4964. В реестре browser-use действия ``hover`` нет вообще, а обойти
		это через ``evaluate`` с ``dispatchEvent(new MouseEvent('mouseover'))``
		НЕЛЬЗЯ: синтетическое событие не двигает внутреннюю позицию мыши браузера,
		поэтому CSS ``:hover`` не активируется. Всё, что показывается чисто на
		``:hover`` — меню по наведению, кнопки действий в строке списка, тултипы,
		мега-меню — синтетикой недостижимо. Проверяется это в smoke.py парой
		тестов: тот же элемент через ``evaluate`` не появляется, через
		``browser_hover`` появляется.

		Поэтому единственный рабочий путь — CDP ``Input.dispatchMouseEvent`` типа
		``mouseMoved``: он идёт через тот же вход, что и настоящая мышь, и обновляет
		hover-состояние движка.
		"""
		waiting_mod = bu_mcp_module('waiting')
		journal_mod = bu_mcp_module('journal')
		session, _ = await self._ensure_session()
		await self._check_domain_gate('click')

		node, live_index, info = await self._resolve(session, int(args['index']), what='hovered')
		await journal_mod.capture_entry(session, live_index)
		before = await self._delta_start(session)
		journal_mod.note(url_before=before.get('url'), resolved_index=live_index)
		geo = await self._hover_point(session, node, live_index)
		cdp_session, session_id, point = geo['cdp'], geo['session_id'], geo['point']

		try:
			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={'type': 'mouseMoved', 'x': point['x'], 'y': point['y'], 'buttons': 0},
				session_id=session_id,
			)
			await asyncio.sleep(HOVER_MOVE_GAP)
			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={'type': 'mouseMoved', 'x': point['x2'], 'y': point['y2'], 'buttons': 0},
				session_id=session_id,
			)
		except Exception as exc:
			raise ToolError(f'hover on [{live_index}] failed: {type(exc).__name__}: {exc}') from exc

		hit = await self._hover_hit(cdp_session, session_id, geo['backend_node_id'], point)
		waiting = await waiting_mod.wait_for_page_ready(session, timeout=float(args.get('timeout') or 3.0))
		delta = await self._delta_end(session, before)

		action = (
			f'Pointer moved to ({point["x"]:.0f}, {point["y"]:.0f}) on element [{live_index}] via CDP '
			f'Input.dispatchMouseEvent — a real pointer move, so CSS :hover is active.'
		)
		if hit.get('self') is False:
			action += (
				f' WARNING: the topmost element at that point is {hit.get("hit")!r}, not the requested '
				f'element — something is covering it, and :hover applies to the overlay instead.'
			)
		payload: dict[str, Any] = {
			'action': action,
			**info,
			'point': f'{point["x"]:.0f},{point["y"]:.0f}',
			'rect': point['rect'],
			'viewport': point['viewport'],
			'hit': hit,
			'url': await self._current_url(),
			'waiting': waiting,
			'delta': delta,
		}
		journal_mod.note(url_after=payload['url'], delta=delta)
		if point['clipped']:
			payload['clipped'] = True
		return self._text(payload, compact=True)

	async def _hover_hit(self, cdp_session: Any, session_id: Any, backend_node_id: Any, point: dict[str, Any]) -> dict[str, Any]:
		"""Что реально под курсором. Fail-open: не смогли — ``{'hit': None, 'self': None}``."""
		try:
			resolved = await cdp_session.cdp_client.send.DOM.resolveNode(
				params={'backendNodeId': backend_node_id}, session_id=session_id
			)
			object_id = resolved['object']['objectId']
			out = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
				params={
					'functionDeclaration': HOVER_HIT_JS,
					'objectId': object_id,
					'arguments': [{'value': point['x']}, {'value': point['y']}],
					'returnByValue': True,
				},
				session_id=session_id,
			)
			value = out.get('result', {}).get('value')
			if isinstance(value, dict):
				return value
		except Exception:
			pass
		return {'hit': None, 'self': None}

	# --- browser_click ----------------------------------------------------- #

	async def _tool_browser_click(self, args: dict[str, Any]) -> Sequence[types.ContentBlock]:
		waiting_mod = bu_mcp_module('waiting')
		journal_mod = bu_mcp_module('journal')
		session, tools = await self._ensure_session()
		await self._check_domain_gate('click')

		_node, live_index, info = await self._resolve(session, int(args['index']))
		# Хендл в журнал снимается ЗДЕСЬ: индекс уже разрешён, элемент ещё жив.
		await journal_mod.capture_entry(session, live_index)
		tabs_before = await self._tab_snapshot(session)
		# Снимок вкладок уже есть — второй раз за ним не ходим.
		before = await self._delta_start(session, tabs=tabs_before)
		# URL «до» берётся из пробы дельты, а не отдельным вызовом: он там уже есть.
		journal_mod.note(url_before=before.get('url'), resolved_index=live_index)
		try:
			result = await tools.registry.execute_action(
				'click',
				{'index': live_index},
				browser_session=session,
				file_system=self._file_system,
			)
		except Exception as exc:
			raise ToolError(f'click on [{live_index}] failed: {type(exc).__name__}: {exc}') from exc

		# Между `_resolve` и `execute_action` узел мог умереть — тогда апстрим
		# вернёт «Element index N not available» БЕЗ error, и без контрактной
		# проверки в `_action_result_text` это уехало бы клиенту успехом. Резолв
		# эту гонку не закрывает: он смотрит на состояние ДО действия.
		action_text = self._action_result_text('click', result)
		tabs_after = await self._tab_snapshot(session)
		action_text, tab_info = self._reconcile_new_tab(action_text, tabs_before, tabs_after)
		waiting = await waiting_mod.wait_for_page_ready(session, timeout=float(args.get('timeout') or 8.0))
		delta = await self._delta_end(session, before, tabs=tabs_after)
		url = await self._current_url()
		# `tab` в записи — сверка вкладок, а не украшение: по ней видно, что шаг
		# сценария был многовкладочным (to_macro такие шаги помечает).
		journal_mod.note(url_after=url, delta=delta, tab=tab_info)
		return self._text(
			{
				'action': action_text,
				**info,
				'tab': tab_info,
				'url': url,
				'waiting': waiting,
				'delta': delta,
			},
			compact=True,
		)

	# --- browser_type ------------------------------------------------------ #

	async def _tool_browser_type(self, args: dict[str, Any]) -> Sequence[types.ContentBlock]:
		waiting_mod = bu_mcp_module('waiting')
		journal_mod = bu_mcp_module('journal')
		session, tools = await self._ensure_session()
		await self._check_domain_gate('input')

		_node, live_index, info = await self._resolve(session, int(args['index']), what='typed into')
		await journal_mod.capture_entry(session, live_index)
		before = await self._delta_start(session)
		journal_mod.note(url_before=before.get('url'), resolved_index=live_index)
		try:
			result = await tools.registry.execute_action(
				'input',
				{'index': live_index, 'text': args['text'], 'clear': bool(args.get('clear', True))},
				browser_session=session,
				file_system=self._file_system,
			)
		except Exception as exc:
			raise ToolError(f'input into [{live_index}] failed: {type(exc).__name__}: {exc}') from exc

		action_text = self._action_result_text('input', result)
		waiting = await waiting_mod.wait_for_page_ready(session, timeout=float(args.get('timeout') or 8.0))
		delta = await self._delta_end(session, before)
		url = await self._current_url()
		journal_mod.note(url_after=url, delta=delta)
		return self._text(
			{'action': action_text, **info, 'url': url, 'waiting': waiting, 'delta': delta},
			compact=True,
		)
