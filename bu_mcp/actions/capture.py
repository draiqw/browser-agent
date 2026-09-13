"""``browser_state`` / ``browser_screenshot``: ``CaptureActionsMixin`` для ``BuMcpServer``.

Вынесено из server.py при разбиении на подмодули (docs/WORKLOG.md).
"""

from __future__ import annotations

import base64
import io
from typing import Any

import mcp.types as types

from bu_mcp.server_shared import DEFAULT_SCREENSHOT_MAX_DIM, DEFAULT_STATE_MAX_CHARS, ToolError, bu_mcp_module


class CaptureActionsMixin:
	# --- browser_state ------------------------------------------------------- #

	async def _tool_browser_state(self, args: dict[str, Any]) -> list[types.ContentBlock]:
		state_mod = bu_mcp_module('state')
		session, _ = await self._ensure_session()
		await self._check_domain_gate('browser_state')
		max_chars = int(args.get('max_chars') or DEFAULT_STATE_MAX_CHARS)
		state = await state_mod.serialize_state(session, max_chars=max_chars)
		# Кешируем вьюпорт для browser_screenshot: он не должен ради размеров
		# перестраивать DOM через get_browser_state_summary().
		if state.get('viewport'):
			self._last_viewport = {**state['viewport'], 'url': state.get('url')}
		# Два блока, а не один JSON: см. _state_header.
		return [
			types.TextContent(type='text', text=self._json(self._state_header(state), compact=True)),
			types.TextContent(type='text', text=state.get('tree') or '[empty page]'),
		]

	@staticmethod
	def _state_header(state: dict[str, Any]) -> dict[str, Any]:
		"""Конверт состояния: всё, кроме самого дерева.

		Дерево уезжает ОТДЕЛЬНЫМ текстовым блоком MCP-ответа, а не полем внутри
		JSON. Строковое поле JSON обязано экранировать каждый перевод строки и
		каждый таб — а дерево из них состоит: на coursera это 1031 символ, за
		которые модель платит и не получает ничего. Отдельный блок стоит ноль.

		Конверт ужат до того, что клиенту действительно нужно на КАЖДОМ вызове.
		Убрано (и почему):

		* ``scroll.pixels_above/below``, ``pages_above/below`` и
		  ``elements.hidden_above/below`` — дословный дубль того, что дерево уже
		  печатает в своих маркерах ``[Start of page]`` / ``... (N more elements
		  below - scroll to reveal)``. Одно и то же число дважды в одном ответе.
		* ``elements.visibility_threshold_px`` — константа сборки (1000), она не
		  меняется от вызова к вызову и от страницы не зависит.
		* ``page``, когда он совпадает с вьюпортом — то есть когда страница не
		  прокручивается и говорить не о чем.
		* заголовки чужих вкладок — url их опознаёт, а название дублирует его же
		  словами; заголовок текущей вкладки остаётся на верхнем уровне.
		* полные 32-символьные ``target_id`` -> ``tab_id`` из последних 4
		  символов. Это не усечение ради байтов: ровно эти 4 символа принимают
		  ``switch``/``close`` (``browser_use/tools/service.py:1005``), так что
		  клиенту теперь не нужно догадываться, что от id надо взять хвост.

		Осталось ровно то, что нельзя восстановить из дерева: где мы (url,
		title), сколько всего индексов (elements), геометрия окна, позиция
		скролла, флаг усечения, список вкладок и href_map для плейсхолдеров.
		"""

		def dims(box: Any) -> str | None:
			if not isinstance(box, dict):
				return None
			w, h = box.get('width'), box.get('height')
			return f'{w}x{h}' if w is not None and h is not None else None

		scroll = state.get('scroll') or {}
		viewport = dims(state.get('viewport'))
		page = dims(state.get('page'))

		tabs: list[dict[str, Any]] = []
		for tab in state.get('tabs') or []:
			url = str(tab.get('url') or '')
			entry: dict[str, Any] = {
				'tab_id': str(tab.get('target_id') or '')[-4:],
				'url': url if len(url) <= 120 else url[:119] + '…',
			}
			if tab.get('current'):
				entry['current'] = True
			tabs.append(entry)

		header: dict[str, Any] = {
			'url': state.get('url'),
			'title': state.get('title'),
			'elements': (state.get('elements') or {}).get('interactive'),
			'viewport': viewport,
			'scroll': f'{scroll.get("x", 0)},{scroll.get("y", 0)}',
			'truncated': bool(state.get('truncated')),
			'tabs': tabs,
		}
		if page and page != viewport:
			header['page'] = page
		# Последним: на плотных страницах карта длиннее всего остального вместе взятого.
		if state.get('href_map'):
			header['href_map'] = state['href_map']
		return header

	# --- browser_screenshot ------------------------------------------------ #

	async def _tool_browser_screenshot(self, args: dict[str, Any]) -> list[types.ContentBlock]:
		session, _ = await self._ensure_session()
		await self._check_domain_gate('screenshot')

		max_dim = int(args.get('max_dim') or DEFAULT_SCREENSHOT_MAX_DIM)
		full_page = bool(args.get('full_page', False))
		try:
			raw = await session.take_screenshot(full_page=full_page)
		except Exception as exc:
			raise ToolError(f'screenshot failed: {type(exc).__name__}: {exc}') from exc

		data, meta = self._downscale_png(raw, max_dim)
		meta['full_page'] = full_page
		# Вьюпорт — из последнего снятого состояния. Ради двух чисел не зовём
		# get_browser_state_summary(): он перестраивает DOM и снимает ещё один
		# кадр (ровно то, что делает штатный сервер). Навигация кеш не сбрасывает:
		# вьюпорт — свойство окна, а не документа, и от перехода не меняется.
		# На всякий случай в ответе указано, с какой страницы он снят.
		if self._last_viewport:
			meta['viewport'] = {k: v for k, v in self._last_viewport.items() if k in ('width', 'height')}
			meta['viewport_source'] = f'cached from browser_state on {self._last_viewport.get("url")}'
		else:
			meta['viewport'] = None
			meta['viewport_source'] = 'unknown (call browser_state first; not fetched to avoid a DOM rebuild)'

		return [
			types.TextContent(type='text', text=self._json(meta, compact=True)),
			types.ImageContent(type='image', data=base64.b64encode(data).decode(), mimeType='image/png'),
		]

	@staticmethod
	def _downscale_png(raw: bytes, max_dim: int) -> tuple[bytes, dict[str, Any]]:
		try:
			from PIL import Image
		except Exception:
			return raw, {'size_bytes': len(raw), 'downscaled': False, 'note': 'Pillow unavailable, image returned as captured'}

		with Image.open(io.BytesIO(raw)) as img:
			width, height = img.size
			meta: dict[str, Any] = {'captured': {'width': width, 'height': height}}
			longest = max(width, height)
			if longest <= max_dim:
				meta.update({'size_bytes': len(raw), 'downscaled': False, 'image': {'width': width, 'height': height}})
				return raw, meta
			scale = max_dim / longest
			new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
			resized = img.convert('RGB').resize(new_size, Image.Resampling.LANCZOS)
			buf = io.BytesIO()
			resized.save(buf, format='PNG', optimize=True)
			out = buf.getvalue()

		meta.update(
			{
				'size_bytes': len(out),
				'downscaled': True,
				'scale': round(scale, 4),
				'image': {'width': new_size[0], 'height': new_size[1]},
			}
		)
		return out, meta
