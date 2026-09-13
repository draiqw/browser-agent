"""Жизненный цикл CDP-сессии: ``CdpSessionMixin`` для ``BuMcpServer``.

Вынесено из server.py при разбиении на подмодули (docs/WORKLOG.md).
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.filesystem.file_system import FileSystem
from browser_use.tools.service import Tools
from bu_mcp.server_shared import ToolError, bu_mcp_module, cdp_reachable

logger = logging.getLogger('bu_mcp.server')


class CdpSessionMixin:
	_session: BrowserSession | None
	_tools: Tools | None
	_file_system: FileSystem | None
	_session_lock: asyncio.Lock

	# -- сессия ------------------------------------------------------------- #

	@staticmethod
	def _headless() -> bool:
		"""Режим браузера, который browser-use поднимет САМ. По умолчанию headless."""
		import os

		return os.getenv('BU_MCP_HEADLESS', '1').strip().lower() not in ('0', 'false', 'no', 'off')

	@classmethod
	def _profile(cls, cdp_url: str) -> BrowserProfile:
		"""Профиль подключения. ``headless`` проставляется ЯВНО, viewport — не трогается.

		Про headless. Мы всегда ПОДКЛЮЧАЕМСЯ к уже работающему Chrome по
		``cdp_url``, а в этом режиме флаг ни на что не влияет: режим окна
		определился при запуске браузера, и ``on_BrowserStartEvent`` вообще не
		доходит до ветки запуска (``if not self.cdp_url``). То есть это НЕ защита
		от выскакивающего окна в нормальной работе — считать её таковой нельзя.
		Значение имеет ровно один путь: если ``BU_MCP_CDP_URL`` окажется пустым
		или недостижимым так, что browser-use решит поднять браузер сам. Тогда
		без явного значения ``headless`` остаётся ``None``, и
		``detect_display_configuration`` выводит его из наличия дисплея — на
		машине владельца это ``False``, то есть окно поверх всего. Ставим явно,
		чтобы значение не зависело ни от дисплея, ни от ``config.json``
		(его наш путь и так не читает: ``BrowserProfile`` конструируется здесь
		напрямую, а ``load_browser_use_config`` живёт в CLI/штатном MCP).

		Про viewport. ``headless=True`` тянет за собой побочку, которая на пути
		ПОДКЛЮЧЕНИЯ уже совсем не безобидна: ``detect_display_configuration``
		выставляет ``viewport = screen`` и ``no_viewport = False``, после чего
		browser-use шлёт ``Emulation.setDeviceMetricsOverride`` на каждую вкладку,
		которую создаёт ИЛИ на которую переводит фокус
		(``on_TabCreatedEvent`` / ``on_AgentFocusChangedEvent``). А фокус он
		переводит в том числе автоматически — на произвольную соседнюю вкладку,
		когда наша отсоединяется. Это значит «поменять размер вьюпорта в чужой
		вкладке владельца», чего мы себе не позволяем. Поэтому при подключении
		геометрия возвращается к тому, чем она была без явного headless:
		``viewport=None``, ``no_viewport=True`` — то есть никакого override.
		Для ветки запуска (``cdp_url`` пуст) не трогаем ничего: там viewport
		описывает НАШ браузер и обязан работать штатно.
		"""
		# Скачанное кладём в известную папку, а не во временный каталог со
		# случайным именем: результат работы должен лежать там, где его найдут.
		downloads = str(bu_mcp_module('downloads').download_dir())
		profile = BrowserProfile(cdp_url=cdp_url, is_local=True, headless=cls._headless(), downloads_path=downloads)
		if cdp_url and profile.viewport is not None and not profile.no_viewport:
			try:
				profile.viewport = None
				profile.no_viewport = True
			except Exception as exc:
				logger.warning('cannot drop the viewport override for a browser we did not launch: %r', exc)
		return profile

	async def _session_alive(self) -> bool:
		"""Жива ли кэшированная сессия.

		Проба — настоящий круговой запрос по CDP. Спрашивать
		``get_current_page_url()`` бесполезно: он глотает ошибку и на мёртвом
		сокете возвращает ``about:blank``, то есть выглядит как успех. На том же
		месте ``Target.getTargets`` честно бросает ``Client is not started``.

		Штатная реконнект-логика browser-use здесь тоже не спасает: она стучится
		в прежний ``ws://.../devtools/browser/<uuid>``, а у перезапущенного
		Chrome uuid другой, и все три попытки получают HTTP 404.
		"""
		if self._session is None:
			return False
		try:
			await self._session.cdp_client.send.Target.getTargets()
			return True
		except Exception:
			return False

	async def _drop_session(self) -> None:
		"""Отцепиться от мёртвой сессии, не трогая браузер.

		``keep_alive`` у профиля стоит именно затем, чтобы наш выход не убивал
		чужой Chrome, так что здесь мы только отпускаем свою сторону.
		"""
		session, self._session = self._session, None
		if session is None:
			return
		try:
			await session.stop()
		except Exception as exc:
			logger.debug('stale session did not close cleanly: %r', exc)

	async def _ensure_session(self) -> tuple[BrowserSession, Tools]:
		"""Поднять сессию к живому Chrome на первом обращении к браузеру.

		Сессия кэшируется, но не навечно. Chrome автоматизации перезапускается
		штатно — например, `scripts/chrome-automation.sh login` гасит его, чтобы
		показать окно для ручного логина. Без проверки живости после такого
		перезапуска все инструменты отвечали бы ошибкой сокета до перезапуска
		самого MCP-сервера, а клиент видел бы это как «браузер не запущен».
		"""
		import os

		async with self._session_lock:
			if self._session is not None and not await self._session_alive():
				logger.info('bu-mcp session is stale (Chrome restarted?), reattaching')
				await self._drop_session()
			if self._session is None:
				cdp_url = os.getenv('BU_MCP_CDP_URL', 'http://127.0.0.1:9222')
				# Живой ли CDP — спрашиваем САМИ, до создания сессии. Иначе решение
				# принимает browser-use, а у него на этот случай есть запасной путь:
				# поднять свой Chromium из кеша playwright. Это худший из возможных
				# исходов — вместо нашего профиля с логинами появляется чужой пустой
				# браузер, у владельца выскакивает окно, а агент считает, что всё в
				# порядке, и работает не в том браузере. Лучше честный отказ.
				if not cdp_reachable(cdp_url):
					raise ToolError(
						f'No Chrome with an open CDP at {cdp_url}. Nothing was done: bu-mcp never launches a '
						f'browser of its own, it only attaches to the one you run. Start it with '
						f'`scripts/chrome-automation.sh start` (add --profile NAME for a non-default profile).'
					)
				profile = self._profile(cdp_url)
				session = BrowserSession(browser_profile=profile)
				await session.start()
				self._session = session
				logger.info('bu-mcp attached to %s', cdp_url)
			if self._tools is None:
				tools = Tools()
				self._apply_domain_policy(tools)
				self._tools = tools
			if self._file_system is None:
				# Нужен реестровым действиям, которые пишут артефакты (save_as_pdf).
				self._file_system = FileSystem(base_dir=Path(tempfile.mkdtemp(prefix='bu-mcp-')))
		assert self._session is not None and self._tools is not None
		return self._session, self._tools

	def _registry_tools(self) -> Tools:
		"""``Tools()`` для построения списка инструментов, без старта браузера."""
		if self._tools is None:
			tools = Tools()
			self._apply_domain_policy(tools)
			self._tools = tools
		return self._tools

	async def _current_url(self) -> str | None:
		if self._session is None:
			return None
		try:
			return await self._session.get_current_page_url()
		except Exception:
			return None

	async def _evaluate(self, session: BrowserSession, expression: str) -> Any:
		"""Runtime.evaluate в текущей вкладке, без смены фокуса."""
		cdp_session = await session.get_or_create_cdp_session(target_id=None, focus=False)
		result = await cdp_session.cdp_client.send.Runtime.evaluate(
			params={'expression': expression, 'returnByValue': True, 'awaitPromise': False},
			session_id=cdp_session.session_id,
		)
		if result.get('exceptionDetails'):
			raise RuntimeError(result['exceptionDetails'].get('text', 'JS exception'))
		return (result or {}).get('result', {}).get('value')
