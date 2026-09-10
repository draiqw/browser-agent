"""Задача = что сделать + какая схема на выходе + чем проверить результат.

Проверка обязательна и делает её КОД, а не модель. Без внешнего эталона «агент
что-то вернул» и «агент вернул правду» — неразличимые события, а на деньгах это
недопустимо. Тот же принцип, что и fail-closed в `bu_mcp`: молчаливый ложный
успех — худший из возможных исходов.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

#: Схема результата конкретной задачи. verify/summary привязаны к ней же, а не к
#: голому BaseModel — иначе типы этих колбэков не сходятся с их реальной сигнатурой.
T = TypeVar('T', bound=BaseModel)


@dataclass(frozen=True)
class Task(Generic[T]):
	name: str
	prompt: str
	schema: type[T]
	verify: Callable[[T], list[str]]
	"""Возвращает список нарушений. Пустой список = результат сошёлся с эталоном."""
	profile: str = 'extract'
	max_steps: int = 20
	summary: Callable[[T], str] | None = None
	setup: Callable[[], object] | None = None
	"""Подготовка перед прогоном: поднять локальный сервер, сгенерировать фикстуру.
	Возвращаемое значение раннер игнорирует (`object`, а не `None`) — некоторые
	`setup` также вызываются напрямую ради своего результата (см. `clickgate.setup`)."""
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


_REGISTRY: dict[str, Task[Any]] = {}


def register(task: Task[Any]) -> Task[Any]:
	_REGISTRY[task.name] = task
	return task


def all_tasks() -> dict[str, Task[Any]]:
	if not _REGISTRY:
		from bu_eval import tasks  # noqa: F401  — импорт наполняет реестр
	return _REGISTRY


def get(name: str) -> Task[Any]:
	t = all_tasks().get(name)
	if not t:
		raise KeyError(f'Нет задачи {name!r}. Есть: {", ".join(sorted(all_tasks()))}')
	return t
