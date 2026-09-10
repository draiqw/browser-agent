#!/usr/bin/env python3
"""Перенос залогиненных аккаунтов между машинами.

Зачем не копировать профиль. На macOS куки помечены `v10`, а ключа к ним в
профиле НЕТ: `os_crypt` в `Local State` пустой, пароль лежит только в Keychain
(`Chrome Safe Storage`). Скопированная папка на другой машине даёт разлогиненный
Chrome. Подложить свой ключ в чужой Keychain тоже нельзя — сломаются все уже
существующие там профили, их куки шифровались другим ключом. На Linux схема
третья (gnome-keyring либо пароль `peanuts`), на Windows четвёртая (DPAPI, а с
Chrome 127+ ещё и App-Bound Encryption, которую снаружи браузера не открыть).

Поэтому мы не трогаем файлы профиля вообще. Живой Chrome отдаёт куки по CDP уже
расшифрованными — вместе с httpOnly, которые из JS не достать. Один код
работает на macOS, Linux и Windows одинаково, и никакая App-Bound Encryption не
мешает: расшифровывает сам браузер, а мы просто спрашиваем.

    ./accounts.py export --domains chatgpt.com,openai.com --out ~/accounts.age
    ./accounts.py import --in ~/accounts.age

Файл содержит ЖИВЫЕ токены сессий — это полный доступ к аккаунтам, поэтому он
всегда шифруется парольной фразой (AES-256-GCM, ключ из scrypt). Открытый JSON
пишется только по явному `--plaintext`.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import secrets
import sys
import urllib.request
from pathlib import Path
from typing import Any

MAGIC = b'BUACCT1\n'
CDP_URL = os.getenv('BU_MCP_CDP_URL', 'http://127.0.0.1:9222')


# -- шифрование файла --------------------------------------------------- #


def _key(passphrase: str, salt: bytes) -> bytes:
	import hashlib

	# scrypt с параметрами, которые на ноутбуке считаются ~0.1 с: подбор по
	# словарю становится дорогим, а разовое открытие файла остаётся мгновенным.
	# `maxmem` задан явно: у OpenSSL дефолтный потолок 32 МБ, а n=2**15 при r=8
	# требует чуть больше и падает на `memory limit exceeded`.
	return hashlib.scrypt(passphrase.encode(), salt=salt, n=2**15, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)


def seal(data: bytes, passphrase: str) -> bytes:
	from cryptography.hazmat.primitives.ciphers.aead import AESGCM

	salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
	blob = AESGCM(_key(passphrase, salt)).encrypt(nonce, data, None)
	return MAGIC + salt + nonce + blob


def unseal(raw: bytes, passphrase: str) -> bytes:
	from cryptography.hazmat.primitives.ciphers.aead import AESGCM

	if not raw.startswith(MAGIC):
		raise SystemExit('файл не похож на выгрузку accounts.py (нет заголовка)')
	body = raw[len(MAGIC) :]
	salt, nonce, blob = body[:16], body[16:28], body[28:]
	try:
		return AESGCM(_key(passphrase, salt)).decrypt(nonce, blob, None)
	except Exception:
		raise SystemExit('не расшифровалось: неверная парольная фраза или файл повреждён') from None


# -- разговор с браузером ------------------------------------------------ #


class CDP:
	"""Минимальный клиент CDP поверх websockets: нам хватает нескольких команд."""

	def __init__(self, ws: Any) -> None:
		self._ws = ws
		self._id = 0

	@classmethod
	async def connect(cls) -> Any:
		import websockets

		def _version() -> dict:
			return json.load(urllib.request.urlopen(f'{CDP_URL}/json/version', timeout=5))

		try:
			# urllib блокирующий; в async-функции его положено уводить в поток (ASYNC210).
			ver = await asyncio.to_thread(_version)
		except Exception as exc:
			raise SystemExit(f'Chrome не отвечает на {CDP_URL}: {exc}\nПодними его: scripts/chrome-automation.sh') from None
		return await websockets.connect(ver['webSocketDebuggerUrl'], max_size=None)

	async def send(self, method: str, params: dict | None = None) -> dict:
		self._id += 1
		mine = self._id
		await self._ws.send(json.dumps({'id': mine, 'method': method, 'params': params or {}}))
		while True:
			msg = json.loads(await self._ws.recv())
			if msg.get('id') != mine:
				continue
			if 'error' in msg:
				raise SystemExit(f'CDP {method}: {msg["error"]}')
			return msg.get('result', {})


def _matches(domain: str, wanted: list[str]) -> bool:
	if not wanted:
		return True
	d = domain.lstrip('.').lower()
	return any(d == w or d.endswith('.' + w) for w in wanted)


async def _origin_storage(cdp: CDP, origins: list[str]) -> dict[str, dict[str, str]]:
	"""localStorage по origin'ам.

	Куки — не всё: часть сайтов держит в localStorage состояние, без которого
	сессия выглядит как чужая. Читаем через `DOM.getDocument`-независимый путь:
	открываем вкладку на origin и спрашиваем у страницы.
	"""
	out: dict[str, dict[str, str]] = {}
	for origin in origins:
		try:
			target = await cdp.send('Target.createTarget', {'url': origin, 'background': True})
			tid = target['targetId']
		except SystemExit:
			continue
		try:
			att = await cdp.send('Target.attachToTarget', {'targetId': tid, 'flatten': True})
			sid = att['sessionId']
			await asyncio.sleep(2.0)
			self_id = cdp._id + 1
			cdp._id = self_id
			await cdp._ws.send(
				json.dumps(
					{
						'id': self_id,
						'sessionId': sid,
						'method': 'Runtime.evaluate',
						'params': {
							'expression': 'JSON.stringify(Object.fromEntries(Object.entries(localStorage)))',
							'returnByValue': True,
						},
					}
				)
			)
			while True:
				msg = json.loads(await cdp._ws.recv())
				if msg.get('id') == self_id:
					break
			val = (msg.get('result', {}).get('result') or {}).get('value')
			if val:
				parsed = json.loads(val)
				if parsed:
					out[origin] = parsed
		except Exception as exc:
			print(f'  localStorage {origin}: пропущен ({exc})', file=sys.stderr)
		finally:
			await cdp.send('Target.closeTarget', {'targetId': tid})
	return out


async def do_export(domains: list[str], with_storage: bool) -> dict:
	ws = await CDP.connect()
	async with ws:
		cdp = CDP(ws)
		cookies = [c for c in (await cdp.send('Storage.getCookies'))['cookies'] if _matches(c['domain'], domains)]
		bundle: dict = {'version': 1, 'cookies': cookies, 'localStorage': {}}
		print(f'кук отобрано: {len(cookies)}')
		if with_storage and domains:
			origins = [f'https://{d}' for d in domains]
			bundle['localStorage'] = await _origin_storage(cdp, origins)
			print(
				f'localStorage: {sum(len(v) for v in bundle["localStorage"].values())} ключей '
				f'на {len(bundle["localStorage"])} origin(ах)'
			)
		return bundle


async def do_import(bundle: dict) -> None:
	ws = await CDP.connect()
	async with ws:
		cdp = CDP(ws)
		cookies = bundle.get('cookies') or []
		# Именно Storage.setCookies, а не Network.setCookies: вторая живёт в
		# домене страницы, и на browser-сессии её просто нет
		# ('Network.setCookies' wasn't found). Первая — browser-level.
		#
		# Поля ниже CDP на вход не принимает, они справочные (`size`, `session`)
		# либо выводятся из самой куки (`sourcePort`, `sourceScheme`).
		drop = {'size', 'session', 'sourcePort', 'sourceScheme'}
		clean = [{k: v for k, v in c.items() if k not in drop} for c in cookies]
		await cdp.send('Storage.setCookies', {'cookies': clean})
		print(f'кук записано: {len(clean)}')

		store = bundle.get('localStorage') or {}
		for origin, kv in store.items():
			target = await cdp.send('Target.createTarget', {'url': origin, 'background': True})
			tid = target['targetId']
			try:
				att = await cdp.send('Target.attachToTarget', {'targetId': tid, 'flatten': True})
				sid = att['sessionId']
				await asyncio.sleep(2.0)
				cdp._id += 1
				mine = cdp._id
				await cdp._ws.send(
					json.dumps(
						{
							'id': mine,
							'sessionId': sid,
							'method': 'Runtime.evaluate',
							'params': {
								'expression': f'(function(d){{for(const k in d)localStorage.setItem(k,d[k]);return Object.keys(d).length}})({json.dumps(kv)})',
								'returnByValue': True,
							},
						}
					)
				)
				while True:
					msg = json.loads(await cdp._ws.recv())
					if msg.get('id') == mine:
						break
				print(f'  localStorage {origin}: {len(kv)} ключей')
			finally:
				await cdp.send('Target.closeTarget', {'targetId': tid})


def main() -> None:
	ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	sub = ap.add_subparsers(dest='cmd', required=True)

	e = sub.add_parser('export', help='выгрузить аккаунты из живого Chrome')
	e.add_argument('--domains', default='', help='через запятую; пусто — все домены')
	e.add_argument('--out', required=True, type=Path)
	e.add_argument('--plaintext', action='store_true', help='НЕ шифровать (только для отладки)')
	e.add_argument('--no-storage', action='store_true', help='только куки, без localStorage')

	i = sub.add_parser('import', help='залить аккаунты в живой Chrome')
	i.add_argument('--in', dest='src', required=True, type=Path)

	a = ap.parse_args()

	if a.cmd == 'export':
		domains = [d.strip().lower() for d in a.domains.split(',') if d.strip()]
		bundle = asyncio.run(do_export(domains, not a.no_storage))
		raw = json.dumps(bundle, ensure_ascii=False).encode()
		if a.plaintext:
			a.out.write_bytes(raw)
			print(f'ЗАПИСАНО БЕЗ ШИФРОВАНИЯ: {a.out} — в файле живые токены сессий')
		else:
			p1 = getpass.getpass('парольная фраза для файла: ')
			if p1 != getpass.getpass('ещё раз: '):
				raise SystemExit('фразы не совпали')
			if not p1:
				raise SystemExit('пустая фраза не годится')
			a.out.write_bytes(seal(raw, p1))
			print(f'записано: {a.out}')
		os.chmod(a.out, 0o600)
		print('перенеси файл на целевую машину и там: accounts.py import --in <файл>')
		return

	raw = a.src.read_bytes()
	if raw.startswith(MAGIC):
		raw = unseal(raw, getpass.getpass('парольная фраза файла: '))
	asyncio.run(do_import(json.loads(raw)))
	print('готово. Проверь: открой сайт в этом Chrome — должен быть залогинен.')


if __name__ == '__main__':
	main()
