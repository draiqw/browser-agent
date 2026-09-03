"""Оценка слоя `bu_mcp` с моделью в цикле.

`bu_mcp/bench.py` меряет слой механически (символы, латентность, элементы,
протухшие хендлы), `bu_mcp/smoke.py` — протокол. Ни то, ни другое не отвечает на
вопрос «помогает ли слой модели решать задачи». Отвечает этот пакет: одни и те
же задачи с внешним эталоном прогоняются двумя бэкендами — ванильным
`browser_use.Agent` и моделью, ходящей в браузер только через наш MCP-сервер по
stdio.

    from bu_eval import run
    rep = run('clickgate', 'openai:gpt-5-mini', backend='bu-mcp')
    print(rep.verified, rep.steps, rep.cost, rep.problems)

Два свойства пакета намеренные и их нельзя терять:

* `bu_eval` — НЕ MCP-сервер и наружу инструментами не выводится. Харнесс это то,
  чем мы меряем агента, а не то, что агент дёргает.
* клиентские SDK (`openai`, `anthropic`) живут только здесь. `bu_mcp` в рантайме
  их не импортирует и не должен: MCP-серверу неоткуда знать, какой моделью его
  крутят.

Стиль здесь fail-open: упавшая проверка, сбойный провайдер или один плохой
прогон становятся строкой отчёта, а не исключением на всю матрицу. В `bu_mcp`
всё наоборот, fail-closed, и это расхождение осознанное — оценочному коду важна
полнота матрицы, исполнительному важен громкий отказ.
"""

from bu_eval.backends import BACKENDS, RunReport
from bu_eval.models import available, make_model
from bu_eval.profiles import PROFILES, Profile
from bu_eval.runner import Matrix, run, run_matrix, save
from bu_eval.task import Task, all_tasks, register
from bu_eval.task import get as get_task

__all__ = [
	'BACKENDS',
	'Matrix',
	'PROFILES',
	'Profile',
	'RunReport',
	'Task',
	'all_tasks',
	'available',
	'get_task',
	'make_model',
	'register',
	'run',
	'run_matrix',
	'save',
]
