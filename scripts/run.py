"""
browser-use на твоём живом Chrome через CDP, модель — через OpenRouter.

Chrome должен быть запущен с --remote-debugging-port=9222.
Ключ берётся из файла, заданного BU_ENV_FILE (в код не попадает).

  ~/browser-use/.venv/bin/python ~/bu-lab/run.py "задача"
"""

import asyncio
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from browser_use import Agent, ChatOpenRouter
from browser_use.browser import BrowserProfile, BrowserSession

load_dotenv(os.getenv('BU_ENV_FILE', Path.home() / '.claude-accounts' / 'openrouter.env'))

CDP_URL = 'http://127.0.0.1:9222'
MODEL = os.getenv('BU_MODEL', 'anthropic/claude-haiku-4.5')
TASK = ' '.join(sys.argv[1:]) or 'Открой example.com и скажи, что написано в заголовке'


def check_cdp() -> None:
    try:
        r = httpx.get(f'{CDP_URL}/json/version', timeout=2)
        print(f'CDP ok: {r.json().get("Browser")}')
    except Exception:
        sys.exit(
            'Chrome не слушает 9222.\n'
            'Закрой Chrome и запусти его так:\n'
            '  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222\n'
        )


async def main() -> None:
    check_cdp()
    if not os.getenv('OPENROUTER_API_KEY'):
        sys.exit('OPENROUTER_API_KEY не найден в ~/.claude-accounts/openrouter.env')

    session = BrowserSession(browser_profile=BrowserProfile(cdp_url=CDP_URL, is_local=True))
    agent = Agent(
        task=TASK,
        llm=ChatOpenRouter(model=MODEL),
        browser_session=session,
    )
    history = await agent.run(max_steps=15)
    print('\n=== РЕЗУЛЬТАТ ===')
    print(history.final_result())


if __name__ == '__main__':
    asyncio.run(main())
