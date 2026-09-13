"""Папка вложений: единственное место, откуда агенту разрешено прикладывать файлы.

Зачем отдельный слой. Загрузка файла на сайт — действие, выносящее данные с
машины наружу. Пускать агента грузить произвольный путь нельзя: одна ошибка в
рассуждении — и в чат уехал `~/.ssh/id_rsa`. Поэтому граница простая и
проверяемая глазами: есть ОДНА папка, агент может приложить только то, что
лежит в ней. Что не в папке — то не грузится, без исключений (fail closed, как
и весь остальной слой bu-mcp).

Где папка. По умолчанию `bu_mcp/uploads/` прямо в репозитории — рядом с кодом,
далеко ходить не надо. Путь перекрывается `BU_MCP_UPLOAD_DIR`. Содержимое папки
в git не попадает (см. `.gitignore`): это рабочие файлы пользователя, а не код.

Механизм самой загрузки — не здесь. Файл прикладывается через
`DOM.setFileInputFiles` на `<input type=file>` страницы (см. `server.upload_file`
и `browser_use ... UploadFileEvent`): никакого нативного диалога ОС, работает и
в фоновой вкладке. Этот модуль отвечает только за «какой файл можно» и «как имя
превратить в абсолютный путь внутри папки».
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ['upload_dir', 'allowed_files', 'resolve', 'relative_name', 'list_names', 'NotAllowedError']


class NotAllowedError(ValueError):
	"""Файл нельзя приложить: его нет в папке вложений, он пуст или это не файл."""


def upload_dir() -> Path:
	"""Папка вложений. ``BU_MCP_UPLOAD_DIR`` перекрывает дефолт ``bu_mcp/uploads``.

	Создаётся при первом обращении: пустая папка — нормальное стартовое
	состояние, а не ошибка.
	"""
	raw = os.getenv('BU_MCP_UPLOAD_DIR')
	path = Path(raw).expanduser() if raw else Path(__file__).resolve().parent / 'uploads'
	try:
		path.mkdir(parents=True, exist_ok=True)
	except Exception:
		# Не смогли создать (права, гонка) — вернём как есть; resolve всё равно
		# упадёт с понятной ошибкой, когда файла не окажется.
		pass
	return path


def _within(path: Path, root: Path) -> bool:
	"""Лежит ли ``path`` внутри ``root`` после раскрытия симлинков. Fail closed."""
	try:
		rp = os.path.realpath(path)
		rr = os.path.realpath(root)
	except Exception:
		return False
	return rp == rr or rp.startswith(rr + os.sep)


def allowed_files() -> list[str]:
	"""Абсолютные (realpath) пути всех обычных файлов в папке, рекурсивно.

	Именно этот список уходит в ``available_file_paths`` реестрового действия
	upload: сверка идёт по точному совпадению пути, поэтому здесь realpath.
	Симлинки, ведущие наружу папки, отсеиваются — иначе граница дырявая.
	"""
	root = upload_dir()
	out: list[str] = []
	try:
		for p in sorted(root.rglob('*')):
			if p.is_file() and _within(p, root):
				out.append(os.path.realpath(p))
	except Exception:
		pass
	return out


def resolve(name: str) -> str:
	"""Имя из папки -> абсолютный путь внутри неё. Иначе ``NotAllowedError``.

	``name`` — имя файла или путь относительно папки вложений (``report.pdf``,
	``sub/pic.png``). Абсолютные пути и ``..``-выходы за папку отклоняются, а не
	«очищаются»: молча подставить другой файл хуже, чем отказать. Файл обязан
	существовать и быть непустым — пустой чаще всего значит «не докачался».
	"""
	root = upload_dir()
	raw = (name or '').strip()
	if not raw:
		raise NotAllowedError(_no_file_msg('<empty>', root))
	candidate = (root / raw).expanduser()
	if not _within(candidate, root):
		raise NotAllowedError(
			f'{raw!r} resolves outside the upload folder {root}. Only files inside it can be attached; '
			f'put the file there (or set BU_MCP_UPLOAD_DIR) instead of passing an absolute or ../ path.'
		)
	real = Path(os.path.realpath(candidate))
	if not real.is_file():
		raise NotAllowedError(_no_file_msg(raw, root))
	if real.stat().st_size == 0:
		raise NotAllowedError(f'{raw!r} in {root} is empty (0 bytes) — it may not have finished copying.')
	return str(real)


def relative_name(path: str) -> str:
	"""Путь -> имя относительно папки вложений (для журнала и макроса).

	В журнал и макрос кладём ИМЯ, а не абсолютный путь: так сценарий переносится
	на другую машину, где та же папка лежит по другому абсолютному пути. Если
	файл вне папки (не должно случаться) — вернём basename."""
	root = upload_dir()
	try:
		return os.path.relpath(os.path.realpath(path), os.path.realpath(root))
	except Exception:
		return os.path.basename(path)


def list_names() -> list[str]:
	"""Имена файлов в папке относительно неё — для подсказок в сообщениях."""
	root = upload_dir()
	return [relative_name(p) for p in allowed_files()]


def _no_file_msg(name: str, root: Path) -> str:
	names = list_names()
	have = ('available now: ' + ', '.join(names)) if names else 'the folder is currently empty'
	return f'{name!r} is not in the upload folder {root} ({have}). Put the file there, then attach it by name.'
