"""Отчёты: строка на прогон и сводная таблица по (задача, бэкенд, модель, профиль).

Бэкенд вынесен в отдельный столбец нарочно: пара строк с одинаковой задачей и
разными бэкендами — это и есть замер слоя.
"""

from __future__ import annotations

from collections import defaultdict

from bu_eval.backends import RunReport


def _money(c: float | None) -> str:
	return '—' if c is None else f'${c:.4f}'


def line(rep: RunReport) -> str:
	mark = 'OK  ' if rep.verified else ('ДАННЫЕ, НО НЕ СОШЛИСЬ' if rep.ok else 'ПРОВАЛ')
	s = (
		f'[{mark}] {rep.task} | {rep.backend} | {rep.model} | {rep.profile} | '
		f'шагов {rep.steps} | {rep.seconds:.0f}s | '
		f'токенов {rep.tokens} (cached {rep.tok_cached}) | {_money(rep.cost)}'
	)
	if rep.tools_allowed:
		s += f' | инструментов {rep.tools_allowed}/{rep.tools_offered}'
	if rep.stopped:
		s += f' | стоп: {rep.stopped}'
	if rep.summary:
		s += f'\n         {rep.summary}'
	if rep.problems:
		s += '\n         нарушения: ' + '; '.join(rep.problems)[:300]
	if rep.errors:
		s += f'\n         ошибок агента: {len(rep.errors)} -> {rep.errors[0][:140]}'
	return s


def table(reports: list[RunReport]) -> str:
	"""Сводка: доля сошедшихся прогонов, шаги min/med/max, среднее время и цена."""
	groups: dict[tuple[str, str, str, str], list[RunReport]] = defaultdict(list)
	for r in reports:
		groups[(r.task, r.backend, r.model, r.profile)].append(r)

	rows = []
	for (task, backend, model, prof), rs in sorted(groups.items()):
		good = sum(1 for r in rs if r.verified)
		steps = sorted(r.steps for r in rs)
		med = steps[len(steps) // 2] if steps else 0
		costs = [r.cost for r in rs if r.cost is not None]
		avg = sum(costs) / len(costs) if costs else None
		secs = sum(r.seconds for r in rs) / len(rs)
		rows.append(
			(
				task,
				backend,
				model,
				prof,
				f'{good}/{len(rs)}',
				f'{min(steps)}/{med}/{max(steps)}' if steps else '—',
				f'{secs:.0f}s',
				_money(avg),
			)
		)

	head = ('задача', 'бэкенд', 'модель', 'профиль', 'сошлось', 'шаги min/med/max', 'время', 'цена/прогон')
	widths = [max(len(str(r[i])) for r in [head, *rows]) for i in range(len(head))]
	fmt = '  '.join('{:<%d}' % w for w in widths)
	out = [fmt.format(*head), '  '.join('-' * w for w in widths)]
	out += [fmt.format(*r) for r in rows]
	return '\n'.join(out)


def markdown(reports: list[RunReport]) -> str:
	t = table(reports).splitlines()
	if len(t) < 2:
		return ''
	cols = [c.strip() for c in t[0].split('  ') if c.strip()]
	md = ['| ' + ' | '.join(cols) + ' |', '|' + '---|' * len(cols)]
	for row in t[2:]:
		cells = [c.strip() for c in row.split('  ') if c.strip()]
		md.append('| ' + ' | '.join(cells) + ' |')
	return '\n'.join(md)
