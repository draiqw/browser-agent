"""Курсы ЦБ РФ: браузерное извлечение против официального XML.

Макет банковского коннектора: типизированная схема, инварианты и независимый эталон.
"""

from __future__ import annotations

import ssl
import urllib.request
import xml.etree.ElementTree as ET

from pydantic import BaseModel, Field

from bu_eval.task import Task, register

URL = 'https://www.cbr.ru/currency_base/daily/'
XML = 'https://www.cbr.ru/scripts/XML_daily.asp'


class Rate(BaseModel):
	char_code: str = Field(description='Буквенный код валюты, например USD')
	nominal: int = Field(description='Единиц иностранной валюты')
	name: str = Field(description='Название валюты')
	value: float = Field(description='Курс в рублях за указанный номинал')


class Rates(BaseModel):
	date: str = Field(description='Дата курсов в формате ДД.ММ.ГГГГ')
	rates: list[Rate]


_GT: tuple[str, dict[str, tuple[int, float]]] | None = None


def ground_truth(refresh: bool = False):
	"""Эталон из официального XML ЦБ."""
	global _GT
	if _GT is not None and not refresh:
		return _GT
	import certifi  # у uv-питона нет системного CA-бандла

	ctx = ssl.create_default_context(cafile=certifi.where())
	with urllib.request.urlopen(XML, timeout=30, context=ctx) as r:
		root = ET.fromstring(r.read().decode('windows-1251'))
	_GT = (
		root.attrib['Date'],
		{
			v.find('CharCode').text: (int(v.find('Nominal').text), float(v.find('Value').text.replace(',', '.')))
			for v in root.findall('Valute')
		},
	)
	return _GT


def verify(data: Rates) -> list[str]:
	problems: list[str] = []
	gt_date, gt = ground_truth()

	# инварианты, эталон не нужен
	if not data.rates:
		problems.append('пустой список курсов')
	codes = [r.char_code.upper() for r in data.rates]
	dups = {c for c in codes if codes.count(c) > 1}
	if dups:
		problems.append(f'дубли кодов: {sorted(dups)}')
	bad = [r.char_code for r in data.rates if r.value <= 0 or r.nominal <= 0]
	if bad:
		problems.append(f'неположительные значения: {bad}')

	# сверка с официальным XML
	if data.date != gt_date:
		problems.append(f'дата {data.date} != эталон {gt_date}')
	missing = sorted(set(gt) - set(codes))
	if missing:
		problems.append(f'потеряно валют: {len(missing)} → {missing[:10]}')
	extra = sorted(set(codes) - set(gt))
	if extra:
		problems.append(f'лишние коды: {extra[:10]}')

	wrong = []
	for r in data.rates:
		c = r.char_code.upper()
		if c not in gt:
			continue
		gn, gv = gt[c]
		if r.nominal != gn:
			wrong.append(f'{c}: номинал {r.nominal} != {gn}')
		elif abs(r.value - gv) > 0.0001:
			wrong.append(f'{c}: курс {r.value} != {gv}')
	if wrong:
		problems.append(f'расхождений в значениях: {len(wrong)} → {wrong[:5]}')
	return problems


def summary(data: Rates) -> str:
	_, gt = ground_truth()
	return f'извлечено {len(data.rates)} строк из {len(gt)} эталонных, дата {data.date}'


register(
	Task(
		name='cbr',
		prompt=(
			f'Открой {URL}. На странице таблица курсов валют ЦБ РФ со столбцами '
			'«Цифр. код», «Букв. код», «Единиц», «Валюта», «Курс». '
			'Извлеки ВСЕ строки таблицы без исключения и дату, на которую установлены курсы. '
			'В поле value записывай курс как число с точкой (запятую в исходнике замени на точку).'
		),
		schema=Rates,
		verify=verify,
		summary=summary,
		profile='extract',
		max_steps=20,
		note='длинная таблица + сверка с официальным XML; макет банковского коннектора',
	)
)
