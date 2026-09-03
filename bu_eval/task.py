"""Задача = что сделать + какая схема на выходе + чем проверить результат.

Проверка обязательна и делает её КОД, а не модель. Без внешнего эталона «агент
что-то вернул» и «агент вернул правду» — неразличимые события, а на деньгах это
недопустимо. Тот же принцип, что и fail-closed в `bu_mcp`: молчаливый ложный
успех — худший из возможных исходов.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class Task:
	name: str
	prompt: str
	schema: type[BaseModel]
	verify: Callable[[BaseModel], list[str]]
	"""Возвращает список нарушений. Пустой список = результат сошёлся с эталоном."""
	profile: str = 'extract'
	max_steps: int = 20
	summary: Callable[[BaseModel], str] | None = None
	setup: Callable[[], None] | None = None
	"""Подготовка перед прогоном: поднять локальный сервер, сгенерировать фикстуру."""
	script: Callable | None = None
	"""Записанное решение задачи БЕЗ модели: `async script(call) -> BaseModel | None`,
    где `call(имя, аргументы)` — вызов MCP-инструмента.

    Нужно бэкенду `scripted`: он прогоняет ту же трубу (сервер по stdio, набор
    инструментов профиля, схема результата, внешняя проверка) с той единственной
    разницей, что решения принимает не модель. Отвечает на вопрос «может ли слой
    решить задачу в принципе», отделяя способности слоя от способностей модели,
    и стоит ноль долларов."""
	needs_network: bool = True
	note: str = ''


_REGISTRY: dict[str, Task] = {}


def register(task: Task) -> Task:
	_REGISTRY[task.name] = task
	return task


def all_tasks() -> dict[str, Task]:
	if not _REGISTRY:
		from bu_eval import tasks  # noqa: F401  — импорт наполняет реестр
	return _REGISTRY


def get(name: str) -> Task:
	t = all_tasks().get(name)
	if not t:
		raise KeyError(f'Нет задачи {name!r}. Есть: {", ".join(sorted(all_tasks()))}')
	return t
