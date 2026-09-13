"""Профиль — это способ вести браузер, независимый от модели.

Одна и та же модель в разных профилях ведёт себя по-разному, и сравнивать надо
именно пары (модель, профиль).

Профиль здесь описан ДВАЖДЫ, по разу на бэкенд, и это главная переделка при
переносе харнесса из browseruse-lab:

* для бэкенда `browser-use` профиль — это `Tools(exclude_actions=...)` плюс
  параметры `Agent`, ровно как было в оригинале;
* для бэкенда `bu-mcp` профиль — это **набор разрешённых MCP-инструментов**:
  наш сервер отдаёт 25 инструментов, и клиент видит ровно те, которые профиль
  назвал. Именно это у нас соответствует «урезанному набору действий».

Одно имя профиля на оба бэкенда — не косметика: без общего имени сравнение
«ванильный browser-use против нас» пришлось бы делать на разных наборах
возможностей, и разница в результате ничего бы не значила.

Чего в наборе МCP-инструментов нет и быть не может: координатных кликов.
`bu_mcp` их наружу не выводит вовсе, поэтому профиль `act-coords` помечен как
недоступный нашему бэкенду, а не подменён похожим. Второе честное расхождение —
зрение: MCP-цикл текстовый, `browser_screenshot` попадает в набор только у
профиля `raw`, тогда как апстримный `Agent` шлёт скриншот сам при `use_vision`.
Оба расхождения — в отчёте, а не в тихой подгонке.
"""

from __future__ import annotations

from dataclasses import dataclass

# Агент по своей инициативе лез писать файлы, снимать PDF и жать кнопки — лишние
# шаги и деньги. Для чистого извлечения оставляем только навигацию, чтение и done.
NOISY_ACTIONS = [
	'write_file',
	'read_file',
	'replace_file',
	'save_as_pdf',
	'upload_file',
	'screenshot',
	'evaluate',
	'close',
	'search',
]

# Правила компенсируют слабость мелких моделей: они ошибаются в селекторах и не
# восстанавливаются после ошибки инструмента.
EXTRACT_RULES = """
ПРАВИЛА ИЗВЛЕЧЕНИЯ ДАННЫХ:
1. Для получения текста и таблиц предпочитай чтение состояния страницы — не ковыряй DOM селекторами.
2. Если используешь CSS-селектор и получил ошибку "Invalid CSS selector" — это твоя ошибка,
   исправь её и повтори НЕМЕДЛЕННО. Частая причина: id начинается с цифры,
   тогда пиши [id="123"], а не #123.
3. Любая ошибка инструмента — не повод завершать задачу. Меняй подход и продолжай.
4. Не завершай задачу частичным результатом с оговорками. Либо полные данные по схеме,
   либо продолжай работать до исчерпания шагов.
5. Не кликай ничего, кроме навигации, если в задаче не сказано иначе.
"""

# Для профилей, которые меряют работу с интерфейсом, JS-исполнение должно быть закрыто.
UI_ONLY = ('write_file', 'read_file', 'replace_file', 'save_as_pdf', 'upload_file', 'evaluate')

ACT_RULES = """
ПРАВИЛА ДЕЙСТВИЙ НА СТРАНИЦЕ:
1. Индексы элементов пересчитываются после каждой перезагрузки страницы. Никогда не
   используй индекс, полученный до перехода или перезагрузки — сначала посмотри состояние заново.
2. Если действие вернуло "Element index N not available", это не конец задачи:
   перечитай страницу и найди элемент заново.
3. Элементы с opacity:0 в списке не появятся. Если нужного чекбокса или тоггла в списке нет,
   кликай по видимой подписи или обёртке, а не ищи невидимый элемент.
4. После каждого действия проверяй, изменилось ли состояние страницы так, как ты ожидал.
"""

#: Все инструменты сервера, какие есть. Список не хардкодим — сервер отдаёт его
#: сам на ``tools/list``, а профиль просто не фильтрует.
MCP_ALL: tuple[str, ...] = ('*',)

#: Чтение страницы без единого действия, меняющего состояние.
MCP_READ = (
	'browser_navigate',
	'browser_state',
	'search_page',
	'find_text',
	'scroll',
	'go_back',
	'wait',
)

#: То же плюс работа с интерфейсом. `evaluate` намеренно снаружи: с ним модель
#: решает задачу скриптом мимо интерфейса, и профиль перестаёт мерить то, ради
#: чего заведён.
MCP_ACT = MCP_READ + (
	'browser_click',
	'browser_type',
	'browser_hover',
	'send_keys',
	'dropdown_options',
	'select_dropdown',
	'find_elements',
	'switch',
)


@dataclass(frozen=True)
class Profile:
	name: str
	# --- бэкенд browser-use --------------------------------------------- #
	lean: bool = True  # выкинуть шумные действия
	coordinates: bool = False  # клики по координатам вне зависимости от списка апстрима
	vision: bool | str = 'auto'
	flash: bool = False  # без размышлений: быстрее и дешевле, точность ниже
	rules: str = EXTRACT_RULES
	max_failures: int = 5
	exclude: tuple[str, ...] = ()
	# --- бэкенд bu-mcp --------------------------------------------------- #
	mcp: tuple[str, ...] | None = None
	"""Разрешённые MCP-инструменты. ``MCP_ALL`` — все, ``None`` — профиль этому
    бэкенду недоступен (например, координатные клики, которых у нас нет)."""
	note: str = ''

	# -- browser-use ------------------------------------------------------- #

	def build_tools(self):
		from browser_use import Tools

		excluded = list(self.exclude) + (NOISY_ACTIONS if self.lean else [])
		tools = Tools(exclude_actions=sorted(set(excluded))) if excluded else Tools()
		if self.coordinates:
			# Апстрим включает это только пяти избранным моделям (agent/service.py).
			# Мы включаем явно и любой модели — проверено доктором, что API на месте.
			tools.set_coordinate_clicking(True)
		return tools

	def agent_kwargs(self) -> dict:
		kw = {
			'extend_system_message': self.rules,
			'max_failures': self.max_failures,
			'flash_mode': self.flash,
		}
		if self.vision != 'auto':
			kw['use_vision'] = self.vision
		return kw

	# -- bu-mcp ------------------------------------------------------------ #

	@property
	def supports_mcp(self) -> bool:
		return self.mcp is not None

	def filter_mcp_tools(self, offered: list[str]) -> list[str]:
		"""Отфильтровать список инструментов сервера по профилю.

		Fail-closed в мелочи: если профиль назвал инструмент, которого сервер не
		отдал, это не тихое сужение набора, а явная ошибка — иначе профиль
		`act` однажды молча выродится в `extract` и разница в результатах будет
		приписана модели.
		"""
		if self.mcp is None:
			raise ValueError(f'профиль {self.name!r} не поддержан бэкендом bu-mcp (нужны координатные клики)')
		if self.mcp == MCP_ALL:
			return list(offered)
		unknown = sorted(set(self.mcp) - set(offered))
		if unknown:
			raise ValueError(f'профиль {self.name!r}: сервер не отдал инструменты {unknown}; есть {sorted(offered)}')
		return [t for t in offered if t in set(self.mcp)]


PROFILES: dict[str, Profile] = {
	p.name: p
	for p in [
		Profile('extract', mcp=MCP_READ, note='только чтение: навигация, состояние, поиск по странице, done'),
		Profile(
			'extract-flash',
			flash=True,
			mcp=MCP_READ,
			note='то же самое без размышлений — дешевле и быстрее, но глупее (только browser-use)',
		),
		Profile(
			'act',
			lean=False,
			rules=ACT_RULES,
			exclude=UI_ONLY,
			mcp=MCP_ACT,
			note='манипуляции на странице по индексам элементов, без обхода через JS',
		),
		Profile(
			'act-coords',
			lean=False,
			rules=ACT_RULES,
			coordinates=True,
			vision=True,
			exclude=UI_ONLY,
			mcp=None,
			note='клики по координатам принудительно; bu_mcp такого инструмента не отдаёт',
		),
		Profile(
			'act-js',
			lean=False,
			rules=ACT_RULES,
			exclude=('write_file', 'read_file', 'replace_file', 'save_as_pdf', 'upload_file'),
			mcp=MCP_ACT + ('evaluate',),
			note='то же, но с evaluate: быстрее и надёжнее, но интерфейс не проверяется',
		),
		Profile(
			'raw',
			lean=False,
			rules='',
			mcp=MCP_ALL,
			note='без наших правил и без урезания набора — база для сравнения',
		),
	]
}
