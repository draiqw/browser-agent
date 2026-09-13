"""Контракт ``ActionResult``: тексты-нооп. ``NoopMixin`` для ``BuMcpServer``.

Вынесено из server.py при разбиении на подмодули (docs/WORKLOG.md).
"""

from __future__ import annotations

import asyncio
from typing import Any

from browser_use.agent.views import ActionResult
from browser_use.browser import BrowserSession
from browser_use.mcp.server_shared import NOOP_MARKERS, NoopMarker, NoopResultError, ToolError


class NoopMixin:
	@staticmethod
	def _classify_noop(name: str, text: str) -> NoopMarker | None:
		"""Найти в тексте результата известный маркер «действие не выполнилось».

		Гейт по имени действия — часть контракта, а не оптимизация: ``search_page``,
		``find_elements`` и ``evaluate`` кладут в ``extracted_content`` куски самой
		страницы, и «Element index 5 not available» вполне может быть просто текстом
		на странице. Проверяем только те действия, чьи функции апстрима эти строки
		действительно порождают.
		"""
		if not text:
			return None
		for marker in NOOP_MARKERS:
			if name in marker.actions and marker.pattern.search(text):
				return marker
		return None

	@classmethod
	def _action_result_text(cls, name: str, result: Any) -> str:
		"""Свернуть ``ActionResult`` в текст; невыполненное действие поднять как ToolError.

		Две ступени:

		1. ``result.error`` — как было;
		2. КОНТРАКТНАЯ ПРОВЕРКА по ``NOOP_MARKERS``: шесть мест browser-use
		   возвращают «страница, возможно, изменилась» / «текста нет» / «такой
		   опции нет» вообще без ``error``, и без этой ступени они уезжали
		   клиенту успехом. См. комментарий у ``NOOP_MARKERS``.

		Проверяются оба текстовых поля: у ``select_dropdown`` признак провала
		сидит в ``long_term_memory``, тогда как ``extracted_content`` держит
		вполне невинный список опций.
		"""
		if isinstance(result, ActionResult):
			if result.error:
				raise ToolError(f'{name} failed: {result.error}')
			parts = [p for p in (result.extracted_content, result.long_term_memory) if p]
			text = parts[0] if parts else f'{name}: ok'

			marker = cls._classify_noop(name, '\n'.join(parts))
			if marker is not None:
				raise NoopResultError(
					f'{name} did NOT run, but browser-use reported it as a normal result '
					f'[{marker.code}]. browser-use said: {text.strip()!r}. {marker.hint}',
					code=marker.code,
					raw=text,
				)

			if result.attachments:
				text += f'\nAttachments: {", ".join(result.attachments)}'
			return text
		if result is None:
			return f'{name}: ok'
		return str(result)

	async def _enrich_noop(self, session: BrowserSession, exc: NoopResultError) -> NoopResultError:
		"""Дописать в сообщение то, чего апстрим не различил.

		Пока единственный такой случай — ``find_text``: у него ОДИН
		``except Exception`` на «текста нет» и «CDP умер», и наружу оба выходят
		одной строкой. Отличить их постфактум можно только пробой живости, что
		мы и делаем. Проба fail-open: не смогли — так и пишем.
		"""
		if exc.code != 'text-not-found':
			return exc
		alive = await self._page_alive(session)
		if alive is True:
			extra = (
				'Liveness probe: the page is alive and answered CDP, so this is genuinely '
				'"no such text on the page" — not a dead connection.'
			)
		elif alive is False:
			extra = (
				'Liveness probe: the page did NOT answer CDP. browser-use cannot tell these two '
				'apart (one except Exception covers both), but here the transport looks broken, '
				'not the text missing. Re-check browser_state before trusting anything else.'
			)
		else:
			extra = 'Liveness probe was inconclusive; browser-use cannot tell "no such text" from "dead page" here.'
		return NoopResultError(f'{exc} {extra}', code=exc.code, raw=exc.raw)

	async def _page_alive(self, session: BrowserSession) -> bool | None:
		"""``True`` / ``False`` / ``None`` (проверить не удалось)."""
		try:
			value = await asyncio.wait_for(self._evaluate(session, '1+1'), timeout=3.0)
		except Exception:
			return False
		return True if value == 2 else None
