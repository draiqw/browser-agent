"""``switch``: проверка фактом. ``SwitchActionsMixin`` для ``BuMcpServer``.

Вынесено из server.py при разбиении на подмодули (docs/WORKLOG.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import mcp.types as types

from browser_use.mcp.server_shared import ToolError


class SwitchActionsMixin:
	async def _tool_switch(self, args: dict[str, Any]) -> Sequence[types.ContentBlock]:
		"""``switch`` реестра + сверка фактического фокуса.

		Апстрим (browser_use/tools/service.py, ``switch``) берёт результат
		``SwitchTabEvent`` с ``raise_if_any=False, raise_if_none=False`` и, если
		тот вернул ``None`` (то есть переключение провалилось), всё равно пишет
		``f'Switched to tab #{params.tab_id}'``. Плюс ``except Exception`` отдаёт
		``'Attempted to switch to tab #...'`` — тоже как успех. Первое ловится
		только сверкой ``agent_focus_target_id``, второе — таблицей NOOP_MARKERS.
		"""
		session, tools = await self._ensure_session()
		await self._check_domain_gate('switch')

		before = await self._tab_snapshot(session)
		try:
			result = await tools.registry.execute_action('switch', args, browser_session=session, file_system=self._file_system)
		except ToolError:
			raise
		except Exception as exc:
			raise ToolError(f'switch failed: {type(exc).__name__}: {exc}') from exc
		upstream = self._action_result_text('switch', result)

		after = await self._tab_snapshot(session)
		focus_after = self._short_tab_id(after.get('focus'))
		requested = str(args.get('tab_id') or '').strip()
		if requested and (focus_after or '').lower() != requested[-4:].lower():
			raise ToolError(
				f'switch did NOT change the active tab, but browser-use reported {upstream.strip()!r}. '
				f'Requested tab #{requested}, active tab is still #{self._short_tab_id(before.get("focus"))}. '
				f'browser-use takes the SwitchTabEvent result with raise_if_none=False and prints the '
				f'requested id even when the event returned nothing. Call browser_state for the live tab list.'
			)
		return self._text(
			{
				'action': f'Active tab is #{focus_after} (verified by target_id).',
				'upstream_report': upstream.strip(),
				'tab': {'focus_before': self._short_tab_id(before.get('focus')), 'focus_after': focus_after},
				'url': await self._current_url(),
			},
			compact=True,
		)
