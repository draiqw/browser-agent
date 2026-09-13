"""``upload_file``: ``FilesActionsMixin`` для ``BuMcpServer``.

Вынесено из server.py при разбиении на подмодули (docs/WORKLOG.md).
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from typing import Any

import mcp.types as types

from browser_use.browser import BrowserSession
from bu_mcp.server_shared import ToolError, bu_mcp_module


class FilesActionsMixin:
	async def _find_file_input(self, session: BrowserSession, index: Any) -> tuple[Any, int]:
		"""Найти ``<input type=file>`` для загрузки. Возвращает (узел, живой индекс).

		``index`` задан — берём его (или ближайший к нему file input); иначе ищем
		по всей карте. Скрытость роли не играет: ``setFileInputFiles`` работает и
		по невидимому input, а сайты (ChatGPT) держат его именно спрятанным.
		Предпочитаем input без ограничения ``accept`` (принимает любой файл).
		"""
		if index is not None:
			node, _live, _info = await self._resolve(session, int(index), what='attached to')
			target = node if session.is_file_input(node) else session.find_file_input_near_element(node)
			if target is None:
				raise ToolError(
					f'Element [{index}] is not a file input and no file input sits near it. Point index at the '
					f'attach control, or omit index to search the whole page.'
				)
			return target, session.get_selector_index(target)

		selector_map = await session.get_selector_map()
		inputs = [n for n in selector_map.values() if session.is_file_input(n)]
		if not inputs:
			raise ToolError(
				'No file input is present on the page. Many sites (ChatGPT included) mount it only after you '
				'focus or click the composer / attach control — do that first, then call upload_file. '
				'If an attach button opens a menu, click it, then upload_file.'
			)
		# Предпочтение: без accept (любой тип) -> с accept -> первый попавшийся.
		inputs.sort(key=lambda n: 0 if not ((n.attributes or {}).get('accept') or '').strip() else 1)
		chosen = inputs[0]
		return chosen, session.get_selector_index(chosen)

	async def _file_input_count(self, session: BrowserSession, node: Any) -> int | None:
		"""Сколько файлов реально висит на input. ``None`` — пробу снять не удалось (fail-open)."""
		try:
			cdp = await session.cdp_client_for_node(node)
			resolved = await cdp.cdp_client.send.DOM.resolveNode(
				params={'backendNodeId': node.backend_node_id}, session_id=cdp.session_id
			)
			object_id = resolved.get('object', {}).get('objectId')
			if not object_id:
				return None
			try:
				out = await cdp.cdp_client.send.Runtime.callFunctionOn(
					params={
						'functionDeclaration': 'function(){ return this.files ? this.files.length : -1; }',
						'objectId': object_id,
						'returnByValue': True,
					},
					session_id=cdp.session_id,
				)
			finally:
				# Хендл удалённого объекта живёт до смены контекста — отпускаем сами,
				# как это делает resolve.py.
				with contextlib.suppress(Exception):
					await cdp.cdp_client.send.Runtime.releaseObject(params={'objectId': object_id}, session_id=cdp.session_id)
			value = out.get('result', {}).get('value')
			return int(value) if isinstance(value, (int, float)) and value >= 0 else None
		except Exception:
			return None

	async def _tool_upload_file(self, args: dict[str, Any]) -> Sequence[types.ContentBlock]:
		"""Приложить файл из папки вложений к ``<input type=file>`` страницы.

		Без ``file`` — список того, что в папке (агенту надо знать, чем можно
		грузить). С ``file`` — резолвим имя ВНУТРИ папки (граница безопасности),
		находим file input, ставим файл через реестровое ``upload_file`` с
		точечным allowlist из одного этого пути и сверяем фактом (``files.length``).
		"""
		uploads = bu_mcp_module('uploads')
		raw = args.get('file') if args.get('file') is not None else args.get('path')

		if not raw:
			names = uploads.list_names()
			return self._text(
				{
					'action': (
						f'{len(names)} file(s) in the upload folder. Attach one with upload_file(file="<name>").'
						if names
						else 'The upload folder is empty. Put a file there, then upload_file(file="<name>").'
					),
					'dir': str(uploads.upload_dir()),
					'files': names,
				},
				compact=True,
			)

		try:
			abs_path = uploads.resolve(str(raw))
		except uploads.NotAllowedError as exc:
			raise ToolError(str(exc)) from exc
		rel = uploads.relative_name(abs_path)

		waiting_mod = bu_mcp_module('waiting')
		journal_mod = bu_mcp_module('journal')
		session, tools = await self._ensure_session()
		await self._check_domain_gate('upload_file')

		file_node, live_index = await self._find_file_input(session, args.get('index'))
		await journal_mod.capture_entry(session, live_index)
		before = await self._delta_start(session)
		# В журнал/макрос кладём ИМЯ в папке, не абсолютный путь: сценарий должен
		# переноситься на машину, где папка лежит по другому пути.
		journal_mod.note(url_before=before.get('url'), resolved_index=live_index, params={'file': rel})

		try:
			result = await tools.registry.execute_action(
				'upload_file',
				{'index': live_index, 'path': abs_path},
				browser_session=session,
				file_system=self._file_system,
				# Точечный allowlist: ровно этот путь. Мы его уже проверили внутри
				# папки — второй раз считать весь каталог незачем.
				available_file_paths=[abs_path],
			)
		except Exception as exc:
			raise ToolError(f'upload_file({rel!r}) failed: {type(exc).__name__}: {exc}') from exc

		action_text = self._action_result_text('upload_file', result)
		attached = await self._file_input_count(session, file_node)
		waiting = await waiting_mod.wait_for_page_ready(session, timeout=float(args.get('timeout') or 8.0))
		delta = await self._delta_end(session, before)
		url = await self._current_url()
		journal_mod.note(url_after=url, delta=delta)

		if attached == 0:
			# Реестр отчитался об успехе, но на input ноль файлов — тот самый
			# тихий нооп, против которого написан весь слой. Это ошибка, не отчёт.
			raise ToolError(
				f'upload_file({rel!r}) reported success but the file input holds 0 files. The input may be the '
				f'wrong one, or the site rejected the type. Check browser_state and the attach control.'
			)
		return self._text(
			{
				# Проба не всегда снимается (CDP мог не ответить). Тогда говорим об этом
				# прямо в тексте, а не выдаём непроверенное за проверенное.
				'action': (
					f'Attached {rel!r} to the page.'
					if attached
					else f'Attached {rel!r}, but could NOT verify it landed on the input. Check browser_state.'
				),
				'file': rel,
				'files_on_input': attached,
				'verified': attached is not None,
				'url': url,
				'delta': delta,
				'upstream': action_text,
			},
			compact=True,
		)
