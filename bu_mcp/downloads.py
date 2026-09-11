"""Папка скачанного: куда попадает то, что страница отдала браузеру.

Зачем отдельный слой. По умолчанию browser-use складывает скачанное во
временный каталог со случайным именем (`/tmp/browser-use-downloads-<uuid>`):
файл технически есть, но найти его человеком невозможно, а после перезагрузки
машины он исчезает. Для сценария «сходил на сайт и забрал результат» это
бесполезно — результат должен лежать в одном известном месте.

Где папка. По умолчанию `bu_mcp/downloads/` в репозитории, рядом с папкой
вложений; перекрывается `BU_MCP_DOWNLOAD_DIR`. Содержимое в git не попадает.

Граница здесь мягче, чем у вложений, и это намеренно: отдавать файл наружу
опасно (уехали чужие данные), принимать файл внутрь одной заранее известной
папки — нет. Поэтому тут нет allowlist-логики, только «где лежит» и «что
появилось нового с момента X»: последнее нужно, чтобы честно отвечать на
вопрос «скачалось ли», а не верить кнопке на странице.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

__all__ = ['download_dir', 'list_files', 'snapshot', 'new_since', 'describe', 'mark_baseline', 'baseline']


def download_dir() -> Path:
	"""Папка скачанного. ``BU_MCP_DOWNLOAD_DIR`` перекрывает дефолт ``bu_mcp/downloads``."""
	raw = os.getenv('BU_MCP_DOWNLOAD_DIR')
	path = Path(raw).expanduser() if raw else Path(__file__).resolve().parent / 'downloads'
	try:
		path.mkdir(parents=True, exist_ok=True)
	except Exception:
		pass
	return path


def _partial(name: str) -> bool:
	"""Недокачанный файл Chrome: считать его результатом нельзя."""
	return name.endswith('.crdownload') or name.endswith('.tmp') or name.startswith('.')


def list_files() -> list[Path]:
	"""Что лежит в папке скачанного, свежие первыми. Недокачанное не показываем."""
	out: list[Path] = []
	try:
		for p in download_dir().rglob('*'):
			if p.is_file() and not _partial(p.name):
				out.append(p)
	except Exception:
		pass
	return sorted(out, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)


def snapshot() -> set[str]:
	"""Слепок папки до действия: с чем сравнивать, чтобы понять, что скачалось."""
	return {str(p) for p in list_files()}


def new_since(before: set[str]) -> list[Path]:
	"""Файлы, появившиеся после слепка ``before``."""
	return [p for p in list_files() if str(p) not in before]


def describe(path: Path) -> dict[str, Any]:
	"""Файл для отчёта: имя, размер, путь. Без чтения содержимого."""
	try:
		size = path.stat().st_size
	except Exception:
		size = -1
	try:
		name = str(path.relative_to(download_dir()))
	except Exception:
		name = path.name
	return {'file': name, 'bytes': size, 'path': str(path)}


#: Слепок папки ПЕРЕД очередным действием. Нужен вот зачем: чекпоинт про
#: скачивание идёт отдельным шагом ПОСЛЕ клика, а загрузка успевает закончиться
#: раньше, чем чекпоинт стартует. Снимал бы он папку сам — увидел бы уже
#: скачанный файл как «был до меня» и сказал бы «ничего не скачалось». Поэтому
#: точка отсчёта ставится до действия, а не до проверки.
_BASELINE: set[str] | None = None


def mark_baseline() -> None:
	"""Запомнить состояние папки перед действием. Зовётся на каждое действие."""
	global _BASELINE
	_BASELINE = snapshot()


def baseline() -> set[str]:
	"""Точка отсчёта для «что скачалось»: слепок перед последним действием."""
	return snapshot() if _BASELINE is None else set(_BASELINE)
