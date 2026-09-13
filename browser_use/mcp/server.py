"""MCP-сервер поверх browser-use: мост к реестру действий + свои переопределения.

Зачем он вместо штатного ``browser_use/mcp/server.py``
-----------------------------------------------------
Штатный сервер держит захардкоженный список из 16 инструментов и разбирает их
if/elif-дispatcher'ом. Наружу из браузерных примитивов он отдаёт всего пять
(navigate / click / type / get_state / screenshot), хотя в ``Tools()`` на момент
написания зарегистрировано 24 действия. Здесь наоборот: список инструментов
строится из ``Tools().registry.registry.actions`` на каждый ``list_tools``, так
что новое действие в browser-use появляется у клиента само, без правок здесь.

Пять инструментов реализованы своими руками, потому что реестр (или штатный
сервер поверх него) в этих местах ведёт себя плохо:

* ``browser_state``      — ``browser_use.mcp.state.serialize_state``: компактное текстовое
  дерево вместо плоского JSON, ОТДЕЛЬНЫМ текстовым блоком (внутри JSON-строки
  каждый перевод строки и таб стоили бы по два символа), и БЕЗ скриншота. Штатный
  ``browser_get_state(include_screenshot=False)`` всё равно зовёт
  ``get_browser_state_summary()`` с дефолтным ``include_screenshot=True``,
  снимает кадр и выбрасывает его. Мы эту трату не воспроизводим.
* ``browser_navigate``   — навигация + ``wait_after_navigation`` с baseline,
  снятым ДО действия, + явная стадия гидрации: реестровый ``navigate``
  возвращает управление до того, как документ дорисовался.
* ``browser_click`` / ``browser_type`` — индекс резолвится через
  ``browser_use.mcp.resolve.resolve_index``; протухший или неоднозначный хендл прилетает
  клиенту ЖЁСТКОЙ ошибкой MCP (``isError=True``), а не мягким «page may have
  changed». После действия — ``wait_for_page_ready``, разбивка стадий в ответе.
* ``browser_hover``      — действия ``hover`` в реестре browser-use НЕТ ВООБЩЕ
  (issue #4964), а обойтись ``evaluate`` нельзя: синтетический
  ``dispatchEvent(new MouseEvent('mouseover'))`` не двигает внутреннюю позицию
  мыши браузера, поэтому CSS ``:hover`` не включается и весь класс интерфейсов
  «показывается только по наведению» (меню, кнопки в строке списка, тултипы,
  мега-меню) остаётся недоступен. Здесь — настоящий ``Input.dispatchMouseEvent``
  типа ``mouseMoved`` в точку внутри элемента, с резолвом индекса как у
  ``browser_click`` и с CDP-сессией фрейма (для кросс-доменных iframe координаты
  фрейм-локальные). Точку вне вьюпорта, в отличие от апстримного клика, НЕ
  зажимаем во вьюпорт — это честная ошибка, а не наведение на случайный пиксель.
* ``browser_screenshot`` — даунскейл до ``max_dim`` (по умолчанию 1024). Размеры
  берутся из PNG и из закешированного состояния; ``get_browser_state_summary()``
  ради них не вызывается — штатный сервер из-за этого перестраивает весь DOM и
  снимает второй кадр.

Плюс четыре реестровых действия проходят через верификацию (схема у них остаётся
реестровой, подменяется только доверие к их рапорту):

* ``scroll``  — позиция прокрутки снимается ДО и ПОСЛЕ. У browser-use текстового
  признака провала нет вовсе: цикл по страницам глотает исключения, а при
  ``pages=1.0`` (дефолт!) строка «Scrolled down Npx» печатается независимо от
  того, сдвинулось ли что-нибудь. Не сдвинулось при наличии запаса прокрутки —
  ``ToolError``; не сдвинулось потому, что мы уже в конце — отдельный честный
  статус ``at-end`` (см. ``browser_use.mcp.actions.scroll``).
* ``switch``  — фактический ``agent_focus_target_id`` после переключения
  сверяется с запрошенным ``tab_id``.
* ``select_dropdown`` / ``send_keys`` — конверт ответа заменён на JSON с
  ``delta`` (см. ниже): оба меняют состояние и оба умеют «выполниться» вхолостую.

И ещё одно общее — РАСПИСКА О ПОСЛЕДСТВИЯХ (issues #5137, #4758). Каждое
действие, меняющее состояние (``browser_click``, ``browser_type``,
``browser_hover``, ``select_dropdown``, ``send_keys``), возвращает ключ ``delta``:
что фактически изменилось на странице между «до» и «после». Это третий класс
отказов, который не видят ни ``error``, ни ``NOOP_MARKERS``: клик прошёл, но
ничего не произошло — оверлей перехватил, валидация формы заблокировала,
обработчик молча вышел. Дельта сама по себе ошибку НЕ поднимает («ничего не
изменилось» — законный исход клика по неактивной кнопке), но если действие
рапортует успех при пустой дельте, ставится флаг ``no_effect``. Цена — один
``Runtime.evaluate`` до и один после (~2-6 мс, ~40-90 символов в ответе);
подробности и вторая ступень — у ``browser_use.mcp.delta``.

И ещё одно общее — ЖУРНАЛ И МАКРОСЫ (JOURNAL_CONTRACT.md). Каждое действие,
меняющее состояние, пишется в ``journal.record`` вместе с полным хендлом
элемента (``resolve.describe_handle`` — ровно тот dict, который принимается
обратно как ``hint``), URL до и после, уже посчитанной дельтой и исходом
(``ok`` / ``noop`` / ``error``). Из журнала собирается макрос
(``journal.to_macro``), который прогоняется без модели в цикле
(``macro.run``) — повтор стоит на хендлах, а не на индексах, поэтому переживает
перезагрузку страницы. Наружу это выведено четырьмя инструментами:
``journal_list`` -> ``macro_save`` -> ``macro_run``, плюс ``macro_list``.
Журнал — наблюдатель: его отсутствие или падение НЕ ломает действие, а цена
записи (медиана 0.90 мс) держится ниже цены дельты, к которой она пристёгнута.

И общее для ВСЕХ действий: ``NoopMixin._action_result_text`` проверяет
``ActionResult`` не только на ``error``, но и по таблице ``NOOP_MARKERS`` — шесть
мест browser-use возвращают «ничего не сделано» обычным успешным результатом
(issues #5361, #5438). Плюс рапорт об авто-переключении на новую вкладку (#5529)
переписывается по фактическому target_id.

Реестровые ``navigate``/``click``/``input``/``screenshot`` наружу не выпускаются:
иначе клиент мог бы обойти резолв индексов и ожидания. Плюс исключены
``done`` (агентский), ``write_file``/``replace_file``/``read_file`` (файловая
система, не браузер) и ``extract`` (требует LLM-ключа, которого здесь нет).

Устройство модуля
-----------------
Логика разложена по подмодулям (см. docs/WORKLOG.md), этот файл только собирает
``BuMcpServer`` из миксинов и регистрирует MCP-хендлеры:

* ``browser_use.mcp.server_shared``   — константы, ошибки, JS-сниппеты, схемы инструментов;
* ``browser_use.mcp.domain_gate``     — allowlist доменов (``DomainGateMixin``);
* ``browser_use.mcp.cdp_session``     — жизненный цикл CDP-сессии (``CdpSessionMixin``);
* ``browser_use.mcp.registry_bridge`` — мост к ``Tools()`` (``RegistryBridgeMixin``);
* ``browser_use.mcp.noop``            — контракт ActionResult (``NoopMixin``);
* ``browser_use.mcp.delta``           — расписка о последствиях (``DeltaMixin``);
* ``browser_use.mcp.actions.*``       — обработчики самих MCP-инструментов, по одному
  модулю на группу (navigate, interact, capture, scroll, switch, files, macros);
* ``browser_use.mcp.journal`` / ``browser_use.mcp.macro`` — журнал действий и повтор макросов.

Запуск: ``python -m browser_use.mcp.server`` (транспорт stdio, как у штатного сервера).

Переменные окружения
--------------------
``BU_MCP_CDP_URL``            CDP живого Chrome, по умолчанию http://127.0.0.1:9222
``BU_MCP_ALLOWED_DOMAINS``    allowlist доменов через запятую, пустая = без ограничений
``BU_MCP_STATE_MAX_CHARS``    дефолтный бюджет дерева для browser_state (40000)
``BU_MCP_HYDRATE_TIMEOUT``    дефолтный бюджет стадии гидрации в browser_navigate (3.0)
``BU_MCP_HEADLESS``           режим браузера, который browser-use поднимет САМ (по умолчанию 1)
``BU_MCP_JOURNAL``            0 полностью выключает журнал (читает browser_use.mcp.journal)
``BU_MCP_HOME``               корень журналов и макросов (по умолчанию ~/.config/bu-mcp)
"""

from __future__ import annotations

import os
import sys

# До любого импорта browser_use: иначе его логи уедут в stdout и порвут JSON-RPC.
os.environ.setdefault('BROWSER_USE_LOGGING_LEVEL', 'critical')
os.environ.setdefault('BROWSER_USE_SETUP_LOGGING', 'false')
os.environ.setdefault('ANONYMIZED_TELEMETRY', 'false')

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

logging.basicConfig(stream=sys.stderr, level=logging.WARNING, force=True)

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from browser_use.browser import BrowserSession
from browser_use.filesystem.file_system import FileSystem
from browser_use.mcp.actions.capture import CaptureActionsMixin
from browser_use.mcp.actions.files import FilesActionsMixin
from browser_use.mcp.actions.interact import InteractActionsMixin
from browser_use.mcp.actions.macros import MacroToolsMixin
from browser_use.mcp.actions.navigate import NavigateActionsMixin
from browser_use.mcp.actions.scroll import ScrollActionsMixin
from browser_use.mcp.actions.switch import SwitchActionsMixin
from browser_use.mcp.cdp_session import CdpSessionMixin
from browser_use.mcp.delta import DeltaMixin
from browser_use.mcp.domain_gate import DomainGateMixin
from browser_use.mcp.noop import NoopMixin
from browser_use.mcp.registry_bridge import RegistryBridgeMixin
from browser_use.mcp.server_shared import BRIDGE_EXCLUDE, JOURNALED_TOOLS, SECURITY_BOUNDARY, ToolError, bu_mcp_module
from browser_use.mcp.server_shared import (
	JOURNAL_FIELDS as JOURNAL_FIELDS,  # re-exported: browser_use.mcp.smoke reads it off this module
)
from browser_use.mcp.server_shared import (
	NEW_TAB_CLAIM_RE as NEW_TAB_CLAIM_RE,  # re-exported: benchmark/upstream.py reads it off this module
)
from browser_use.mcp.server_shared import (
	NEW_TAB_NOTE_RE as NEW_TAB_NOTE_RE,  # re-exported: benchmark/upstream.py reads it off this module
)
from browser_use.mcp.server_shared import (
	NOOP_MARKERS as NOOP_MARKERS,  # re-exported: benchmark/upstream.py reads it off this module
)
from browser_use.tools.service import Tools

logger = logging.getLogger('browser_use.mcp.server')


class BuMcpServer(
	DomainGateMixin,
	CdpSessionMixin,
	RegistryBridgeMixin,
	NoopMixin,
	DeltaMixin,
	NavigateActionsMixin,
	InteractActionsMixin,
	CaptureActionsMixin,
	ScrollActionsMixin,
	SwitchActionsMixin,
	FilesActionsMixin,
	MacroToolsMixin,
):
	"""MCP-фасад над browser-use: реестр действий + пять переопределений."""

	def __init__(self) -> None:
		self.server: Server = Server('bu-mcp')
		self._session: BrowserSession | None = None
		self._tools: Tools | None = None
		self._file_system: FileSystem | None = None
		self._session_lock = asyncio.Lock()
		#: Вьюпорт из последнего serialize_state — чтобы browser_screenshot не
		#: дёргал get_browser_state_summary() ради двух чисел.
		self._last_viewport: dict[str, Any] | None = None
		self._allowed_domains: list[str] = self._parse_allowed_domains()
		self._register_handlers()

	# -- регистрация хендлеров ---------------------------------------------- #

	def _register_handlers(self) -> None:
		@self.server.list_tools()
		async def handle_list_tools() -> list[types.Tool]:
			page_url = await self._current_url()
			return self._build_tool_list(page_url)

		@self.server.list_resources()
		async def handle_list_resources() -> list[types.Resource]:
			return []

		@self.server.list_prompts()
		async def handle_list_prompts() -> list[types.Prompt]:
			return []

		@self.server.call_tool()
		async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> Sequence[types.ContentBlock]:
			args = arguments or {}
			overrides = {
				'browser_state': self._tool_browser_state,
				'browser_navigate': self._tool_browser_navigate,
				'browser_click': self._tool_browser_click,
				'browser_type': self._tool_browser_type,
				'browser_screenshot': self._tool_browser_screenshot,
				# Не подмена инструмента, а верификация реестрового: схему эти два
				# по-прежнему берут из реестра (см. _build_tool_list), меняется
				# только то, что результат сверяется с фактом, а не берётся на веру.
				'browser_hover': self._tool_browser_hover,
				'scroll': self._tool_scroll,
				'switch': self._tool_switch,
				# То же самое для действий, меняющих состояние: схема реестровая,
				# добавлена только расписка о последствиях (delta).
				'select_dropdown': lambda a: self._tool_registry_with_delta('select_dropdown', a),
				'send_keys': lambda a: self._tool_registry_with_delta('send_keys', a),
				# Журнал и макросы: браузер трогает только macro_run, остальные три
				# работают с файлами и отвечают даже при мёртвом Chrome.
				'journal_list': self._tool_journal_list,
				'macro_save': self._tool_macro_save,
				'macro_list': self._tool_macro_list,
				'macro_run': self._tool_macro_run,
				'macro_record': self._tool_macro_record,
				# Единственное наблюдение, которое журналируется: оно становится
				# шагом-проверкой макроса.
				'checkpoint': self._tool_checkpoint,
				# Загрузка файла из папки вложений: свой обработчик вместо моста.
				'upload_file': self._tool_upload_file,
			}
			if name in overrides:
				run = overrides[name]
				# Действия, меняющие состояние, идут через журнал: он пишет любой
				# исход, включая отказ, и никогда не мешает самому действию.
				if name in JOURNALED_TOOLS:
					return await bu_mcp_module('journal').run_journaled(name, args, run)
				return await run(args)

			tools = self._registry_tools()
			if name in BRIDGE_EXCLUDE or name not in tools.registry.registry.actions:
				raise ToolError(f'Unknown tool: {name}')

			return self._text(await self._run_registry_action(name, args))

	# -- жизненный цикл ----------------------------------------------------- #

	async def close(self) -> None:
		if self._session is not None:
			try:
				# stop(), не kill(): Chrome не наш, мы к нему только подключились.
				await self._session.stop()
			except Exception:
				pass
			self._session = None

	async def run(self) -> None:
		if sys.stdin is None:
			raise RuntimeError('MCP stdio transport requires stdin, but this process was launched without one.')

		instructions = (
			f'{SECURITY_BOUNDARY}\n\n'
			'Browser automation over a live Chrome instance (browser-use under the hood).\n\n'
			'Workflow: browser_navigate -> browser_state -> browser_click / browser_type / '
			'browser_hover by the indices you saw in browser_state. Indices are only valid for the '
			'snapshot they came from; if an element moved or vanished, the call fails loudly instead '
			'of clicking something else. Take a fresh browser_state and retry.\n\n'
			'Use browser_hover for anything that only appears on pointer hover (menus, row action '
			'buttons, tooltips): a synthetic MouseEvent from evaluate() cannot do this, it does not '
			'move the browser pointer and does not trigger CSS :hover.\n\n'
			'Every state-changing action returns a `delta` key: what measurably changed on the page. '
			'`delta.no_effect` means the action reported success but nothing changed — treat that step '
			'as NOT done and check browser_state before continuing.\n\n'
			'Every state-changing action is also written to a journal with the element handle it used. '
			'Teach-then-replay: when the user walks you through a task they will want repeated, call '
			'macro_record action="start" with a name first, do the task (add a checkpoint after every action '
			'whose result matters — a reply appeared, a page changed), then macro_record action="stop": the '
			'macro is built from exactly that stretch of the journal. macro_run replays it with no model in '
			'the loop; so does `python -m browser_use.mcp.macro run NAME` from a shell. Replay re-identifies elements '
			'from their handles, so it survives a reload that invalidates every index; a step that cannot be '
			'reproduced, or a checkpoint that does not hold, fails the call at that step. To repair: redo the '
			'work from that step under macro_record and stop with replace_from=N.\n\n'
			'Domain policy: a single allowlist from BU_MCP_ALLOWED_DOMAINS (comma separated, empty '
			'means unrestricted). There is no deny list, so there is no allow-vs-deny precedence to '
			'reason about: what is not listed is blocked.'
		)

		async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
			try:
				await self.server.run(
					read_stream,
					write_stream,
					InitializationOptions(
						server_name='bu-mcp',
						server_version='0.1.0',
						capabilities=self.server.get_capabilities(
							notification_options=NotificationOptions(),
							experimental_capabilities={},
						),
						instructions=instructions,
					),
				)
			except BrokenPipeError:
				logger.warning('MCP client disconnected; shutting down cleanly.')
			finally:
				await self.close()


async def main() -> None:
	await BuMcpServer().run()


if __name__ == '__main__':
	asyncio.run(main())
