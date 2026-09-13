"""``browser_navigate`` + резолв индекса: ``NavigateActionsMixin`` для ``BuMcpServer``.

Вынесено из server.py при разбиении на подмодули (docs/WORKLOG.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import mcp.types as types

from browser_use.browser import BrowserSession
from bu_mcp.server_shared import DEFAULT_HYDRATE_TIMEOUT, ToolError, bu_mcp_module


class NavigateActionsMixin:
	# --- browser_navigate -------------------------------------------------- #

	async def _tool_browser_navigate(self, args: dict[str, Any]) -> Sequence[types.ContentBlock]:
		waiting_mod = bu_mcp_module('waiting')
		journal_mod = bu_mcp_module('journal')
		session, tools = await self._ensure_session()
		url = args['url']
		await self._check_domain_gate('navigate', target_url=url)

		# Baseline СНИМАЕТСЯ ДО ДЕЙСТВИЯ. Наоборот было нельзя: реестровый
		# `navigate` возвращает управление уже после того, как документ
		# закоммитился и, как правило, выдал `load`, поэтому baseline снимался с
		# НОВОГО документа, разницы loaderId не оставалось и стадия
		# `navigation_start` честно докладывала «no navigation detected», а потом
		# впустую опрашивала фрейм всё стартовое окно (2.5 с при timeout=10).
		# Замерено: 45 из 48 навигаций в BENCH.md.
		baseline = await waiting_mod.navigation_baseline(session)
		# baseline уже держит URL прежнего документа — второй раз за ним не ходим.
		journal_mod.note(url_before=baseline.get('url'))
		# Хендла у навигации нет, а контекст страницы нужен: навигация — самый
		# частый ПЕРВЫЙ шаг сценария, и `recorded_on.viewport` макроса берётся из
		# первой же записи. Снимается ДО перехода: вьюпорт от него не меняется,
		# а вот дожидаться нового документа ради двух чисел незачем.
		await journal_mod.capture_entry(session)

		try:
			result = await tools.registry.execute_action(
				'navigate',
				{'url': url, 'new_tab': bool(args.get('new_tab', False))},
				browser_session=session,
				file_system=self._file_system,
			)
		except Exception as exc:
			raise ToolError(f'navigate to {url!r} failed: {type(exc).__name__}: {exc}') from exc

		action_text = self._action_result_text('navigate', result)
		waiting = await waiting_mod.wait_after_navigation(session, timeout=float(args.get('timeout') or 10.0), baseline=baseline)
		await self._hydrate(waiting, args.get('hydrate'))
		url = await self._current_url()
		journal_mod.note(url_after=url)
		return self._text(
			{
				'action': action_text,
				'url': url,
				'waiting': waiting,
			},
			compact=True,
		)

	async def _hydrate(self, waiting: dict[str, Any], requested: Any) -> None:
		"""Явная стадия «дать странице догрузиться» поверх завершённой навигации.

		Зачем она вообще есть. До починки baseline (см. выше) `browser_navigate`
		возвращал управление на ~2.5 с позже штатного сервера — и эти секунды не
		были ожиданием, это был опрос вхолостую. Но побочный эффект был
		полезным: за них SPA успевала гидрироваться, и состояние отдавало
		заметно больше элементов (google_maps 47 против 8, coursera 172 против
		36 у штатного сервера). Чинить гонку, не заменив побочку, значило бы
		обменять реальные элементы на секунду латентности.

		Поэтому дожидание оставлено, но перестало быть побочкой:

		* у него своё имя (стадии в разбивке помечены `hydration.`),
		* свой бюджет (`hydrate`, по умолчанию 3 с, `0` выключает),
		* и, в отличие от `sleep(2.5)`, оно измеряет страницу, а не часы:
		  лестница `wait_for_page_ready` (спиннеры -> сетевая тишина ->
		  MutationObserver) выходит раньше, когда странице нечего догружать.
		  Пустая статическая страница стоит теперь ~0.5 с вместо 2.5 с, а
		  живая SPA получает свои секунды и, главное, отчитывается,
		  дождались её или бюджет кончился.

		`ready` остаётся конъюнкцией всех стадий: теперь он означает
		«документ доехал И перестал шевелиться», а не «ждать было нечего».
		"""
		budget = DEFAULT_HYDRATE_TIMEOUT if requested is None else float(requested)
		if budget <= 0:
			waiting['hydrated'] = None
			return
		waiting_mod = bu_mcp_module('waiting')
		session, _ = await self._ensure_session()
		settle = await waiting_mod.wait_for_page_ready(session, timeout=budget)
		for stage in settle.get('stages', []):
			waiting.setdefault('stages', []).append({**stage, 'name': f'hydration.{stage["name"]}'})
		waiting['hydrated'] = bool(settle.get('ready'))
		waiting['ready'] = bool(waiting.get('ready')) and waiting['hydrated']
		waiting['elapsed'] = round(float(waiting.get('elapsed') or 0.0) + float(settle.get('elapsed') or 0.0), 3)

	# --- резолв индекса ---------------------------------------------------- #

	async def _resolve(
		self, session: BrowserSession, index: int, *, what: str = 'clicked or typed'
	) -> tuple[Any, int, dict[str, Any]]:
		"""Индекс -> (узел, живой индекс, телеметрия). Протухший/неоднозначный хендл = жёсткая ошибка.

		Узел возвращается наружу ради ``browser_hover``: тому нужен не индекс, а
		``backend_node_id`` и CDP-сессия ФРЕЙМА этого узла — координаты квада
		фрейм-локальные, и слать их в корневую сессию нельзя.
		"""
		resolve_mod = bu_mcp_module('resolve')
		try:
			node = await resolve_mod.resolve_index(session, index)
		except resolve_mod.StaleHandleError as exc:
			raise ToolError(
				f'STALE ELEMENT HANDLE [{index}]: {exc} '
				f'Nothing was {what}. Call browser_state to get a fresh snapshot '
				f'and use an index from it.'
			) from exc
		except resolve_mod.AmbiguousHandleError as exc:
			raise ToolError(
				f'AMBIGUOUS ELEMENT HANDLE [{index}]: {exc} '
				f'Refusing to guess which element you meant; nothing was {what}. '
				f'Call browser_state and pick a specific index.'
			) from exc
		except ToolError:
			raise
		except Exception as exc:
			raise ToolError(f'Cannot resolve element index [{index}]: {type(exc).__name__}: {exc}') from exc

		live_index = session.get_selector_index(node)
		info: dict[str, Any] = {'requested_index': index, 'resolved_index': live_index}
		try:
			last = resolve_mod.last_resolution(session)
			if last:
				info['resolution'] = last
		except Exception:
			pass
		return node, live_index, info
