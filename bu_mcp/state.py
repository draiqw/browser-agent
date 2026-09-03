"""Compact tree serialization of a live ``BrowserSession`` for MCP clients.

The stock MCP server (``browser_use/mcp/server.py:883``) hands the client a flat
JSON list of interactive elements: index, tag, 100 chars of text, placeholder,
href.  Everything that makes browser-use's own agent prompt usable -- hierarchy,
``aria-*``, ``role``, ``|SHADOW(open)|`` markers, ``*`` for newly appeared
elements, per-container scroll info -- lives in
``SerializedDOMState.llm_representation()`` (``browser_use/dom/views.py:939``)
and never leaves the library.

This module reuses that representation verbatim and adds:

* ``href`` (not in ``DEFAULT_INCLUDE_ATTRIBUTES`` at all, so the compact tree
  would otherwise be less addressable than the flat JSON it replaces), with
  Skyvern-style placeholdering of long URLs -- but only for the URLs where the
  indirection is measurably cheaper than printing the link inline;
* Private Use Area glyphs (icon fonts) collapsed to ``[icon:<code points>]`` --
  one legible token per icon, still one token per distinct glyph;
* attribute-detected custom dropdowns rendered as ``<select>``;
* the ``... N more options...`` marker that upstream slices off every
  ``<select>`` with more than four options (issue #5195) recomputed from
  ``count=`` and put back, pointing at ``dropdown_options`` for the full list;
* explicit "what you are not seeing" markers -- page-level pages above/below
  and a count of interactive elements cut off by the viewport+1000px
  visibility threshold (``browser_use/dom/service.py:64``).
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from browser_use.dom.views import DEFAULT_INCLUDE_ATTRIBUTES

if TYPE_CHECKING:  # pragma: no cover - typing only
	from browser_use.browser import BrowserSession

__all__ = ['serialize_state']

# --------------------------------------------------------------------------- #
# Tuning knobs
# --------------------------------------------------------------------------- #

#: Length of one ``{{_<sha256[:12]>}}`` placeholder: ``{{_`` + 12 hex + ``}}``.
_PLACEHOLDER_LEN = 17

#: Cost of one ``href_map`` entry in the emitted payload *minus* the URL itself:
#: the quoted placeholder, the ``":"``, the separator and whatever indentation
#: the transport adds.  Measured against both shapes ``bu_mcp/server.py`` can
#: emit -- pretty ``json.dumps(..., indent=2)`` costs 31, compact
#: ``separators=(',', ':')`` costs 23 -- and deliberately set to the larger:
#: an entry that pays for itself at 31 also pays for itself at 23, so the rule
#: cannot flip into the net loss it exists to prevent when the envelope format
#: changes underneath it.  The self-check re-derives both numbers from ``json``
#: and fails if either exceeds this constant.
_MAP_ENTRY_OVERHEAD = 31

#: Must mirror ``DomService(viewport_threshold=1000)`` -- everything further than
#: this from the viewport is dropped from the DOM tree before we ever see it.
VIEWPORT_THRESHOLD_PX = 1000

#: Interactive elements are already capped by the DOM service; this only caps
#: how many off-screen ones we bother to count in the page.
_MAX_COUNTED_OFFSCREEN = 500

# Private Use Area: icon fonts (Font Awesome, Material Icons, ...) render as
# these code points and carry zero information for the model.
_PUA_RUN = re.compile('[\ue000-\uf8ff](?:[\ue000-\uf8ff]|\\s(?=[\ue000-\uf8ff]))*')

#: How many code points of one icon run are spelled out before the rest is
#: summarised as ``+Nmore``.  Bounds the worst case of a pathologically long run.
_ICON_MAX_CODEPOINTS = 4

#: ``count=N,options=A|B|C|D`` inside a ``compound_components=(...)`` group.
#: The option list runs to the next ``,`` (a following ``format=``, or the next
#: group) or to the ``)`` that closes the group -- neither can appear inside an
#: option label without the label having been unescaped at the source anyway.
_COMPOUND_OPTIONS = re.compile(r'count=(?P<count>\d+),options=(?P<options>[^,)]*)(?=,|\))')

#: Mirrors the ``first_options[:4]`` slice in
#: ``browser_use/dom/serializer/serializer.py:1074`` -- the number of real option
#: labels the tree can ever show.
_MAX_SHOWN_OPTIONS = 4

#: What goes back where that slice ate the marker.  ~29 characters, once per
#: truncated ``<select>``.
_OPTIONS_MARKER = '|+{n} more via dropdown_options'

# A serialized element line, e.g.
#   \t\t|SHADOW(open)|*|scroll element[12]<div role=button aria-label=Menu /> (0.0 pages above, 1.2 pages below)
_ELEMENT_LINE = re.compile(
	r'^(?P<indent>[\t ]*)'
	r'(?P<prefix>(?:\|SHADOW\((?:open|closed)\)\|)?\*?(?:\|scroll element)?)'
	r'\[(?P<index>\d+)\]'
	r'<(?P<tag>[A-Za-z][\w:.-]*)'
	r'(?P<attrs>.*?)'
	r'\s*/>'
	r'(?P<tail>.*)$'
)

_ROLES_SELECTABLE = {'combobox', 'listbox'}
_HASPOPUP_SELECTABLE = {'listbox', 'menu', 'tree', 'grid'}
_NATIVE_SELECT_TAGS = {'select', 'option', 'optgroup', 'datalist'}

# Class fragments used by the common custom-dropdown widget libraries.
_SELECTABLE_CLASS_HINTS = (
	'select2',
	'selectize',
	'chosen-container',
	'react-select',
	'ant-select',
	'mui-select',
	'muiselect',
	'v-select',
	'ng-select',
	'custom-select',
	'dropdown-toggle',
	'dropdown-trigger',
	'dropdown-select',
	'combobox',
	'listbox',
	'typeahead',
	'autocomplete-input',
)


# --------------------------------------------------------------------------- #
# Token-economy helpers
# --------------------------------------------------------------------------- #


def _href_placeholder(url: str) -> str:
	"""``{{_<first 12 chars of sha256>}}`` -- stable, short, addressable.

	Hashed over the *resolved* URL, so ``/cart``, ``./cart`` and
	``https://site/cart`` collapse onto one placeholder and one map entry.  That
	sharing is the entire reason the indirection can ever pay off.
	"""
	return '{{_' + hashlib.sha256(url.encode('utf-8', 'surrogatepass')).hexdigest()[:12] + '}}'


def _worth_placeholdering(url: str, raw_lengths: list[int]) -> bool:
	"""Is moving ``url`` into ``href_map`` cheaper than printing it inline?

	Indirection cannot compress: the full URL still has to be written down, just
	somewhere else.  The only thing it can do is *share* one copy between several
	occurrences.  So the whole decision is one inequality, in payload characters:

	* inline  = sum of the hrefs as they appear on their element lines;
	* mapped  = ``k`` placeholders in the tree, plus one ``href_map`` entry.

	With ``k`` occurrences of an absolute URL of length ``L`` (and the href
	written the same way on every line) the break-even is
	``k*L > 17*k + L + 31``, i.e. ``L > (17k + 31) / (k - 1)``:

	====  ==========================================
	k     shortest URL for which the map is cheaper
	====  ==========================================
	1     never -- a unique URL is a pure loss
	2     66
	3     42
	4     34
	5     30
	10    23
	====  ==========================================

	The old fixed ``HREF_MAX_LEN = 150`` threshold (inherited from Skyvern)
	answered a different question than the one that matters: how long the URL is,
	never how often it repeats.  On the bench corpus it fired on exactly one site
	-- 28 links on amazon, every one of them unique -- and cost 1,862 characters
	on a pinned DOM snapshot, 2,117 end to end through the MCP server (~10% of
	that whole observation), while missing the repeated 132-char URLs on coursera
	that do pay off.  Length alone cannot tell those two cases apart; ``k`` can.
	"""
	inline = sum(raw_lengths)
	mapped = _PLACEHOLDER_LEN * len(raw_lengths) + len(url) + _MAP_ENTRY_OVERHEAD
	return mapped < inline


def _icon_token(match: re.Match[str]) -> str:
	"""One collapsed icon run, as ``[icon:<hex>[+<hex>...][+Nmore]]``."""
	points = [f'{ord(ch):x}' for ch in match.group(0) if '\ue000' <= ch <= '\uf8ff']
	head = points[:_ICON_MAX_CODEPOINTS]
	rest = len(points) - len(head)
	return f'[icon:{"+".join(head)}{f"+{rest}more" if rest else ""}]'


def collapse_pua(text: str) -> str:
	"""Collapse runs of Private Use Area glyphs (icon fonts) into ``[icon:e001]``.

	Unlike the href map this is a substitution, not an indirection: nothing is
	kept twice, so it cannot be a net loss the way an unshared placeholder is.

	The first version of this emitted a bare ``[icon]`` and threw the code point
	away.  That is free on a page where every icon button also carries an
	``aria-label`` -- which is every page in the 16-site bench corpus, where the
	substitution never fires at all -- and a dead end on the dense unlabelled
	toolbars the corpus does not contain.  There the glyph *is* the only thing
	telling one button from the next, so erasing it made five buttons textually
	identical and left the model nothing to name the one it wanted.  Keeping the
	code point restores that: the token is a pure function of the code points, so
	one glyph gives one string, the same string on every snapshot.

	A run of several glyphs stays **one** token and lists every code point --
	``[icon:e001+e002]``.  Icon fonts stack a base glyph and a modifier into what
	renders as a single icon, so splitting the run would invent elements that are
	not there; keeping only the first code point would re-merge exactly the runs
	the code point is here to separate.  Long runs are capped at
	``_ICON_MAX_CODEPOINTS`` spelled-out code points plus a ``+Nmore`` count,
	which bounds the worst case at the price of colliding two runs that agree on
	their first four code points -- a trade worth making, since the alternative is
	an unbounded token.

	Cost: a one-glyph run goes from 1 character to 11 (``[icon:e001]``) against 6
	for the old bare marker, i.e. 3 bytes to 11 in the UTF-8 the payload is
	actually serialized as.  On the bench corpus both forms measure exactly 0, no
	site there emitting a PUA run at all, so the whole cost lands on the pages the
	corpus is missing -- which are also the only pages that get anything back.
	"""
	if not text:
		return text
	return _PUA_RUN.sub(_icon_token, text)


def restore_option_marker(text: str) -> str:
	"""Put back the ``... N more options...`` marker upstream slices off.

	``_extract_select_options`` (``browser_use/dom/serializer/serializer.py:415``)
	builds ``first_options`` as four option labels **plus a fifth element** -- the
	``... N more options...`` marker.  Its caller then prints
	``first_options[:4]`` (:1074), which cuts off exactly that fifth element and
	nothing else.  The model is handed ``count=6,options=A|B|C|D``: the count is there,
	but nothing on the line says the four labels are not the whole list, so
	picking the best of A..D reads as a complete choice.  Deterministic, fires on
	every ``<select>`` with more than four options (issue #5195).

	The serializer is not ours to patch and does not need to be: both halves of
	the fact survive into the tree.  ``count=N`` against the number of labels
	actually printed is the entire signal, so the marker is recomputed from the
	text.  It names ``dropdown_options`` because that -- not scrolling, not
	guessing -- is where the rest of the list lives in this server.

	Deliberately narrow: it fires only on the exact shape of the upstream bug,
	``count > 4`` with exactly four labels shown.  Anything else is left alone --
	fewer labels means the parse hit a ``,`` or ``)`` inside a label (the option
	list is not escaped at the source, so no parse of it can be exact), more
	labels or a marker already present means upstream stopped truncating and a
	second marker would be a lie.  It only ever inserts; it never deletes text.

	Cost: ~29 characters, once per truncated ``<select>``, and zero on any tree
	that has none.
	"""

	def _sub(m: re.Match[str]) -> str:
		count = int(m.group('count'))
		shown = m.group('options').split('|')
		if len(shown) != _MAX_SHOWN_OPTIONS or count <= _MAX_SHOWN_OPTIONS:
			return m.group(0)
		if any('more options' in s or 'more via dropdown_options' in s for s in shown):
			return m.group(0)
		return m.group(0) + _OPTIONS_MARKER.format(n=count - _MAX_SHOWN_OPTIONS)

	if 'options=' not in text:
		return text
	return _COMPOUND_OPTIONS.sub(_sub, text)


def _rewrite_text(line: str) -> str:
	"""The two text-level fixes, in the order they have to run.

	Marker first, so the literal it inserts can never be mistaken for page text;
	icons second, so an option label that is itself a glyph still collapses.
	"""
	return collapse_pua(restore_option_marker(line))


def _node_attrs(node: Any) -> dict[str, str]:
	attrs = getattr(node, 'attributes', None) or {}
	return {str(k).lower(): str(v) for k, v in attrs.items()}


def _node_role(node: Any) -> str:
	attrs = _node_attrs(node)
	role = attrs.get('role', '')
	if not role:
		ax = getattr(node, 'ax_node', None)
		role = getattr(ax, 'role', '') or ''
	return role.strip().lower()


def looks_selectable(node: Any) -> bool:
	"""True when a non-``<select>`` element behaves like a dropdown.

	Detection is attribute-only (role / aria-haspopup / widget class names), the
	same signal Skyvern's serializer uses to normalize custom dropdowns.

	This one never claims to save characters and cannot backfire the way the href
	map did: rewriting ``<div ...>`` to ``<select ... was=div>`` costs a flat +11
	characters per element whatever the tag is (``6 - len(tag)`` for the new tag
	name plus ``5 + len(tag)`` for ``was=``), with no second copy of anything.
	The bench corpus rewrites at most 5 elements on a page (google_flights: 55
	characters, 0.8% of that observation), and what it buys is that the model can
	tell a dropdown from a div and reach for ``select_dropdown``.  Kept.
	"""
	tag = (getattr(node, 'tag_name', '') or '').lower()
	if tag in _NATIVE_SELECT_TAGS:
		return False

	attrs = _node_attrs(node)

	if _node_role(node) in _ROLES_SELECTABLE:
		return True
	if attrs.get('aria-haspopup', '').strip().lower() in _HASPOPUP_SELECTABLE:
		return True
	# aria-haspopup="true" only counts together with an expandable state.
	if attrs.get('aria-haspopup', '').strip().lower() == 'true' and 'aria-expanded' in attrs:
		return True

	class_attr = attrs.get('class', '').lower()
	if class_attr and any(hint in class_attr for hint in _SELECTABLE_CLASS_HINTS):
		return True

	return False


def _node_href(node: Any) -> str | None:
	href = _node_attrs(node).get('href')
	if not href:
		return None
	href = href.strip()
	if not href or href == '#' or href.lower().startswith('javascript:'):
		return None
	return href


# --------------------------------------------------------------------------- #
# Tree rewriting
# --------------------------------------------------------------------------- #


def _line_href(m: re.Match[str], node: Any, base_url: str) -> tuple[str, str] | None:
	"""The href this serialized line would gain, as ``(as written, resolved)``.

	``None`` when the element has no usable href or when browser-use already put
	one on the line itself.
	"""
	if ' href=' in f' {m.group("attrs").strip()} ':
		return None
	href = _node_href(node)
	if not href:
		return None
	return href, (urljoin(base_url, href) if base_url else href)


def _plan_hrefs(tree: str, selector_map: dict[int, Any], base_url: str) -> set[str]:
	"""Absolute URLs that are cheaper behind a placeholder than written inline.

	One pass to count how often each URL occurs, then ``_worth_placeholdering``
	per URL.  The costs of distinct URLs are additive and the ``href_map`` entry
	overhead is flat (31 characters each, no per-map constant), so deciding each
	URL on its own is exactly the cheapest possible split, not a heuristic.
	"""
	occurrences: dict[str, list[int]] = {}
	for line in tree.split('\n'):
		m = _ELEMENT_LINE.match(line)
		if not m:
			continue
		node = selector_map.get(int(m.group('index')))
		if node is None:
			continue
		pair = _line_href(m, node, base_url)
		if pair:
			occurrences.setdefault(pair[1], []).append(len(pair[0]))
	return {url for url, lengths in occurrences.items() if _worth_placeholdering(url, lengths)}


def _rewrite_tree(tree: str, selector_map: dict[int, Any], base_url: str) -> tuple[str, dict[str, str]]:
	"""Post-process ``llm_representation`` output.

	Adds hrefs (placeholdered only where the map pays for itself), retags
	dropdown-like elements as ``<select>``, collapses icon glyphs and restores the
	truncated-option marker upstream drops.  Returns ``(tree, href_map)``.
	"""
	placeholdered = _plan_hrefs(tree, selector_map, base_url)
	href_map: dict[str, str] = {}
	out: list[str] = []

	for line in tree.split('\n'):
		m = _ELEMENT_LINE.match(line)
		if not m:
			out.append(_rewrite_text(line))
			continue

		index = int(m.group('index'))
		node = selector_map.get(index)
		if node is None:
			out.append(_rewrite_text(line))
			continue

		tag = m.group('tag')
		attrs = m.group('attrs')
		extra: list[str] = []

		# 3) custom dropdown -> <select>
		if looks_selectable(node):
			extra.append(f'was={tag}')
			tag = 'select'

		# 1) href, behind a placeholder only when that is the cheaper of the two
		pair = _line_href(m, node, base_url)
		if pair:
			href, absolute = pair
			if absolute in placeholdered:
				placeholder = _href_placeholder(absolute)
				href_map[placeholder] = absolute
				extra.append(f'href={placeholder}')
			else:
				extra.append(f'href={href}')

		attrs = attrs.rstrip()
		if extra:
			attrs = f'{attrs} {" ".join(extra)}' if attrs else ' ' + ' '.join(extra)

		rebuilt = f'{m.group("indent")}{m.group("prefix")}[{index}]<{tag}{attrs} />{m.group("tail")}'
		out.append(_rewrite_text(rebuilt))

	return '\n'.join(out), href_map


# --------------------------------------------------------------------------- #
# "What the model cannot see" markers
# --------------------------------------------------------------------------- #

_OFFSCREEN_JS = """
(() => {
  const TH = %d;
  const sel = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'summary', 'details',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]',
    '[role=tab]', '[role=menuitem]', '[role=option]', '[role=combobox]',
    '[role=switch]', '[role=textbox]', '[onclick]', '[contenteditable=""]',
    '[contenteditable=true]', '[tabindex]:not([tabindex="-1"])'
  ].join(',');
  const vh = window.innerHeight || document.documentElement.clientHeight || 0;
  let above = 0, below = 0, seen = 0;
  let nodes;
  try { nodes = document.querySelectorAll(sel); } catch (e) { return null; }
  for (const el of nodes) {
    if (seen++ > %d) break;
    let r;
    try { r = el.getBoundingClientRect(); } catch (e) { continue; }
    if (r.width <= 0 && r.height <= 0) continue;
    let cs;
    try { cs = getComputedStyle(el); } catch (e) { continue; }
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const op = parseFloat(cs.opacity);
    if (!isNaN(op) && op <= 0) continue;
    if (r.bottom < -TH) above++;
    else if (r.top > vh + TH) below++;
  }
  return {above: above, below: below};
})()
""" % (VIEWPORT_THRESHOLD_PX, _MAX_COUNTED_OFFSCREEN)


async def _count_offscreen_interactive(session: 'BrowserSession') -> dict[str, int]:
	"""Count interactive elements dropped by the viewport+threshold filter.

	browser-use only emits this hint for iframes
	(``browser_use/dom/serializer/serializer.py:1180``); for the top-level
	document the elements simply vanish.  Fails open with zeros.
	"""
	try:
		cdp_session = await session.get_or_create_cdp_session(target_id=None, focus=False)
		if not cdp_session:
			return {'above': 0, 'below': 0}
		result = await cdp_session.cdp_client.send.Runtime.evaluate(
			params={'expression': _OFFSCREEN_JS, 'returnByValue': True, 'awaitPromise': False},
			session_id=cdp_session.session_id,
		)
		value = (result or {}).get('result', {}).get('value')
		if not isinstance(value, dict):
			return {'above': 0, 'below': 0}
		return {'above': int(value.get('above') or 0), 'below': int(value.get('below') or 0)}
	except Exception:
		return {'above': 0, 'below': 0}


def _scroll_metrics(page_info: Any) -> dict[str, Any]:
	if not page_info:
		return {}
	vh = getattr(page_info, 'viewport_height', 0) or 0
	pixels_above = getattr(page_info, 'pixels_above', 0) or 0
	pixels_below = getattr(page_info, 'pixels_below', 0) or 0
	return {
		'x': getattr(page_info, 'scroll_x', 0),
		'y': getattr(page_info, 'scroll_y', 0),
		'pixels_above': pixels_above,
		'pixels_below': pixels_below,
		'pages_above': round(pixels_above / vh, 1) if vh else 0.0,
		'pages_below': round(pixels_below / vh, 1) if vh else 0.0,
	}


def _frame_tree(body: str, scroll: dict[str, Any], offscreen: dict[str, int]) -> tuple[str, str]:
	"""Build the header/footer visibility markers around the tree body."""
	pages_above = scroll.get('pages_above', 0.0)
	pages_below = scroll.get('pages_below', 0.0)

	header: list[str] = []
	if pages_above > 0:
		header.append(f'... ({pages_above:.1f} pages above, {pages_below:.1f} pages below - scroll up to reveal)')
	else:
		header.append('[Start of page]')
	if offscreen.get('above'):
		header.append(f'... ({offscreen["above"]} more elements above - scroll to reveal)')

	footer: list[str] = []
	if offscreen.get('below'):
		footer.append(f'... ({offscreen["below"]} more elements below - scroll to reveal)')
	if pages_below > 0:
		footer.append(f'... ({pages_above:.1f} pages above, {pages_below:.1f} pages below - scroll down to reveal)')
	else:
		footer.append('[End of page]')

	return '\n'.join(header), '\n'.join(footer)


def _truncate_lines(body: str, budget: int) -> tuple[str, bool]:
	"""Cut ``body`` on a line boundary so it fits into ``budget`` characters."""
	if budget <= 0:
		total_lines = body.count('\n') + 1 if body else 0
		return f'... (truncated: entire tree omitted, {total_lines} lines / {len(body)} chars - raise max_chars)', True
	if len(body) <= budget:
		return body, False

	lines = body.split('\n')
	kept: list[str] = []
	used = 0
	for i, line in enumerate(lines):
		cost = len(line) + (1 if kept else 0)
		if used + cost > budget:
			break
		kept.append(line)
		used += cost
	dropped = lines[len(kept) :]
	dropped_chars = sum(len(x) + 1 for x in dropped)
	note = f'... (truncated: {len(dropped)} more lines / {dropped_chars} more chars omitted - raise max_chars or scroll)'
	if not kept:
		return note, True
	# Make room for the note itself.
	while kept and used + len(note) + 1 > budget:
		removed = kept.pop()
		used -= len(removed) + 1
		dropped_chars += len(removed) + 1
		note = f'... (truncated: {len(lines) - len(kept)} more lines / {dropped_chars} more chars omitted - raise max_chars or scroll)'
	return '\n'.join(kept + [note]), True


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


async def serialize_state(session: 'BrowserSession', *, max_chars: int = 40000) -> dict:
	"""Serialize the current browser state into a compact text tree.

	Args:
		session: a live ``browser_use.browser.BrowserSession``.
		max_chars: hard cap on the size of the ``tree`` string.

	Returns a dict with ``url``, ``title``, ``tabs``, ``viewport``, ``scroll``,
	``tree`` (str), ``truncated`` (bool), plus ``page``, ``href_map`` and
	``elements`` for addressability.
	"""
	state = await session.get_browser_state_summary(include_screenshot=False)
	_focus = getattr(session, 'agent_focus_target_id', None)

	selector_map = dict(state.dom_state.selector_map or {})
	raw_tree = state.dom_state.llm_representation(include_attributes=DEFAULT_INCLUDE_ATTRIBUTES)
	body, href_map = _rewrite_tree(raw_tree, selector_map, state.url or '')

	scroll = _scroll_metrics(state.page_info)
	offscreen = await _count_offscreen_interactive(session)
	header, footer = _frame_tree(body, scroll, offscreen)

	budget = max_chars - len(header) - len(footer) - 2
	body, truncated = _truncate_lines(body, budget)
	tree = f'{header}\n{body}\n{footer}'

	# Drop placeholders whose lines were truncated away.
	if truncated and href_map:
		href_map = {k: v for k, v in href_map.items() if k in tree}

	result: dict[str, Any] = {
		'url': state.url,
		'title': state.title,
		'tabs': [
			{
				'target_id': getattr(tab, 'target_id', None),
				'url': tab.url,
				'title': tab.title,
				# Авторитетный источник — фокус сессии. Сравнение url+title даёт
				# несколько «текущих» вкладок, когда две открыты на одной странице,
				# и клиент, закрывающий «текущую», попадает не в ту.
				'current': (
					getattr(tab, 'target_id', None) == _focus
					if _focus
					else tab.url == state.url and tab.title == state.title
				),
			}
			for tab in (state.tabs or [])
		],
		'viewport': {},
		'page': {},
		'scroll': scroll,
		'tree': tree,
		'truncated': truncated,
		'href_map': href_map,
		'elements': {
			'interactive': len(selector_map),
			'hidden_above': offscreen.get('above', 0),
			'hidden_below': offscreen.get('below', 0),
			'visibility_threshold_px': VIEWPORT_THRESHOLD_PX,
		},
	}

	if state.page_info:
		pi = state.page_info
		result['viewport'] = {'width': pi.viewport_width, 'height': pi.viewport_height}
		result['page'] = {'width': pi.page_width, 'height': pi.page_height}

	return result


# --------------------------------------------------------------------------- #
# Self-check
# --------------------------------------------------------------------------- #


def _legacy_flat_state(state: Any) -> str:
	"""Verbatim copy of ``MCPServer._get_browser_state`` (server.py:883) payload.

	Kept here only so the self-check can measure the size difference; it is not
	part of the public API.
	"""
	import json

	result: dict[str, Any] = {
		'url': state.url,
		'title': state.title,
		'tabs': [{'url': tab.url, 'title': tab.title} for tab in state.tabs],
		'interactive_elements': [],
	}
	if state.page_info:
		pi = state.page_info
		result['viewport'] = {'width': pi.viewport_width, 'height': pi.viewport_height}
		result['page'] = {'width': pi.page_width, 'height': pi.page_height}
		result['scroll'] = {'x': pi.scroll_x, 'y': pi.scroll_y}
	for index, element in state.dom_state.selector_map.items():
		elem_info: dict[str, Any] = {
			'index': index,
			'tag': element.tag_name,
			'text': element.get_all_children_text(max_depth=2)[:100],
		}
		if element.attributes.get('placeholder'):
			elem_info['placeholder'] = element.attributes['placeholder']
		if element.attributes.get('href'):
			elem_info['href'] = element.attributes['href']
		result['interactive_elements'].append(elem_info)
	return json.dumps(result, indent=2)


async def _selfcheck() -> int:
	import asyncio
	import json

	from browser_use.browser import BrowserSession
	from browser_use.browser.events import CloseTabEvent, NavigateToUrlEvent
	from browser_use.browser.profile import BrowserProfile

	urls = ['https://example.com', 'https://github.com/browser-use/browser-use']

	session = BrowserSession(browser_profile=BrowserProfile(cdp_url='http://127.0.0.1:9222', is_local=True))
	await session.start()

	before = {t.target_id for t in await session.get_tabs()}
	ours: set[str] = set()
	failures: list[str] = []
	totals = {'old': 0, 'new': 0}

	try:
		for url in urls:
			await session.event_bus.dispatch(NavigateToUrlEvent(url=url, new_tab=True))
			await asyncio.sleep(1.5)
			ours |= {t.target_id for t in await session.get_tabs()} - before

			state = await session.get_browser_state_summary(include_screenshot=False)
			old = _legacy_flat_state(state)
			new = await serialize_state(session)

			print('=' * 78)
			print(f'URL: {new["url"]}   title: {new["title"]!r}')
			print(f'viewport={new["viewport"]} page={new["page"]} scroll={new["scroll"]}')
			print(f'elements={new["elements"]}  truncated={new["truncated"]}  href_map={len(new["href_map"])}')
			print('-' * 78)
			print(new['tree'])
			if new['href_map']:
				print('-' * 78)
				print('href_map:')
				for k, v in list(new['href_map'].items())[:10]:
					print(f'  {k} -> {v}')
			print('-' * 78)

			old_len, new_len = len(old), len(new['tree'])
			totals['old'] += old_len
			totals['new'] += new_len
			delta = old_len - new_len
			pct = (delta / old_len * 100) if old_len else 0.0
			print(f'flat JSON (server.py:883): {old_len:>7} chars  (~{old_len // 4} tokens)')
			print(f'compact tree             : {new_len:>7} chars  (~{new_len // 4} tokens)')
			print(f'saved                    : {delta:>7} chars  ({pct:+.1f}%,  ~{delta // 4} tokens)')

			# --- assertions -------------------------------------------------
			for key in ('url', 'title', 'tabs', 'viewport', 'scroll', 'tree', 'truncated'):
				if key not in new:
					failures.append(f'{url}: missing key {key}')
			if not isinstance(new['tree'], str) or not new['tree'].strip():
				failures.append(f'{url}: empty tree')
			if new['tree'].lstrip().startswith('{'):
				failures.append(f'{url}: tree looks like JSON, not a tree')
			head = new['tree'].split('\n', 1)[0]
			tail = new['tree'].rsplit('\n', 1)[-1]
			if not (head.startswith('[Start of page]') or 'pages above' in head):
				failures.append(f'{url}: no visibility marker at top: {head!r}')
			if not (tail.startswith('[End of page]') or 'pages below' in tail or 'more elements' in tail):
				failures.append(f'{url}: no visibility marker at bottom: {tail!r}')
			if new['elements']['interactive'] == 0:
				failures.append(f'{url}: zero interactive elements')
			if '\t' not in new['tree'] and '  ' not in new['tree']:
				failures.append(f'{url}: tree has no indentation (not hierarchical)')
			for placeholder, full in new['href_map'].items():
				k = new['tree'].count(placeholder)
				if not k:
					failures.append(f'{url}: href_map placeholder {placeholder} not in tree')
				if not full.startswith(('http', '/')):
					failures.append(f'{url}: href_map value not a URL: {full}')
				# Every entry must be cheaper than the links it replaced. The hrefs as
				# written are never longer than the resolved URL, so k*len(full) is an
				# upper bound on what inlining would have cost: if the map does not beat
				# that, it definitely does not beat the real thing.
				if _PLACEHOLDER_LEN * k + len(full) + _MAP_ENTRY_OVERHEAD >= k * len(full):
					failures.append(f'{url}: href_map entry does not pay for itself (x{k}, {len(full)} chars): {full}')

			# truncation behaviour: ask for half of what the page actually produces
			cap = max(200, new_len // 2)
			small = await serialize_state(session, max_chars=cap)
			if not small['truncated']:
				failures.append(f'{url}: max_chars={cap} did not set truncated')
			if len(small['tree']) > cap:
				failures.append(f'{url}: max_chars={cap} overflow ({len(small["tree"])} chars)')
			if 'truncated:' not in small['tree']:
				failures.append(f'{url}: truncation note missing')
			if small['tree'].rsplit('\n', 1)[-1] not in new['tree'].rsplit('\n', 1)[-1] and not small['tree'].endswith(
				new['tree'].rsplit('\n', 1)[-1]
			):
				failures.append(f'{url}: truncated tree lost its bottom visibility marker')
			print(f'truncation @{cap}: len={len(small["tree"])} truncated={small["truncated"]}')

		# --- synthetic pages -------------------------------------------------
		# Both fixes target shapes the 16-site bench corpus does not contain
		# (unlabelled icon fonts, <select> with more than four options), so the
		# only place they can be exercised is a page we build ourselves.
		async def _evaluate(expr: str) -> Any:
			cdp = await session.get_or_create_cdp_session(session.agent_focus_target_id, focus=False)
			res = await cdp.cdp_client.send.Runtime.evaluate(
				params={'expression': expr, 'returnByValue': True, 'awaitPromise': True},
				session_id=cdp.session_id,
			)
			if 'exceptionDetails' in res:
				raise RuntimeError(f'JS failed: {res["exceptionDetails"]}')
			return res.get('result', {}).get('value')

		await session.event_bus.dispatch(NavigateToUrlEvent(url='https://example.com', new_tab=True))
		await asyncio.sleep(1.5)
		ours |= {t.target_id for t in await session.get_tabs()} - before

		print('=' * 78)
		print('SYNTHETIC 1: toolbar of five icon buttons whose only label is a')
		print('             Private Use Area glyph -- nothing else tells them apart.')
		# The glyph goes in aria-label rather than in the button text on purpose:
		# upstream drops any text node shorter than two characters
		# (browser_use/dom/serializer/serializer.py:555), so a lone glyph in the
		# element body never reaches the tree at all.  Attributes are where PUA
		# actually shows up -- aria-label, title, placeholder, value, alt.
		await _evaluate(
			"(() => { let h = ''; for (let i = 1; i <= 5; i++) { "
			"h += '<button aria-label=\"' + String.fromCharCode(0xe000 + i) + '\"></button>'; } "
			'document.body.innerHTML = h; return true; })()'
		)
		icons = await serialize_state(session)
		print('-' * 78)
		print(icons['tree'])
		print('-' * 78)
		tokens = re.findall(r'\[icon:[^\]]+\]', icons['tree'])
		btn_lines = [ln.strip() for ln in icons['tree'].split('\n') if '[icon:' in ln]
		# Strip the element index: what is left is what the model has to name the
		# button by.  Under the old bare [icon] all five collapse onto one string.
		bare = {re.sub(r'\[\d+\]', '[N]', re.sub(r'\[icon:[^\]]+\]', '[icon]', ln)) for ln in btn_lines}
		kept = {re.sub(r'\[\d+\]', '[N]', ln) for ln in btn_lines}
		print(f'icon tokens          : {tokens}')
		print(f'distinct lines, [icon]      : {len(bare)}  (old form -- all five buttons identical)')
		print(f'distinct lines, [icon:<cp>] : {len(kept)}  (new form)')
		if sorted(set(tokens)) != [f'[icon:e00{i}]' for i in range(1, 6)]:
			failures.append(f'icons: expected e001..e005, got {sorted(set(tokens))}')
		if len(kept) != 5:
			failures.append(f'icons: {len(kept)} distinct button lines, expected 5')
		if len(bare) != 1:
			failures.append(f'icons: old form should collapse to 1 line, got {len(bare)}')
		if any('\ue000' <= ch <= '\uf8ff' for ch in icons['tree']):
			failures.append('icons: raw PUA glyph left in the tree')
		icon_cost = len(icons['tree']) - len(re.sub(r'\[icon:[^\]]+\]', '[icon]', icons['tree']))
		print(f'cost of keeping the code points: {icon_cost:+d} chars over 5 icons')

		print('=' * 78)
		print('SYNTHETIC 2: <select> with six options -- upstream prints four and')
		print('             slices off its own "... N more options..." marker (#5195)')
		await _evaluate(
			"(() => { const labels = ['Alpha','Bravo','Charlie','Delta','Echo','Foxtrot']; "
			"document.body.innerHTML = '<select>' + labels.map(t => '<option>' + t + '</option>').join('') "
			"+ '</select>'; return true; })()"
		)
		sel = await serialize_state(session)
		print('-' * 78)
		print(sel['tree'])
		print('-' * 78)
		sel_line = next((ln.strip() for ln in sel['tree'].split('\n') if 'count=' in ln and 'options=' in ln), '')
		upstream_line = sel_line.replace(_OPTIONS_MARKER.format(n=2), '')
		print(f'upstream : {upstream_line}')
		print(f'ours     : {sel_line}')
		print(f'cost     : {len(sel_line) - len(upstream_line):+d} chars')
		if not sel_line:
			failures.append('select: no compound_components line in the tree')
		elif 'count=6' not in sel_line:
			failures.append(f'select: expected count=6, got {sel_line!r}')
		elif '+2 more via dropdown_options' not in sel_line:
			failures.append(f'select: marker not restored: {sel_line!r}')
	finally:
		for target_id in ours:
			try:
				await session.event_bus.dispatch(CloseTabEvent(target_id=target_id))
			except Exception as exc:  # pragma: no cover
				print(f'warning: could not close own tab {target_id[-4:]}: {exc}')
		await asyncio.sleep(1.0)
		left = {t.target_id for t in await session.get_tabs()}
		leaked = ours & left
		if leaked:
			print(f'warning: tabs left open: {sorted(leaked)}')
		if before - left:
			print(f'WARNING: pre-existing tabs disappeared: {sorted(before - left)}')
		await session.stop()

	print('=' * 78)
	old_t, new_t = totals['old'], totals['new']
	print(
		f'TOTAL flat={old_t} chars (~{old_t // 4} tok)  tree={new_t} chars (~{new_t // 4} tok)  '
		f'saved={old_t - new_t} chars ({(old_t - new_t) / old_t * 100:+.1f}%)'
		if old_t
		else 'TOTAL: nothing measured'
	)

	# Offline unit checks for the token-economy tricks.
	# Icons: one token per run, but the code points survive, so two buttons that
	# differ only by glyph stay two different strings.
	assert collapse_pua('Menu \ue000\ue001 open') == 'Menu [icon:e000+e001] open', collapse_pua('Menu \ue000\ue001 open')
	assert collapse_pua('\ue001') == '[icon:e001]' and collapse_pua('\ue002') == '[icon:e002]'
	assert collapse_pua('\ue001') != collapse_pua('\ue002'), 'glyphs must stay distinguishable'
	assert collapse_pua('\ue001') == collapse_pua('\ue001'), 'same glyph must give the same token'
	assert collapse_pua('a\ue001b\ue002') == 'a[icon:e001]b[icon:e002]'
	# Long runs are capped, not unbounded.
	assert collapse_pua('\ue000\ue001\ue002\ue003\ue004\ue005') == '[icon:e000+e001+e002+e003+2more]', collapse_pua(
		'\ue000\ue001\ue002\ue003\ue004\ue005'
	)
	assert collapse_pua('plain text') == 'plain text'
	assert collapse_pua('') == ''

	# <select>: the marker upstream's first_options[:4] slice eats (issue #5195).
	_sel = '[5]<select compound_components=(role=listbox,name=Options,count=6,options=A|B|C|D) />'
	assert restore_option_marker(_sel).endswith('options=A|B|C|D|+2 more via dropdown_options) />'), restore_option_marker(_sel)
	# ...still restored when a format hint follows the option list.
	_fmt = 'count=7,options=A|B|C|D,format=numeric)'
	assert (
		restore_option_marker(_fmt) == 'count=7,options=A|B|C|D|+3 more via dropdown_options,format=numeric)'
	), restore_option_marker(_fmt)
	# Nothing missing -> nothing added.
	assert restore_option_marker('count=4,options=A|B|C|D)') == 'count=4,options=A|B|C|D)'
	assert restore_option_marker('count=3,options=A|B|C)') == 'count=3,options=A|B|C)'
	# Upstream marker already present -> no second one.
	_already = 'count=9,options=A|B|C|... 5 more options...)'
	assert restore_option_marker(_already) == _already, restore_option_marker(_already)
	assert restore_option_marker(restore_option_marker(_sel)) == restore_option_marker(_sel), 'not idempotent'
	# Not a select line at all -> untouched.
	assert restore_option_marker('[1]<a href=/x />') == '[1]<a href=/x />'
	long_url = 'https://x.test/' + 'a' * 200
	ph = _href_placeholder(long_url)
	assert re.fullmatch(r'\{\{_[0-9a-f]{12}\}\}', ph), ph
	assert len(ph) == _PLACEHOLDER_LEN, ph

	# The cost model has to bound the serializer it is modelling, in both the
	# pretty and the compact shape server.py can emit the envelope in.
	_probe = 'https://x.test/probe'
	_costs = {}
	for _name, _kw in (('pretty', {'indent': 2}), ('compact', {'separators': (',', ':')})):
		_empty = len(json.dumps({'href_map': {}}, ensure_ascii=False, **_kw))
		_one = len(json.dumps({'href_map': {_href_placeholder(_probe): _probe}}, ensure_ascii=False, **_kw))
		_costs[_name] = _one - _empty - len(_probe)
		assert _costs[_name] <= _MAP_ENTRY_OVERHEAD, (_name, _costs[_name])
	assert _costs['pretty'] == _MAP_ENTRY_OVERHEAD, _costs

	# Break-even, straight off the inequality in _worth_placeholdering.
	assert not _worth_placeholdering(long_url, [len(long_url)]), 'unique URL must stay inline'
	assert _worth_placeholdering('h' * 66, [66, 66]), 'x2 at 66 chars must be mapped'
	assert not _worth_placeholdering('h' * 65, [65, 65]), 'x2 at 65 chars must stay inline'
	assert _worth_placeholdering('h' * 42, [42] * 3) and not _worth_placeholdering('h' * 41, [41] * 3)

	class _FakeNode:
		def __init__(self, tag, attrs):
			self.tag_name = tag
			self.attributes = attrs
			self.ax_node = None

	assert looks_selectable(_FakeNode('div', {'role': 'combobox'}))
	assert looks_selectable(_FakeNode('div', {'aria-haspopup': 'listbox'}))
	assert looks_selectable(_FakeNode('span', {'class': 'form-control select2-selection'}))
	assert not looks_selectable(_FakeNode('div', {'class': 'container'}))
	assert not looks_selectable(_FakeNode('select', {'role': 'combobox'}))
	# End-to-end check of the rewrite pass on a synthetic serializer line.
	repeated = 'https://x.test/' + 'b' * 90  # 105 chars, twice -> above the k=2 break-even
	ph_rep = _href_placeholder(repeated)
	fake_map = {
		7: _FakeNode('a', {'href': long_url}),
		8: _FakeNode('div', {'role': 'combobox', 'aria-expanded': 'false'}),
		9: _FakeNode('a', {'href': '/short'}),
		10: _FakeNode('a', {'href': repeated}),
		11: _FakeNode('a', {'href': '/' + 'b' * 90}),  # same link, written relative
	}
	fake_tree = (
		'\t*[7]<a aria-label=Docs />\n'
		'\t|scroll element[8]<div aria-expanded=false /> (0.0 pages above, 1.0 pages below)\n'
		'\t\t[9]<a />\n'
		'\t\t[10]<a />\n'
		'\t\t[11]<a />'
	)
	rewritten, fmap = _rewrite_tree(fake_tree, fake_map, 'https://x.test/page')
	assert '[8]<select' in rewritten and 'was=div' in rewritten, rewritten
	assert '(0.0 pages above, 1.0 pages below)' in rewritten, rewritten
	assert '|scroll element[8]' in rewritten and '*[7]' in rewritten, rewritten
	assert 'href=/short' in rewritten, rewritten
	# A unique URL is never worth a map entry, however long it is: the map would
	# hold a second full copy of it and the tree would still pay 17 for the stub.
	assert ph not in rewritten and ph not in fmap, 'unique long href was placeholdered'
	assert f'href={long_url}' in rewritten, rewritten
	# A repeated one is, and both spellings of it share a single entry.
	assert rewritten.count(f'href={ph_rep}') == 2, rewritten
	assert fmap == {ph_rep: repeated}, fmap
	assert repeated not in rewritten.replace(ph_rep, ''), 'repeated href leaked into tree'
	print('unit checks: ok (pua collapse, href break-even, selectable detection, tree rewrite)')

	if failures:
		print('\nSELF-CHECK FAILED:')
		for f in failures:
			print(f'  - {f}')
		return 1
	print('\nSELF-CHECK PASSED on both pages.')
	return 0


if __name__ == '__main__':
	import asyncio
	import sys

	sys.exit(asyncio.run(_selfcheck()))
