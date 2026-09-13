"""``scroll``: проверка фактом. ``ScrollActionsMixin`` для ``BuMcpServer``.

Вынесено из server.py при разбиении на подмодули (docs/WORKLOG.md).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import mcp.types as types

from browser_use.browser import BrowserSession
from bu_mcp.server_shared import SCROLL_PROBE_JS, SCROLL_TARGET_JS, ToolError, bu_mcp_module


class ScrollActionsMixin:
	async def _scroll_probe(self, session: BrowserSession, node: Any) -> dict[str, Any]:
		"""Позиция прокрутки: корневой скроллер + подпись контейнеров (+ цель, если задан index).

		Fail-open: если проба не удалась, возвращается ``{'ok': False}``, и
		верификация переходит в статус ``unverified`` вместо ложной ошибки.
		"""
		probe: dict[str, Any] = {'ok': False}
		try:
			page = await asyncio.wait_for(self._evaluate(session, SCROLL_PROBE_JS), timeout=3.0)
		except Exception:
			return probe
		if not isinstance(page, dict):
			return probe
		probe = {'ok': True, 'page': page, 'target': None}
		if node is None:
			return probe
		try:
			cdp_session = await session.cdp_client_for_node(node)
			resolved = await cdp_session.cdp_client.send.DOM.resolveNode(
				params={'backendNodeId': node.backend_node_id}, session_id=cdp_session.session_id
			)
			object_id = resolved['object'].get('objectId')
			if object_id is None:
				raise ValueError('resolveNode returned no objectId')
			out = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
				params={'functionDeclaration': SCROLL_TARGET_JS, 'objectId': object_id, 'returnByValue': True},
				session_id=cdp_session.session_id,
			)
			value = out.get('result', {}).get('value')
			if isinstance(value, dict) and value.get('found'):
				probe['target'] = value
		except Exception:
			probe['target'] = None
		return probe

	@staticmethod
	def _scroll_moved(before: dict[str, Any], after: dict[str, Any]) -> bool:
		"""Сдвинулось ли хоть что-нибудь: корневой скроллер, подпись или цель."""
		bp, ap = before.get('page') or {}, after.get('page') or {}
		if (bp.get('y'), bp.get('x'), bp.get('sig')) != (ap.get('y'), ap.get('x'), ap.get('sig')):
			return True
		bt, at = before.get('target') or {}, after.get('target') or {}
		return bool(bt) and bool(at) and (bt.get('y'), bt.get('x')) != (at.get('y'), at.get('x'))

	async def _tool_scroll(self, args: dict[str, Any]) -> Sequence[types.ContentBlock]:
		"""``scroll`` реестра + верификация фактом по scrollY/scrollX.

		Зачем override. У ``scroll`` в browser_use/tools/service.py нет текста,
		по которому провал можно опознать: цикл по страницам глотает исключения
		(`logger.warning` + continue), а при ``pages == 1.0`` — это ДЕФОЛТ —
		итоговая строка `f'Scrolled {direction} {target} {viewport_height}px'`
		печатается независимо от ``completed_scrolls``. Полный провал прокрутки
		выглядит дословно как успех. Поэтому меряем позицию до и после.

		Три исхода:

		* прокрутилось -> успех, в ответе фактическая дельта;
		* не прокрутилось, НО крутить было некуда (мы уже в конце / страница не
		  прокручивается) -> ЧЕСТНЫЙ СТАТУС ``at-end``, а НЕ ошибка. Обоснование:
		  ничего не сломано, клиент увидел ровно тот мир, который просил показать;
		  ``ToolError`` здесь сломал бы совершенно нормальный цикл «мотать вниз,
		  пока не кончится страница» и толкал бы клиента на бессмысленные ретраи.
		  Но и молчать нельзя: строка «Scrolled down 479px» в этом случае — ложь,
		  поэтому ``action`` переписывается, а ``scrolled`` равен ``false``;
		* не прокрутилось, хотя крутить БЫЛО куда -> ``ToolError``. Это и есть
		  закрываемая дыра.
		"""
		session, tools = await self._ensure_session()
		await self._check_domain_gate('scroll')

		journal_mod = bu_mcp_module('journal')
		# get_current_page_url() читается из session_manager, не по CDP (замерено:
		# медиана 0.000 мс), так что журнал здесь ничего не стоит.
		journal_mod.note(url_before=await self._current_url())

		index = args.get('index')
		node = None
		scroll_target = index if index is not None and int(index) != 0 else None
		# Контекст страницы снимается и без элемента: скролл может быть первым
		# шагом сценария, и тогда вьюпорт в макрос попадёт только отсюда.
		await journal_mod.capture_entry(session, scroll_target)
		if scroll_target is not None:
			try:
				node = await session.get_element_by_index(int(scroll_target))
			except Exception:
				node = None

		before = await self._scroll_probe(session, node)
		try:
			result = await tools.registry.execute_action('scroll', args, browser_session=session, file_system=self._file_system)
		except ToolError:
			raise
		except Exception as exc:
			raise ToolError(f'scroll failed: {type(exc).__name__}: {exc}') from exc
		upstream = self._action_result_text('scroll', result)

		# Плавная прокрутка (scroll-behavior: smooth) доезжает не мгновенно —
		# даём ей досесть, но только если с первой пробы ничего не сдвинулось.
		after = await self._scroll_probe(session, node)
		for _ in range(4):
			if not (before.get('ok') and after.get('ok')) or self._scroll_moved(before, after):
				break
			await asyncio.sleep(0.1)
			after = await self._scroll_probe(session, node)

		# _scroll_verdict умеет бросить ToolError (прокрутка была заблокирована) —
		# тогда запись в журнал сделает journal.run_journaled со статусом error.
		verdict = self._scroll_verdict(args, before, after, upstream)
		journal_mod.note(
			url_after=await self._current_url(),
			delta={
				'changed': verdict.get('scrolled'),
				'status': verdict.get('status'),
				'fields': {'scroll': verdict.get('delta')} if verdict.get('delta') else {},
			},
		)
		return self._text(verdict, compact=True)

	@classmethod
	def _scroll_verdict(
		cls, args: dict[str, Any], before: dict[str, Any], after: dict[str, Any], upstream: str
	) -> dict[str, Any]:
		"""Чистая часть проверки скролла: два снимка + рапорт апстрима -> ответ клиенту."""
		down = bool(args.get('down', True))
		direction = 'down' if down else 'up'
		payload: dict[str, Any] = {'upstream_report': upstream.strip()}

		if not (before.get('ok') and after.get('ok')):
			payload.update(
				action=(
					f'{upstream.strip()} — NOT VERIFIED: could not read the scroll position '
					f'(CDP probe failed), so this report comes from browser-use unchecked.'
				),
				scrolled=None,
				status='unverified',
			)
			return payload

		bp, ap = before['page'], after['page']
		target_before, target_after = before.get('target'), after.get('target')
		scope = 'element' if target_before and target_after else 'page'
		# Условие продублировано (а не через scope), чтобы pyright сузил
		# target_before/target_after до dict в истинной ветке тернарника.
		ref_before: dict[str, Any] = target_before if target_before and target_after else bp
		ref_after: dict[str, Any] = target_after if target_before and target_after else ap

		delta_y = int(ref_after.get('y', 0)) - int(ref_before.get('y', 0))
		delta_x = int(ref_after.get('x', 0)) - int(ref_before.get('x', 0))
		room = int(ref_before.get('max_y', 0)) - int(ref_before.get('y', 0)) if down else int(ref_before.get('y', 0))
		payload.update(
			scope=scope,
			delta={'y': delta_y, 'x': delta_x},
			position={
				'y': int(ref_after.get('y', 0)),
				'x': int(ref_after.get('x', 0)),
				'max_y': int(ref_after.get('max_y', 0)),
				'max_x': int(ref_after.get('max_x', 0)),
			},
		)

		if cls._scroll_moved(before, after):
			payload.update(scrolled=True, status='scrolled')
			at_end = int(ref_after.get('y', 0)) >= int(ref_after.get('max_y', 0)) if down else int(ref_after.get('y', 0)) <= 0
			payload['at_end'] = at_end
			tail = f' — {"bottom" if down else "top"} reached.' if at_end else '.'
			if delta_y:
				payload['action'] = (
					f'Scrolled {direction} {abs(delta_y)}px '
					f'(scrollY {ref_before.get("y", 0)} -> {ref_after.get("y", 0)} '
					f'of {ref_after.get("max_y", 0)}){tail}'
				)
			elif delta_x:
				payload['action'] = (
					f'Scrolled {abs(delta_x)}px horizontally '
					f'(scrollX {ref_before.get("x", 0)} -> {ref_after.get("x", 0)} '
					f'of {ref_after.get("max_x", 0)}){tail}'
				)
			else:
				# Корневой скроллер стоит, но подпись контейнеров изменилась:
				# уехал какой-то вложенный div, а не страница.
				payload['at_end'] = False
				payload['action'] = (
					f'Scrolled {direction}: the {scope} scroller did not move (y={ref_after.get("y", 0)}), '
					f'but a nested scroll container did. browser-use reported {upstream.strip()!r}.'
				)
			return payload

		if bp.get('truncated'):
			# Подпись контейнеров считалась не по всему документу — «не сдвинулось»
			# может быть артефактом обрезки. Ложную ошибку не поднимаем.
			payload.update(
				scrolled=None,
				status='unverified',
				action=(
					f'{upstream.strip()} — NOT VERIFIED: the document is too large to check every '
					f'scroll container, and the root scroller did not move.'
				),
			)
			return payload

		if room <= 1:
			payload.update(scrolled=False, status='at-end', at_end=True)
			nothing = int(ref_before.get('max_y', 0)) <= 1
			payload['action'] = (
				f'Nothing scrolled: this {scope} does not scroll at all (content fits). '
				if nothing
				else f'Nothing scrolled: already at the {"bottom" if down else "top"} of this {scope} '
				f'(y={ref_before.get("y", 0)} of {ref_before.get("max_y", 0)}). '
			) + f'browser-use reported {upstream.strip()!r}; that is its fixed string, not a measurement.'
			return payload

		raise ToolError(
			f'scroll did NOT move anything, but browser-use reported {upstream.strip()!r} as a success. '
			f'Measured: {scope} scroll position stayed at y={ref_before.get("y", 0)} while {room}px of '
			f'content remain {"below" if down else "above"} (max_y={ref_before.get("max_y", 0)}). '
			f'browser-use cannot detect this: its per-page loop swallows failed scrolls and at the '
			f'default pages=1.0 prints a fixed "Scrolled ... px" string regardless. '
			f'Something is blocking the scroll — overflow:hidden on the document, a modal scroll-lock, '
			f'or the real scroller is a nested container. Try scroll(index=<container index>) or '
			f'send_keys(keys="PageDown").'
		)
