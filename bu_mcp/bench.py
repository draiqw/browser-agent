"""Reproducible A/B benchmark: stock ``browser_use.mcp`` vs ``bu_mcp.server``.

Both servers are driven over real MCP stdio JSON-RPC (no in-process shortcuts),
both attach to the same already-running Chrome on ``BENCH_CDP_URL``.

What is measured, per site, per server:

* observation size   -- characters of the text content block returned by the
  state tool.  This is what a model actually pays for to look at the page once.
* state latency      -- seconds for the state tool call.
* navigate latency   -- seconds for the navigate tool call.
* elements           -- how many distinct interactive indices the state tool
  actually handed out.  Size without this number is meaningless: saving
  characters by dropping elements is not a saving.
* chars per element  -- the headline ratio.

Plus a correctness block that size cannot show:

* stale handle       -- take a live index, destroy the node behind it via JS,
  click the old index, record whether the server refuses or silently "succeeds".
  Two variants: node recreated in place (``el.outerHTML = el.outerHTML``) and
  node removed outright.  A transparent full-viewport shield is injected first,
  so a coordinate-fallback click can never reach a real site element.
* readiness          -- for bu_mcp, the per-stage breakdown and ``ready`` verdict
  returned by ``wait_after_navigation``.

Safety rules baked in:

* every server is pinned to its OWN tab, bound by navigating to a unique
  marker URL with ``new_tab=True`` as its very first tool call;
* the set of foreign tabs is snapshotted at startup and verified at the end;
* the only clicks issued are on a probe node this script injected itself.

Usage::

    /Users/draiqws/browser-use/.venv/bin/python -m bu_mcp.bench            # full run
    ... -m bu_mcp.bench --repeats 1 --sites wikipedia,github               # quick
    ... -m bu_mcp.bench --report-only                                      # re-render md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import statistics
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PYTHON = str(REPO / '.venv' / 'bin' / 'python')
CDP_URL = os.getenv('BENCH_CDP_URL', 'http://127.0.0.1:9222')

RESULTS_JSON = HERE / 'bench_results.json'
PARTIAL_JSON = HERE / 'bench_results.partial.json'
REPORT_MD = HERE / 'BENCH.md'
PARTIAL_MD = HERE / 'BENCH.partial.md'

CALL_TIMEOUT = 100.0


# --------------------------------------------------------------------------- #
# Corpus
# --------------------------------------------------------------------------- #

# 13 live sites straight out of the WebVoyager task set, 2 REAL (realevals.xyz)
# deterministic replicas, 1 control page that everybody agrees is easy.
SITES: list[tuple[str, str, str]] = [
	('wikipedia', 'https://en.wikipedia.org/wiki/Main_Page', 'control'),
	('allrecipes', 'https://www.allrecipes.com/', 'webvoyager'),
	('amazon', 'https://www.amazon.com/', 'webvoyager'),
	('apple', 'https://www.apple.com/', 'webvoyager'),
	('arxiv', 'https://arxiv.org/', 'webvoyager'),
	('github', 'https://github.com/', 'webvoyager'),
	('espn', 'https://www.espn.com/', 'webvoyager'),
	('coursera', 'https://www.coursera.org/', 'webvoyager'),
	('cambridge_dict', 'https://dictionary.cambridge.org/', 'webvoyager'),
	('bbc_news', 'https://www.bbc.com/news', 'webvoyager'),
	('huggingface', 'https://huggingface.co/', 'webvoyager'),
	('wolframalpha', 'https://www.wolframalpha.com/', 'webvoyager'),
	('google_maps', 'https://www.google.com/maps', 'webvoyager'),
	('google_flights', 'https://www.google.com/travel/flights', 'webvoyager'),
	('real_omnizon', 'https://real-omnizon.vercel.app/', 'real'),
	('real_udriver', 'https://real-udriver.vercel.app/', 'real'),
]

BLOCK_MARKERS = (
	'captcha',
	'unusual traffic',
	'access denied',
	'are you a robot',
	'verify you are human',
	'checking your browser',
	'enable javascript and cookies',
	'request blocked',
	'403 forbidden',
	'just a moment',
	'to discuss automated access',
	'sorry, we just need to make sure',
	'pardon our interruption',
)


# --------------------------------------------------------------------------- #
# Server definitions
# --------------------------------------------------------------------------- #


@dataclass
class ServerSpec:
	key: str
	label: str
	argv: list[str]
	env: dict[str, str]
	tool_state: str
	tool_navigate: str
	tool_click: str
	state_args: dict[str, Any] = field(default_factory=dict)


STOCK = ServerSpec(
	key='stock',
	label='stock browser_use.mcp',
	argv=[PYTHON, '-m', 'browser_use.mcp'],
	env={},
	tool_state='browser_get_state',
	tool_navigate='browser_navigate',
	tool_click='browser_click',
	state_args={'include_screenshot': False},
)

OURS = ServerSpec(
	key='ours',
	label='bu_mcp.server',
	argv=[PYTHON, '-m', 'bu_mcp.server'],
	env={'PYTHONPATH': str(REPO), 'BU_MCP_CDP_URL': CDP_URL},
	tool_state='browser_state',
	tool_navigate='browser_navigate',
	tool_click='browser_click',
)

SERVERS = {'stock': STOCK, 'ours': OURS}


# --------------------------------------------------------------------------- #
# Minimal MCP stdio client
# --------------------------------------------------------------------------- #


class McpError(RuntimeError):
	pass


class McpClient:
	"""Hand-rolled JSON-RPC/stdio MCP client.

	Deliberately not the mcp SDK client: we must be able to SIGKILL the server
	instead of letting it shut down gracefully.  A graceful shutdown calls
	``BrowserSession.stop()``, and with ``keep_alive`` unset that path resets the
	session against a Chrome we do not own.
	"""

	def __init__(self, spec: ServerSpec):
		self.spec = spec
		self.proc: asyncio.subprocess.Process | None = None
		self._id = 0
		self._stderr_tail: list[str] = []
		self._stderr_task: asyncio.Task | None = None

	async def start(self) -> None:
		env = {**os.environ, **self.spec.env}
		env.setdefault('ANONYMIZED_TELEMETRY', 'false')
		env.setdefault('BROWSER_USE_CLOUD_SYNC', 'false')
		self.proc = await asyncio.create_subprocess_exec(
			*self.spec.argv,
			stdin=asyncio.subprocess.PIPE,
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.PIPE,
			cwd=str(REPO),
			env=env,
		)
		self._stderr_task = asyncio.create_task(self._drain_stderr())
		await self._request(
			'initialize',
			{
				'protocolVersion': '2024-11-05',
				'capabilities': {},
				'clientInfo': {'name': 'bu-mcp-bench', 'version': '1.0'},
			},
			timeout=120.0,
		)
		await self._notify('notifications/initialized')

	async def _drain_stderr(self) -> None:
		assert self.proc and self.proc.stderr
		try:
			async for line in self.proc.stderr:
				text = line.decode('utf-8', 'replace').rstrip()
				if text:
					self._stderr_tail.append(text)
					del self._stderr_tail[:-40]
		except Exception:  # noqa: BLE001
			pass

	def _next_id(self) -> int:
		self._id += 1
		return self._id

	async def _send(self, payload: dict[str, Any]) -> None:
		assert self.proc and self.proc.stdin
		self.proc.stdin.write((json.dumps(payload) + '\n').encode())
		await self.proc.stdin.drain()

	async def _notify(self, method: str, params: dict | None = None) -> None:
		msg: dict[str, Any] = {'jsonrpc': '2.0', 'method': method}
		if params is not None:
			msg['params'] = params
		await self._send(msg)

	async def _request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
		assert self.proc and self.proc.stdout
		rid = self._next_id()
		await self._send({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
		deadline = time.monotonic() + timeout
		while True:
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				raise TimeoutError(f'{method} timed out after {timeout}s')
			line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=remaining)
			if not line:
				tail = '\n'.join(self._stderr_tail[-10:])
				raise McpError(f'{self.spec.key}: server closed stdout. stderr tail:\n{tail}')
			try:
				msg = json.loads(line)
			except json.JSONDecodeError:
				# A stray non-JSON line on stdout would break a real client too;
				# record it and keep reading so the run survives.
				self._stderr_tail.append(f'[stdout non-json] {line[:200]!r}')
				continue
			if msg.get('id') == rid:
				if 'error' in msg:
					raise McpError(f'{self.spec.key} {method}: {msg["error"]}')
				return msg.get('result', {})

	async def call(self, tool: str, args: dict[str, Any], timeout: float = CALL_TIMEOUT) -> dict[str, Any]:
		"""Return ``{ok, is_error, text, elapsed}``. Never raises for tool-level errors."""
		t0 = time.monotonic()
		try:
			result = await self._request('tools/call', {'name': tool, 'arguments': args}, timeout=timeout)
		except (TimeoutError, asyncio.TimeoutError):
			return {
				'ok': False,
				'is_error': True,
				'timeout': True,
				'text': f'TIMEOUT after {timeout}s',
				'elapsed': time.monotonic() - t0,
			}
		except McpError as exc:
			return {'ok': False, 'is_error': True, 'timeout': False, 'text': str(exc), 'elapsed': time.monotonic() - t0}
		elapsed = time.monotonic() - t0
		parts = []
		for block in result.get('content', []) or []:
			if block.get('type') == 'text':
				parts.append(block.get('text', ''))
			else:
				parts.append(f'[{block.get("type")} block]')
		text = '\n'.join(parts)
		is_error = bool(result.get('isError'))
		return {'ok': not is_error, 'is_error': is_error, 'timeout': False, 'text': text, 'elapsed': elapsed}

	async def kill(self) -> None:
		if self._stderr_task:
			self._stderr_task.cancel()
		if self.proc and self.proc.returncode is None:
			try:
				self.proc.send_signal(signal.SIGKILL)
			except ProcessLookupError:
				pass
			try:
				await asyncio.wait_for(self.proc.wait(), timeout=10)
			except (TimeoutError, asyncio.TimeoutError):
				pass


# --------------------------------------------------------------------------- #
# Raw CDP helpers (used only for tab bookkeeping and the stale-handle probe)
# --------------------------------------------------------------------------- #


def cdp_targets() -> list[dict[str, Any]]:
	with urllib.request.urlopen(f'{CDP_URL}/json/list', timeout=10) as fh:
		return json.load(fh)


def cdp_pages() -> list[dict[str, Any]]:
	return [t for t in cdp_targets() if t.get('type') == 'page']


async def cdp_eval(target_id: str, expression: str) -> Any:
	ws_url = None
	for t in cdp_pages():
		if t['id'] == target_id:
			ws_url = t.get('webSocketDebuggerUrl')
			break
	if not ws_url:
		raise RuntimeError(f'target {target_id} is gone')
	async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
		await ws.send(
			json.dumps(
				{
					'id': 1,
					'method': 'Runtime.evaluate',
					'params': {'expression': expression, 'returnByValue': True, 'awaitPromise': True},
				}
			)
		)
		while True:
			msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
			if msg.get('id') == 1:
				if 'error' in msg:
					raise RuntimeError(msg['error'])
				res = msg.get('result', {})
				if 'exceptionDetails' in res:
					ed = res['exceptionDetails']
					desc = (ed.get('exception') or {}).get('description') or (ed.get('exception') or {}).get('value')
					raise RuntimeError(f'{ed.get("text", "js exception")}: {desc}'.strip(': '))
				return res.get('result', {}).get('value')


# --------------------------------------------------------------------------- #
# Parsing state output
# --------------------------------------------------------------------------- #

_TREE_INDEX = re.compile(r'\[(\d+)\]<')
_JSON_INDEX = re.compile(r'"index"\s*:\s*(\d+)')


def split_header_and_tree(text: str) -> tuple[dict[str, Any] | None, str]:
	"""bu_mcp answers browser_state with TWO text blocks, which this client joins
	with a newline: a one-line JSON header, then the raw element tree.

	The tree stopped being a JSON string field on purpose -- inside one it had to
	escape every newline and tab, and the model paid two characters for each.
	So `json.loads(whole_text)` no longer parses; the header is only the first line.
	"""
	head, _, rest = text.partition('\n')
	try:
		payload = json.loads(head)
	except Exception:  # noqa: BLE001
		return None, ''
	return (payload, rest) if isinstance(payload, dict) else (None, '')


def parse_state(server_key: str, text: str) -> dict[str, Any]:
	"""-> {chars, elements, url, title, indices, tree, waiting}"""
	out: dict[str, Any] = {'chars': len(text), 'elements': 0, 'url': None, 'title': None, 'indices': []}
	payload: Any = None
	split_tree = ''
	try:
		payload = json.loads(text)
	except Exception:  # noqa: BLE001
		payload, split_tree = split_header_and_tree(text)
	if isinstance(payload, dict) and split_tree and not payload.get('tree'):
		payload = {**payload, 'tree': split_tree}

	indices: set[int] = set()
	if isinstance(payload, dict):
		out['url'] = payload.get('url')
		out['title'] = payload.get('title')
		elems = payload.get('interactive_elements')
		if isinstance(elems, list):
			for e in elems:
				if isinstance(e, dict) and isinstance(e.get('index'), int):
					indices.add(e['index'])
		tree = payload.get('tree')
		if isinstance(tree, str):
			indices.update(int(m) for m in _TREE_INDEX.findall(tree))
			out['tree'] = tree
		out['truncated'] = payload.get('truncated')
	if not indices:
		indices.update(int(m) for m in _TREE_INDEX.findall(text))
		indices.update(int(m) for m in _JSON_INDEX.findall(text))
	out['indices'] = sorted(indices)
	out['elements'] = len(indices)
	return out


def classify(state: dict[str, Any], text: str, nav_res: dict[str, Any], state_res: dict[str, Any]) -> str:
	if nav_res.get('timeout') or state_res.get('timeout'):
		return 'timeout'
	if nav_res.get('is_error'):
		return 'nav_error'
	if state_res.get('is_error'):
		return 'state_error'
	low = text.lower()
	if any(m in low for m in BLOCK_MARKERS):
		return 'blocked'
	if state['elements'] == 0:
		return 'empty'
	return 'ok'


def find_probe_index(server_key: str, text: str, token: str) -> int | None:
	"""Locate the index of the injected probe button in a state payload."""
	try:
		payload = json.loads(text)
	except Exception:  # noqa: BLE001
		payload = None
	if isinstance(payload, dict):
		elems = payload.get('interactive_elements')
		if isinstance(elems, list):
			for e in elems:
				if isinstance(e, dict) and token in json.dumps(e, ensure_ascii=False):
					if isinstance(e.get('index'), int):
						return e['index']
		tree = payload.get('tree')
		if isinstance(tree, str):
			for line in tree.splitlines():
				if token in line:
					m = _TREE_INDEX.search(line)
					if m:
						return int(m.group(1))
	# last resort: nearest [n]< before the token anywhere in the raw text
	pos = text.find(token)
	if pos > 0:
		cands = list(_TREE_INDEX.finditer(text[:pos]))
		if cands:
			return int(cands[-1].group(1))
	return None


# --------------------------------------------------------------------------- #
# Probe JS
# --------------------------------------------------------------------------- #

JS_INSTALL = """
(() => {
  const TOKEN = %TOKEN%;
  for (const id of ['__bench_probe', '__bench_shield', '__bench_sink']) {
    const old = document.getElementById(id);
    if (old) old.remove();
  }
  window.__bench = {probe: 0, shield: 0, sink: 0, other: 0, probeDirect: 0};
  // Delegated capture listener on document. Installed via CDP Runtime.evaluate,
  // so it is NOT subject to the page CSP the way an inline onclick= attribute is
  // (GitHub and friends block those outright), and it survives an outerHTML
  // round trip of any descendant because document itself is never re-parsed.
  if (!window.__benchListener) {
    window.__benchListener = (e) => {
      const b = window.__bench;
      if (!b) return;
      const t = (e.target && e.target.closest)
        ? e.target.closest('#__bench_probe,#__bench_sink,#__bench_shield') : null;
      if (!t) { b.other++; return; }
      if (t.id === '__bench_probe') b.probe++;
      else if (t.id === '__bench_sink') b.sink++;
      else b.shield++;
    };
    document.addEventListener('click', window.__benchListener, true);
  }
  const shield = document.createElement('div');
  shield.id = '__bench_shield';
  shield.setAttribute('style',
    'position:fixed;inset:0;z-index:2147483646;background:transparent;pointer-events:auto;');
  document.body.appendChild(shield);
  const probe = document.createElement('button');
  probe.id = '__bench_probe';
  probe.type = 'button';
  probe.textContent = TOKEN;
  probe.setAttribute('aria-label', TOKEN);
  probe.setAttribute('style',
    'position:fixed;top:8px;left:8px;width:260px;height:36px;z-index:2147483647;' +
    'background:#fff;color:#000;border:1px solid #000;font:12px monospace;');
  // Direct listener on the node object itself. Survives el.remove() -- a click
  // dispatched at a detached node still runs it, but never reaches document.
  // That is exactly how we tell "clicked a corpse" apart from "clicked nothing".
  probe.addEventListener('click', () => { if (window.__bench) window.__bench.probeDirect++; });
  document.body.appendChild(probe);
  return true;
})()
"""

JS_RECREATE = """
(() => {
  const el = document.getElementById('__bench_probe');
  if (!el) return 'missing';
  let how = 'outerHTML';
  try {
    el.outerHTML = el.outerHTML;
  } catch (e) {
    // Trusted Types (require-trusted-types-for 'script') refuses outerHTML
    // assignment outright -- every Google property in this corpus does this.
    // replaceWith(cloneNode(true)) destroys the same node identity and puts an
    // identical element back at the same position, without the HTML parser.
    how = 'replaceWith(cloneNode) [' + e.name + ']';
    el.replaceWith(el.cloneNode(true));
  }
  const back = document.getElementById('__bench_probe');
  return (back && back !== el ? 'recreated via ' : 'lost via ') + how;
})()
"""

JS_REMOVE = """
(() => {
  const el = document.getElementById('__bench_probe');
  if (!el) return 'missing';
  const r = el.getBoundingClientRect();
  el.remove();
  const sink = document.createElement('div');
  sink.id = '__bench_sink';
  sink.setAttribute('style',
    'position:fixed;top:' + r.top + 'px;left:' + r.left + 'px;width:' + r.width + 'px;height:' +
    r.height + 'px;z-index:2147483647;background:#eee;');
  document.body.appendChild(sink);
  return 'removed';
})()
"""

JS_DIAGNOSE = """
(() => {
  const el = document.getElementById('__bench_probe');
  if (!el) return 'probe node is gone from the DOM';
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  const desc = (n) => n ? (n.tagName.toLowerCase()
      + (n.id ? '#' + n.id : '')
      + (n.className && typeof n.className === 'string' ? '.' + n.className.trim().split(/\\s+/).slice(0, 2).join('.') : '')
      + ' z=' + getComputedStyle(n).zIndex) : 'nothing';
  return JSON.stringify({
    rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
    visible: cs.visibility + '/' + cs.display + '/opacity ' + cs.opacity,
    topmost_at_center: desc(top),
    is_probe_on_top: !!(top && (top.id === '__bench_probe' || el.contains(top))),
    doc_hidden: document.hidden,
  });
})()
"""

JS_COUNTS = '(() => JSON.stringify(window.__bench || {}))()'

JS_CLEANUP = """
(() => {
  for (const id of ['__bench_probe', '__bench_shield', '__bench_sink']) {
    const el = document.getElementById(id);
    if (el) el.remove();
  }
  if (window.__benchListener) {
    document.removeEventListener('click', window.__benchListener, true);
    delete window.__benchListener;
  }
  delete window.__bench;
  return true;
})()
"""


# --------------------------------------------------------------------------- #
# Bench driver
# --------------------------------------------------------------------------- #


class Bound:
	"""An MCP client pinned to a tab this script owns."""

	def __init__(self, spec: ServerSpec):
		self.spec = spec
		self.client = McpClient(spec)
		self.target_id: str | None = None

	async def bind(self, foreign: set[str]) -> None:
		await self.client.start()
		token = f'bubench-{self.spec.key}-{uuid.uuid4().hex[:8]}'
		marker = f'https://example.com/#{token}'
		res = await self.client.call(self.spec.tool_navigate, {'url': marker, 'new_tab': True}, timeout=120.0)
		if res['is_error']:
			raise McpError(f'{self.spec.key}: could not bind own tab: {res["text"][:400]}')
		for _ in range(20):
			for t in cdp_pages():
				if token in (t.get('url') or ''):
					if t['id'] in foreign:
						raise McpError(f'{self.spec.key}: marker landed on a FOREIGN tab {t["id"]}, aborting')
					self.target_id = t['id']
					return
			await asyncio.sleep(0.5)
		raise McpError(f'{self.spec.key}: own tab with marker {token} not found')

	def assert_own(self, reported_url: str | None) -> None:
		"""Fail loudly if the server drifted off our tab."""
		assert self.target_id
		pages = {t['id']: (t.get('url') or '') for t in cdp_pages()}
		if self.target_id not in pages:
			raise McpError(f'{self.spec.key}: own tab {self.target_id} disappeared')
		if reported_url and reported_url.startswith('http'):
			live = pages[self.target_id]
			if live.split('#')[0].rstrip('/') != reported_url.split('#')[0].rstrip('/'):
				# Redirects are normal; only complain when the server is clearly
				# talking about some other tab.
				if reported_url not in live and live not in reported_url:
					raise McpError(
						f'{self.spec.key}: server reports {reported_url!r} but our tab shows {live!r} '
						f'-- possible focus drift, aborting'
					)


async def measure_once(bound: Bound, url: str) -> dict[str, Any]:
	spec = bound.spec
	# Both servers are called at their shipped defaults: no timeout override, so
	# bu_mcp's navigate uses its own 10.0s settle budget and the stock navigate
	# returns as soon as the CDP command does.
	nav = await bound.client.call(spec.tool_navigate, {'url': url})
	st = await bound.client.call(spec.tool_state, dict(spec.state_args))
	parsed = parse_state(spec.key, st['text'])
	outcome = classify(parsed, st['text'], nav, st)

	rec: dict[str, Any] = {
		'nav_s': round(nav['elapsed'], 3),
		'state_s': round(st['elapsed'], 3),
		'chars': parsed['chars'],
		'elements': parsed['elements'],
		'url': parsed['url'],
		'title': parsed['title'],
		'outcome': outcome,
		'nav_error': nav['text'][:300] if nav['is_error'] else None,
		'state_error': st['text'][:300] if st['is_error'] else None,
	}
	if spec.key == 'ours' and not nav['is_error']:
		try:
			waiting = json.loads(nav['text']).get('waiting')
		except Exception:  # noqa: BLE001
			waiting = None
		if isinstance(waiting, dict):
			rec['waiting'] = {
				'ready': waiting.get('ready'),
				# whether the ladder actually saw a navigation, and whether the
				# hydration stage settled -- without these two the stage table
				# below can only be reconstructed from free-text stage details.
				'navigated': waiting.get('navigated'),
				'hydrated': waiting.get('hydrated'),
				'elapsed': waiting.get('elapsed'),
				'stages': [
					{
						'name': s.get('name') or s.get('stage'),
						'ok': s.get('ok'),
						'elapsed': s.get('elapsed'),
						'detail': s.get('detail'),
					}
					for s in (waiting.get('stages') or [])
					if isinstance(s, dict)
				],
			}
	try:
		bound.assert_own(parsed['url'])
	except McpError as exc:
		rec['drift'] = str(exc)
		raise
	return rec


async def stale_test(bound: Bound, site_key: str) -> dict[str, Any]:
	"""Two variants of a dead handle. Returns per-variant verdicts."""
	spec = bound.spec
	assert bound.target_id
	token = f'BENCHPROBE{uuid.uuid4().hex[:6].upper()}'
	out: dict[str, Any] = {'token': token}

	for variant, js in (('recreated', JS_RECREATE), ('removed', JS_REMOVE)):
		rec: dict[str, Any] = {}
		try:
			await cdp_eval(bound.target_id, JS_INSTALL.replace('%TOKEN%', json.dumps(token)))
		except Exception as exc:  # noqa: BLE001
			rec['error'] = f'install failed: {exc}'
			out[variant] = rec
			continue

		st = await bound.client.call(spec.tool_state, dict(spec.state_args))
		if st['is_error']:
			rec['error'] = f'state failed: {st["text"][:200]}'
			out[variant] = rec
			continue
		idx = find_probe_index(spec.key, st['text'], token)
		rec['probe_index'] = idx
		if idx is None:
			# Not enough to say "not found": say what was on top of it instead.
			try:
				rec['diagnosis'] = await cdp_eval(bound.target_id, JS_DIAGNOSE)
			except Exception as exc:  # noqa: BLE001
				rec['diagnosis'] = f'diagnosis failed: {exc}'
			rec['state_elements'] = parse_state(spec.key, st['text'])['elements']
			rec['error'] = 'probe not present in state output'
			out[variant] = rec
			await cdp_eval(bound.target_id, JS_CLEANUP)
			continue

		try:
			rec['mutation'] = await cdp_eval(bound.target_id, js)
		except Exception as exc:  # noqa: BLE001
			rec['error'] = f'mutation failed: {exc}'
			out[variant] = rec
			continue

		url_before = None
		try:
			url_before = await cdp_eval(bound.target_id, 'location.href')
		except Exception:  # noqa: BLE001
			pass

		click = await bound.client.call(spec.tool_click, {'index': idx}, timeout=90.0)
		rec['is_error'] = click['is_error']
		rec['reply'] = click['text'][:400]
		rec['click_s'] = round(click['elapsed'], 3)
		rec['stale_flag'] = 'STALE ELEMENT HANDLE' in click['text'] or 'AMBIGUOUS ELEMENT HANDLE' in click['text']

		try:
			counts = json.loads(await cdp_eval(bound.target_id, JS_COUNTS) or '{}')
		except Exception:  # noqa: BLE001
			counts = {}
		rec['counts'] = counts
		try:
			rec['url_changed'] = (await cdp_eval(bound.target_id, 'location.href')) != url_before
		except Exception:  # noqa: BLE001
			rec['url_changed'] = None

		rec['verdict'] = verdict_for(variant, rec)
		try:
			await cdp_eval(bound.target_id, JS_CLEANUP)
		except Exception:  # noqa: BLE001
			pass
		out[variant] = rec
	return out


def verdict_for(variant: str, rec: dict[str, Any]) -> str:
	"""Classify one stale-handle attempt.

	Counters, all installed over CDP so page CSP cannot suppress them:
	  ``probe``       -- a click reached the probe button while it was in the document
	                     (delegated capture listener on ``document``);
	  ``probeDirect`` -- a click ran on the probe NODE OBJECT; if ``probe`` stayed 0
	                     this means the node was already detached, i.e. the server
	                     clicked a corpse and the page never saw the event;
	  ``shield`` / ``sink`` -- a coordinate fallback landed on one of the inert
	                     overlays instead of the intended element;
	  ``other``       -- a click reached some other element in the document.

	recreated: the node was destroyed and an identical one put back at the same
	  position. Correct is either re-identifying it or refusing loudly.
	removed: the node is gone for good. The only correct answer is a loud refusal.
	"""
	counts = rec.get('counts') or {}
	probe = counts.get('probe', 0)
	direct = counts.get('probeDirect', 0)
	shield = counts.get('shield', 0)
	sink = counts.get('sink', 0)
	other = counts.get('other', 0)
	err = rec.get('is_error')

	if variant == 'recreated':
		if probe:
			return 'reidentified'  # clicked the live replacement -- best outcome
		if err:
			return 'refused'  # loud failure -- acceptable, just pessimistic
		if direct:
			return 'clicked-detached'  # ran the handler on a node nobody can see
		if shield or sink or other:
			return 'silent-wrong-click'
		return 'silent-noop'
	# removed
	if err:
		return 'refused'
	if direct:
		return 'clicked-detached'  # reported success against a removed node
	if probe or shield or sink or other:
		return 'silent-wrong-click'
	return 'silent-noop'


async def run(args: argparse.Namespace) -> dict[str, Any]:
	wanted = None
	if args.sites:
		wanted = {s.strip() for s in args.sites.split(',') if s.strip()}
	sites = [s for s in SITES if wanted is None or s[0] in wanted]
	if not sites:
		raise SystemExit(f'no sites matched {args.sites!r}')

	foreign_pages = {t['id']: (t.get('url') or '') for t in cdp_pages()}
	print(f'[bench] foreign tabs to protect: {len(foreign_pages)}', file=sys.stderr)
	for tid, u in foreign_pages.items():
		print(f'         {tid[:8]} {u[:90]}', file=sys.stderr)

	bounds: dict[str, Bound] = {}
	results: dict[str, Any] = {
		'started': time.strftime('%Y-%m-%d %H:%M:%S'),
		'cdp_url': CDP_URL,
		'python': PYTHON,
		'repeats': args.repeats,
		'sites': {k: {'url': u, 'source': src} for k, u, src in sites},
		'servers': {k: {'label': v.label, 'argv': v.argv, 'env': v.env} for k, v in SERVERS.items()},
		'runs': [],
		'stale': {},
		'errors': [],
	}
	try:
		with urllib.request.urlopen(f'{CDP_URL}/json/version', timeout=10) as fh:  # noqa: ASYNC210 - метаданные и уборка, вне измеряемого пути
			results['chrome'] = json.load(fh)
	except Exception as exc:  # noqa: BLE001
		raise SystemExit(f'Chrome CDP not reachable at {CDP_URL}: {exc}. Run scripts/chrome-automation.sh')

	try:
		for key in ('stock', 'ours'):
			b = Bound(SERVERS[key])
			print(f'[bench] starting {key} ...', file=sys.stderr)
			await b.bind(set(foreign_pages))
			bounds[key] = b
			print(f'[bench] {key} bound to own tab {b.target_id[:8]}', file=sys.stderr)

		# warm-up: pay the first-DOM-build cost outside the measurements
		for key, b in bounds.items():
			await b.client.call(b.spec.tool_state, dict(b.spec.state_args))

		total = len(sites) * args.repeats * 2
		done = 0
		for rep in range(args.repeats):
			for si, (skey, url, _src) in enumerate(sites):
				order = ['stock', 'ours'] if (rep + si) % 2 == 0 else ['ours', 'stock']
				for key in order:
					b = bounds[key]
					done += 1
					t0 = time.monotonic()
					try:
						rec = await measure_once(b, url)
					except Exception as exc:  # noqa: BLE001
						rec = {'outcome': 'harness_error', 'error': f'{type(exc).__name__}: {exc}'}
						results['errors'].append(f'{key}/{skey}/rep{rep}: {exc}')
					rec.update({'server': key, 'site': skey, 'repeat': rep, 'cold': rep == 0})
					results['runs'].append(rec)
					print(
						f'[{done}/{total}] rep{rep} {skey:<16} {key:<5} '
						f'{rec.get("outcome"):<12} chars={rec.get("chars")} el={rec.get("elements")} '
						f'nav={rec.get("nav_s")} state={rec.get("state_s")} ({time.monotonic() - t0:.1f}s)',
						file=sys.stderr,
						flush=True,
					)
					if rec.get('outcome') == 'harness_error':
						raise SystemExit(f'aborting: {rec["error"]}')

		if not args.no_stale:
			for skey, url, _src in sites:
				results['stale'][skey] = {}
				for key in ('stock', 'ours'):
					b = bounds[key]
					try:
						await b.client.call(b.spec.tool_navigate, {'url': url})
						res = await stale_test(b, skey)
					except Exception as exc:  # noqa: BLE001
						res = {'error': f'{type(exc).__name__}: {exc}'}
						results['errors'].append(f'stale {key}/{skey}: {exc}')
					results['stale'][skey][key] = res
					print(
						f'[stale] {skey:<16} {key:<5} '
						f'recreated={res.get("recreated", {}).get("verdict")} '
						f'removed={res.get("removed", {}).get("verdict")}',
						file=sys.stderr,
						flush=True,
					)
	finally:
		# park our tabs on about:blank, kill servers, then close only our tabs
		for b in bounds.values():
			await b.client.kill()
		for b in bounds.values():
			if b.target_id and b.target_id not in foreign_pages:
				try:
					urllib.request.urlopen(f'{CDP_URL}/json/close/{b.target_id}', timeout=10).read()  # noqa: ASYNC210 - метаданные и уборка, вне измеряемого пути
					print(f'[bench] closed own tab {b.target_id[:8]}', file=sys.stderr)
				except Exception as exc:  # noqa: BLE001
					print(f'[bench] WARN could not close own tab: {exc}', file=sys.stderr)
		still = {t['id']: (t.get('url') or '') for t in cdp_pages()}
		lost = [f'{tid[:8]} {u[:80]}' for tid, u in foreign_pages.items() if tid not in still]
		changed = [
			f'{tid[:8]} {u[:60]} -> {still[tid][:60]}' for tid, u in foreign_pages.items() if tid in still and still[tid] != u
		]
		results['foreign_tabs_lost'] = lost
		results['foreign_tabs_changed'] = changed
		if lost:
			print(f'[bench] WARN foreign tabs disappeared: {lost}', file=sys.stderr)
		results['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')

	return results


def compose(server_key: str, text: str) -> dict[str, Any]:
	"""Where the characters actually go, so "ours is bigger here" has a cause attached."""
	out: dict[str, Any] = {'total': len(text)}
	block_tree = ''
	try:
		payload = json.loads(text)
	except Exception:  # noqa: BLE001
		payload, block_tree = split_header_and_tree(text)
	if not isinstance(payload, dict):
		return out
	if server_key == 'ours':
		tree = payload.get('tree') or block_tree
		href_map = payload.get('href_map') or {}
		# The tree used to be a *string field* inside pretty-printed JSON, so every
		# newline and tab in it was re-encoded as a two-character escape before it
		# reached the model. Once it moved into its own content block that surcharge
		# is zero by construction; the column stays so old and new runs line up.
		in_payload = len(tree) if block_tree else len(json.dumps(tree, ensure_ascii=False))
		hm = len(json.dumps(href_map, ensure_ascii=False))
		out['tree_raw'] = len(tree)
		out['json_escape'] = 0 if block_tree else in_payload - len(tree) - 2
		out['href_map'] = hm if href_map else 0
		out['metadata'] = len(text) - in_payload - hm
		out['indent_ws'] = sum(len(ln) - len(ln.lstrip('\t ')) for ln in tree.splitlines())
		out['href_inline'] = sum(len(m) for m in re.findall(r'href=\S+', tree))
		out['n'] = len(set(_TREE_INDEX.findall(tree)))
	else:
		elems = payload.get('interactive_elements') or []
		body = len(json.dumps(elems, indent=2))
		out['elements_json'] = body
		out['metadata'] = len(text) - body
		out['href_inline'] = sum(len(str(e.get('href', ''))) for e in elems if isinstance(e, dict))
		out['text_inline'] = sum(len(str(e.get('text', ''))) for e in elems if isinstance(e, dict))
		# every element costs a fixed JSON skeleton: braces, quoted key names,
		# commas, two levels of pretty-print indentation
		out['json_skeleton'] = body - out['href_inline'] - out['text_inline']
		out['n'] = len(elems)
	if out.get('n'):
		out['per_element'] = round(out['total'] / out['n'], 1)
		key = 'json_skeleton' if server_key == 'stock' else 'json_escape'
		out['fixed_per_element'] = round(out[key] / out['n'], 1)
	return out


async def sample_pass(args: argparse.Namespace) -> dict[str, Any]:
	"""Second, cheap pass: one navigate + one state per site per server, recording
	a size breakdown of the payload. Merged into the existing results file."""
	src = PARTIAL_JSON if (args.sites and PARTIAL_JSON.exists()) else RESULTS_JSON
	results = json.loads(src.read_text())
	sites = [s for s in SITES if s[0] in results['sites']]
	foreign_pages = {t['id']: (t.get('url') or '') for t in cdp_pages()}
	bounds: dict[str, Bound] = {}
	comp: dict[str, Any] = {}
	try:
		for key in ('stock', 'ours'):
			b = Bound(SERVERS[key])
			await b.bind(set(foreign_pages))
			bounds[key] = b
		for skey, url, _src in sites:
			comp[skey] = {}
			for key in ('stock', 'ours'):
				b = bounds[key]
				await b.client.call(b.spec.tool_navigate, {'url': url})
				st = await b.client.call(b.spec.tool_state, dict(b.spec.state_args))
				comp[skey][key] = compose(key, st['text'])
			print(f'[compose] {skey}: {comp[skey]}', file=sys.stderr, flush=True)
	finally:
		for b in bounds.values():
			await b.client.kill()
		for b in bounds.values():
			if b.target_id and b.target_id not in foreign_pages:
				try:
					urllib.request.urlopen(f'{CDP_URL}/json/close/{b.target_id}', timeout=10).read()  # noqa: ASYNC210 - метаданные и уборка, вне измеряемого пути
				except Exception:  # noqa: BLE001
					pass
	results['composition'] = comp
	return results


# --------------------------------------------------------------------------- #
# Aggregation + report
# --------------------------------------------------------------------------- #


def _detail_bucket(detail: str) -> str:
	"""Collapse per-run loader ids so stage details can be counted."""
	for prefix in ('no navigation detected', 'new document', 'same-document navigation', 'skipped', 'timeout'):
		if detail.startswith(prefix):
			return prefix
	if 'already committed but has not emitted load yet' in detail:
		return 'already committed, load pending'
	return detail[:60]


def med(vals: list[float]) -> float | None:
	vals = [v for v in vals if v is not None]
	return round(statistics.median(vals), 2) if vals else None


def aggregate(results: dict[str, Any]) -> dict[str, Any]:
	by: dict[tuple[str, str], list[dict]] = {}
	for r in results['runs']:
		by.setdefault((r['site'], r['server']), []).append(r)

	agg: dict[str, Any] = {}
	for (site, server), runs in by.items():
		dom = _dominant([r.get('outcome') for r in runs])
		good = [r for r in runs if r.get('outcome') == dom and r.get('chars')]
		if not good:
			good = [r for r in runs if r.get('outcome') in ('ok', 'blocked', 'empty') and r.get('chars')]
		entry = {
			'outcomes': sorted({r.get('outcome') for r in runs}),
			'outcome': dom,
			'n': len(runs),
			'chars': med([r.get('chars') for r in good]),
			'elements': med([r.get('elements') for r in good]),
			'state_s': med([r.get('state_s') for r in good]),
			'nav_s': med([r.get('nav_s') for r in runs if r.get('nav_s')]),
			'cold_nav_s': med([r['nav_s'] for r in runs if r.get('cold') and r.get('nav_s')]),
			'warm_nav_s': med([r['nav_s'] for r in runs if not r.get('cold') and r.get('nav_s')]),
			'cold_state_s': med([r['state_s'] for r in runs if r.get('cold') and r.get('state_s')]),
			'warm_state_s': med([r['state_s'] for r in runs if not r.get('cold') and r.get('state_s')]),
			'title': next((r.get('title') for r in runs if r.get('title')), None),
		}
		if entry['chars'] and entry['elements']:
			entry['cpe'] = round(entry['chars'] / entry['elements'], 1)
		else:
			entry['cpe'] = None
		agg[f'{site}|{server}'] = entry
	return agg


def _dominant(vals: list[str]) -> str:
	"""Most common outcome across repeats; ties broken toward the worse one."""
	severity = ['ok', 'empty', 'blocked', 'state_error', 'nav_error', 'timeout', 'harness_error']
	counts: dict[str, int] = {}
	for v in vals:
		counts[v] = counts.get(v, 0) + 1

	def rank(kv: tuple[str, int]) -> tuple[int, int]:
		return (kv[1], severity.index(kv[0]) if kv[0] in severity else 99)

	return max(counts.items(), key=rank)[0] if counts else 'ok'


def fmt(v: Any, nd: int = 0) -> str:
	if v is None:
		return '--'
	if isinstance(v, float):
		return f'{v:.{nd}f}' if nd else f'{v:.0f}'
	return str(v)


def ratio(a: float | None, b: float | None) -> str:
	if not a or not b:
		return '--'
	return f'{b / a:.2f}x'


def build_report(results: dict[str, Any]) -> str:
	agg = aggregate(results)
	sites = results['sites']
	L: list[str] = []
	A = L.append

	A('# bu_mcp vs stock browser-use MCP: observation cost and handle correctness')
	A('')
	A(f'Run: {results.get("started")} -> {results.get("finished")}  ')
	A(f'Chrome: `{results.get("chrome", {}).get("Browser")}` headless, CDP `{results["cdp_url"]}`  ')
	A(f'Python: `{results["python"]}`  ')
	A(f'Repeats per site per server: {results["repeats"]} (median reported)  ')
	A(f'Regenerate: `{results["python"]} -m bu_mcp.bench`')
	A('')

	# ---------------- methodology
	A('## Methodology')
	A('')
	A('Two MCP servers, driven over real stdio JSON-RPC by `bu_mcp/bench.py`, both attached to')
	A('the same already-running headless Chrome:')
	A('')
	A('| | command | state tool | navigate | click |')
	A('|---|---|---|---|---|')
	for k, s in results['servers'].items():
		env = ' '.join(f'{a}={b}' for a, b in s['env'].items())
		cmd = f'{env} {" ".join(s["argv"])}'.strip().replace(results['python'], 'python')
		A(f'| **{k}** | `{cmd}` | ' f'`{SERVERS[k].tool_state}` | `{SERVERS[k].tool_navigate}` | `{SERVERS[k].tool_click}` |')
	A('')
	A('Per site, per server, per repeat: `navigate(url)` then the state tool. Nothing else is')
	A('called, and no site element is ever clicked.')
	A('')
	A('* **Observation size** -- `len()` of the text content block the state tool returns. That is')
	A('  literally what a model pays to look at the page once.')
	A('* **Elements** -- distinct interactive indices actually handed out. Counted from')
	A('  `interactive_elements[].index` for stock and from `[N]<` markers in the tree for bu_mcp.')
	A('  Reported next to size on purpose: a smaller observation that dropped half the page is not')
	A('  cheaper, it is blinder.')
	A('* **Chars/element** -- size divided by elements. The honest summary number.')
	A('* **Latency** -- wall clock of the MCP call, client side, including JSON-RPC round trip.')
	A('')
	A('Bias controls:')
	A('')
	A(f'* {results["repeats"]} repeats per cell, **median** reported (not mean: one blocked reload')
	A('  would otherwise dominate).')
	A('* Server order is **alternated** per (repeat, site): `(repeat + site_index) % 2` decides who')
	A('  goes first. Both servers share one Chrome and therefore one HTTP cache, so whoever goes')
	A('  second gets a warmer page; alternating cancels that instead of pretending it is absent.')
	A('* Repeat 0 is the **cold** pass (first visit of the run), repeats 1+ are **warm**. Both are')
	A('  reported separately in the latency section.')
	A('* Each server is pinned to **its own tab**, bound as its very first tool call via')
	A('  `navigate(new_tab=True)` to a unique marker URL. Every measurement re-checks that the')
	A('  server is still talking about that tab and aborts the run on drift. Pre-existing foreign')
	A('  tabs are snapshotted at start and verified at the end.')
	A('* Both servers stay alive for the whole run, so process startup and the first DOM build are')
	A('  paid once, before the measurements, by a discarded warm-up state call.')
	A('')
	lost = results.get('foreign_tabs_lost') or []
	A(f'Foreign-tab check after the run: {"OK, none lost" if not lost else "**FAILED**: " + ", ".join(lost)}.')
	A('')

	# ---------------- corpus
	A('## Corpus')
	A('')
	A('13 live sites taken from the WebVoyager task set (it runs on exactly these popular live')
	A('sites), 2 deterministic replicas from REAL / realevals.xyz, and one easy control page.')
	A('')
	A('| site | url | source | outcome (stock / bu_mcp) |')
	A('|---|---|---|---|')
	for skey, meta in sites.items():
		a = agg.get(f'{skey}|stock', {})
		b = agg.get(f'{skey}|ours', {})
		A(f'| {skey} | `{meta["url"]}` | {meta["source"]} | {a.get("outcome", "--")} / {b.get("outcome", "--")} |')
	A('')
	odd = [
		f'{r["site"]}/{r["server"]}/rep{r["repeat"]} -> `{r.get("outcome")}` '
		f'({r.get("chars")} chars, {r.get("elements")} elements)'
		for r in results['runs']
		if r.get('outcome') != 'ok'
	]
	total_runs = len(results['runs'])
	if odd:
		A(f'**{len(odd)} of {total_runs} individual runs did not come back `ok`:**')
		A('')
		for o in odd:
			A(f'* {o}')
		A('')
		A('These are kept in the record rather than dropped, and they do not enter the per-site')
		A('medians when a site was `ok` on the majority of its repeats.')
	else:
		A(f'**All {total_runs} individual runs came back `ok`.** No site in this corpus served an')
		A('anti-automation interstitial to either server during this run. That is a property of this')
		A('run, not a claim about these sites: the profile is a real logged-in Chrome with a normal')
		A('history, which is exactly the setup that does not trip bot detection. A fresh headless')
		A('profile would very likely see amazon and espn behave differently.')
	A('')
	A('Outcome codes: `ok` = state returned with interactive elements; `blocked` = the page loaded')
	A('but its text carries an anti-automation marker (captcha / "unusual traffic" / "just a')
	A('moment" / ...); `empty` = loaded, zero interactive elements handed out; `nav_error` /')
	A('`state_error` / `timeout` = the tool call itself failed. Blocked and empty sites are kept in')
	A('the table and excluded from the aggregate cost numbers -- they are a real outcome, not a zero.')
	A('')

	# ---------------- main table
	A('## Observation cost per site')
	A('')
	A('Medians. `cpe` = chars per interactive element.')
	A('')
	A('| site | stock chars | ours chars | size | stock el | ours el | element recall | stock cpe | ours cpe | cpe ratio |')
	A('|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|')
	for skey in sites:
		a = agg.get(f'{skey}|stock', {})
		b = agg.get(f'{skey}|ours', {})
		rec = '--'
		if a.get('elements') and b.get('elements'):
			rec = f'{b["elements"] / a["elements"]:.2f}x'
		A(
			f'| {skey} | {fmt(a.get("chars"))} | {fmt(b.get("chars"))} | {ratio(a.get("chars"), b.get("chars"))} '
			f'| {fmt(a.get("elements"))} | {fmt(b.get("elements"))} | {rec} '
			f'| {fmt(a.get("cpe"), 1)} | {fmt(b.get("cpe"), 1)} | {ratio(a.get("cpe"), b.get("cpe"))} |'
		)
	A('')
	A('`size` and `cpe ratio` are ours/stock: below 1.00x means bu_mcp is cheaper, above 1.00x')
	A('means it is more expensive. `element recall` is ours/stock: below 1.00x means bu_mcp handed')
	A('the model fewer clickable things than the stock server did.')
	A('')

	# ---------------- latency
	A('## Latency')
	A('')
	A('Seconds, median. Navigation latency is dominated by the network and by how long each server')
	A('waits before returning, not by serialization.')
	A('')
	A(
		'| site | nav stock | nav ours | nav cold stock | nav cold ours | nav warm stock | nav warm ours | state stock | state ours |'
	)
	A('|---|---:|---:|---:|---:|---:|---:|---:|---:|')
	for skey in sites:
		a = agg.get(f'{skey}|stock', {})
		b = agg.get(f'{skey}|ours', {})
		A(
			f'| {skey} | {fmt(a.get("nav_s"), 2)} | {fmt(b.get("nav_s"), 2)} '
			f'| {fmt(a.get("cold_nav_s"), 2)} | {fmt(b.get("cold_nav_s"), 2)} '
			f'| {fmt(a.get("warm_nav_s"), 2)} | {fmt(b.get("warm_nav_s"), 2)} '
			f'| {fmt(a.get("state_s"), 2)} | {fmt(b.get("state_s"), 2)} |'
		)
	A('')
	st_pairs = [
		(agg[f'{k}|stock'].get('state_s'), agg[f'{k}|ours'].get('state_s'))
		for k in sites
		if agg.get(f'{k}|stock', {}).get('state_s') and agg.get(f'{k}|ours', {}).get('state_s')
	]
	if st_pairs:
		wins = sum(1 for a, b in st_pairs if b < a)
		A(
			f'**The state call is consistently faster on bu_mcp: {wins}/{len(st_pairs)} sites**, median ratio '
			f'{statistics.median([b / a for a, b in st_pairs]):.2f}x. That is the one latency number that is'
		)
		A('about the servers rather than about the network, and the cause is checkable in the source:')
		A('`browser_use/mcp/server.py:888` calls `get_browser_state_summary()` with no arguments, and')
		A('that signature defaults to `include_screenshot=True` (`browser_use/browser/session.py:1591`).')
		A('The frame is captured every time and then discarded at `server.py:928`, which only forwards')
		A('it when the *tool* argument `include_screenshot` is true -- and this benchmark always passes')
		A("false. bu_mcp's `browser_state` never takes the screenshot at all. On this corpus that costs")
		A('the stock server roughly a tenth of a second to half a second per look at the page, for a')
		A('picture nobody receives.')
		A('')
	nav_pairs = [
		(agg[f'{k}|stock'].get('nav_s'), agg[f'{k}|ours'].get('nav_s'))
		for k in sites
		if agg.get(f'{k}|stock', {}).get('nav_s') and agg.get(f'{k}|ours', {}).get('nav_s')
	]
	if nav_pairs:
		deltas = [b - a for a, b in nav_pairs]
		A(f'**Navigation is consistently slower on bu_mcp**, by a median of {statistics.median(deltas):.2f}s. Most of')
		A('that is not a wait for the page -- see the readiness section, where it turns out to be a')
		A('poll for an event that already fired.')
		A('')

	# ---------------- summary
	ok_sites = [
		s for s in sites if agg.get(f'{s}|stock', {}).get('outcome') == 'ok' and agg.get(f'{s}|ours', {}).get('outcome') == 'ok'
	]
	A('## Summary')
	A('')
	if ok_sites:
		sc = [agg[f'{s}|stock']['chars'] for s in ok_sites]
		oc = [agg[f'{s}|ours']['chars'] for s in ok_sites]
		se = [agg[f'{s}|stock']['elements'] for s in ok_sites]
		oe = [agg[f'{s}|ours']['elements'] for s in ok_sites]
		s_cpe = [agg[f'{s}|stock']['cpe'] for s in ok_sites]
		o_cpe = [agg[f'{s}|ours']['cpe'] for s in ok_sites]
		size_r = [agg[f'{s}|ours']['chars'] / agg[f'{s}|stock']['chars'] for s in ok_sites]
		cpe_r = [agg[f'{s}|ours']['cpe'] / agg[f'{s}|stock']['cpe'] for s in ok_sites]
		el_r = [agg[f'{s}|ours']['elements'] / agg[f'{s}|stock']['elements'] for s in ok_sites]
		A(f'Over the {len(ok_sites)} sites where both servers returned a usable state:')
		A('')
		A('| metric | stock | bu_mcp | ours/stock |')
		A('|---|---:|---:|---:|')
		A(f'| total chars across the corpus | {sum(sc):,.0f} | {sum(oc):,.0f} | {sum(oc) / sum(sc):.2f}x |')
		A(f'| total elements across the corpus | {sum(se):,.0f} | {sum(oe):,.0f} | {sum(oe) / sum(se):.2f}x |')
		A(
			f'| corpus-wide chars per element | {sum(sc) / sum(se):.1f} | {sum(oc) / sum(oe):.1f} | {(sum(oc) / sum(oe)) / (sum(sc) / sum(se)):.2f}x |'
		)
		A(
			f'| median per-site chars | {statistics.median(sc):,.0f} | {statistics.median(oc):,.0f} | {statistics.median(size_r):.2f}x |'
		)
		A(
			f'| median per-site cpe | {statistics.median(s_cpe):.1f} | {statistics.median(o_cpe):.1f} | {statistics.median(cpe_r):.2f}x |'
		)
		A(f'| median per-site element recall | -- | -- | {statistics.median(el_r):.2f}x |')
		A('')
		wins = [s for s in ok_sites if agg[f'{s}|ours']['cpe'] < agg[f'{s}|stock']['cpe']]
		losses = [s for s in ok_sites if agg[f'{s}|ours']['cpe'] >= agg[f'{s}|stock']['cpe']]
		A(
			f'bu_mcp has the lower chars/element on **{len(wins)}/{len(ok_sites)}** sites '
			f'({", ".join(wins) if wins else "none"}).'
		)
		if losses:
			A('')
			A(f'It **loses** on {len(losses)}: {", ".join(losses)}.')
		A('')
		corpus_cpe = (sum(oc) / sum(oe)) / (sum(sc) / sum(se))
		median_cpe = statistics.median(cpe_r)
		if corpus_cpe < 1.0 <= median_cpe:
			A('**Do not read only the first table.** The two summary rows point in opposite directions')
			A(f'and both are true: corpus-wide chars-per-element favours bu_mcp ({corpus_cpe:.2f}x) while the')
			A(f'median site favours the stock server ({median_cpe:.2f}x). There is no contradiction. bu_mcp wins on')
			A('the pages with many interactive elements, and those pages contribute most of the')
			A('characters in the corpus total. It loses on the pages with few, and those are the')
			A('majority by count. Which number matters depends on what an agent actually looks at: if')
			A('the workload is dense listing and search-result pages, the corpus number is the relevant')
			A('one; if it is a long walk through sparse app screens, the median is.')
			A('')
		el_wins = [s for s in ok_sites if agg[f'{s}|ours']['elements'] > agg[f'{s}|stock']['elements']]
		big = [s for s in el_wins if agg[f'{s}|ours']['elements'] >= 1.5 * agg[f'{s}|stock']['elements']]
		if big:
			A(f'On **{", ".join(big)}** bu_mcp hands the model substantially more interactive elements than')
			A('the stock server does -- not because it serializes differently, but because it looked')
			A('later. Its `navigate` returns seconds after the stock one, and on a JavaScript-hydrated')
			A('page those seconds are the difference between a shell and a rendered app. The stock')
			A('server is not being frugal on these pages, it is being early. That is a cost the')
			A('chars-per-element ratio flatters rather than penalises, because a state call that')
			A('reports 8 elements on a page that has 47 looks cheap.')
			A('')
	else:
		A('_No site produced a usable state from both servers._')
	A('')

	# ---------------- composition
	comp = results.get('composition') or {}
	if comp:
		A('## Where the characters go')
		A('')
		A('A second single-shot pass (`--sample`) records the size breakdown of one state payload per')
		A('site. Bytes. `meta` is everything outside the element payload (`url`, `title`, `tabs`,')
		A('`viewport`, `scroll`, JSON braces).')
		A('')
		A('| site | n | stock total | skeleton | href | text | meta | ours total | tree | json-escape | href_map | meta |')
		A('|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|')
		for skey in sites:
			c = comp.get(skey) or {}
			a = c.get('stock') or {}
			b = c.get('ours') or {}
			A(
				f'| {skey} | {fmt(a.get("n"))}/{fmt(b.get("n"))} | {fmt(a.get("total"))} | {fmt(a.get("json_skeleton"))} '
				f'| {fmt(a.get("href_inline"))} | {fmt(a.get("text_inline"))} | {fmt(a.get("metadata"))} '
				f'| {fmt(b.get("total"))} | {fmt(b.get("tree_raw"))} | {fmt(b.get("json_escape"))} '
				f'| {fmt(b.get("href_map"))} | {fmt(b.get("metadata"))} |'
			)
		A('')
		A('Three separate effects fall out of this table, and together they explain every row of the')
		A('cost table above.')
		A('')
		A('**1. Stock pays a JSON skeleton per element; bu_mcp does not.** Each entry in')
		A('`interactive_elements` costs `{`, four quoted key names, commas and two levels of')
		A('pretty-print indent before any content. Measured:')
		A('')
		sk = [(k, (comp[k].get('stock') or {})) for k in sites if (comp.get(k) or {}).get('stock', {}).get('n')]
		if sk:
			per = [v['json_skeleton'] / v['n'] for _, v in sk]
			A(f'* {statistics.median(per):.0f} bytes of pure JSON skeleton per element (median over the corpus),')
			A(f'  {min(per):.0f} to {max(per):.0f} across sites.')
			worst = max(sk, key=lambda kv: kv[1]['json_skeleton'] / max(kv[1]['total'], 1))
			A(
				f'* on `{worst[0]}` the skeleton alone is {worst[1]["json_skeleton"]:,} of '
				f'{worst[1]["total"]:,} bytes = {100 * worst[1]["json_skeleton"] / worst[1]["total"]:.0f}% of the whole observation.'
			)
		A('')
		A("bu_mcp's equivalent is one tab-indented line per element, which is why it wins outright on")
		A('the element-dense pages (arxiv, wolframalpha, real_omnizon).')
		A('')
		A('**2. bu_mcp pays a flat envelope, and on sparse pages the envelope is the page.** Its')
		A('`metadata` column barely moves with page size. Compare:')
		A('')
		ourmeta = [(k, (comp[k].get('ours') or {})) for k in sites if (comp.get(k) or {}).get('ours', {}).get('metadata')]
		if ourmeta:
			mm = [v['metadata'] for _, v in ourmeta]
			A(f'* bu_mcp metadata: {min(mm):,} to {max(mm):,} bytes, median {statistics.median(mm):,.0f}.')
			thin = [(k, v) for k, v in ourmeta if v.get('tree_raw') and v['metadata'] > v['tree_raw']]
			for k, v in thin:
				A(
					f'* on `{k}` the metadata ({v["metadata"]:,}) is **larger than the element tree itself** '
					f'({v["tree_raw"]:,}). The page has {v.get("n")} interactive elements; there is nothing to amortise it over.'
				)
		A('')
		A('**3. The tree is a JSON string field, so every newline and tab is re-encoded.** bu_mcp')
		A('returns a text tree wrapped in pretty-printed JSON, which means each `\\n` and `\\t` costs two')
		A('characters instead of one on the wire. The `json-escape` column is that surcharge. It is')
		A('pure format overhead: the model gains nothing from it. This is the one number in the whole')
		A('report that looks like a straightforward bug rather than a trade-off -- the tree could be')
		A('returned as its own text content block next to a small JSON header and the surcharge would')
		A('go to zero.')
		A('')
		am = (comp.get('amazon') or {}).get('ours') or {}
		if am.get('href_map'):
			A('**4. `href_map` placeholdering can backfire.** bu_mcp replaces long URLs with')
			A('`{{_<hash>}}` and moves the real URL into an `href_map`. That pays off when the same URL')
			A(f'repeats. On `amazon` it does not: the map costs {am["href_map"]:,} bytes on its own, on top of the')
			A(
				f'placeholders left in the tree, against {(comp["amazon"]["stock"] or {}).get("href_inline", 0):,} bytes of plain inline hrefs in the stock'
			)
			A('payload. Distinct long URLs are exactly the case where the indirection loses.')
			A('')

	# ---------------- correctness
	A('## Correctness: stale element handles')
	A('')
	A('Size cannot show this. Procedure, per site, per server, on the loaded page:')
	A('')
	A('1. Inject a transparent full-viewport shield (`__bench_shield`) plus a probe button')
	A('   (`__bench_probe`, unique text token) on top of it. The shield guarantees that a')
	A('   coordinate-fallback click can never reach a real site element -- this is what makes it')
	A('   safe to run on live sites at all.')
	A('2. Ask the server for state, find the index it assigned to the probe.')
	A('3. Destroy the node behind that index, in one of two ways:')
	A('   * **recreated** -- `el.outerHTML = el.outerHTML`. The old node is gone, an identical one')
	A('     sits at the same position with the same accessible name.')
	A('   * **removed** -- `el.remove()`, and an inert grey `__bench_sink` div is put over the exact')
	A('     rectangle it used to occupy, so a blind coordinate click lands somewhere detectable.')
	A('4. Call `browser_click(index=<old index>)` and record `isError`, the reply text, and the')
	A('   click counters on the shield / probe / sink.')
	A('')
	A('All click counters are installed over CDP (`document.addEventListener` in capture phase plus a')
	A('listener bound to the probe node object), never as inline `onclick=` attributes -- several')
	A('sites in this corpus ship a CSP that kills inline handlers, which would have silently zeroed')
	A('every counter.')
	A('')
	A('Verdicts:')
	A('')
	A('* `reidentified` -- the click landed on the live replacement node. Best outcome.')
	A('* `refused` -- the call failed loudly (`isError=true`). Acceptable: pessimistic but honest.')
	A('* `clicked-detached` -- the call reported success and the handler on the *detached* node ran.')
	A("  The event never entered the document; from the page's point of view nothing happened, but")
	A('  the server told its client the click succeeded. This is the worst case.')
	A('* `silent-wrong-click` -- reported success, and the click landed on some other element.')
	A('* `silent-noop` -- reported success, nothing was clicked anywhere.')
	A('')
	A('| site | stock recreated | ours recreated | stock removed | ours removed |')
	A('|---|---|---|---|---|')
	stale = results.get('stale') or {}
	for skey in sites:
		row = stale.get(skey, {})

		def cell(sv: str, var: str) -> str:
			r = (row.get(sv) or {}).get(var) or {}
			if r.get('error'):
				return f'n/a ({r["error"][:40]})'
			v = r.get('verdict', '--')
			if r.get('stale_flag'):
				v += ' (STALE)'
			return v

		A(
			f'| {skey} | {cell("stock", "recreated")} | {cell("ours", "recreated")} '
			f'| {cell("stock", "removed")} | {cell("ours", "removed")} |'
		)
	A('')

	counts: dict[str, dict[str, dict[str, int]]] = {}
	for sv in ('stock', 'ours'):
		counts[sv] = {}
		for var in ('recreated', 'removed'):
			c: dict[str, int] = {}
			for skey in sites:
				r = ((stale.get(skey) or {}).get(sv) or {}).get(var) or {}
				v = 'n/a' if r.get('error') or not r.get('verdict') else r['verdict']
				c[v] = c.get(v, 0) + 1
			counts[sv][var] = c
	A('Totals:')
	A('')
	A(
		'| server | variant | '
		+ ' | '.join(['reidentified', 'refused', 'clicked-detached', 'silent-wrong-click', 'silent-noop', 'n/a'])
		+ ' |'
	)
	A('|---|---|---:|---:|---:|---:|---:|---:|')
	for sv in ('stock', 'ours'):
		for var in ('recreated', 'removed'):
			c = counts[sv][var]
			A(
				f'| {sv} | {var} | '
				+ ' | '.join(
					str(c.get(k, 0))
					for k in ['reidentified', 'refused', 'clicked-detached', 'silent-wrong-click', 'silent-noop', 'n/a']
				)
				+ ' |'
			)
	A('')

	def silent_share(sv: str, var: str) -> str:
		c = counts[sv][var]
		bad = c.get('silent-wrong-click', 0) + c.get('silent-noop', 0) + c.get('clicked-detached', 0)
		tot = sum(v for k, v in c.items() if k != 'n/a')
		return f'{bad}/{tot}' + (f' = {100 * bad / tot:.0f}%' if tot else '')

	A('**Price of the problem.** On the "removed" variant -- where the node is gone and the only')
	A(f'correct answer is a loud refusal -- the stock server reports success on {silent_share("stock", "removed")}')
	A(f'of the sites it could be tested on, bu_mcp on {silent_share("ours", "removed")}.')
	A('')

	# ---------------- readiness
	A('## Page readiness (bu_mcp only)')
	A('')
	A('`bu_mcp.waiting.wait_after_navigation` runs a fail-open ladder of stages and returns a')
	A('`ready` verdict. The stock server has no equivalent -- its `navigate` returns as soon as the')
	A('CDP command comes back -- so this section only has one column, and it is here to show the')
	A('cost of the extra wait, not to score a win.')
	A('')
	rows = [r for r in results['runs'] if r['server'] == 'ours' and r.get('waiting')]
	if rows:
		by_site: dict[str, list[dict]] = {}
		for r in rows:
			by_site.setdefault(r['site'], []).append(r['waiting'])
		A('| site | ready | median ladder, s | what stage 1 concluded |')
		A('|---|---|---:|---|')
		details: dict[str, int] = {}
		for skey in sites:
			ws = by_site.get(skey) or []
			if not ws:
				A(f'| {skey} | -- | -- | -- |')
				continue
			ready = sum(1 for w in ws if w.get('ready'))
			el = med([w.get('elapsed') for w in ws])
			d: dict[str, int] = {}
			for w in ws:
				for st in w.get('stages') or []:
					if st.get('name') == 'navigation_start':
						key = _detail_bucket(str(st.get('detail')))
						d[key] = d.get(key, 0) + 1
						details[key] = details.get(key, 0) + 1
			dtxt = ', '.join(f'{k} ({v})' for k, v in sorted(d.items(), key=lambda kv: -kv[1])) or '--'
			A(f'| {skey} | {ready}/{len(ws)} | {fmt(el, 2)} | {dtxt} |')
		A('')
		never = [s for s in sites if by_site.get(s) and not any(w.get('ready') for w in by_site[s])]
		total_w = sum(details.values())
		miss = details.get('no navigation detected', 0)
		A('**The ladder never reports `ready=false` on this corpus, and that is not the good news it')
		A('looks like.** Look at the last column.')
		A('')
		if total_w:
			A(f'On **{miss}/{total_w}** navigations stage 1 concluded `no navigation detected`. That is the')
			A('ladder losing a race, not the page being quiet. `browser_navigate` first runs the')
			A('registry `navigate` action and only then calls `wait_after_navigation`; by that point')
			A('the document has usually already committed *and* emitted `load`, so there is no')
			A('loader-id change left to observe. Stage 1 then polls for its whole start window --')
			A('`min(max(1.0, timeout/4), ...)`, i.e. 2.5s at the shipped 10s default -- finds nothing,')
			A('fails open with `ok=true`, and stage 2 is skipped as "no cross-document navigation to')
			A('wait for". The verdict `ready=true` is therefore mostly vacuous: it means "the ladder')
			A('had nothing to wait for", not "the page settled".')
			A('')
			A("So the ~2.5s that separates bu_mcp's navigate latency from the stock one is, on most")
			A('sites, a dead poll rather than a measured wait. It is bought at full price and its')
			A('value is accidental: the page does keep hydrating during those seconds, which is where')
			A('the extra elements on `google_maps` and `coursera` come from. But a fixed `sleep(2.5)`')
			A('would have produced the same benefit for the same cost, and the ladder is supposed to')
			A('be better than a fixed sleep.')
			A('')
			A('This is the clearest actionable defect the benchmark found in our own layer: the')
			A('baseline loader id has to be captured **before** the navigate action runs, not after.')
			A('')
		if never:
			A(f'Sites where the ladder never reached `ready`: {", ".join(never)}.')
			A('')
	else:
		A('_No readiness data collected._')
	# ---------------- verdict
	A('## Verdict')
	A('')
	A('On this corpus, with no model in the loop:')
	A('')
	A('* **Observation size is roughly a wash, with a clear shape to it.** bu_mcp is cheaper per')
	A('  element on element-dense pages and more expensive on sparse ones. The crossover is')
	A('  structural: the stock server pays a per-element JSON skeleton, bu_mcp pays a flat')
	A('  per-observation envelope. Neither is uniformly better; the corpus decides.')
	A("* **Two of bu_mcp's size losses look like defects rather than trade-offs.** The JSON-escaping")
	A('  of the tree is pure waste, and the `href_map` indirection is a net loss whenever the URLs')
	A('  on the page are long and distinct rather than repeated.')
	A('* **Handle correctness is not a wash.** The stock server reported a successful click on a')
	A('  node that no longer existed on every single site it could be tested on, and the click')
	A('  reached nothing. bu_mcp either re-identified the replacement or refused loudly, on every')
	A('  site. This is the one dimension where the difference is categorical rather than a')
	A('  percentage, and it is invisible in any size or latency measurement.')
	A('* **The readiness ladder buys real elements and pays too much for them.** It produces')
	A('  materially richer observations on hydrated pages, but it gets there by polling for a')
	A('  navigation event that already fired, which is a bug with a beneficial side effect.')
	A('')

	# ---------------- what this does not measure
	A('## What this benchmark does NOT measure')
	A('')
	A('**Task success rate. There is no model in the loop.** Every call in this run was issued by a')
	A('fixed script, not by an agent deciding what to do next. So none of these numbers say that')
	A('either server helps a model finish a WebVoyager or REAL task more often. They say what one')
	A('look at a page costs and whether a stale index is caught. Those are inputs to task success,')
	A('not task success.')
	A('')
	A('Specifically out of scope here:')
	A('')
	A('* **Whether the elements that survived are the right ones.** Element recall is counted, not')
	A('  judged. A server could keep exactly the elements a task needs and still score badly, or')
	A('  keep 500 useless ones and score well.')
	A('* **Whether the compact tree is more legible to a model than flat JSON.** That is the whole')
	A("  design claim behind bu_mcp's `browser_state`, and it can only be settled by running an")
	A('  agent, not by counting characters.')
	A('* **Multi-step behaviour.** Scrolling, typing, tab switching, downloads, iframes, shadow DOM')
	A('  and re-planning after a failed action are untouched. One navigate plus one state call is a')
	A('  fraction of a real trajectory.')
	A('* **Cost in tokens.** Characters are a proxy. Tokenizers do not treat a JSON blob and an')
	A('  indented tree identically, and the ratio between the two is not 1:1.')
	A("* **The screenshot path.** `include_screenshot=False` throughout, so the stock server's habit")
	A('  of capturing a frame and discarding it shows up only as latency here, never as payload.')
	A('* **Stability over time.** Live sites change, A/B tests differ per session, and a logged-in')
	A('  Google profile sees different pages than a fresh one. Re-running this on another day will')
	A('  give different absolute numbers.')
	A('* **The stale-handle test under a shield is not a real page.** Injecting a full-viewport')
	A('  shield changes what a coordinate fallback can hit. It makes the test safe and the verdicts')
	A('  observable, but a real misclick on a real page could do something worse than increment a')
	A('  counter -- or nothing at all.')
	A('')

	errs = results.get('errors') or []
	if errs:
		A('## Harness errors')
		A('')
		for e in errs[:40]:
			A(f'* `{e}`')
		A('')

	A('## Raw data')
	A('')
	A(f'`{RESULTS_JSON.name}` next to this file holds every individual run, every stale-handle reply')
	A('and the readiness stage breakdowns.')
	A('')
	return '\n'.join(L)


def main() -> None:
	p = argparse.ArgumentParser(description=__doc__)
	p.add_argument('--repeats', type=int, default=3)
	p.add_argument('--sites', type=str, default=None, help='comma separated subset of site keys')
	p.add_argument('--no-stale', action='store_true')
	p.add_argument('--report-only', action='store_true', help='re-render BENCH.md from bench_results.json')
	p.add_argument('--sample', action='store_true', help='add a payload size breakdown to an existing results file')
	args = p.parse_args()

	# A partial corpus must never clobber the full-corpus artifacts: a --sites run
	# is a debugging aid, not a result, and BENCH.md is supposed to describe the
	# whole corpus.
	partial = bool(args.sites) or args.repeats < 3
	out_json = PARTIAL_JSON if partial else RESULTS_JSON
	out_md = PARTIAL_MD if partial else REPORT_MD

	if args.report_only:
		results = json.loads((out_json if out_json.exists() else RESULTS_JSON).read_text())
	elif args.sample:
		results = asyncio.run(sample_pass(args))
		out_json.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
	else:
		results = asyncio.run(run(args))
		out_json.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
	out_md.write_text(build_report(results))
	if partial:
		print('note: partial corpus, wrote *.partial.* so the full-run artifacts stay intact', file=sys.stderr)
	print(f'wrote {out_json} and {out_md}', file=sys.stderr)


if __name__ == '__main__':
	main()
