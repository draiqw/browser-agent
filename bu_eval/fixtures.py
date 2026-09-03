"""Локальный статический сервер для офлайновых задач.

`file://` browser-use блокирует `SecurityWatchdog`'ом, поэтому фикстуры отдаём
по http с петлевого адреса. Сервер живёт в фоновом потоке до конца процесса.

Оба бэкенда ходят к нему одинаково: baseline поднимает свой Chrome, наш MCP-слой
подключён к уже работающему на CDP — петлевой адрес видят оба.
"""

from __future__ import annotations

import functools
import http.server
import os
import socket
import socketserver
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent / 'fixtures'


def _free_port(start: int = 8777, tries: int = 40) -> int:
	"""Порт выбираем при импорте: 8777 может быть занят чужим сервером,
	а адрес нужно знать заранее — он попадает в текст задачи."""
	for port in range(start, start + tries):
		with socket.socket() as s:
			s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
			try:
				s.bind(('127.0.0.1', port))
				return port
			except OSError:
				continue
	raise RuntimeError(f'Нет свободного порта в диапазоне {start}..{start + tries}')


PORT = int(os.getenv('BU_EVAL_FIXTURE_PORT') or _free_port())

_server: socketserver.TCPServer | None = None
_lock = threading.Lock()


class _Quiet(http.server.SimpleHTTPRequestHandler):
	def log_message(self, *a):  # без шума в консоли прогона
		pass


def base_url() -> str:
	"""Поднимает сервер при первом обращении и возвращает адрес корня фикстур."""
	global _server
	ROOT.mkdir(parents=True, exist_ok=True)
	with _lock:
		if _server is None:
			handler = functools.partial(_Quiet, directory=str(ROOT))
			socketserver.TCPServer.allow_reuse_address = True
			_server = socketserver.TCPServer(('127.0.0.1', PORT), handler)
			threading.Thread(target=_server.serve_forever, daemon=True).start()
	return f'http://127.0.0.1:{PORT}'


def url_for(name: str) -> str:
	return f'{base_url()}/{name}'


def stop() -> None:
	global _server
	with _lock:
		if _server is not None:
			_server.shutdown()
			_server.server_close()
			_server = None
