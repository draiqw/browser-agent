"""Живые ожидания готовности страницы поверх browser-use.

Зачем это существует
--------------------
В browser-use ожидания — самое слабое место, и это проверяется по коду:

* `BrowserProfile.minimum_wait_page_load_time` и
  `BrowserProfile.wait_for_network_idle_page_load_time`
  (`browser_use/browser/profile.py:691-692`) документированы, мапятся на env
  (`browser_use/beta/service.py:954-955`) и принимаются в `BrowserSession.__init__`,
  но НИ РАЗУ не читаются как `self.browser_profile.<...>` — мёртвый код.
  Для сравнения, соседний `wait_between_actions` из того же блока реально
  читается в `browser_use/agent/service.py:2769`.
* Вся «стабилизация» перед снятием состояния — одна проверка pending-ресурсов и
  `asyncio.sleep(0.3)` (`browser_use/browser/watchdogs/dom_watchdog.py:281-288`).
* После клика навигация не ожидается вообще: `on_ClickElementEvent`
  (`browser_use/browser/watchdogs/default_action_watchdog.py:337`) в самом
  «долгом» пути делает `asyncio.sleep(0.05)` с комментарием
  «Navigation is handled by BrowserSession via events» — и на этом всё.

Здесь — замена: лестница fail-open по образцу `wait_for_page_ready()` из Skyvern.
Каждая стадия имеет свой жёсткий кап, при таймауте логируется и пропускается,
наружу не бросает ничего. Отдельно — честное ожидание навигации по `loaderId`
через CDP lifecycle events (механика взята из
`BrowserSession._navigate_and_wait`, `browser_use/browser/session.py:1010-1113`).

Почему капы такие маленькие: Playwright официально помечает `networkidle` как
DISCOURAGED и прямо пишет, что универсального признака готовности не существует.
Поэтому сетевая стадия — 3 секунды и fail-open: some pages never go idle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ['wait_for_page_ready', 'wait_after_navigation']


# Капы отдельных стадий (секунды). Каждый ещё и урезается остатком общего бюджета.
_CAP_LOADING_INDICATORS = 5.0
_CAP_NETWORK_QUIET = 3.0

# Окно покоя для MutationObserver.
_MUTATION_QUIET_MS = 300.0

# Сколько бюджета стадии резервируется под отключение наблюдателя.
_MUTATION_TEARDOWN_BUDGET = 0.3

# Шаг опроса.
_POLL_INTERVAL = 0.1

# Сколько ждать одиночный Runtime.evaluate, прежде чем считать его зависшим.
_EVAL_TIMEOUT = 2.0

# Ключ, под которым в window живёт состояние наблюдателя мутаций.
_MUTATION_STATE_KEY = '__buMcpMutationQuiet'


# --------------------------------------------------------------------------- #
# JS
# --------------------------------------------------------------------------- #

# Стадия 1. Индикаторы загрузки: спиннеры, скелетоны, прогрессбары.
# Считаем только реально отрисованные элементы — иначе вечно висящий в разметке
# скрытый .spinner не даст сойтись никогда.
_JS_LOADING_INDICATORS = r"""
(() => {
  const SELECTORS = [
    '[aria-busy="true"]',
    // ТОЛЬКО индетерминированные прогрессбары. По ARIA наличие aria-valuenow
    // означает определённый прогресс — это визуализация данных, а не загрузка.
    // Живой пример: полоса языков на github.com имеет role="progressbar"
    // с aria-valuenow="99" и висит на странице вечно.
    '[role="progressbar"]:not([aria-valuenow])',
    'progress:not([value])',
    '[class*="spinner" i]',
    '[class*="loader" i]',
    '[class*="loading" i]',
    '[class*="skeleton" i]',
    '[class*="shimmer" i]',
    '[class*="placeholder-glow" i]',
    '[data-loading="true"]',
    '[data-testid*="loading" i]',
    '[data-testid*="spinner" i]',
    '[data-testid*="skeleton" i]',
    '.MuiSkeleton-root, .MuiCircularProgress-root',
    '.ant-spin, .ant-skeleton',
    '.chakra-skeleton',
    '.v-progress-circular, .v-skeleton-loader',
    '.react-loading-skeleton',
  ];

  const rendered = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return false;
    if (parseFloat(cs.opacity || '1') < 0.05) return false;
    return true;
  };

  let count = 0;
  const samples = [];
  for (const sel of SELECTORS) {
    let nodes;
    try { nodes = document.querySelectorAll(sel); } catch (e) { continue; }
    for (const el of nodes) {
      if (!rendered(el)) continue;
      count++;
      if (samples.length < 3) {
        samples.push(sel + ' => ' + el.tagName.toLowerCase()
          + (el.className && typeof el.className === 'string'
              ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.')
              : ''));
      }
    }
  }
  return { count: count, samples: samples };
})()
"""


# Стадия 2. Сетевая тишина.
# Ядро и денилист доменов аналитики взяты один-в-один из
# browser_use/browser/watchdogs/dom_watchdog.py:108-174 (_get_pending_network_requests):
# performance.getEntriesByType('resource') + responseEnd === 0 + отсев рекламы,
# долгих (>10s, скорее всего вечный поллинг) и некритичных ресурсов.
_JS_NETWORK_PENDING = r"""
(() => {
  try { performance.setResourceTimingBufferSize(1000); } catch (e) {}

  const now = performance.now();
  const resources = performance.getEntriesByType('resource');

  // денилист из dom_watchdog.py:114-133
  const adDomains = [
    'doubleclick.net', 'googlesyndication.com', 'googletagmanager.com',
    'facebook.net', 'analytics', 'ads', 'tracking', 'pixel',
    'hotjar.com', 'clarity.ms', 'mixpanel.com', 'segment.com',
    'demdex.net', 'omtrdc.net', 'adobedtm.com', 'ensighten.com',
    'newrelic.com', 'nr-data.net', 'google-analytics.com',
    'connect.facebook.net', 'platform.twitter.com', 'platform.linkedin.com',
    '.cloudfront.net/image/', '.akamaized.net/image/',
    '/tracker/', '/collector/', '/beacon/', '/telemetry/', '/log/',
    '/events/', '/eventBatch', '/track.', '/metrics/'
  ];

  const pending = [];
  for (const entry of resources) {
    if (entry.responseEnd !== 0) continue;
    const url = entry.name;
    if (adDomains.some((d) => url.includes(d))) continue;
    if (url.startsWith('data:') || url.length > 500) continue;

    const loadingDuration = now - entry.startTime;
    if (loadingDuration > 10000) continue;              // застрявший/вечный поллинг

    const type = entry.initiatorType || 'unknown';
    if (['img', 'image', 'icon', 'font'].includes(type) && loadingDuration > 3000) continue;
    if (/\.(jpg|jpeg|png|gif|webp|svg|ico)(\?|$)/i.test(url) && loadingDuration > 3000) continue;

    pending.push({ url: url.slice(0, 120), ms: Math.round(loadingDuration), type: type });
  }

  return {
    pending: pending.length,
    samples: pending.slice(0, 3),
    ready_state: document.readyState,
    buffer_full: resources.length >= 990,
  };
})()
"""


# Стадия 3. Тишина мутаций.
# Ключевая деталь: мутации АТРИБУТОВ считаем только на элементах с ненулевым
# getBoundingClientRect. Иначе фоновая возня в невидимом DOM (перекраска классов
# на <head>, скрытые A/B-контейнеры, порталы вне вьюпорта) не даст сойтись никогда.
# По той же причине childList игнорируется, если добавлены/удалены только
# нерендерящиеся узлы (<script>, <style>, <link>, <meta>) — так аналитика,
# доклеивающая теги в <head>, не держит нас вечно.
_JS_MUTATION_INSTALL = (
    r"""
(() => {
  const KEY = '"""
    + _MUTATION_STATE_KEY
    + r"""';
  const prev = window[KEY];
  if (prev && prev.observer) { try { prev.observer.disconnect(); } catch (e) {} }
  if (!document.documentElement) return false;

  const NON_RENDERED = new Set(['SCRIPT', 'STYLE', 'LINK', 'META', 'TITLE', 'NOSCRIPT', 'HEAD']);

  const isRenderedNode = (n) => {
    if (!n) return false;
    if (n.nodeType === 3) return (n.textContent || '').trim().length > 0;   // текст
    if (n.nodeType !== 1) return false;                                     // комментарии и пр.
    return !NON_RENDERED.has(n.nodeName);
  };

  const hasBox = (n) => {
    const el = n && n.nodeType === 1 ? n : (n ? n.parentElement : null);
    if (!el || typeof el.getBoundingClientRect !== 'function') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const state = { last: performance.now(), total: 0, ignored: 0, observer: null, lastKind: null };

  const counts = (rec) => {
    if (rec.type === 'attributes') {
      // ТОЛЬКО на видимых (ненулевой bounding rect) элементах
      return hasBox(rec.target);
    }
    if (rec.type === 'characterData') {
      return hasBox(rec.target);
    }
    // childList
    const touched = [...rec.addedNodes, ...rec.removedNodes];
    if (!touched.some(isRenderedNode)) return false;
    return true;
  };

  const obs = new MutationObserver((records) => {
    let hit = null;
    for (const rec of records) {
      if (counts(rec)) { hit = rec.type; break; }
      state.ignored++;
    }
    if (hit) {
      state.last = performance.now();
      state.total++;
      state.lastKind = hit;
    }
  });

  obs.observe(document.documentElement, {
    childList: true, subtree: true, attributes: true, characterData: true,
  });

  state.observer = obs;
  window[KEY] = state;
  return true;
})()
"""
)

_JS_MUTATION_READ = (
    r"""
(() => {
  const s = window['"""
    + _MUTATION_STATE_KEY
    + r"""'];
  if (!s) return null;
  return { quiet_ms: performance.now() - s.last, total: s.total, ignored: s.ignored, last_kind: s.lastKind };
})()
"""
)

_JS_MUTATION_TEARDOWN = (
    r"""
(() => {
  const s = window['"""
    + _MUTATION_STATE_KEY
    + r"""'];
  if (s && s.observer) { try { s.observer.disconnect(); } catch (e) {} }
  try { delete window['"""
    + _MUTATION_STATE_KEY
    + r"""']; } catch (e) { window['"""
    + _MUTATION_STATE_KEY
    + r"""'] = undefined; }
  return true;
})()
"""
)


# --------------------------------------------------------------------------- #
# Мелкие помощники
# --------------------------------------------------------------------------- #


def _now() -> float:
    return asyncio.get_event_loop().time()


async def _cdp(session: Any, target_id: str | None):
    """CDP-сессия для конкретной вкладки (без перехвата фокуса, если target задан)."""
    if target_id:
        return await session.get_or_create_cdp_session(target_id, focus=False)
    return await session.get_or_create_cdp_session()


async def _evaluate(session: Any, target_id: str | None, expression: str, *, budget: float = _EVAL_TIMEOUT) -> Any:
    """Runtime.evaluate с собственным таймаутом. Бросает — вызывающая стадия ловит."""
    cdp_session = await asyncio.wait_for(_cdp(session, target_id), timeout=budget)
    result = await asyncio.wait_for(
        cdp_session.cdp_client.send.Runtime.evaluate(
            params={'expression': expression, 'returnByValue': True, 'awaitPromise': False},
            session_id=cdp_session.session_id,
        ),
        timeout=budget,
    )
    if result.get('exceptionDetails'):
        details = result['exceptionDetails']
        text = details.get('exception', {}).get('description') or details.get('text') or 'JS exception'
        raise RuntimeError(str(text)[:200])
    return result.get('result', {}).get('value')


async def _current_url(session: Any, target_id: str | None) -> str:
    try:
        if target_id is not None and getattr(session, 'session_manager', None) is not None:
            target = session.session_manager.get_target(target_id)
            if target is not None and getattr(target, 'url', None):
                return str(target.url)
        return str(await asyncio.wait_for(session.get_current_page_url(), timeout=2.0))
    except Exception:
        return ''


def _lifecycle_has(lifecycle: Any, loader_id: str, names: set[str] | None = None) -> bool:
    """Есть ли в буфере lifecycle-событие для данного loaderId (опционально — с именем из names)."""
    if lifecycle is None or not loader_id:
        return False
    try:
        for event in list(lifecycle):
            if event.get('loaderId') != loader_id:
                continue
            if names is None or event.get('name') in names:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


async def _main_frame_loader_id(session: Any, target_id: str | None) -> tuple[str | None, str | None]:
    """(loaderId, url) главного фрейма через Page.getFrameTree."""
    try:
        cdp_session = await asyncio.wait_for(_cdp(session, target_id), timeout=2.0)
        tree = await asyncio.wait_for(
            cdp_session.cdp_client.send.Page.getFrameTree(session_id=cdp_session.session_id),
            timeout=2.0,
        )
        frame = tree.get('frameTree', {}).get('frame', {})
        return frame.get('loaderId'), frame.get('url')
    except Exception as exc:
        logger.debug('waiting: Page.getFrameTree failed: %r', exc)
        return None, None


# --------------------------------------------------------------------------- #
# Стадии
# --------------------------------------------------------------------------- #


async def _stage_loading_indicators(session: Any, target_id: str | None, deadline: float) -> str:
    """Ждём, пока с экрана уйдут спиннеры/скелетоны. Нужны два подряд нулевых замера."""
    zero_streak = 0
    last: dict[str, Any] = {}
    while _now() < deadline:
        data = await _evaluate(session, target_id, _JS_LOADING_INDICATORS)
        last = data if isinstance(data, dict) else {}
        if int(last.get('count', 0)) == 0:
            zero_streak += 1
            if zero_streak >= 2:
                return 'no visible loading indicators'
        else:
            zero_streak = 0
        await asyncio.sleep(_POLL_INTERVAL)
    samples = ', '.join(last.get('samples') or []) or 'n/a'
    raise TimeoutError(f'{last.get("count", "?")} indicator(s) still visible: {samples}')


async def _stage_network_quiet(session: Any, target_id: str | None, deadline: float) -> str:
    """Сетевая тишина. Кап жёсткий и fail-open: some pages never go idle."""
    quiet_streak = 0
    last: dict[str, Any] = {}
    while _now() < deadline:
        data = await _evaluate(session, target_id, _JS_NETWORK_PENDING)
        last = data if isinstance(data, dict) else {}
        pending = int(last.get('pending', 0))
        complete = last.get('ready_state') == 'complete'
        if pending == 0 and complete:
            quiet_streak += 1
            if quiet_streak >= 2:
                note = 'network quiet, readyState=complete'
                if last.get('buffer_full'):
                    note += ' (resource timing buffer near full — счёт может быть занижен)'
                return note
        else:
            quiet_streak = 0
        await asyncio.sleep(_POLL_INTERVAL)
    samples = '; '.join(f'{s.get("type")} {s.get("ms")}ms {s.get("url")}' for s in (last.get('samples') or []))
    raise TimeoutError(
        f'{last.get("pending", "?")} pending request(s), readyState={last.get("ready_state")}'
        + (f' [{samples}]' if samples else '')
    )


async def _stage_mutation_quiet(session: Any, target_id: str | None, deadline: float) -> str:
    """MutationObserver, окно покоя 300 мс (мутации атрибутов — только на видимых элементах)."""
    installed = await _evaluate(session, target_id, _JS_MUTATION_INSTALL)
    if installed is not True:
        raise RuntimeError('MutationObserver not installed (no documentElement?)')

    # Резервируем хвост бюджета под teardown: иначе отключение наблюдателя в finally
    # задерживает проброс TimeoutError, внешний wait_for срабатывает первым и
    # диагностика («что именно всё ещё мутирует») теряется.
    loop_deadline = deadline - _MUTATION_TEARDOWN_BUDGET - 0.05

    last: dict[str, Any] = {}
    try:
        while _now() < loop_deadline:
            data = await _evaluate(session, target_id, _JS_MUTATION_READ)
            if data is None:
                # window обнулился — страница ушла в навигацию. Переустанавливаем.
                await _evaluate(session, target_id, _JS_MUTATION_INSTALL)
                await asyncio.sleep(_POLL_INTERVAL)
                continue
            last = data
            if float(last.get('quiet_ms', 0.0)) >= _MUTATION_QUIET_MS:
                return f'DOM quiet for {last["quiet_ms"]:.0f}ms after {last.get("total", 0)} counted mutation(s)'
            await asyncio.sleep(min(_POLL_INTERVAL, max(0.02, _MUTATION_QUIET_MS / 1000 / 3)))
        raise TimeoutError(
            f'DOM still mutating: {last.get("total", "?")} counted '
            f'({last.get("ignored", "?")} ignored as invisible), last quiet window '
            f'{float(last.get("quiet_ms", 0.0)):.0f}ms, last kind={last.get("last_kind")}'
        )
    finally:
        try:
            await _evaluate(session, target_id, _JS_MUTATION_TEARDOWN, budget=_MUTATION_TEARDOWN_BUDGET)
        except Exception as exc:  # noqa: BLE001 — teardown никогда не должен ломать ожидание
            logger.debug('waiting: mutation observer teardown failed: %r', exc)


# --------------------------------------------------------------------------- #
# Публичное API
# --------------------------------------------------------------------------- #


async def wait_for_page_ready(session: Any, *, timeout: float = 8.0) -> dict:
    """Лестница fail-open: ждём, пока страница перестанет шевелиться.

    Стадии идут по очереди, каждая со своим капом и остатком общего бюджета:

    1. ``loading_indicators`` — исчезновение видимых спиннеров/скелетонов, кап 5 с;
    2. ``network_quiet``      — нет незавершённых ресурсов (денилист аналитики
       из browser-use), кап 3 с;
    3. ``mutation_quiet``     — MutationObserver, окно покоя 300 мс.

    Ни одна стадия не бросает наружу: таймаут или ошибка = ``ok: False`` в
    разбивке и переход к следующей.

    Returns:
        ``{'ready': bool, 'stages': [{'name', 'ok', 'elapsed', 'detail'}], 'elapsed': float}``
        ``ready`` истинно, только если сошлись все стадии.
    """
    started = _now()
    deadline = started + max(0.0, timeout)
    stages: list[dict[str, Any]] = []

    target_id = getattr(session, 'agent_focus_target_id', None)

    def _finish() -> dict:
        return {
            'ready': bool(stages) and all(s['ok'] for s in stages),
            'stages': stages,
            'elapsed': round(_now() - started, 3),
        }

    url = await _current_url(session, target_id)
    if url and not url.lower().startswith(('http://', 'https://')):
        # about:blank, chrome://, devtools:// — ждать нечего, JS туда не заедет.
        for name in ('loading_indicators', 'network_quiet', 'mutation_quiet'):
            stages.append({'name': name, 'ok': True, 'elapsed': 0.0, 'detail': f'skipped: non-http page ({url})'})
        return _finish()

    ladder = (
        ('loading_indicators', _stage_loading_indicators, _CAP_LOADING_INDICATORS),
        ('network_quiet', _stage_network_quiet, _CAP_NETWORK_QUIET),
        ('mutation_quiet', _stage_mutation_quiet, None),
    )

    for name, runner, cap in ladder:
        stage_start = _now()
        remaining = deadline - stage_start
        budget = remaining if cap is None else min(cap, remaining)

        if budget <= 0.05:
            stages.append(
                {'name': name, 'ok': False, 'elapsed': 0.0, 'detail': 'skipped: overall timeout budget exhausted'}
            )
            logger.debug('wait_for_page_ready: stage %s skipped, no budget left', name)
            continue

        stage_deadline = stage_start + budget
        try:
            # Внешний wait_for — жёсткая гарантия капа на случай, если зависнет сам CDP-вызов.
            detail = await asyncio.wait_for(
                runner(session, target_id, stage_deadline),
                timeout=budget + 0.25,
            )
            ok = True
        except (TimeoutError, asyncio.TimeoutError) as exc:
            ok = False
            detail = f'timeout after {budget:.2f}s' + (f': {exc}' if str(exc) else '')
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — fail-open, это весь смысл лестницы
            ok, detail = False, f'{type(exc).__name__}: {exc}'

        elapsed = round(_now() - stage_start, 3)
        stages.append({'name': name, 'ok': ok, 'elapsed': elapsed, 'detail': str(detail)})
        if not ok:
            logger.debug('wait_for_page_ready: stage %s not satisfied in %.2fs — %s', name, elapsed, detail)

    return _finish()


async def wait_after_navigation(session: Any, *, timeout: float = 10.0) -> dict:
    """Дождаться завершения навигации, начавшейся от предыдущего действия (клика).

    В browser-use после клика навигация не ожидается вообще (см. модульный
    docstring), поэтому состояние читается с наполовину уехавшей страницы.

    Механика взята из ``BrowserSession._navigate_and_wait``
    (``browser_use/browser/session.py:1010``, цикл опроса — ``:1090-1113``):
    lifecycle-события копятся в общем per-target буфере SessionManager
    (``session_manager.get_lifecycle_events(target_id)``), и мы ждём в нём
    ``load``/``networkIdle`` с НУЖНЫМ ``loaderId``. Отличие от оригинала: там
    loaderId возвращает ``Page.navigate``, а здесь навигацию инициировали не мы,
    поэтому loaderId снимается с главного фрейма через ``Page.getFrameTree``
    до и после — смена значения и есть новый документ.

    Стадии:
      * ``navigation_start`` — засекли ли вообще новую навигацию;
      * ``lifecycle_load``   — дождались ли ``load``/``networkIdle`` по новому loaderId.

    Returns:
        ``{'ready': bool, 'stages': [...], 'elapsed': float, 'navigated': bool, 'url': str}``
        Если навигации не было — это не ошибка: ``ready: True``, ``navigated: False``.
    """
    started = _now()
    deadline = started + max(0.0, timeout)
    stages: list[dict[str, Any]] = []
    navigated = False

    target_id = getattr(session, 'agent_focus_target_id', None)
    before_loader, _ = await _main_frame_loader_id(session, target_id)
    before_url = await _current_url(session, target_id)

    # Буфер lifecycle-событий: тот же, что читает _navigate_and_wait.
    lifecycle: Any = None
    try:
        if target_id is not None and getattr(session, 'session_manager', None) is not None:
            lifecycle = session.session_manager.get_lifecycle_events(target_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug('wait_after_navigation: lifecycle buffer unavailable: %r', exc)

    # --- стадия 1: началась ли навигация -------------------------------------
    stage_start = _now()
    # Окно на «проснуться»: клик мог ещё не успеть инициировать переход.
    start_window = min(max(1.0, timeout / 4), max(0.0, deadline - stage_start))
    new_loader: str | None = None
    detail = 'no navigation detected'
    try:
        # Случай A: навигация уже успела закоммититься между действием и этим вызовом.
        # Тогда loaderId сменился ДО того, как мы сняли baseline, и сравнивать не с чем.
        # Признак: текущий документ мы уже отслеживаем (в буфере есть его события),
        # но 'load' по нему ещё не приходил — значит загрузка в полёте.
        if (
            before_loader
            and _lifecycle_has(lifecycle, before_loader)
            and not _lifecycle_has(lifecycle, before_loader, {'load', 'networkIdle'})
        ):
            new_loader, navigated = before_loader, True
            detail = f'document {before_loader[:8]} already committed but has not emitted load yet'

        window_deadline = stage_start + start_window
        while not new_loader and _now() < window_deadline:
            loader, url = await _main_frame_loader_id(session, target_id)
            if loader and before_loader and loader != before_loader:
                new_loader, navigated = loader, True
                detail = f'new document: loaderId {before_loader[:8]} -> {loader[:8]}'
                break
            if loader and not before_loader:
                new_loader, navigated = loader, True
                detail = f'new document: loaderId {loader[:8]} (no baseline)'
                break
            current = url or await _current_url(session, target_id)
            if current and before_url and current != before_url:
                # Тот же документ, другой URL — same-document navigation
                # (#fragment / History API). Lifecycle-событий не будет вовсе,
                # ровно как отмечено в session.py:1066-1072. Ждать нечего.
                navigated = True
                detail = f'same-document navigation: {before_url} -> {current}'
                break
            await asyncio.sleep(0.05)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — fail-open
        detail = f'{type(exc).__name__}: {exc}'

    stages.append(
        {'name': 'navigation_start', 'ok': True, 'elapsed': round(_now() - stage_start, 3), 'detail': detail}
    )

    # --- стадия 2: дождаться load по новому loaderId ---------------------------
    stage_start = _now()
    if not new_loader:
        stages.append(
            {
                'name': 'lifecycle_load',
                'ok': True,
                'elapsed': 0.0,
                'detail': 'skipped: no cross-document navigation to wait for',
            }
        )
    elif lifecycle is None:
        stages.append(
            {
                'name': 'lifecycle_load',
                'ok': False,
                'elapsed': 0.0,
                'detail': 'skipped: lifecycle event buffer unavailable',
            }
        )
    else:
        acceptable = {'load', 'networkIdle'}
        seen: list[str] = []
        ok = False
        detail = ''
        try:
            while _now() < deadline:
                for event in list(lifecycle):
                    name = event.get('name')
                    loader_id = event.get('loaderId')
                    marker = f'{name}(loader={loader_id[:8] if loader_id else "none"})'
                    if marker not in seen:
                        seen.append(marker)
                    # События предыдущего документа несут старый loaderId — пропускаем.
                    if loader_id and loader_id != new_loader:
                        continue
                    # Событие без loaderId доверяем только если оно пришло после старта.
                    if not loader_id and event.get('timestamp', 0) < started:
                        continue
                    if name in acceptable:
                        ok = True
                        detail = f'{name} for loaderId {new_loader[:8]}'
                        break
                if ok:
                    break
                await asyncio.sleep(0.05)
            if not ok:
                detail = f'timeout waiting for load/networkIdle; saw: {", ".join(seen[-6:]) or "nothing"}'
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — fail-open
            ok, detail = False, f'{type(exc).__name__}: {exc}'

        stages.append(
            {'name': 'lifecycle_load', 'ok': ok, 'elapsed': round(_now() - stage_start, 3), 'detail': detail}
        )
        if not ok:
            logger.debug('wait_after_navigation: %s', detail)

    return {
        'ready': all(s['ok'] for s in stages),
        'stages': stages,
        'elapsed': round(_now() - started, 3),
        'navigated': navigated,
        'url': await _current_url(session, target_id),
    }


# --------------------------------------------------------------------------- #
# Самопроверка
# --------------------------------------------------------------------------- #

_CHAOS_JS = r"""
(() => {
  if (window.__buChaos) return 'already running';
  const box = document.createElement('div');
  box.id = '__bu_chaos';
  box.style.cssText = 'position:fixed;left:0;top:0;width:240px;height:48px;'
    + 'background:#eee;color:#333;font:12px monospace;z-index:2147483647';
  document.body.appendChild(box);
  window.__buChaos = setInterval(() => {
    box.setAttribute('data-tick', String(Date.now()));      // атрибут на ВИДИМОМ элементе
    const dot = document.createElement('span');
    dot.textContent = '.';
    box.appendChild(dot);
    if (box.children.length > 40) box.textContent = 'tick ' + Date.now();
    fetch('/?bu_chaos=' + Date.now(), { cache: 'no-store' }).catch(() => {});
  }, 100);
  return 'started';
})()
"""


def _print_result(label: str, url: str, result: dict) -> None:
    flag = 'READY' if result['ready'] else 'NOT READY'
    print(f'\n=== {label} ===')
    print(f'  url:     {url}')
    print(f'  verdict: {flag}   total {result["elapsed"]:.2f}s')
    for stage in result['stages']:
        mark = 'ok  ' if stage['ok'] else 'FAIL'
        print(f'    [{mark}] {stage["name"]:<20} {stage["elapsed"]:>6.2f}s  {stage.get("detail", "")}')
    for key in ('navigated', 'url'):
        if key in result and key != 'url':
            print(f'  {key}: {result[key]}')


async def _selfcheck() -> int:
    from browser_use.browser import BrowserSession
    from browser_use.browser.events import NavigateToUrlEvent
    from browser_use.browser.profile import BrowserProfile

    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')

    session = BrowserSession(browser_profile=BrowserProfile(cdp_url='http://127.0.0.1:9222', is_local=True))
    my_tab: str | None = None
    failures: list[str] = []

    await session.start()
    try:
        pre_existing = {t.target_id for t in session.session_manager.get_all_page_targets()}
        print(f'connected; {len(pre_existing)} pre-existing tab(s) — их не трогаем')

        # --- своя вкладка ----------------------------------------------------
        event = session.event_bus.dispatch(NavigateToUrlEvent(url='https://example.com', new_tab=True))
        await event
        my_tab = session.agent_focus_target_id
        if my_tab in pre_existing:
            raise RuntimeError('не удалось открыть свою вкладку — отказываюсь трогать чужие')
        print(f'own tab: {my_tab[:12]}...')

        # --- 1. статика ------------------------------------------------------
        res = await wait_for_page_ready(session)
        _print_result('1/4 статика: https://example.com', await _current_url(session, my_tab), res)
        if not res['ready']:
            failures.append('static page did not converge')
        if res['elapsed'] > 4.0:
            failures.append(f'static page too slow: {res["elapsed"]:.2f}s')

        # --- 2. навигация от клика (ровно тот случай, который browser-use не ждёт) ---
        # Кликаем по ссылке и НЕ ждём ничего — как это делает on_ClickElementEvent.
        clicked = await _evaluate(session, my_tab, "document.querySelector('a').click(), 'clicked'")
        print(f'\nclicked link, nothing awaited: {clicked}')
        nav_res = await wait_after_navigation(session, timeout=10.0)
        _print_result('2/4 wait_after_navigation после клика по ссылке', nav_res['url'], nav_res)
        if not nav_res['navigated']:
            failures.append('wait_after_navigation прозевал навигацию от клика')
        if not nav_res['ready']:
            failures.append('wait_after_navigation не дождался load')
        if 'example.com' in nav_res['url']:
            failures.append(f'клик не увёл со страницы: {nav_res["url"]}')

        # --- 3. тяжёлая SPA --------------------------------------------------
        nav = session.event_bus.dispatch(
            NavigateToUrlEvent(url='https://github.com/browser-use/browser-use', new_tab=False)
        )
        await nav
        res = await wait_for_page_ready(session)
        _print_result('3/4 тяжёлая SPA: github.com/browser-use/browser-use', await _current_url(session, my_tab), res)
        if not res['ready']:
            failures.append(f'SPA did not converge: {[s for s in res["stages"] if not s["ok"]]}')
        if res['elapsed'] > 9.5:
            failures.append(f'SPA overshot the 8s budget: {res["elapsed"]:.2f}s')

        # --- 4. вечная фоновая активность -------------------------------------
        nav = session.event_bus.dispatch(NavigateToUrlEvent(url='https://example.com', new_tab=False))
        await nav
        started = None
        for attempt in range(3):  # CDP-роундтрип сразу после навигации иногда тормозит
            try:
                started = await _evaluate(session, my_tab, _CHAOS_JS, budget=8.0)
                break
            except Exception as exc:  # noqa: BLE001
                print(f'  chaos injection attempt {attempt + 1} failed: {exc!r}')
                await asyncio.sleep(1.0)
        if started is None:
            raise RuntimeError('не смог внедрить бесконечную фоновую активность')
        print(f'\ninjected infinite setInterval+fetch: {started}')
        res = await wait_for_page_ready(session)
        _print_result('4/4 бесконечная фоновая активность (example.com + вечный setInterval/fetch)',
                      await _current_url(session, my_tab), res)
        if res['ready']:
            failures.append('chaos page reported ready — ладдер слишком доверчив')
        if res['elapsed'] > 10.0:
            failures.append(f'chaos page hung instead of timing out honestly: {res["elapsed"]:.2f}s')
        if not (7.0 <= res['elapsed'] <= 10.0):
            failures.append(f'chaos page did not consume the expected ~8s budget: {res["elapsed"]:.2f}s')

    finally:
        if my_tab:
            try:
                await session.close_page(my_tab)
                print(f'\nclosed own tab {my_tab[:12]}...')
            except Exception as exc:  # noqa: BLE001
                print(f'\nWARN: не смог закрыть свою вкладку: {exc!r}')
        try:
            await session.stop()  # отключается, но НЕ убивает чужой Chrome
        except Exception as exc:  # noqa: BLE001
            print(f'WARN: session.stop() failed: {exc!r}')

    print('\n' + '-' * 70)
    if failures:
        for f in failures:
            print(f'SELFCHECK FAILURE: {f}')
        return 1
    print('SELFCHECK PASSED')
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(_selfcheck()))
