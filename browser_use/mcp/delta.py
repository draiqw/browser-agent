"""Расписка о последствиях действия: ``DeltaMixin`` для ``BuMcpServer``.

Вынесено из server.py при разбиении на подмодули (docs/WORKLOG.md).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from browser_use.browser import BrowserSession
from browser_use.mcp.server_shared import (
	DELTA_INFORMATIONAL,
	DELTA_PROBE_JS,
	DELTA_RECHECK_DELAY,
	DELTA_RECHECKS,
	DELTA_SIGNIFICANT,
	NEW_TAB_CLAIM_RE,
	NEW_TAB_NOTE_RE,
)


class DeltaMixin:
	# --- дельта: что фактически изменилось --------------------------------- #

	async def _delta_capture(self, session: BrowserSession) -> dict[str, Any]:
		"""Один дешёвый снимок признаков страницы. Fail-open: ``{'ok': False}``."""
		try:
			value = await asyncio.wait_for(self._evaluate(session, DELTA_PROBE_JS), timeout=3.0)
		except Exception:
			return {'ok': False}
		if not isinstance(value, dict):
			return {'ok': False}
		return {'ok': True, **value}

	async def _delta_start(self, session: BrowserSession, *, tabs: dict[str, Any] | None = None) -> dict[str, Any]:
		"""Снимок ДО действия. Число вкладок берётся из готового снимка, если он уже есть."""
		started = time.perf_counter()
		snap = await self._delta_capture(session)
		if tabs is None:
			tabs = await self._tab_snapshot(session)
		snap['tabs'] = len(tabs.get('ids') or [])
		snap['ms'] = (time.perf_counter() - started) * 1000.0
		return snap

	async def _delta_end(
		self,
		session: BrowserSession,
		before: dict[str, Any],
		*,
		tabs: dict[str, Any] | None = None,
		reported_ok: bool = True,
		extra_significant: tuple[str, ...] = (),
	) -> dict[str, Any]:
		"""Снимок ПОСЛЕ + вердикт. Лестница из двух ступеней.

		Ступень 1 (всегда): один ``Runtime.evaluate``. Если он показал изменение —
		вопрос закрыт, доплачивать не за что.

		Ступень 2 (только если ступень 1 показала ПУСТУЮ дельту, а действие
		отрапортовало успех): та же проба ещё до ``DELTA_RECHECKS`` раз с паузой
		``DELTA_RECHECK_DELAY``. Это и есть подъём цены — но ровно на той ветке,
		где он окупается: «сразу ничего не изменилось» бывает у нормального клика,
		который дёрнул fetch и перерисуется через 100 мс, а вот «не изменилось и
		после того, как страница успокоилась» — это уже настоящий нооп.

		Чего здесь СОЗНАТЕЛЬНО нет. Напрашивающаяся третья ступень — сравнить
		полное дерево из ``state.serialize_state`` до и после — нереализуема без
		того, чтобы платить за неё ВСЕГДА: сторону «до» нельзя снять задним
		числом, а предсказать, понадобится ли она, невозможно. Это ровно то
		удвоение цены самого дорогого вызова, которого мы избегаем. Поэтому
		ступень 1 сделана достаточно чувствительной (геометрия + состояние формы
		+ признак отрисованности каждого узла), а не поверхностной.
		"""
		started = time.perf_counter()
		if tabs is None:
			tabs = await self._tab_snapshot(session)
		tabs_after = len(tabs.get('ids') or [])

		after = await self._delta_capture(session)
		after['tabs'] = tabs_after
		probes, settled = 1, 0.0
		if before.get('ok') and after.get('ok') and reported_ok:
			while probes <= DELTA_RECHECKS and not self._delta_diff(before, after, extra_significant)['significant']:
				await asyncio.sleep(DELTA_RECHECK_DELAY)
				settled += DELTA_RECHECK_DELAY * 1000.0
				fresh = await self._delta_capture(session)
				probes += 1
				if not fresh.get('ok'):
					break
				fresh['tabs'] = tabs_after
				after = fresh

		cost = float(before.get('ms') or 0.0) + (time.perf_counter() - started) * 1000.0 - settled
		return self._delta_verdict(
			before,
			after,
			probes=probes,
			cost_ms=cost,
			settle_ms=settled,
			reported_ok=reported_ok,
			extra_significant=extra_significant,
		)

	@staticmethod
	def _delta_diff(before: dict[str, Any], after: dict[str, Any], extra_significant: tuple[str, ...] = ()) -> dict[str, Any]:
		"""Чистое сравнение двух снимков -> изменившиеся поля + флаг значимости."""

		def show(key: str, value: Any) -> Any:
			if key == 'digest':
				return 'changed'
			if isinstance(value, str) and len(value) > 100:
				return value[:99] + '…'
			return value

		counts = set(DELTA_SIGNIFICANT) | set(extra_significant)
		fields: dict[str, Any] = {}
		significant = False
		for key in DELTA_SIGNIFICANT + DELTA_INFORMATIONAL:
			if key not in before or key not in after:
				continue
			if before[key] == after[key]:
				continue
			fields[key] = [show(key, before[key]), show(key, after[key])] if key != 'digest' else 'changed'
			if key in counts:
				significant = True
		return {'fields': fields, 'significant': significant}

	@classmethod
	def _delta_verdict(
		cls,
		before: dict[str, Any],
		after: dict[str, Any],
		*,
		probes: int,
		cost_ms: float,
		settle_ms: float = 0.0,
		reported_ok: bool = True,
		extra_significant: tuple[str, ...] = (),
	) -> dict[str, Any]:
		"""Расписка о последствиях. НИКОГДА не бросает: пустая дельта — законный исход.

		«Ничего не изменилось» после клика по неактивной кнопке или по уже
		выбранному пункту — нормальный, честный результат, и превращать его в
		``ToolError`` значило бы ломать рабочие сценарии. Но и молчать нельзя:
		если действие отрапортовало успех, а на странице не сдвинулось ничего,
		модель по одному только тексту действия решит, что задача выполнена.
		Поэтому ставится явный флаг ``no_effect`` с объяснением, а не ошибка.
		"""
		payload: dict[str, Any] = {'cost_ms': round(cost_ms, 1), 'probes': probes}
		if settle_ms:
			payload['settle_ms'] = round(settle_ms)
		if not (before.get('ok') and after.get('ok')):
			payload.update(
				changed=None,
				status='unavailable',
				note='Could not read page state before/after (CDP probe failed); nothing was verified.',
			)
			return payload

		diff = cls._delta_diff(before, after, extra_significant)
		payload['changed'] = bool(diff['significant'])
		payload['status'] = 'changed' if diff['significant'] else 'no-change'
		if diff['fields']:
			payload['fields'] = diff['fields']
		if before.get('truncated') or after.get('truncated'):
			payload['truncated'] = True

		if not diff['significant'] and reported_ok:
			payload['no_effect'] = True
			payload['note'] = (
				f'Reported success, but NOTHING measurably changed after {probes} probe(s): same URL, '
				f'tab count, rendered elements, layout and form state. The action may have been swallowed '
				f'(overlay, form validation, a handler that returned early). Do not treat this step as done '
				f'without checking browser_state.'
			)
		return payload

	# --- вкладки: факт вместо оптимизма (#5529) ---------------------------- #

	@staticmethod
	def _short_tab_id(target_id: str | None) -> str | None:
		"""Последние 4 символа target_id — ровно то, что принимают switch/close."""
		return target_id[-4:] if target_id else None

	async def _tab_snapshot(self, session: BrowserSession) -> dict[str, Any]:
		"""Кто в фокусе и какие вкладки открыты. Fail-open: не смогли — пустой снимок."""
		snapshot: dict[str, Any] = {'focus': getattr(session, 'agent_focus_target_id', None), 'ids': []}
		try:
			snapshot['ids'] = [t.target_id for t in await session.get_tabs()]
		except Exception:
			snapshot['ids'] = []
		return snapshot

	@classmethod
	def _reconcile_new_tab(cls, text: str, before: dict[str, Any], after: dict[str, Any]) -> tuple[str, dict[str, Any]]:
		"""Сверить апстримный рапорт о новой вкладке с фактическим фокусом.

		Issue #5529: ``_detect_new_tab_opened`` в browser_use/tools/service.py
		дёргает ``SwitchTabEvent`` с ``raise_if_any=False, raise_if_none=False``,
		не смотрит на результат (``None`` = переключение провалилось) и ВСЕГДА
		возвращает «Automatically switched to new tab». Мы эту фразу выкидываем и
		пишем то, что видно по ``agent_focus_target_id`` и списку target_id до и
		после клика.

		Функция чистая: весь ввод — два снимка и строка. Так её можно прогнать
		тестом на дословном тексте апстрима, не воспроизводя гонку в браузере.
		"""
		short = cls._short_tab_id
		before_ids = list(before.get('ids') or [])
		after_ids = list(after.get('ids') or [])
		focus_before, focus_after = before.get('focus'), after.get('focus')
		opened = [t for t in after_ids if t not in before_ids]
		closed = [t for t in before_ids if t not in after_ids]

		info: dict[str, Any] = {
			'focus_before': short(focus_before),
			'focus_after': short(focus_after),
			'opened': [short(t) for t in opened],
			'switched': bool(focus_after and focus_after != focus_before),
		}
		if closed:
			info['closed'] = [short(t) for t in closed]

		claim = NEW_TAB_CLAIM_RE.search(text)
		if claim is None:
			# Апстрим ничего не заявил. Честная ветка `_detect_new_tab_opened`
			# («Note: This opened a new tab …») уже всё сказала — не дублируем.
			if opened and not NEW_TAB_NOTE_RE.search(text):
				info['claim'] = 'silent-open'
				text = (
					f'{text.rstrip(". ")}. Note: {len(opened)} new tab(s) opened '
					f'(tab_id: {", ".join(str(short(t)) for t in opened)}); focus stayed on '
					f'tab #{short(focus_after)}. Use switch(tab_id=...) to go there.'
				)
			elif info['switched'] and not opened:
				info['claim'] = 'silent-switch'
				text = (
					f'{text.rstrip(". ")}. Note: focus moved from tab #{short(focus_before)} '
					f'to tab #{short(focus_after)} without browser-use saying so.'
				)
			else:
				info['claim'] = 'none' if not opened else 'upstream-note'
			return text, info

		claimed = claim.group(1)
		info['claimed'] = claimed
		body = NEW_TAB_CLAIM_RE.sub('', text).rstrip(' .')
		claimed_lc = claimed.lower()
		focus_after_short = (short(focus_after) or '').lower()

		if focus_after_short and focus_after_short == claimed_lc:
			info['claim'] = 'verified'
			return f'{body}. Opened a new tab and switched to it (tab_id: {claimed}) — verified by target_id.', info

		if focus_after == focus_before:
			info['claim'] = 'false'
			gone = ' (that tab no longer exists)' if claimed not in [short(t) for t in after_ids] else ''
			return (
				f'{body}. WARNING: browser-use claimed it automatically switched to a new tab '
				f'(tab_id: {claimed}){gone}, but the active tab is still #{short(focus_before)}. '
				f'The switch did not happen (browser-use issue #5529: it reports the switch '
				f'without checking the result). Call switch(tab_id="{claimed}") if you want that tab.',
				info,
			)

		info['claim'] = 'mismatch'
		return (
			f'{body}. WARNING: browser-use claimed a switch to tab #{claimed}, but the active tab '
			f'is #{short(focus_after)}. Trust the latter; call browser_state to see where you are.',
			info,
		)
