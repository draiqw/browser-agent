"""Hacker News: короткая задача с проверкой по публичному API.

Нужна как быстрый и дешёвый тест общего здоровья связки модель+профиль: одна страница,
30 строк, эталон берётся из Firebase API самого HN. Список на главной живёт минутами,
поэтому сверка допускает сдвиг: важно совпадение состава, а не позиций.
"""

from __future__ import annotations

import json
import ssl
import urllib.request

from pydantic import BaseModel, Field

from bu_eval.task import Task, register

URL = 'https://news.ycombinator.com/'
API = 'https://hacker-news.firebaseio.com/v0'
TOLERANCE = 0.8  # какая доля извлечённых заголовков обязана найтись в эталоне


class Story(BaseModel):
	rank: int = Field(description='Позиция в списке, начиная с 1')
	title: str = Field(description='Заголовок новости')
	points: int = Field(description='Число очков; если очков нет, ставь 0')
	comments: int = Field(description='Число комментариев; если их нет, ставь 0')


class Front(BaseModel):
	stories: list[Story]


def _get(url: str):
	import certifi

	ctx = ssl.create_default_context(cafile=certifi.where())
	with urllib.request.urlopen(url, timeout=30, context=ctx) as r:
		return json.load(r)


_GT: set[str] | None = None


def ground_truth(limit: int = 45) -> set[str]:
	"""Заголовки верхушки списка по API. Берём с запасом — страница успевает сдвинуться."""
	global _GT
	if _GT is None:
		ids = _get(f'{API}/topstories.json')[:limit]
		titles = []
		for i in ids:
			try:
				item = _get(f'{API}/item/{i}.json')
				if item and item.get('title'):
					titles.append(item['title'])
			except Exception:  # noqa: BLE001 — эталон с дыркой лучше, чем упавшая проверка
				continue
		_GT = {_norm(t) for t in titles}
	return _GT


def _norm(s: str) -> str:
	return ' '.join(s.lower().split())


def verify(data: Front) -> list[str]:
	problems = []
	if len(data.stories) < 25:
		problems.append(f'извлечено {len(data.stories)} новостей, ожидалось около 30')
	ranks = [s.rank for s in data.stories]
	if len(set(ranks)) != len(ranks):
		problems.append('позиции повторяются')
	if ranks and sorted(ranks) != list(range(min(ranks), min(ranks) + len(ranks))):
		problems.append(f'дырки в позициях: {sorted(ranks)[:12]}')
	if any(s.points < 0 or s.comments < 0 for s in data.stories):
		problems.append('отрицательные очки или комментарии')
	if all(s.points == 0 for s in data.stories) and data.stories:
		problems.append('у всех новостей 0 очков — похоже, столбец не прочитан')

	gt = ground_truth()
	if gt:
		hit = sum(1 for s in data.stories if _norm(s.title) in gt)
		share = hit / max(len(data.stories), 1)
		if share < TOLERANCE:
			miss = [s.title for s in data.stories if _norm(s.title) not in gt][:5]
			problems.append(
				f'совпало с API {hit}/{len(data.stories)} ({share:.0%}), ' f'порог {TOLERANCE:.0%}; примеры расхождений: {miss}'
			)
	else:
		problems.append('эталон HN недоступен — проверка неполная')
	return problems


register(
	Task(
		name='hn',
		prompt=(
			f'Открой {URL}. Извлеки первые 30 новостей: позицию, заголовок, '
			'число очков и число комментариев. Если очков или комментариев нет, ставь 0.'
		),
		schema=Front,
		verify=verify,
		summary=lambda d: f'извлечено {len(d.stories)} новостей',
		profile='extract',
		max_steps=15,
		note='быстрый общий тест, эталон из Firebase API самого HN',
	)
)
