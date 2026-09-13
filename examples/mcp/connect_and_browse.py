"""Подключиться к нашему MCP-серверу (browser_use.mcp.server, BuMcpServer) как обычный
MCP-клиент, без Claude Code и без остальной обвязки — просто протокол.

Показывает минимальный цикл: список инструментов -> навигация -> чтение состояния
страницы. Это не инструмент browser_use.Agent с LLM в цикле — здесь решения
принимает сам этот скрипт, а не модель; так выглядит протокол снаружи, если
писать своего MCP-клиента на Python.

Требует уже запущенный Chrome с открытым CDP (по умолчанию 127.0.0.1:9222):

    scripts/chrome-automation.sh

Запуск из корня репозитория:

    uv run python examples/mcp/connect_and_browse.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parents[2]


def text_blocks(result) -> list[str]:
	return [c.text for c in result.content if getattr(c, 'type', None) == 'text']


async def main() -> None:
	params = StdioServerParameters(
		command=sys.executable,
		args=['-m', 'browser_use.mcp.server'],
		env={**os.environ, 'PYTHONPATH': str(REPO_ROOT)},
		cwd=str(REPO_ROOT),
	)

	async with stdio_client(params) as (read, write):
		async with ClientSession(read, write) as session:
			await session.initialize()

			tools = await session.list_tools()
			print(f'{len(tools.tools)} инструментов, например: {[t.name for t in tools.tools[:5]]}')

			await session.call_tool('browser_navigate', {'url': 'https://example.com', 'new_tab': True})

			state = await session.call_tool('browser_state', {})
			blocks = text_blocks(state)
			head = json.loads(blocks[0]) if blocks else {}
			print(f"url={head.get('url')!r}, вкладок={len(head.get('tabs', []))}")
			if len(blocks) > 1:
				print('--- дерево страницы (обрезано) ---')
				print(blocks[1][:500])


if __name__ == '__main__':
	asyncio.run(main())
