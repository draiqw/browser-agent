"""Локальная задача на клики: проверяет ровно тот провал, ради которого написан resolve.py.

Тогглы на странице кастомные: сам `input` скрыт через `opacity:0`, видна только
подпись. Такие элементы не попадают в карту элементов browser-use, и агент,
который умеет кликать только по индексам, их не видит. Ровно так устроены
фильтры в банковских личных кабинетах.

Задача офлайновая, бесплатная и детерминированная: фикстура генерируется кодом,
эталонный код считается формулой (`expected_code`), сеть не нужна. Значит её
можно гонять как регрессию на наш слой сколько угодно раз.

Замеренная у автора матрица на ванильном browser-use: `act` 0/2,
`act-coords` 0/2, `act-js` 2/2 — то есть без обхода через JS интерфейс не
поддавался вовсе. Это и есть та цифра, которую наш слой обязан сдвинуть.

Локальный http-сервер обязателен: `file://` режется `SecurityWatchdog`.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from bu_eval.fixtures import PORT, ROOT, url_for
from bu_eval.task import Task, register

FIXTURE = ROOT / 'clickgate.html'

TOGGLES = [
	('t1', 'Расчётный счёт 40702'),
	('t2', 'Специальный счёт 40802'),
	('t3', 'Валютный счёт'),
	('t4', 'Депозит'),
]
# Правильная комбинация: включены только «Специальный счёт 40802» и «Депозит».
WANT = ('t2', 't4')


def _mask(ids) -> int:
	return sum(1 << i for i, (tid, _) in enumerate(TOGGLES) if tid in ids)


def expected_code() -> str:
	return f'GATE-{1000 + (_mask(WANT) * 7919) % 9000}'


HTML = """<!doctype html>
<meta charset="utf-8"><title>Фильтр счетов</title>
<style>
 body{font:16px/1.5 system-ui;margin:40px;max-width:520px}
 .row{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid #eee}
 /* Кастомный тоггл: настоящий input невидим, кликабельна только подпись. */
 input[type=checkbox]{position:absolute;opacity:0;width:0;height:0}
 .sw{width:44px;height:24px;border-radius:12px;background:#ccc;position:relative;
     cursor:pointer;transition:.15s;flex:none}
 .sw::after{content:"";position:absolute;top:3px;left:3px;width:18px;height:18px;
     border-radius:50%;background:#fff;transition:.15s}
 input:checked + .sw{background:#2b8a3e}
 input:checked + .sw::after{left:23px}
 #code{margin-top:28px;padding:16px;border-radius:8px;background:#f1f3f5;font-size:20px}
 .hint{color:#868e96;font-size:14px}
</style>
<h1>Фильтр счетов</h1>
<p class="hint">Выберите счета и получите код выгрузки.</p>
__ROWS__
<div id="code">код появится после выбора нужных счетов</div>
<script>
 const want = __WANT__;
 function refresh() {
   const on = [...document.querySelectorAll('input[type=checkbox]')]
     .filter(i => i.checked).map(i => i.id).sort();
   const ok = JSON.stringify(on) === JSON.stringify(want.slice().sort());
   const mask = [...document.querySelectorAll('input[type=checkbox]')]
     .reduce((m, i, idx) => m + (i.checked ? (1 << idx) : 0), 0);
   document.getElementById('code').textContent = ok
     ? 'КОД ВЫГРУЗКИ: GATE-' + (1000 + (mask * 7919) % 9000)
     : 'код появится после выбора нужных счетов';
 }
 document.addEventListener('change', refresh);
 refresh();
</script>
"""


def build() -> Path:
	rows = '\n'.join(
		f'<div class="row"><input type="checkbox" id="{tid}">'
		f'<label class="sw" for="{tid}"></label><label for="{tid}">{label}</label></div>'
		for tid, label in TOGGLES
	)
	html = HTML.replace('__ROWS__', rows).replace('__WANT__', str(list(WANT)).replace("'", '"'))
	FIXTURE.parent.mkdir(parents=True, exist_ok=True)
	FIXTURE.write_text(html, encoding='utf-8')
	return FIXTURE


def setup() -> str:
	"""Генерируем страницу и поднимаем локальный сервер: file:// browser-use блокирует."""
	build()
	return url_for('clickgate.html')


class Gate(BaseModel):
	code: str = Field(description='Код выгрузки, показанный страницей после выбора счетов')
	enabled: list[str] = Field(description='Названия счетов, которые в итоге включены')


def verify(data: Gate) -> list[str]:
	problems = []
	want_code = expected_code()
	if want_code not in data.code.upper().replace(' ', ''):
		problems.append(f'код {data.code!r} != эталон {want_code}')
	names = {n.lower() for n in data.enabled}
	for tid, label in TOGGLES:
		should = tid in WANT
		got = any(label.lower() in n or n in label.lower() for n in names)
		if should and not got:
			problems.append(f'не включён: {label}')
		if not should and got:
			problems.append(f'включён лишний: {label}')
	return problems


URL = f'http://127.0.0.1:{PORT}/clickgate.html'

#: Клик по подписи тоггла. Ровно то, что сделал бы человек и что должна была бы
#: сделать модель, если бы подписи попадали в карту элементов.
_CLICK_JS = """(() => {
  const want = %s;
  const hit = [];
  for (const id of want) {
    const label = document.querySelector(`label[for="${id}"]:not(.sw)`);
    if (!label) continue;
    label.click();
    hit.push(id);
  }
  return JSON.stringify({clicked: hit});
})()"""


async def script(call):
	"""Решение без модели: показывает предел СЛОЯ, а не предел модели.

	Сценарий намеренно кликает по подписям, а не переключает `checked` напрямую:
	иначе он проверял бы, что в JS можно выставить свойство, а не что кнопка
	нажимается. Читает результат он тоже глазами клиента — из `browser_state`,
	а не из того же `evaluate`.

	Профиль обязан быть `act-js`: подписи тогглов в карту элементов не попадают
	(замерено — `browser_state` отдаёт `elements: 0`), поэтому пути через
	`browser_click` для этой страницы не существует ни у нас, ни у апстрима.
	Это и есть та дыра, которую задача документирует.
	"""
	import json
	import re

	await call('browser_navigate', {'url': URL})
	res = await call('evaluate', {'code': _CLICK_JS % json.dumps(list(WANT))})
	if res['is_error']:
		return None
	state = await call('browser_state', {})
	m = re.search(r'GATE-\d{4}', state['text'] or '')
	if not m:
		return None
	labels = dict(TOGGLES)
	return Gate(code=m.group(0), enabled=[labels[t] for t in WANT])


register(
	Task(
		name='clickgate',
		prompt=(
			f'Открой http://127.0.0.1:{PORT}/clickgate.html. На странице четыре переключателя счетов. '
			'Включи ТОЛЬКО «Специальный счёт 40802» и «Депозит», остальные должны остаться выключенными. '
			'После этого страница покажет код выгрузки — верни его и список включённых счетов. '
			'Переключатели кастомные: сам чекбокс невидим, кликать нужно по подписи или по самому переключателю.'
		),
		schema=Gate,
		verify=verify,
		summary=lambda d: f'код: {d.code} | включено: {", ".join(d.enabled)}',
		profile='act',
		max_steps=25,
		setup=setup,
		script=script,
		needs_network=False,
		note='кастомные тогглы с opacity:0 — офлайн, бесплатно, эталон известен точно',
	)
)
