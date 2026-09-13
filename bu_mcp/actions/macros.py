"""Журнал и макросы: тонкие обработчики MCP-инструментов. ``MacroToolsMixin``.

Тяжёлая логика (сборка макроса, резолв переменных, вычисление адресов для
доменного гейта) живёт в ``bu_mcp.journal`` и ``bu_mcp.macro`` — эти методы
только достают аргументы, зовут её и заворачивают ответ.

Вынесено из server.py при разбиении на подмодули (docs/WORKLOG.md).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import mcp.types as types

from bu_mcp.server_shared import ToolError, bu_mcp_module


class MacroToolsMixin:
	def _macro_domain_gate(self, macro: dict[str, Any], values: dict[str, Any] | None = None) -> None:
		"""Проверить allowlist по адресам, на которые макрос уведёт браузер.

		Без этого макрос был бы дырой в доменной политике: его шаги исполняет
		``macro.py`` напрямую, минуя ``_check_domain_gate``, поэтому сохранённый
		когда-то ``browser_navigate`` увёл бы браузер куда угодно. Проверка идёт
		ДО первого шага: частично выполненный запрещённый макрос хуже, чем
		невыполненный.

		``values`` — значения переменных этого прогона (см. ``macro.resolve_values``).
		Без них проверялось бы содержимое файла, а исполнялось бы что-то другое.
		"""
		if not self._allowed_domains:
			return
		macro_mod = bu_mcp_module('macro')
		for url, where in macro_mod.urls_for_gate(macro, values):
			if not self._domain_allowed(url, treat_blank_as_allowed=False):
				raise ToolError(
					f'This macro would take the browser to {url!r} ({where}), which does not match '
					f'BU_MCP_ALLOWED_DOMAINS ({", ".join(self._allowed_domains)}). Refusing to run '
					f'any of it: macro steps execute directly and would bypass the per-action gate. '
					f'Note that this check runs on the values this call would actually use, so '
					f'overriding a variable such as `start_url` does not get around it.'
				)

	async def _tool_journal_list(self, args: dict[str, Any]) -> Sequence[types.ContentBlock]:
		"""Что записалось. Позиция ``i`` — абсолютная, её принимает ``macro_save(include=...)``."""
		journal_mod = bu_mcp_module('journal')
		entries, path = journal_mod.read_entries(args)
		try:
			shown_path = str(path or journal_mod.current_path())
		except Exception:
			shown_path = str(path or '<unknown>')

		total = len(entries)
		limit = max(1, int(args.get('limit') or 50))
		start = max(0, total - limit)
		full = bool(args.get('full', False))
		rows = []
		for offset, entry in enumerate(entries[start:]):
			row = dict(entry) if full else journal_mod.summary(entry)
			rows.append({'i': start + offset, **row})
		return self._text(
			{
				'action': f'{len(rows)} of {total} journal entr(ies) from {shown_path}.',
				'path': shown_path,
				'total': total,
				'returned': len(rows),
				'entries': rows,
			},
			compact=True,
		)

	async def _tool_macro_save(self, args: dict[str, Any]) -> Sequence[types.ContentBlock]:
		"""Схлопнуть выбранные записи журнала в макрос и положить его на диск."""
		macro_mod = bu_mcp_module('macro')
		journal_mod = bu_mcp_module('journal')
		name = macro_mod.validate_macro_name(args.get('name'))
		entries, _path = journal_mod.read_entries(args)
		picked, selected_by = macro_mod.pick_journal_entries(journal_mod, entries, args, name=name)
		replace_from = args.get('replace_from')
		payload = macro_mod.build_and_save(
			journal_mod,
			name,
			picked,
			replace_from=None if replace_from is None else int(replace_from),
			selected_by=selected_by,
		)
		return self._text(payload, compact=True)

	async def _tool_macro_record(self, args: dict[str, Any]) -> Sequence[types.ContentBlock]:
		"""Закладки в журнале: начало/конец обучения сценарию.

		``start`` при уже открытой записи — отказ, а не тихое переоткрытие:
		две вложенные записи означали бы, что одна из них потеряна, и лучше
		сказать об этом сразу, чем сохранить не то.
		"""
		macro_mod = bu_mcp_module('macro')
		journal_mod = bu_mcp_module('journal')
		action = str(args.get('action') or '').strip().lower()
		current = journal_mod.recording() if hasattr(journal_mod, 'recording') else None

		if action == 'status':
			if not current:
				return self._text({'action': 'No recording is open.', 'recording': None}, compact=True)
			entries = journal_mod.read()
			since = int(current.get('since') or 0)
			return self._text(
				{
					'action': f'Recording {current["name"]!r} is open: {max(0, len(entries) - since)} journal entr(ies) so far.',
					'recording': current,
					'entries_so_far': max(0, len(entries) - since),
				},
				compact=True,
			)

		if action == 'start':
			name = macro_mod.validate_macro_name(args.get('name'))
			if current:
				raise ToolError(
					f'A recording named {current["name"]!r} is already open. Stop it first '
					f'(macro_record action="stop") — nested recordings would lose one of them.'
				)
			if not journal_mod.enabled():
				raise ToolError('The journal is disabled (BU_MCP_JOURNAL=0), so nothing can be recorded.')
			marker = journal_mod.mark('start', name)
			if not marker:
				raise ToolError('Could not write the start marker to the journal; see the server log.')
			exists = macro_mod.macro_file_path(name).exists()
			return self._text(
				{
					'action': (
						f'Recording {name!r} started. Work through the scenario now; every state-changing action '
						f'and checkpoint is captured. Retries and mistakes are fine, they are collapsed away. '
						f'When the goal is reached, call macro_record action="stop".'
						+ (
							f' A macro named {name!r} already exists: stop will overwrite it, or pass replace_from to repair it.'
							if exists
							else ''
						)
					),
					'name': name,
					'seq': marker.get('seq'),
					'journal': str(journal_mod.current_path()),
					'overwrites_existing': exists,
				},
				compact=True,
			)

		if action == 'stop':
			name = macro_mod.validate_macro_name(args.get('name') or (current or {}).get('name'))
			if not current and not args.get('name'):
				raise ToolError('No recording is open and no name was given. Call macro_record action="start" first.')
			marker = journal_mod.mark('stop', name)
			if marker.get('mismatch'):
				# Остановили не ту запись: открытая осталась открытой, и говорить
				# «записано» про чужое имя — врать агенту.
				raise ToolError(
					f'The open recording is {marker["mismatch"]!r}, not {name!r}; it is still open. '
					f'Stop it by its own name, or pass that name explicitly.'
				)
			entries = journal_mod.read()
			found = journal_mod.span(entries, name=name)
			if not found:
				raise ToolError(
					f'No start marker for {name!r} in the current journal. Call macro_record action="start" first; '
					f'or use macro_save with explicit `include` positions.'
				)
			positions, info = found
			picked = [entries[i] for i in positions]
			save = True if args.get('save') is None else bool(args.get('save'))
			if not save:
				return self._text(
					{
						'action': f'Recording {name!r} closed with {len(picked)} journal entr(ies); not saved (save=false).',
						'name': name,
						'entries': len(picked),
						'seq': marker.get('seq'),
					},
					compact=True,
				)
			replace_from = args.get('replace_from')
			payload = macro_mod.build_and_save(
				journal_mod,
				name,
				picked,
				replace_from=None if replace_from is None else int(replace_from),
				selected_by=f'recording {name!r}',
			)
			return self._text(payload, compact=True)

		raise ToolError(f'Unknown action {action!r}: expected start, stop or status.')

	async def _tool_checkpoint(self, args: dict[str, Any]) -> Sequence[types.ContentBlock]:
		"""Проверить условие на странице, дождавшись его. Не выполнилось — ``ToolError``.

		Считает та же ``macro.checkpoint``, что и при повторе. Провал — ошибка,
		а не ``{'ok': false}``: агент, который «валидирует, если нужно», должен
		видеть невыполненное условие так же громко, как промах по элементу.
		"""
		macro_mod = bu_mcp_module('macro')
		journal_mod = bu_mcp_module('journal')
		try:
			spec = macro_mod.checkpoint_spec(args)
		except ValueError as exc:
			raise ToolError(str(exc)) from exc
		session, _ = await self._ensure_session()
		await self._check_domain_gate('checkpoint')
		journal_mod.note(url_before=await self._current_url())
		try:
			verdict = await macro_mod.checkpoint(session, spec)
		except macro_mod.StepFailed as exc:
			raise ToolError(
				f'{exc} The condition did not hold — the previous action may not have had its effect yet, '
				f'or it produced something else. Take browser_state to see what is actually there.'
			) from exc
		url = verdict.get('url') or await self._current_url()
		journal_mod.note(url_after=url)
		summary = ', '.join(f'{k}={v!r}' for k, v in spec.items() if k in ('text', 'not_text', 'url', 'download'))
		return self._text(
			{
				'action': f'Checkpoint held after {verdict.get("waited")}s: {summary}.',
				'url': url,
				'waited': verdict.get('waited'),
				'attempts': verdict.get('attempts'),
				'snippet': verdict.get('snippet'),
				'downloaded': verdict.get('downloaded'),
				'timeout': spec.get('timeout'),
			},
			compact=True,
		)

	async def _tool_macro_list(self, args: dict[str, Any]) -> Sequence[types.ContentBlock]:
		"""Без имени — что сохранено; с именем — макрос целиком."""
		macro_mod = bu_mcp_module('macro')
		directory = macro_mod.macro_dir()
		if args.get('name'):
			name = macro_mod.validate_macro_name(args['name'])
			path = macro_mod.macro_file_path(name)
			if not path.exists():
				raise ToolError(f'No macro named {name!r} in {directory}. Call macro_list without a name to see what is saved.')
			try:
				macro = json.loads(path.read_text(encoding='utf-8'))
			except Exception as exc:
				raise ToolError(f'Macro {name!r} at {path} is not readable JSON: {type(exc).__name__}: {exc}') from exc
			return self._text(
				{
					'action': f'Macro {name!r}: {len(macro.get("steps") or [])} step(s).',
					'name': name,
					'file': str(path),
					'macro': macro,
				},
				compact=True,
			)

		rows: list[dict[str, Any]] = []
		for path in sorted(directory.glob('*.json')) if directory.exists() else []:
			row: dict[str, Any] = {'name': path.stem, 'file': str(path)}
			try:
				macro = json.loads(path.read_text(encoding='utf-8'))
			except Exception as exc:
				row['error'] = f'{type(exc).__name__}: {exc}'
			else:
				row['steps'] = len(macro.get('steps') or [])
				row['tools'] = [s.get('tool') for s in (macro.get('steps') or []) if isinstance(s, dict)]
				row['vars'] = sorted((macro.get('vars') or {}).keys()) if isinstance(macro.get('vars'), dict) else []
			rows.append(row)
		return self._text(
			{'action': f'{len(rows)} macro(s) in {directory}.', 'dir': str(directory), 'macros': rows},
			compact=True,
		)

	async def _tool_macro_run(self, args: dict[str, Any]) -> Sequence[types.ContentBlock]:
		"""Прогнать сохранённый макрос. Провал шага — ЖЁСТКАЯ ошибка, а не отчёт.

		``strict`` управляет только тем, останавливаться ли на первом расхождении
		или дойти до конца, собрав их все. На видимость провала он НЕ влияет:
		``ok == False`` — это ``ToolError`` в обоих режимах. Иначе получилось бы
		ровно то, против чего написан весь остальной слой: успешный на вид ответ,
		внутри которого лежит невыполненный сценарий.
		"""
		macro_mod = bu_mcp_module('macro')
		name = macro_mod.validate_macro_name(args.get('name'))
		path = macro_mod.macro_file_path(name)
		if not path.exists():
			raise ToolError(f'No macro named {name!r} in {macro_mod.macro_dir()}. Call macro_list to see what is saved.')
		try:
			macro = json.loads(path.read_text(encoding='utf-8'))
		except Exception as exc:
			raise ToolError(f'Macro {name!r} at {path} is not readable JSON: {type(exc).__name__}: {exc}') from exc

		strict = True if args.get('strict') is None else bool(args.get('strict'))
		variables = args.get('vars') or {}
		if not isinstance(variables, dict):
			raise ToolError(f'`vars` must be an object, got {type(variables).__name__}.')
		from_step = max(1, int(args.get('from_step') or 1))
		total_steps = len(macro.get('steps') or [])
		if from_step > total_steps:
			raise ToolError(f'from_step={from_step} is past the end of macro {name!r}, which has {total_steps} step(s).')

		session, _ = await self._ensure_session()
		await self._check_domain_gate('macro_run')
		# Гейт считает те же значения переменных, что и сам прогон: проверять файл,
		# а исполнять подставленное — это и есть обход allowlist через vars.
		self._macro_domain_gate(macro, macro_mod.resolve_values(macro, variables))

		new_tab: str | None = None
		if args.get('new_tab'):
			from browser_use.browser.session import create_target_params

			try:
				created = await session.cdp_client.send.Target.createTarget(params=create_target_params({'url': 'about:blank'}))
				new_tab = str(created['targetId'])
				await session.get_or_create_cdp_session(new_tab, focus=True)
			except Exception as exc:
				raise ToolError(f'Could not open a tab for the macro: {type(exc).__name__}: {exc}') from exc

		try:
			out = await macro_mod.run(session, macro, vars=variables, strict=strict, from_step=from_step)
		except ToolError:
			raise
		except Exception as exc:
			raise ToolError(f'Macro {name!r} FAILED: {type(exc).__name__}: {exc}. Nothing beyond the failing step ran.') from exc

		if not isinstance(out, dict):
			raise ToolError(f'macro.run returned {type(out).__name__}, expected the contract dict with `ok`/`steps`.')

		payload: dict[str, Any] = {'name': name, 'strict': strict, 'file': str(path), **out}
		if new_tab:
			payload['tab'] = self._short_tab_id(new_tab)
		if not out.get('ok'):
			failed_at = out.get('failed_at')
			payload['action'] = f'Macro {name!r} FAILED at step {failed_at}.'
			raise ToolError(
				f'Macro {name!r} FAILED at step {failed_at} (strict={strict}). The remaining '
				f'steps did not run in strict mode. To repair: fix the page by hand and macro_run(from_step={failed_at}), '
				f'or macro_record start name="{name}", redo the work from step {failed_at} on, and stop with '
				f'replace_from={failed_at}. Full report: {self._json(payload, compact=True)}'
			)
		payload['action'] = f'Macro {name!r} replayed {len(out.get("steps") or [])} step(s), all verified.'
		return self._text(payload, compact=True)
