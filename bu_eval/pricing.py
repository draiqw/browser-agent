"""Цена прогона по публичному реестру LiteLLM.

Счётчик browser-use знает не все модели и молча показывает $0.0000 — на дешёвых
моделях это отличалось от реальности в бесконечное число раз. Считаем сами.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.request
from pathlib import Path

URL = 'https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json'
CACHE = Path(__file__).resolve().parent / '.cache' / 'litellm_prices.json'
TTL = 7 * 24 * 3600

_MEM: dict | None = None


def _fetch() -> dict:
	import certifi  # у uv-питона нет системного CA-бандла, без certifi будет SSLCertVerificationError

	ctx = ssl.create_default_context(cafile=certifi.where())
	with urllib.request.urlopen(URL, timeout=30, context=ctx) as r:
		return json.load(r)


def prices(refresh: bool = False) -> dict:
	"""Реестр цен. Кэш на диске живёт неделю; без сети берём просроченный кэш."""
	global _MEM
	if _MEM is not None and not refresh:
		return _MEM

	fresh_enough = CACHE.exists() and (time.time() - CACHE.stat().st_mtime) < TTL
	if fresh_enough and not refresh:
		try:
			_MEM = json.loads(CACHE.read_text())
			return _MEM
		except Exception:  # noqa: BLE001 — битый кэш не повод падать, перекачаем
			pass

	try:
		_MEM = _fetch()
		CACHE.parent.mkdir(parents=True, exist_ok=True)
		CACHE.write_text(json.dumps(_MEM))
	except Exception:  # noqa: BLE001
		# сети нет — лучше просроченный кэш, чем ничего
		_MEM = json.loads(CACHE.read_text()) if CACHE.exists() else {}
	return _MEM


def _entry(model: str) -> dict | None:
	p = prices()
	if model in p:
		return p[model]
	# "openai/gpt-5-mini" -> "gpt-5-mini", "gpt-5-mini-2025-08-07" -> "gpt-5-mini"
	bare = model.split('/')[-1]
	for key in (bare, bare.rsplit('-', 1)[0], bare.rsplit('-', 3)[0]):
		if key in p:
			return p[key]
	return None


def cost_of(model: str, fresh_in: int, cached_in: int, out: int) -> float | None:
	"""Стоимость в долларах или None, если модели нет в реестре (например, локальная)."""
	e = _entry(model)
	if not e:
		return None
	inp = e.get('input_cost_per_token') or 0.0
	return (
		fresh_in * inp + cached_in * (e.get('cache_read_input_token_cost') or inp) + out * (e.get('output_cost_per_token') or 0.0)
	)


def is_free(model: str) -> bool:
	"""Локальные модели считаем бесплатными, а не 'цена неизвестна'."""
	return model.startswith(('ollama', 'local')) or os.getenv('BU_EVAL_FREE_MODELS', '') == '1'
