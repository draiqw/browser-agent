"""Модель по строке-спецификации, без привязки к конкретному провайдеру.

    make_model("openai:gpt-5-mini")
    make_model("anthropic:claude-haiku-4-5-20251001")
    make_model("ollama:qwen3:8b")                     # локально, без ключей
    make_model("compat:qwen2.5-72b", base_url="http://localhost:8000/v1")

Провайдеры различаются в мелочах: у кого-то лимит ответа называется max_tokens,
у кого-то max_completion_tokens, у ChatOllama температуры нет вовсе. Поэтому
kwargs не захардкожены, а сверяются с полями самого класса — при обновлении
browser-use это не сломается молча.

Классы моделей берутся из browser_use и нужны ТОЛЬКО бэкенду-базе
(`browser-use`), который гоняет апстримный `Agent`. Наш MCP-бэкенд ходит в
провайдера сам (см. `bu_eval.loop`) — там из этого модуля берётся только разбор
спецификации и наличие ключа.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


# Имена классов берём строками и импортируем лениво: если провайдер требует
# необязательную зависимость, отсутствие ключа не должно ронять весь харнесс.
@dataclass(frozen=True)
class Provider:
	name: str
	cls_name: str
	env: tuple[str, ...] = ()  # хотя бы одна из переменных должна быть заполнена
	default_base_url: str | None = None
	aliases: tuple[str, ...] = ()  # префиксы имён моделей для угадывания провайдера
	api: str = ''  # какой tool-use диалект понимает: openai | anthropic | пусто = только baseline
	note: str = ''


PROVIDERS: dict[str, Provider] = {
	p.name: p
	for p in [
		Provider('openai', 'ChatOpenAI', ('OPENAI_API_KEY',), aliases=('gpt-', 'o1', 'o3', 'o4'), api='openai'),
		Provider('anthropic', 'ChatAnthropic', ('ANTHROPIC_API_KEY',), aliases=('claude-',), api='anthropic'),
		Provider('google', 'ChatGoogle', ('GOOGLE_API_KEY', 'GEMINI_API_KEY'), aliases=('gemini-',)),
		Provider('groq', 'ChatGroq', ('GROQ_API_KEY',)),
		Provider('deepseek', 'ChatDeepSeek', ('DEEPSEEK_API_KEY',), aliases=('deepseek-',)),
		Provider('openrouter', 'ChatOpenRouter', ('OPENROUTER_API_KEY',), api='openai', note='имя модели вида qwen/qwen3-235b'),
		Provider('cerebras', 'ChatCerebras', ('CEREBRAS_API_KEY',)),
		Provider('mistral', 'ChatMistral', ('MISTRAL_API_KEY',), aliases=('mistral-',)),
		Provider('vercel', 'ChatVercel', ('AI_GATEWAY_API_KEY', 'VERCEL_OIDC_TOKEN')),
		Provider('azure', 'ChatAzureOpenAI', ('AZURE_OPENAI_API_KEY', 'AZURE_OPENAI_KEY')),
		Provider('bedrock', 'ChatAnthropicBedrock', ('AWS_ACCESS_KEY_ID',)),
		Provider('browseruse', 'ChatBrowserUse', ('BROWSER_USE_API_KEY',), note='облачная модель самих browser-use'),
		Provider('ollama', 'ChatOllama', (), note='локально, ключ не нужен; host в OLLAMA_HOST'),
		Provider(
			'compat',
			'ChatOpenAI',
			(),
			api='openai',
			note='любой OpenAI-совместимый эндпоинт: vLLM, LM Studio, together — задай base_url',
		),
	]
}


def _fields(cls) -> set[str]:
	"""Классы browser-use — dataclass'ы, а не pydantic-модели.

	Но апстрим волен это поменять, поэтому смотрим оба варианта, а не один.
	"""
	import dataclasses

	if dataclasses.is_dataclass(cls):
		return {f.name for f in dataclasses.fields(cls)}
	mf = getattr(cls, 'model_fields', None)
	if mf:
		return set(mf)
	import inspect

	return set(inspect.signature(cls.__init__).parameters) - {'self'}


def _load(cls_name: str):
	import browser_use

	cls = getattr(browser_use, cls_name, None)
	if cls is None:
		raise RuntimeError(
			f'В установленном browser-use нет класса {cls_name}. '
			f'Похоже, апстрим переименовал провайдера — проверь `python -m bu_eval doctor`.'
		)
	return cls


def guess_provider(model: str) -> str:
	m = model.lower()
	for p in PROVIDERS.values():
		if any(m.startswith(a) for a in p.aliases):
			return p.name
	raise ValueError(
		f'Не понимаю, чей это модельный идентификатор: {model!r}. '
		f'Укажи явно, например "openrouter:{model}". Провайдеры: {", ".join(PROVIDERS)}'
	)


def parse_spec(spec: str) -> tuple[str, str]:
	"""'openai:gpt-5-mini' -> ('openai','gpt-5-mini'); 'ollama:qwen3:8b' -> ('ollama','qwen3:8b')"""
	head, sep, tail = spec.partition(':')
	if sep and head in PROVIDERS:
		return head, tail
	return guess_provider(spec), spec


def missing_key(provider: str) -> str | None:
	p = PROVIDERS[provider]
	if not p.env or any(os.getenv(e) for e in p.env):
		return None
	return ' или '.join(p.env)


def make_model(
	spec: str,
	temperature: float | None = 0.0,
	max_output_tokens: int | None = 32000,
	base_url: str | None = None,
	**extra,
):
	"""Дефолт browser-use — 4096 токенов на ответ.

	Длинная таблица туда не влезает, структурированный вывод обрезается на
	середине, прогон уходит в мусор. Поднимаем явно.
	"""
	provider, model = parse_spec(spec)
	p = PROVIDERS[provider]

	need = missing_key(provider)
	if need:
		raise RuntimeError(f'Провайдер {provider}: нет {need}. Положи в .env рядом с проектом.')

	cls = _load(p.cls_name)
	fields = _fields(cls)
	kw: dict = {'model': model}

	def put(name, value):
		if value is not None and name in fields:
			kw[name] = value
			return True
		return False

	put('temperature', temperature)
	# лимит на ответ называется по-разному у разных провайдеров
	for name in ('max_completion_tokens', 'max_tokens', 'max_output_tokens'):
		if put(name, max_output_tokens):
			break

	url = base_url or p.default_base_url or os.getenv(f'{provider.upper()}_BASE_URL')
	if url:
		put('base_url', url) or put('host', url)
	if provider == 'ollama' and os.getenv('OLLAMA_HOST'):
		put('host', os.getenv('OLLAMA_HOST'))

	kw.update({k: v for k, v in extra.items() if k in fields})
	return cls(**kw)


def available() -> list[tuple[str, bool, str]]:
	"""Что реально можно запустить прямо сейчас: (провайдер, есть ключ, примечание)."""
	out = []
	for name, p in PROVIDERS.items():
		need = missing_key(name)
		status = ('ключ не нужен' if not p.env else 'ключ найден') if need is None else f'нужен {need}'
		api = f'; tool-use: {p.api}' if p.api else '; только бэкенд browser-use'
		out.append((name, need is None, f'{status}{api}' + (f'; {p.note}' if p.note else '')))
	return out
