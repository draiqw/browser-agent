"""Allowlist доменов: ``DomainGateMixin`` для ``BuMcpServer``.

Вынесено из server.py при разбиении на подмодули (docs/WORKLOG.md).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from browser_use.mcp.server_shared import DOMAIN_EXEMPT, ToolError
from browser_use.tools.registry.views import ActionRegistry
from browser_use.utils import is_new_tab_page

if TYPE_CHECKING:
	from browser_use.tools.service import Tools


class DomainGateMixin:
	_allowed_domains: list[str]

	# -- allowlist ---------------------------------------------------------- #

	@staticmethod
	def _parse_allowed_domains() -> list[str]:
		raw = os.getenv('BU_MCP_ALLOWED_DOMAINS', '') or ''
		return [part.strip() for part in raw.split(',') if part.strip()]

	def _domain_allowed(self, url: str | None, *, treat_blank_as_allowed: bool = True) -> bool:
		"""Проверить URL против allowlist из ``BU_MCP_ALLOWED_DOMAINS``.

		Политика (сознательно ОДНА, без второго списка):

		* allowlist пуст  -> разрешено всё;
		* allowlist задан -> разрешено ТОЛЬКО совпавшее, deny-by-default;
		* пустой/неизвестный URL -> запрещено (fail closed);
		* ``about:blank`` и прочие new-tab страницы разрешены для действий над
		  страницей (``treat_blank_as_allowed``), но НЕ для навигации: там
		  проверяется целевой URL, а не текущий.

		Про ловушку. В browser-use есть два несовместимых слоя:
		``BrowserProfile.allowed_domains`` / ``prohibited_domains``, где прямо
		написано «Allowed domains take precedence over prohibited domains» —
		то есть allow ПОБЕЖДАЕТ deny, ровно наоборот к тому, как это устроено
		почти везде (в Playwright MCP, в браузерных расширениях, в фаерволах
		deny выигрывает). Смешивать эти семантики в одном сервере — это гарантия
		того, что рано или поздно кто-то запретит домен и удивится, что он всё
		равно открывается. Поэтому здесь prohibit-списка нет вовсе: только
		allowlist. Пересечения, а значит и приоритета, не существует.

		Матчинг — та же функция, что использует сам реестр
		(``ActionRegistry._match_domains`` -> ``match_url_with_domain_pattern``),
		чтобы маски вели себя одинаково: ``*.example.com``, ``http*://foo.bar``,
		схема по умолчанию https.
		"""
		if not self._allowed_domains:
			return True
		if not url:
			return False
		if treat_blank_as_allowed and is_new_tab_page(url):
			return True
		return ActionRegistry._match_domains(self._allowed_domains, url)

	def _apply_domain_policy(self, tools: 'Tools') -> None:
		"""Прописать allowlist в ``domains`` реестровых действий.

		Реестр умеет фильтровать действия по маске URL сам (параметр ``domains``
		в декораторе; ``create_action_model(page_url=...)`` пересчитывает набор на
		каждом шаге). Задействуем именно его, чтобы не заводить второй механизм:
		проставляем allowlist всем действиям, кроме сессионных (``DOMAIN_EXEMPT``).

		Само по себе это только фильтрация выдачи — ``execute_action`` домены не
		проверяет. Поэтому тот же самый ответ дополнительно проверяется жёстко в
		``_check_domain_gate`` перед вызовом.
		"""
		if not self._allowed_domains:
			return
		for name, action in tools.registry.registry.actions.items():
			if name in DOMAIN_EXEMPT:
				continue
			if action.domains is None:
				action.domains = list(self._allowed_domains)

	async def _check_domain_gate(self, action_name: str, target_url: str | None = None) -> None:
		"""Жёсткий гейт: либо целевой URL (навигация), либо URL текущей страницы."""
		if not self._allowed_domains or action_name in DOMAIN_EXEMPT:
			return

		if target_url is not None:
			if not self._domain_allowed(target_url, treat_blank_as_allowed=False):
				raise ToolError(
					f'Navigation to {target_url!r} is blocked: it does not match BU_MCP_ALLOWED_DOMAINS '
					f'({", ".join(self._allowed_domains)}).'
				)
			return

		current = await self._current_url()
		if not self._domain_allowed(current):
			raise ToolError(
				f'{action_name} is blocked on {current or "an unknown page"}: it does not match '
				f'BU_MCP_ALLOWED_DOMAINS ({", ".join(self._allowed_domains)}).'
			)
