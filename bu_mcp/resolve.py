"""Жёсткий резолв индексов элементов поверх browser-use.

Зачем этот модуль вообще нужен
------------------------------
В browser-use «индекс элемента» — это CDP ``backendNodeId``
(``browser_use/dom/serializer/serializer.py``, ``_allocate_selector_index``).
Идентификатор живёт ровно столько, сколько живёт конкретный узел в бэкенде
Blink. Любая перерисовка, которая физически пересоздаёт узел (React
перемонтировал поддерево, ``el.outerHTML = el.outerHTML``, htmx подменил
фрагмент), даёт узлу новый ``backendNodeId``, а старый молча перестаёт
существовать.

Что с этим делает сам browser-use:

* ``BrowserSession.get_dom_element_by_index`` (``browser/session.py``) — это
  просто ``dict.get`` по закешированному selector map. Промах → ``None``.
  Живость узла не проверяется вообще: если карта ещё не пересобиралась,
  вернётся объект давно мёртвого узла.
* ``Tools._click_by_index`` (``tools/service.py``) превращает ``None`` в
  ``ActionResult(extracted_content='... page may have changed ...')`` — то есть
  в УСПЕХ с текстовой отговоркой. Действие не выполнено, но никто не упал.

Здесь это чинится двумя изменениями поведения:

1. **Промах ломает действие.** Как в Playwright MCP, который на мёртвый ref
   отвечает ``Ref e9999 not found in the current page snapshot. Try capturing
   new snapshot.`` — у нас это ``StaleHandleError`` с человекочитаемым
   объяснением и инструкцией.
2. **Перед тем как сдаться — переидентификация.** browser-use уже вычисляет всё
   необходимое и выбрасывает: ``EnhancedDOMTreeNode`` несёт ``backend_node_id``,
   ``target_id``, ``frame_id``, ``session_id`` и вычисляемый ``xpath``, а
   ``DOMInteractedElement`` хранит xpath и accessible name с прямым
   комментарием «used for fallback matching when hash/xpath fail». Лестница
   сопоставления уже написана в ``Agent._update_action_indices`` для
   ``rerun_history`` — но применяется ТОЛЬКО при переигрывании истории, а не в
   обычном действии. Мы переиспользуем ровно её идею.

**Fail closed на неоднозначности.** Если после переидентификации кандидатов
больше одного — ``AmbiguousHandleError``, а не «возьмём первый». Формулировка
Skyvern: элемент, чей единственный отличительный признак нестабилен,
идентичности не имеет и должен падать, а не угадываться. Stagehand короче:
«a wrong cached click is worse than a slow click».

Что лестница ловит, а что нет
-----------------------------
Ступень xpath закрыта двумя гардами, потому что xpath — это структурная
позиция, а не элемент:

* если URL страницы отличается от запомненного, ступень выключается целиком
  (``html/body/div/p[2]/a`` совпадёт на любой странице похожей структуры);
* если у запомненного элемента было accessible name, а у кандидата на той же
  позиции имя другое — это промах, а не находка. Так отсекаются
  виртуализованные списки, где строка уничтожена, а её ``li[1]``/``div[17]``
  достался соседней записи.

Ступень атрибутов игнорирует автогенерируемые ``id``/``name``
(``mui-4821``, ``_ngcontent-*``, ``css-1a2b3c``): они выглядят уникальными и
потому особенно опасны.

Известные ограничения, которые здесь НЕ закрыты:

* **canvas/WebGL-приложения** (Figma, карты, редакторы) — интерактивных
  DOM-узлов нет вовсе, опознавать нечего;
* **closed shadow DOM и web-components** — ``xpath`` в browser-use проходит
  сквозь shadow roots насквозь, поэтому два разных кастомных элемента с
  одинаковой внутренней разметкой дают одинаковый путь;
* **кросс-фреймовые переезды** — ``xpath`` обрывается на границе iframe, а
  ``frame_id`` меняется при перезагрузке фрейма, так что сужение по
  таргету/фрейму перестаёт помогать;
* **списки без текстовых различий** (чекбоксы, «Удалить» в каждой строке) —
  ax name у всех одинаков, и корректным ответом становится
  ``AmbiguousHandleError``: резолв честно отказывается, но не работает.

Публичный API
-------------
``resolve_index(session, index, *, hint=None) -> EnhancedDOMTreeNode``
``describe_handle(session, index) -> dict``
``snapshot_handles(session) -> int``      — запомнить текущий selector map
``last_resolution(session) -> dict | None`` — как разрешился прошлый вызов
"""

from __future__ import annotations

import asyncio
import re
import weakref
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - только для типов
    from browser_use.browser.session import BrowserSession
    from browser_use.dom.views import EnhancedDOMTreeNode

__all__ = [
    'HandleError',
    'StaleHandleError',
    'AmbiguousHandleError',
    'resolve_index',
    'describe_handle',
    'snapshot_handles',
    'last_resolution',
]


class HandleError(Exception):
    """Базовый класс для всех отказов резолва. Ловить можно и его."""


class StaleHandleError(HandleError):
    """Индекс не соответствует ни одному живому элементу на текущей странице.

    Бросается вместо тихого ``None``. Аналог Playwright-овского
    «Ref ... not found in the current page snapshot».
    """


class AmbiguousHandleError(HandleError):
    """Переидентификация нашла больше одного кандидата.

    Сознательно НЕ берём первого: неверный клик по протухшему хендлу хуже,
    чем отсутствие клика.
    """


# Атрибуты, которые считаем достаточно отличительными для последней ступени
# лестницы. Порядок = приоритет.
_IDENTIFYING_ATTRS = (
    'data-testid',
    'data-test-id',
    'data-test',
    'data-qa',
    'id',
    'name',
    'aria-label',
)

# Атрибуты, значения которых фреймворки любят генерировать сами. Для них
# включаем проверку `_looks_generated`: `id="mui-4821"` выглядит уникальным, но
# при следующем рендере станет `mui-4822`, и совпадение по нему — ложное.
# `data-*` и `aria-label` не трогаем: их пишет человек.
_VOLATILE_ATTRS = ('id', 'name')

# Ступени лестницы, в порядке применения.
_LADDER = ('backend_node_id', 'xpath', 'accessible_name', 'attribute')

_GENERATOR_PREFIXES = frozenset(
    {'mui', 'css', 'sc', 'jss', 'emotion', 'chakra', 'radix', 'headlessui',
     'ember', 'ext', 'yui', 'gwt', 'mat', 'cdk', 'rc', 'tw', 'ng'}
)

_GENERATED_VALUE_RE = re.compile(
    r"""(?xi)
    ^ : .* : $                                  # React useId: ":r3:"
    | ^ _ng (?: content | host )                # Angular: "_ngcontent-abc-c12"
    | ^ (?: mui | css | sc | jss | emotion | chakra | radix | headlessui
          | ember | ext | yui | gwt | mat | cdk | rc | tw )
        [-_:]? [a-z]? [0-9a-f]{2,} $            # emotion/MUI/styled-components
    | ^ [0-9a-f]{8,} $                          # голый хеш
    | ^ [0-9a-f]{8} - [0-9a-f]{4} -             # uuid
    | ^ [a-z]{1,6} [-_]? [0-9]{3,} $            # "input-1234", "ext-gen1035"
    """
)


def _looks_generated(value: str) -> bool:
    """Похоже ли значение атрибута на автогенерируемое.

    `filter_dynamic_classes` в browser-use фильтрует классы, но не id — а
    именно id генерируют MUI, Angular, emotion и React `useId`. Совпадение по
    такому значению выглядит уникальным и потому особенно опасно.
    """
    value = (value or '').strip()
    if not value:
        return True
    if _GENERATED_VALUE_RE.search(value):
        return True
    segments = re.split(r'[-_:.]', value)
    # Префикс известного генератора + где-то цифра: "emotion-cache-1x2y".
    if segments[0].lower() in _GENERATOR_PREFIXES and any(c.isdigit() for c in value):
        return True
    for segment in segments:
        # Сегмент из 6+ hex-символов с цифрой: "menu-4f8a91", "form_ab12cd34".
        # Слова вроде "facade" не проходят — в них нет цифр.
        if len(segment) >= 6 and re.fullmatch(r'[0-9a-f]+', segment, re.I) and any(c.isdigit() for c in segment):
            return True
        # Короткий буквенный префикс + длинный числовой хвост: "gen1035".
        if re.fullmatch(r'[a-z]{1,6}[0-9]{3,}', segment, re.I):
            return True
    return False


# --------------------------------------------------------------------------- #
# Реестр известных хендлов (per-session, живёт в памяти процесса)
# --------------------------------------------------------------------------- #

_REGISTRY: dict[int, dict[int, dict[str, Any]]] = {}
_LAST: dict[int, dict[str, Any]] = {}


def _registry(session: 'BrowserSession') -> dict[int, dict[str, Any]]:
    """Словарь ``index -> identity`` для конкретной сессии.

    Ключ — ``id(session)``; запись убирается финализатором, когда сессия
    умирает. ``WeakKeyDictionary`` не подходит: ``BrowserSession`` — pydantic
    модель и нехешируема.
    """
    key = id(session)
    reg = _REGISTRY.get(key)
    if reg is None:
        reg = _REGISTRY[key] = {}
        try:
            weakref.finalize(session, _forget, key)
        except TypeError:  # объект без поддержки weakref — переживём
            pass
    return reg


def _forget(key: int) -> None:
    _REGISTRY.pop(key, None)
    _LAST.pop(key, None)


def _target_url(session: 'BrowserSession', target_id: Any) -> str | None:
    """URL страницы, которой принадлежит узел. Синхронно, из session_manager."""
    if not target_id:
        return None
    try:
        target = session.session_manager.get_target(str(target_id))
    except Exception:
        return None
    return getattr(target, 'url', None) if target is not None else None


def _identity(index: int, node: 'EnhancedDOMTreeNode', session: 'BrowserSession') -> dict[str, Any]:
    """Слепок всего, чем элемент можно опознать заново.

    Ровно тот набор полей, который browser-use уже вычисляет
    (``EnhancedDOMTreeNode.xpath``, ``ax_node.role/name``, ``frame_id``…),
    но нигде не сохраняет для обычного действия. Плюс URL страницы: без него
    xpath нельзя применять, потому что `html/body/div/p[2]/a` совпадёт на любой
    странице похожей структуры.
    """
    ax = getattr(node, 'ax_node', None)
    attrs = dict(node.attributes or {})
    return {
        'index': index,
        'url': _target_url(session, node.target_id),
        'backend_node_id': node.backend_node_id,
        'target_id': str(node.target_id) if node.target_id else None,
        'session_id': str(node.session_id) if node.session_id else None,
        'frame_id': node.frame_id,
        'xpath': node.xpath,
        'tag': node.node_name.lower() if node.node_name else None,
        'role': (ax.role if ax else None),
        'accessible_name': (ax.name if ax else None),
        'attributes': {k: v for k, v in attrs.items() if k in _IDENTIFYING_ATTRS},
    }


def snapshot_handles(session: 'BrowserSession') -> int:
    """Запомнить опознавательные признаки всех элементов текущего selector map.

    Вызывать сразу после сериализации состояния: это единственный момент, когда
    индексы ещё гарантированно соответствуют живым узлам. Возвращает число
    записанных хендлов. Идемпотентно, старые записи не стираются — протухший
    индекс остаётся опознаваемым и после пересборки карты.
    """
    reg = _registry(session)
    selector_map = getattr(session, '_cached_selector_map', None) or {}
    for index, node in selector_map.items():
        try:
            reg[index] = _identity(index, node, session)
        except Exception:  # noqa: BLE001 — сломанный узел не должен ронять снапшот
            continue
    return len(selector_map)


def last_resolution(session: 'BrowserSession') -> dict[str, Any] | None:
    """Как разрешился последний ``resolve_index``: уровень лестницы, новый индекс.

    MCP-слой может показать клиенту, что хендл «переехал», вместо того чтобы
    молча подменить элемент.
    """
    return _LAST.get(id(session))


# --------------------------------------------------------------------------- #
# Проверка живости узла через CDP
# --------------------------------------------------------------------------- #


async def _cdp_for(session: 'BrowserSession', target_id: str | None):
    try:
        return await session.get_or_create_cdp_session(target_id, focus=False)
    except Exception:
        try:
            return await session.get_or_create_cdp_session(None, focus=False)
        except Exception:
            return None


async def _is_reachable(session: 'BrowserSession', node: 'EnhancedDOMTreeNode') -> bool:
    """Жив ли backendNodeId И подключён ли узел к документу.

    Двухступенчато, потому что это два разных вида смерти:

    * ``DOM.resolveNode`` падает — узел уже собран сборщиком мусора Blink,
      backendNodeId освобождён;
    * ``resolveNode`` отдаёт объект, но ``isConnected === false`` — узел ещё жив
      как JS-объект, но выкинут из дерева. Клик по нему ничего не сделает,
      а browser-use этого не замечает.
    """
    cdp = await _cdp_for(session, node.target_id)
    if cdp is None:
        return False
    session_id = node.session_id or getattr(cdp, 'session_id', None)

    object_id = None
    try:
        resolved = await asyncio.wait_for(
            cdp.cdp_client.send.DOM.resolveNode(
                params={'backendNodeId': node.backend_node_id},
                session_id=session_id,
            ),
            timeout=5.0,
        )
        object_id = (resolved or {}).get('object', {}).get('objectId')
    except Exception:
        return False

    if not object_id:
        return False

    try:
        res = await asyncio.wait_for(
            cdp.cdp_client.send.Runtime.callFunctionOn(
                params={
                    'objectId': object_id,
                    'functionDeclaration': 'function() { return !!this.isConnected; }',
                    'returnByValue': True,
                },
                session_id=session_id,
            ),
            timeout=5.0,
        )
        return bool((res or {}).get('result', {}).get('value'))
    except Exception:
        return False
    finally:
        try:
            await cdp.cdp_client.send.Runtime.releaseObject(
                params={'objectId': object_id}, session_id=session_id
            )
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Лестница переидентификации
# --------------------------------------------------------------------------- #


def _prefer(
    candidates: list[tuple[int, 'EnhancedDOMTreeNode']],
    ident: dict[str, Any],
) -> list[tuple[int, 'EnhancedDOMTreeNode']]:
    """Сузить кандидатов до того же таргета, затем до того же фрейма.

    Предпочтение, а не фильтр: если в нужном таргете/фрейме кандидатов нет,
    оставляем что есть — решение о неоднозначности примет вызывающий.
    Та же логика, что в ``Agent._update_action_indices``, где элементы из
    ``historical_element.frame_id`` ставятся в начало списка.
    """
    for key, getter in (
        ('target_id', lambda n: str(n.target_id) if n.target_id else None),
        ('frame_id', lambda n: n.frame_id),
    ):
        want = ident.get(key)
        if not want:
            continue
        narrowed = [c for c in candidates if getter(c[1]) == want]
        if narrowed:
            candidates = narrowed
    return candidates


def _tag_ok(node: 'EnhancedDOMTreeNode', ident: dict[str, Any]) -> bool:
    tag = ident.get('tag')
    if not tag:
        return True
    return (node.node_name or '').lower() == tag


def _candidates_for_level(
    level: str,
    items: list[tuple[int, 'EnhancedDOMTreeNode']],
    ident: dict[str, Any],
    session: 'BrowserSession',
) -> tuple[list[tuple[int, 'EnhancedDOMTreeNode']], str]:
    """Кандидаты одной ступени лестницы + описание того, по чему искали."""
    if level == 'backend_node_id':
        want = ident.get('backend_node_id')
        if want is None:
            return [], ''
        found = [
            (i, n)
            for i, n in items
            if n.backend_node_id == want
            and (
                not ident.get('session_id')
                or str(n.session_id) == ident['session_id']
            )
        ]
        return found, f'backendNodeId={want}'

    if level == 'xpath':
        # Самая опасная ступень: xpath — это структурная позиция, а не элемент.
        # Две защиты, обе fail-closed.
        want = ident.get('xpath')
        if not want:
            return [], ''
        want_url = ident.get('url')
        want_name = (ident.get('accessible_name') or '').strip()
        url_blocked: set[str] = set()
        name_mismatch: list[str] = []
        found = []
        for i, n in items:
            if not _tag_ok(n, ident):
                continue
            try:
                if n.xpath != want:
                    continue
            except Exception:
                continue

            # Защита 1: страница сменилась. Позиция в дереве не переносится
            # между документами — на другой странице похожей структуры тот же
            # xpath указывает на совершенно чужой элемент.
            if want_url:
                now_url = _target_url(session, n.target_id)
                if now_url and now_url != want_url:
                    url_blocked.add(now_url)
                    continue

            # Защита 2: виртуализованные списки. Строка уничтожена, а её
            # позиция (`li[1]`, `div[17]`) досталась соседней записи. Если у
            # запомненного элемента было имя, а у кандидата на той же позиции
            # оно другое — это другой элемент, а не наш.
            if want_name:
                ax = getattr(n, 'ax_node', None)
                got = ((ax.name if ax else None) or '').strip()
                if got != want_name:
                    name_mismatch.append(got)
                    continue

            found.append((i, n))

        what = f'xpath={want}'
        if not found and url_blocked:
            what += (
                f' -> REJECTED: page URL changed ({want_url} -> {sorted(url_blocked)[0]}), '
                f'structural position does not carry across documents'
            )
        elif not found and name_mismatch:
            what += (
                f' -> REJECTED: that position now holds a different element '
                f'(accessible name {name_mismatch[0]!r}, expected {want_name!r}) '
                f'- looks like a virtualized or reordered list'
            )
        return found, what

    if level == 'accessible_name':
        want = ident.get('accessible_name')
        if not want:
            return [], ''
        want_role = ident.get('role')
        found = []
        for i, n in items:
            ax = getattr(n, 'ax_node', None)
            if ax is None or ax.name != want:
                continue
            if not _tag_ok(n, ident):
                continue
            if want_role and ax.role and ax.role != want_role:
                continue
            found.append((i, n))
        role_note = f' role={want_role}' if want_role else ''
        return found, f'accessible name={want!r}{role_note}'

    if level == 'attribute':
        attrs = ident.get('attributes') or {}
        skipped: list[str] = []
        for key in _IDENTIFYING_ATTRS:
            value = attrs.get(key)
            if not value:
                continue
            if key in _VOLATILE_ATTRS and _looks_generated(value):
                skipped.append(f'{key}={value!r} (autogenerated)')
                continue
            found = [
                (i, n)
                for i, n in items
                if _tag_ok(n, ident) and (n.attributes or {}).get(key) == value
            ]
            if found:
                return found, f'{key}={value!r}'
        return [], ('skipped autogenerated ' + ', '.join(skipped)) if skipped else ''

    return [], ''


def _describe_candidate(index: int, node: 'EnhancedDOMTreeNode') -> str:
    ax = getattr(node, 'ax_node', None)
    name = (ax.name if ax else None) or ''
    if len(name) > 40:
        name = name[:37] + '...'
    return f'[{index}] <{(node.node_name or "?").lower()}> {node.xpath} name={name!r}'


async def _refresh(session: 'BrowserSession') -> dict[int, 'EnhancedDOMTreeNode']:
    """Пересобрать состояние и вернуть свежий selector map.

    Скриншот не запрашиваем — он тут не нужен и стоит дорого.
    """
    watchdog = getattr(session, '_dom_watchdog', None)
    if watchdog is not None:
        try:
            watchdog.clear_cache()
        except Exception:
            pass
    try:
        await session.get_browser_state_summary(include_screenshot=False, cached=False)
    except Exception:
        pass
    return getattr(session, '_cached_selector_map', None) or {}


# --------------------------------------------------------------------------- #
# Публичные функции
# --------------------------------------------------------------------------- #


async def resolve_index(
    session: 'BrowserSession',
    index: int,
    *,
    hint: dict[str, Any] | None = None,
) -> 'EnhancedDOMTreeNode':
    """Вернуть живой узел для индекса или упасть.

    Args:
        session: живой ``BrowserSession``.
        index: индекс из сериализованного состояния (он же ``backendNodeId``).
        hint: составной хендл, каким его отдавал ``describe_handle``. Позволяет
            переидентифицировать элемент даже в свежем процессе, где реестр
            пуст. Непустые поля перекрывают запомненные.

    Returns:
        ``EnhancedDOMTreeNode``, про который проверено, что его backendNodeId
        жив и узел подключён к документу.

    Raises:
        StaleHandleError: элемента нет и переидентификация не помогла.
        AmbiguousHandleError: переидентификация дала больше одного кандидата.

    Новый индекс переехавшего элемента — ``session.get_selector_index(node)``;
    подробности разрешения — ``last_resolution(session)``.
    """
    reg = _registry(session)
    # Фиксируем текущую карту до любых действий: после пересборки индексы
    # исчезнут, а признаки для лестницы нужно взять именно из неё.
    snapshot_handles(session)

    ident: dict[str, Any] = dict(reg.get(index) or {})
    if hint:
        ident.update({k: v for k, v in hint.items() if v not in (None, '', {})})
    ident.setdefault('index', index)

    # --- Ступень 0: индекс есть в текущей карте и узел реально жив ----------
    node = await session.get_dom_element_by_index(index)
    if node is not None and await _is_reachable(session, node):
        reg[index] = _identity(index, node, session)
        _LAST[id(session)] = {
            'index': index,
            'resolved_index': index,
            'level': 'exact',
            'reidentified': False,
        }
        return node

    stale_reason = (
        'index is not in the current snapshot'
        if node is None
        else 'the node behind this index was destroyed or detached from the document'
    )

    if not any(ident.get(k) for k in ('xpath', 'accessible_name', 'attributes', 'backend_node_id')):
        raise StaleHandleError(
            f'Element handle [{index}] not found in the current page snapshot '
            f'({stale_reason}), and nothing is known about it to re-identify it. '
            f'Capture a new snapshot (browser state) and use a fresh index.'
        )

    # --- Ступени 1..4: переидентификация по свежему состоянию ---------------
    selector_map = await _refresh(session)
    items = list(selector_map.items())
    tried: list[str] = []

    for level in _LADDER:
        candidates, what = _candidates_for_level(level, items, ident, session)
        if not candidates:
            if what:
                tried.append(f'{level} ({what})')
            continue

        candidates = _prefer(candidates, ident)

        if len(candidates) > 1:
            listing = '; '.join(_describe_candidate(i, n) for i, n in candidates[:5])
            more = '' if len(candidates) <= 5 else f' (+{len(candidates) - 5} more)'
            _LAST[id(session)] = {
                'index': index,
                'resolved_index': None,
                'level': level,
                'reidentified': False,
                'ambiguous': len(candidates),
            }
            raise AmbiguousHandleError(
                f'Element handle [{index}] is stale and cannot be re-identified unambiguously: '
                f'{len(candidates)} candidates matched at the "{level}" level by {what}. '
                f'Candidates: {listing}{more}. '
                f'Refusing to guess — acting on the wrong element is worse than not acting. '
                f'Capture a new snapshot and pick the element explicitly.'
            )

        new_index, found = candidates[0]
        # Даже единственного кандидата проверяем на живость: свежая карта могла
        # устареть между сериализацией и этой проверкой. Мёртвый кандидат не
        # прекращает лестницу — идём на следующую ступень.
        if not await _is_reachable(session, found):
            tried.append(f'{level} ({what}, matched but unreachable)')
            continue

        reg.pop(index, None)
        reg[new_index] = _identity(new_index, found, session)
        _LAST[id(session)] = {
            'index': index,
            'resolved_index': new_index,
            'level': level,
            'matched_by': what,
            'reidentified': True,
        }
        return found

    tried_text = ', '.join(tried) if tried else 'no usable identifying features'
    _LAST[id(session)] = {
        'index': index,
        'resolved_index': None,
        'level': None,
        'reidentified': False,
    }
    raise StaleHandleError(
        f'Element handle [{index}] not found in the current page snapshot '
        f'({stale_reason}). Re-identification failed on every level: {tried_text}. '
        f'The element is gone from the page. Capture a new snapshot (browser state) '
        f'and use a fresh index.'
    )


async def describe_handle(session: 'BrowserSession', index: int) -> dict[str, Any]:
    """Всё, что известно об элементе под этим индексом.

    Нужно, чтобы MCP-слой отдавал клиенту составной хендл, а не голое число:
    возвращённый dict можно потом передать обратно как ``hint`` в
    ``resolve_index``.

    Returns:
        ``backend_node_id``, ``frame_id``, ``xpath``, ``role``,
        ``accessible_name``, ``tag``, ``reachable``, плюс ``index``, ``url``,
        ``target_id``, ``session_id``, ``attributes``.

    Raises:
        StaleHandleError: об этом индексе не известно вообще ничего.
    """
    reg = _registry(session)
    snapshot_handles(session)

    node = await session.get_dom_element_by_index(index)
    if node is not None:
        ident = _identity(index, node, session)
        reg[index] = ident
        reachable = await _is_reachable(session, node)
    else:
        ident = dict(reg.get(index) or {})
        if not ident:
            raise StaleHandleError(
                f'Element handle [{index}] is unknown: it is not in the current page '
                f'snapshot and was never seen in this session. Capture a new snapshot '
                f'(browser state) and use a fresh index.'
            )
        reachable = False

    return {
        'index': index,
        'url': ident.get('url'),
        'backend_node_id': ident.get('backend_node_id'),
        'target_id': ident.get('target_id'),
        'session_id': ident.get('session_id'),
        'frame_id': ident.get('frame_id'),
        'xpath': ident.get('xpath'),
        'role': ident.get('role'),
        'accessible_name': ident.get('accessible_name'),
        'tag': ident.get('tag'),
        'attributes': ident.get('attributes') or {},
        'reachable': reachable,
    }


# --------------------------------------------------------------------------- #
# Самопроверка на живом Chrome
# --------------------------------------------------------------------------- #

if __name__ == '__main__':
    import sys

    from browser_use.browser import BrowserProfile, BrowserSession
    from browser_use.browser.events import CloseTabEvent

    CDP_URL = 'http://127.0.0.1:9222'
    PAGE = 'https://example.com/'

    def head(text: str) -> None:
        print(f'\n{"=" * 72}\n{text}\n{"=" * 72}')

    def ok(text: str) -> None:
        print(f'  PASS  {text}')

    def fail(text: str) -> None:
        print(f'  FAIL  {text}')

    async def evaluate(session, expr: str):
        cdp = await session.get_or_create_cdp_session(session.agent_focus_target_id, focus=False)
        res = await cdp.cdp_client.send.Runtime.evaluate(
            params={'expression': expr, 'returnByValue': True, 'awaitPromise': True},
            session_id=cdp.session_id,
        )
        if 'exceptionDetails' in res:
            raise RuntimeError(f'JS failed: {res["exceptionDetails"]}')
        return res.get('result', {}).get('value')

    async def main() -> int:
        failures = 0
        session = BrowserSession(
            browser_profile=BrowserProfile(cdp_url=CDP_URL, is_local=True)
        )
        await session.start()

        tabs_before = {t.target_id for t in await session.get_tabs()}
        await session.navigate_to(PAGE, new_tab=True)
        my_tab = session.agent_focus_target_id
        assert my_tab not in tabs_before, 'ожидали новую вкладку, а получили чужую'
        print(f'own tab: {my_tab}  url={await session.get_current_page_url()}')

        try:
            # ---------------------------------------------------------------
            head('1. Резолв живого индекса')
            await session.get_browser_state_summary(include_screenshot=False, cached=False)
            selector_map = session._cached_selector_map or {}
            snapshot_handles(session)
            link_index = None
            for i, n in selector_map.items():
                if (n.node_name or '').lower() == 'a':
                    link_index = i
                    break
            print(f'selector map: {len(selector_map)} элементов, индексы {list(selector_map)[:8]}')
            assert link_index is not None, 'на example.com не нашлось <a>'

            node = await resolve_index(session, link_index)
            desc = await describe_handle(session, link_index)
            print(f'  resolve_index({link_index}) -> <{node.node_name.lower()}> xpath={node.xpath}')
            print(f'  describe_handle -> {desc}')
            if node.backend_node_id and desc['reachable'] and desc['xpath']:
                ok(f'живой индекс {link_index} резолвится, describe_handle отдаёт составной хендл')
            else:
                failures += 1
                fail('живой индекс отдал неполные данные')

            # ---------------------------------------------------------------
            head('2. Заведомо несуществующий индекс')
            legacy = await session.get_dom_element_by_index(999999)
            print(f'  browser-use get_dom_element_by_index(999999) -> {legacy!r}   <- тихий промах')
            try:
                await resolve_index(session, 999999)
                failures += 1
                fail('999999 не бросил StaleHandleError')
            except StaleHandleError as exc:
                print(f'  resolve_index(999999) -> StaleHandleError: {exc}')
                ok('несуществующий индекс ломает действие, а не возвращает None')
            except Exception as exc:  # noqa: BLE001
                failures += 1
                fail(f'ожидали StaleHandleError, получили {type(exc).__name__}: {exc}')

            # ---------------------------------------------------------------
            head('3. ГЛАВНЫЙ ТЕСТ: узел пересоздан, backendNodeId протух')
            await session.get_browser_state_summary(include_screenshot=False, cached=False)
            snapshot_handles(session)
            selector_map = session._cached_selector_map or {}
            target_index = None
            for i, n in selector_map.items():
                if (n.node_name or '').lower() == 'a':
                    target_index = i
                    break
            assert target_index is not None
            before = await describe_handle(session, target_index)
            print(f'  до пересоздания: index={target_index} backendNodeId={before["backend_node_id"]} '
                  f'xpath={before["xpath"]} reachable={before["reachable"]}')

            await evaluate(session, "(() => { const el = document.querySelector('a'); el.outerHTML = el.outerHTML; return true; })()")
            print('  выполнено: el.outerHTML = el.outerHTML  (узел физически пересоздан)')

            legacy_node = await session.get_dom_element_by_index(target_index)
            legacy_alive = (
                await _is_reachable(session, legacy_node) if legacy_node is not None else None
            )
            print(f'  browser-use get_dom_element_by_index({target_index}) -> '
                  f'{"None" if legacy_node is None else "узел из кеша"}, живой={legacy_alive}'
                  '   <- вот здесь всё ломается молча')

            resolved = await resolve_index(session, target_index)
            info = last_resolution(session)
            print(f'  resolve_index({target_index}) -> <{resolved.node_name.lower()}> '
                  f'backendNodeId={resolved.backend_node_id}')
            print(f'  last_resolution: {info}')
            if (
                info
                and info.get('reidentified')
                and info.get('level') == 'xpath'
                and resolved.backend_node_id != before['backend_node_id']
            ):
                ok('переидентификация по xpath нашла новый узел с НОВЫМ backendNodeId')
            else:
                failures += 1
                fail(f'ожидали переидентификацию по xpath, получили {info}')

            after = await describe_handle(session, session.get_selector_index(resolved))
            print(f'  новый хендл: {after}')

            # ---------------------------------------------------------------
            head('4. Fail closed: несколько кандидатов -> AmbiguousHandleError')
            await session.get_browser_state_summary(include_screenshot=False, cached=False)
            snapshot_handles(session)
            selector_map = session._cached_selector_map or {}
            amb_index = None
            for i, n in selector_map.items():
                if (n.node_name or '').lower() == 'a':
                    amb_index = i
                    break
            assert amb_index is not None
            amb_before = await describe_handle(session, amb_index)
            print(f'  запомнили index={amb_index} xpath={amb_before["xpath"]} '
                  f'ax_name={amb_before["accessible_name"]!r}')

            # Ломаем структуру (xpath больше не совпадёт) и оставляем ДВА
            # элемента с одинаковым accessible name.
            await evaluate(
                session,
                "(() => { const a = document.querySelector('a'); const html = a.outerHTML; "
                "document.body.innerHTML = '<section><span>'+html+'</span></section>'"
                "+'<section><span>'+html+'</span></section>'; return true; })()",
            )
            print('  body переписан: два одинаковых <a> на новых путях')
            try:
                await resolve_index(session, amb_index)
                failures += 1
                fail('неоднозначность не привела к AmbiguousHandleError')
            except AmbiguousHandleError as exc:
                print(f'  resolve_index({amb_index}) -> AmbiguousHandleError: {exc}')
                ok('на двух кандидатах падаем, а не берём первого')
            except StaleHandleError as exc:
                failures += 1
                fail(f'ожидали Ambiguous, получили Stale: {exc}')

            # ---------------------------------------------------------------
            head('5. Элемент удалён совсем -> StaleHandleError')
            await session.get_browser_state_summary(include_screenshot=False, cached=False)
            snapshot_handles(session)
            selector_map = session._cached_selector_map or {}
            gone_index = next(
                (i for i, n in selector_map.items() if (n.node_name or '').lower() == 'a'), None
            )
            assert gone_index is not None
            await describe_handle(session, gone_index)
            await evaluate(session, "(() => { document.body.innerHTML = '<p>gone</p>'; return true; })()")
            try:
                await resolve_index(session, gone_index)
                failures += 1
                fail('удалённый элемент не дал StaleHandleError')
            except StaleHandleError as exc:
                print(f'  resolve_index({gone_index}) -> StaleHandleError: {exc}')
                ok('удалённый элемент честно падает')
            desc_gone = await describe_handle(session, gone_index)
            if desc_gone['reachable'] is False and desc_gone['xpath']:
                ok(f'describe_handle помнит мёртвый хендл: reachable=False, xpath={desc_gone["xpath"]}')
            else:
                failures += 1
                fail(f'describe_handle на мёртвом хендле: {desc_gone}')

            # ---------------------------------------------------------------
            head('6. Смена URL: ступень xpath отключается целиком')
            await session.navigate_to(PAGE)
            await session.get_browser_state_summary(include_screenshot=False, cached=False)
            snapshot_handles(session)
            selector_map = session._cached_selector_map or {}
            url_index = next(
                (i for i, n in selector_map.items() if (n.node_name or '').lower() == 'a'), None
            )
            assert url_index is not None
            url_before = await describe_handle(session, url_index)
            print(f'  запомнили index={url_index} url={url_before["url"]} xpath={url_before["xpath"]}')

            other = PAGE + '?bu-mcp=other-page'
            await session.navigate_to(other)
            # Структура новой страницы та же самая, меняем только текст ссылки,
            # чтобы совпадения по accessible name тоже не было.
            await evaluate(
                session,
                "(() => { const a = document.querySelector('a'); a.textContent = 'Totally different link'; "
                "a.removeAttribute('aria-label'); return true; })()",
            )
            await session.get_browser_state_summary(include_screenshot=False, cached=False)
            trap = [
                (i, n)
                for i, n in (session._cached_selector_map or {}).items()
                if n.xpath == url_before['xpath']
            ]
            print(f'  новый URL: {await session.get_current_page_url()}')
            print(f'  ловушка: на том же xpath сейчас {len(trap)} элемент(ов): '
                  f'{[_describe_candidate(i, n) for i, n in trap]}')
            print('  без защиты ступень xpath вернула бы вот этот чужой элемент')
            try:
                got = await resolve_index(session, url_index)
                failures += 1
                fail(f'смена URL не остановила xpath: вернулся <{got.node_name.lower()}> {got.xpath}')
            except StaleHandleError as exc:
                print(f'  resolve_index({url_index}) -> StaleHandleError: {exc}')
                if 'page URL changed' in str(exc):
                    ok('после смены URL xpath отвергнут, чужой элемент не вернулся')
                else:
                    failures += 1
                    fail('упало, но не из-за смены URL — проверь причину')
            except AmbiguousHandleError as exc:
                failures += 1
                fail(f'ожидали Stale, получили Ambiguous: {exc}')

            # ---------------------------------------------------------------
            head('7. Виртуализованный список: xpath переехал на соседнюю запись')
            await session.navigate_to(PAGE)
            await evaluate(
                session,
                "(() => { let h = ''; for (let i = 1; i <= 5; i++) { "
                "h += '<li><a href=\"#row\" aria-label=\"Row ' + i + '\">Row ' + i + '</a></li>'; } "
                "document.body.innerHTML = '<ul>' + h + '</ul>'; return true; })()",
            )
            await session.get_browser_state_summary(include_screenshot=False, cached=False)
            snapshot_handles(session)
            rows = {}
            for i, n in (session._cached_selector_map or {}).items():
                ax = getattr(n, 'ax_node', None)
                if ax and ax.name and ax.name.startswith('Row '):
                    rows[ax.name] = i
            print(f'  список: {rows}')
            assert 'Row 1' in rows and 'Row 3' in rows, f'список не собрался: {rows}'
            row1_index, row3_index = rows['Row 1'], rows['Row 3']
            row1 = await describe_handle(session, row1_index)
            row3 = await describe_handle(session, row3_index)
            print(f'  Row 1: index={row1_index} xpath={row1["xpath"]}')
            print(f'  Row 3: index={row3_index} xpath={row3["xpath"]}')

            # Перерисовываем список без первой записи: именно так ведёт себя
            # виртуализация — узлы строк уничтожаются и создаются заново, а
            # позиции li[N] достаются другим записям.
            await evaluate(
                session,
                "(() => { let h = ''; for (let i = 2; i <= 5; i++) { "
                "h += '<li><a href=\"#row\" aria-label=\"Row ' + i + '\">Row ' + i + '</a></li>'; } "
                "document.body.innerHTML = '<ul>' + h + '</ul>'; return true; })()",
            )
            print('  список перерисован без первой записи: узлы пересозданы, позиции сдвинулись')
            await session.get_browser_state_summary(include_screenshot=False, cached=False)
            now_at_row1_xpath = [
                (i, n)
                for i, n in (session._cached_selector_map or {}).items()
                if n.xpath == row1['xpath']
            ]
            print(f'  на xpath Row 1 ({row1["xpath"]}) теперь: '
                  f'{[_describe_candidate(i, n) for i, n in now_at_row1_xpath]}')
            print('  без защиты resolve вернул бы Row 2 вместо удалённой Row 1')

            try:
                got = await resolve_index(session, row1_index)
                ax = getattr(got, 'ax_node', None)
                failures += 1
                fail(f'удалённая Row 1 разрешилась в {(ax.name if ax else None)!r}')
            except StaleHandleError as exc:
                print(f'  resolve_index(Row 1 = {row1_index}) -> StaleHandleError: {exc}')
                if 'virtualized' in str(exc):
                    ok('xpath с несовпавшим именем считается промахом, соседняя запись не подставлена')
                else:
                    failures += 1
                    fail('упало, но не из-за несовпадения имени — проверь причину')

            # Контроль: живая запись, чей xpath тоже переехал, обязана
            # разрешиться — через следующую ступень, а не сломаться.
            got3 = await resolve_index(session, row3_index)
            info3 = last_resolution(session)
            ax3 = getattr(got3, 'ax_node', None)
            print(f'  resolve_index(Row 3 = {row3_index}) -> {(ax3.name if ax3 else None)!r}, '
                  f'{info3}')
            if ax3 and ax3.name == 'Row 3' and info3 and info3.get('level') == 'accessible_name':
                ok('живая Row 3 доехала по accessible name — гард роняет ступень, а не весь резолв')
            else:
                failures += 1
                fail(f'Row 3 разрешилась неверно: {(ax3.name if ax3 else None)!r} {info3}')

        finally:
            head('cleanup')
            try:
                if my_tab:
                    event = session.event_bus.dispatch(CloseTabEvent(target_id=my_tab))
                    await event
                    print(f'  своя вкладка {my_tab} закрыта')
            except Exception as exc:  # noqa: BLE001
                print(f'  не удалось закрыть вкладку: {exc}')
            tabs_after = {t.target_id for t in await session.get_tabs()}
            print(f'  чужих вкладок было {len(tabs_before)}, осталось {len(tabs_after & tabs_before)}')
            await session.stop()  # браузер оставляем жить

        head(f'ИТОГ: {"всё зелено" if failures == 0 else f"{failures} провал(ов)"}')
        return 1 if failures else 0

    sys.exit(asyncio.run(main()))
