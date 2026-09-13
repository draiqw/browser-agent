"""Мост к реестру ``Tools()``: ``RegistryBridgeMixin`` для ``BuMcpServer``.

Вынесено из server.py при разбиении на подмодули (docs/WORKLOG.md).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import mcp.types as types

from browser_use.tools.registry.views import ActionRegistry
from bu_mcp.server_shared import (
	BRIDGE_EXCLUDE,
	DELTA_FOCUS_COUNTS,
	DOMAIN_EXEMPT,
	OVERRIDE_DESCRIPTIONS,
	OVERRIDE_SCHEMAS,
	NoopResultError,
	ToolError,
	bu_mcp_module,
)

logger = logging.getLogger('bu_mcp.server')


class RegistryBridgeMixin:
	@staticmethod
	def _describe(name: str, action: Any) -> str:
		"""Описание действия для клиента.

		Мост протекает ровно здесь: часть действий зарегистрирована с пустой
		строкой описания (``search``, ``navigate``, ``upload_file``, ``send_keys``,
		``dropdown_options``) — вся семантика у них живёт в field-описаниях
		param-модели. Достраиваем описание из имени и схемы, а не выдумываем текст.
		"""
		desc = (action.description or '').strip()
		if desc:
			return desc
		doc = (getattr(action.param_model, '__doc__', None) or '').strip()
		if doc:
			return doc
		return f'browser-use action `{name}` (no description in the registry; see inputSchema for parameters).'

	def _build_tool_list(self, page_url: str | None) -> list[types.Tool]:
		tools = self._registry_tools()
		out: list[types.Tool] = []

		# 1. Наши переопределения — всегда сверху и всегда доступны.
		for name, schema in OVERRIDE_SCHEMAS.items():
			out.append(types.Tool(name=name, description=OVERRIDE_DESCRIPTIONS[name], inputSchema=schema))

		# 2. Мост к реестру.
		for name, action in tools.registry.registry.actions.items():
			if name in BRIDGE_EXCLUDE:
				continue
			# Фильтрация по домену — тем же механизмом, что и у реестра,
			# и пересчитывается на каждый list_tools.
			if self._allowed_domains and page_url is not None and name not in DOMAIN_EXEMPT:
				if not ActionRegistry._match_domains(action.domains, page_url):
					continue
			try:
				schema = action.param_model.model_json_schema()
			except Exception as exc:
				logger.warning('cannot build schema for action %s: %r', name, exc)
				continue
			schema.setdefault('type', 'object')
			schema.setdefault('properties', {})
			out.append(types.Tool(name=name, description=self._describe(name, action), inputSchema=schema))

		return out

	# -- форматирование ----------------------------------------------------- #

	@staticmethod
	def _json(payload: Any, *, compact: bool = False) -> str:
		"""JSON для клиента. ``compact`` — без отступов и без пробелов после запятых.

		Отступы в JSON-ответе инструмента — это чистый налог: клиент их парсит, а
		модель платит за них токенами. Читаемости они добавляют ровно там, где её
		и так хватает (наши конверты — плоские объекты в десяток ключей).
		"""
		if compact:
			return json.dumps(payload, separators=(',', ':'), ensure_ascii=False, default=str)
		return json.dumps(payload, indent=2, ensure_ascii=False, default=str)

	@classmethod
	def _text(cls, payload: Any, *, compact: bool = False) -> list[types.TextContent]:
		if isinstance(payload, str):
			return [types.TextContent(type='text', text=payload)]
		return [types.TextContent(type='text', text=cls._json(payload, compact=compact))]

	# -- исполнение --------------------------------------------------------- #

	@staticmethod
	def _wants_file_paths(tools: Any, name: str) -> bool:
		"""Нужен ли действию allowlist файлов. Обход папки — не бесплатный, зря не ходим."""
		try:
			import inspect

			action = tools.registry.registry.actions.get(name)
			if action is None:
				return False
			return 'available_file_paths' in inspect.signature(action.function).parameters
		except Exception:
			return True  # не разобрались — лучше дать allowlist, чем уронить действие

	async def _run_registry_action(self, name: str, args: dict[str, Any]) -> str:
		session, tools = await self._ensure_session()
		await self._check_domain_gate(name)
		try:
			result = await tools.registry.execute_action(
				name,
				args,
				browser_session=session,
				file_system=self._file_system,
				available_file_paths=(bu_mcp_module('uploads').allowed_files() if self._wants_file_paths(tools, name) else None),
			)
		except ToolError:
			raise
		except Exception as exc:
			raise ToolError(f'{name} failed: {type(exc).__name__}: {exc}') from exc
		try:
			return self._action_result_text(name, result)
		except NoopResultError as exc:
			raise await self._enrich_noop(session, exc) from None

	async def _tool_registry_with_delta(self, name: str, args: dict[str, Any]) -> Sequence[types.ContentBlock]:
		"""``select_dropdown`` / ``send_keys``: реестровое действие + расписка о последствиях.

		Схема у них остаётся реестровой (см. ``_build_tool_list``), подменяется
		только конверт ответа: голый текст -> компактный JSON с ``url`` и
		``delta``. Оба меняют состояние страницы и оба умеют «выполниться» вхолостую:
		``send_keys('Enter')`` в форме, которую заблокировала валидация, и
		``select_dropdown`` в кастомном комбобоксе, где выбор не применился, дают
		дословно тот же текст, что и сработавшие.
		"""
		session, _ = await self._ensure_session()
		await self._check_domain_gate(name)
		before = await self._delta_start(session)
		bu_mcp_module('journal').note(url_before=before.get('url'))
		# У send_keys индекса нет, у select_dropdown есть — хендл снимается только
		# там, где ему есть на что смотреть.
		await bu_mcp_module('journal').capture_entry(session, args.get('index'))
		text = await self._run_registry_action(name, args)
		extra = ('active',) if name in DELTA_FOCUS_COUNTS else ()
		delta = await self._delta_end(session, before, extra_significant=extra)
		url = await self._current_url()
		bu_mcp_module('journal').note(url_after=url, delta=delta)
		return self._text({'action': text, 'url': url, 'delta': delta}, compact=True)
