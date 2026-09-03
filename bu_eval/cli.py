"""Командная строка харнесса.

    python -m bu_eval doctor      состояние допущений об апстриме (харнесс + bu_mcp)
    python -m bu_eval selftest    проверить сам харнесс, без LLM и без денег
    python -m bu_eval models      какие провайдеры готовы к работе
    python -m bu_eval tasks       список задач, профилей и бэкендов
    python -m bu_eval run -t clickgate -m openai:gpt-5-mini -b bu-mcp
    python -m bu_eval run -t clickgate -m openai:gpt-5-mini -b bu-mcp -b browser-use -r 3

Прогоны стоят денег. Матрица печатает свой размер до старта, а `--dry-run`
показывает ячейки и не тратит ни цента.

Браузер нужен уже работающий и headless: `scripts/chrome-automation.sh` без
флагов. Флага «показать браузер» у харнесса нет намеренно — см. `backends.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv

# Ключи берём из .env рядом с репозиторием, а если он живёт в другом месте —
# из файла, на который указывает BU_EVAL_ENV_FILE. Свой .env харнесс не создаёт:
# ключ провайдера — не то, что стоит раскладывать по репозиториям молча.
load_dotenv(os.getenv('BU_EVAL_ENV_FILE') or None)


def cmd_doctor(args) -> int:
	from bu_eval.upstream import run_all, version

	print(f'browser-use {version()}')
	bad = 0
	group = None
	for c in run_all():
		if c.group != group:
			group = c.group
			title = 'ДОПУЩЕНИЯ ХАРНЕССА' if group == 'harness' else 'ДОПУЩЕНИЯ BU_MCP'
			print(f'\n{title}')
		print(('  OK    ' if c.ok else '  СЛОМ  ') + f'{c.name:32} {c.detail}')
		bad += not c.ok
	if bad:
		print(
			f'\nСломано допущений: {bad}. Смотри bu_eval/upstream.py — там написано, ' f'на что именно мы опирались и что чинить.'
		)
	return 1 if bad else 0


def cmd_selftest(args) -> int:
	from bu_eval.selftest import run_all

	bad = 0
	for c in run_all():
		print(('  OK    ' if c.ok else '  СЛОМ  ') + f'{c.name:22} {c.detail}')
		bad += not c.ok
	return 1 if bad else 0


def cmd_models(args) -> int:
	from bu_eval.models import available

	for name, ok, note in available():
		print(f'  {"готов" if ok else "  —  "}  {name:12} {note}')
	print(
		'\nСпецификация модели: провайдер:модель, например openai:gpt-5-mini, '
		'anthropic:claude-haiku-4-5-20251001, ollama:qwen3:8b'
	)
	return 0


def cmd_tasks(args) -> int:
	from bu_eval.backends import BACKENDS
	from bu_eval.profiles import MCP_ALL, PROFILES
	from bu_eval.task import all_tasks

	print('ЗАДАЧИ')
	for name, t in sorted(all_tasks().items()):
		net = 'сеть' if t.needs_network else 'офлайн'
		print(f'  {name:12} профиль={t.profile:12} шагов<={t.max_steps:<3} [{net}] {t.note}')
	print('\nПРОФИЛИ')
	for name, p in PROFILES.items():
		if p.mcp is None:
			mcp = 'bu-mcp: нет'
		elif p.mcp == MCP_ALL:
			mcp = 'bu-mcp: все'
		else:
			mcp = f'bu-mcp: {len(p.mcp)}'
		print(f'  {name:14} {mcp:14} {p.note}')
	print('\nБЭКЕНДЫ')
	for name in BACKENDS:
		print(f'  {name}')
	return 0


def cmd_run(args) -> int:
	from bu_eval import report as rep_mod
	from bu_eval.runner import Matrix, run_matrix, save

	mx = Matrix(
		tasks=args.task,
		models=args.model,
		profiles=args.profile or [],
		backends=args.backend or ['bu-mcp'],
		repeats=args.repeats,
		max_steps=args.max_steps,
		verify=not args.no_verify,
	)
	cells = mx.cells()
	print(f'Матрица: {len(cells)} пар × {args.repeats} повтор(ов) = {len(cells) * args.repeats} прогонов\n')
	if args.dry_run:
		for t, m, p, b in cells:
			print(f'  {t.name} | {b} | {m} | {p}')
		return 0

	def show(r, i, total):
		# flush обязателен: при перенаправлении в файл вывод буферизуется блоками,
		# и в фоновом прогоне результаты не видно до самого конца
		print(rep_mod.line(r) + (f'   [повтор {i}/{total}]' if total > 1 else ''), flush=True)

	reports = asyncio.run(run_matrix(mx, on_result=show))

	print('\n' + rep_mod.table(reports))
	if args.md:
		print('\n' + rep_mod.markdown(reports))
	if args.out:
		print(f'\nсохранено: {save(reports, args.out, with_data=args.with_data)}')

	good = sum(1 for r in reports if r.verified)
	spent = sum(r.cost or 0.0 for r in reports)
	print(f'\nсошлось {good}/{len(reports)} прогонов, потрачено ${spent:.4f}')
	return 0 if good == len(reports) else 1


def main(argv=None) -> int:
	p = argparse.ArgumentParser(prog='bu_eval', description='Оценка bu_mcp с моделью в цикле')
	sub = p.add_subparsers(dest='cmd', required=True)

	sub.add_parser('doctor', help='состояние допущений об апстриме').set_defaults(fn=cmd_doctor)
	sub.add_parser('selftest', help='проверки без LLM и без денег').set_defaults(fn=cmd_selftest)
	sub.add_parser('models', help='доступные провайдеры').set_defaults(fn=cmd_models)
	sub.add_parser('tasks', help='задачи, профили и бэкенды').set_defaults(fn=cmd_tasks)

	r = sub.add_parser('run', help='прогнать матрицу задача × модель × профиль × бэкенд')
	r.add_argument('-t', '--task', action='append', required=True)
	r.add_argument('-m', '--model', action='append', required=True)
	r.add_argument('-p', '--profile', action='append', help='по умолчанию профиль задачи')
	r.add_argument('-b', '--backend', action='append', help='bu-mcp (по умолчанию) и/или browser-use')
	r.add_argument('-r', '--repeats', type=int, default=1)
	r.add_argument('--max-steps', type=int, default=None)
	r.add_argument('--out', default='', help='куда сложить JSON с прогонами')
	r.add_argument('--with-data', action='store_true', help='класть в JSON и сами извлечённые данные')
	r.add_argument('--md', action='store_true', help='ещё и markdown-таблица')
	r.add_argument('--no-verify', action='store_true')
	r.add_argument('--dry-run', action='store_true', help='показать ячейки матрицы и не тратить денег')
	r.set_defaults(fn=cmd_run)

	args = p.parse_args(argv)
	return args.fn(args)


if __name__ == '__main__':
	sys.exit(main())
