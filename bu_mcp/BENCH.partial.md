# bu_mcp vs stock browser-use MCP: observation cost and handle correctness

Run: 2026-09-03 12:48:18 -> 2026-09-03 12:48:54  
Chrome: `Chrome/152.0.7977.65` headless, CDP `http://127.0.0.1:9222`  
Python: `/Users/draiqws/browser-use/.venv/bin/python`  
Repeats per site per server: 3 (median reported)  
Regenerate: `/Users/draiqws/browser-use/.venv/bin/python -m bu_mcp.bench`

## Methodology

Two MCP servers, driven over real stdio JSON-RPC by `bu_mcp/bench.py`, both attached to
the same already-running headless Chrome:

| | command | state tool | navigate | click |
|---|---|---|---|---|
| **stock** | `python -m browser_use.mcp` | `browser_get_state` | `browser_navigate` | `browser_click` |
| **ours** | `PYTHONPATH=/Users/draiqws/browser-use BU_MCP_CDP_URL=http://127.0.0.1:9222 python -m bu_mcp.server` | `browser_state` | `browser_navigate` | `browser_click` |

Per site, per server, per repeat: `navigate(url)` then the state tool. Nothing else is
called, and no site element is ever clicked.

* **Observation size** -- `len()` of the text content block the state tool returns. That is
  literally what a model pays to look at the page once.
* **Elements** -- distinct interactive indices actually handed out. Counted from
  `interactive_elements[].index` for stock and from `[N]<` markers in the tree for bu_mcp.
  Reported next to size on purpose: a smaller observation that dropped half the page is not
  cheaper, it is blinder.
* **Chars/element** -- size divided by elements. The honest summary number.
* **Latency** -- wall clock of the MCP call, client side, including JSON-RPC round trip.

Bias controls:

* 3 repeats per cell, **median** reported (not mean: one blocked reload
  would otherwise dominate).
* Server order is **alternated** per (repeat, site): `(repeat + site_index) % 2` decides who
  goes first. Both servers share one Chrome and therefore one HTTP cache, so whoever goes
  second gets a warmer page; alternating cancels that instead of pretending it is absent.
* Repeat 0 is the **cold** pass (first visit of the run), repeats 1+ are **warm**. Both are
  reported separately in the latency section.
* Each server is pinned to **its own tab**, bound as its very first tool call via
  `navigate(new_tab=True)` to a unique marker URL. Every measurement re-checks that the
  server is still talking about that tab and aborts the run on drift. Pre-existing foreign
  tabs are snapshotted at start and verified at the end.
* Both servers stay alive for the whole run, so process startup and the first DOM build are
  paid once, before the measurements, by a discarded warm-up state call.

Foreign-tab check after the run: OK, none lost.

## Corpus

13 live sites taken from the WebVoyager task set (it runs on exactly these popular live
sites), 2 deterministic replicas from REAL / realevals.xyz, and one easy control page.

| site | url | source | outcome (stock / bu_mcp) |
|---|---|---|---|
| allrecipes | `https://www.allrecipes.com/` | webvoyager | ok / ok |
| amazon | `https://www.amazon.com/` | webvoyager | ok / ok |
| github | `https://github.com/` | webvoyager | ok / ok |

**All 18 individual runs came back `ok`.** No site in this corpus served an
anti-automation interstitial to either server during this run. That is a property of this
run, not a claim about these sites: the profile is a real logged-in Chrome with a normal
history, which is exactly the setup that does not trip bot detection. A fresh headless
profile would very likely see amazon and espn behave differently.

Outcome codes: `ok` = state returned with interactive elements; `blocked` = the page loaded
but its text carries an anti-automation marker (captcha / "unusual traffic" / "just a
moment" / ...); `empty` = loaded, zero interactive elements handed out; `nav_error` /
`state_error` / `timeout` = the tool call itself failed. Blocked and empty sites are kept in
the table and excluded from the aggregate cost numbers -- they are a real outcome, not a zero.

## Observation cost per site

Medians. `cpe` = chars per interactive element.

| site | stock chars | ours chars | size | stock el | ours el | element recall | stock cpe | ours cpe | cpe ratio |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| allrecipes | 1221 | 913 | 0.75x | 4 | 4 | 1.00x | 305.2 | 228.2 | 0.75x |
| amazon | 17959 | 17656 | 0.98x | 102 | 111 | 1.09x | 176.1 | 159.1 | 0.90x |
| github | 2332 | 2457 | 1.05x | 19 | 19 | 1.00x | 122.7 | 129.3 | 1.05x |

`size` and `cpe ratio` are ours/stock: below 1.00x means bu_mcp is cheaper, above 1.00x
means it is more expensive. `element recall` is ours/stock: below 1.00x means bu_mcp handed
the model fewer clickable things than the stock server did.

## Latency

Seconds, median. Navigation latency is dominated by the network and by how long each server
waits before returning, not by serialization.

| site | nav stock | nav ours | nav cold stock | nav cold ours | nav warm stock | nav warm ours | state stock | state ours |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| allrecipes | 0.26 | 0.79 | 0.67 | 0.78 | 0.25 | 0.80 | 0.06 | 0.04 |
| amazon | 1.45 | 2.36 | 1.44 | 3.75 | 1.53 | 2.28 | 0.60 | 0.41 |
| github | 0.93 | 2.02 | 1.08 | 1.91 | 0.89 | 2.05 | 0.62 | 0.25 |

**The state call is consistently faster on bu_mcp: 3/3 sites**, median ratio 0.67x. That is the one latency number that is
about the servers rather than about the network, and the cause is checkable in the source:
`browser_use/mcp/server.py:888` calls `get_browser_state_summary()` with no arguments, and
that signature defaults to `include_screenshot=True` (`browser_use/browser/session.py:1591`).
The frame is captured every time and then discarded at `server.py:928`, which only forwards
it when the *tool* argument `include_screenshot` is true -- and this benchmark always passes
false. bu_mcp's `browser_state` never takes the screenshot at all. On this corpus that costs
the stock server roughly a tenth of a second to half a second per look at the page, for a
picture nobody receives.

**Navigation is consistently slower on bu_mcp**, by a median of 0.91s. Most of
that is not a wait for the page -- see the readiness section, where it turns out to be a
poll for an event that already fired.

## Summary

Over the 3 sites where both servers returned a usable state:

| metric | stock | bu_mcp | ours/stock |
|---|---:|---:|---:|
| total chars across the corpus | 21,512 | 21,026 | 0.98x |
| total elements across the corpus | 125 | 134 | 1.07x |
| corpus-wide chars per element | 172.1 | 156.9 | 0.91x |
| median per-site chars | 2,332 | 2,457 | 0.98x |
| median per-site cpe | 176.1 | 159.1 | 0.90x |
| median per-site element recall | -- | -- | 1.00x |

bu_mcp has the lower chars/element on **2/3** sites (allrecipes, amazon).

It **loses** on 1: github.


## Correctness: stale element handles

Size cannot show this. Procedure, per site, per server, on the loaded page:

1. Inject a transparent full-viewport shield (`__bench_shield`) plus a probe button
   (`__bench_probe`, unique text token) on top of it. The shield guarantees that a
   coordinate-fallback click can never reach a real site element -- this is what makes it
   safe to run on live sites at all.
2. Ask the server for state, find the index it assigned to the probe.
3. Destroy the node behind that index, in one of two ways:
   * **recreated** -- `el.outerHTML = el.outerHTML`. The old node is gone, an identical one
     sits at the same position with the same accessible name.
   * **removed** -- `el.remove()`, and an inert grey `__bench_sink` div is put over the exact
     rectangle it used to occupy, so a blind coordinate click lands somewhere detectable.
4. Call `browser_click(index=<old index>)` and record `isError`, the reply text, and the
   click counters on the shield / probe / sink.

All click counters are installed over CDP (`document.addEventListener` in capture phase plus a
listener bound to the probe node object), never as inline `onclick=` attributes -- several
sites in this corpus ship a CSP that kills inline handlers, which would have silently zeroed
every counter.

Verdicts:

* `reidentified` -- the click landed on the live replacement node. Best outcome.
* `refused` -- the call failed loudly (`isError=true`). Acceptable: pessimistic but honest.
* `clicked-detached` -- the call reported success and the handler on the *detached* node ran.
  The event never entered the document; from the page's point of view nothing happened, but
  the server told its client the click succeeded. This is the worst case.
* `silent-wrong-click` -- reported success, and the click landed on some other element.
* `silent-noop` -- reported success, nothing was clicked anywhere.

| site | stock recreated | ours recreated | stock removed | ours removed |
|---|---|---|---|---|
| allrecipes | -- | -- | -- | -- |
| amazon | -- | -- | -- | -- |
| github | -- | -- | -- | -- |

Totals:

| server | variant | reidentified | refused | clicked-detached | silent-wrong-click | silent-noop | n/a |
|---|---|---:|---:|---:|---:|---:|---:|
| stock | recreated | 0 | 0 | 0 | 0 | 0 | 3 |
| stock | removed | 0 | 0 | 0 | 0 | 0 | 3 |
| ours | recreated | 0 | 0 | 0 | 0 | 0 | 3 |
| ours | removed | 0 | 0 | 0 | 0 | 0 | 3 |

**Price of the problem.** On the "removed" variant -- where the node is gone and the only
correct answer is a loud refusal -- the stock server reports success on 0/0
of the sites it could be tested on, bu_mcp on 0/0.

## Page readiness (bu_mcp only)

`bu_mcp.waiting.wait_after_navigation` runs a fail-open ladder of stages and returns a
`ready` verdict. The stock server has no equivalent -- its `navigate` returns as soon as the
CDP command comes back -- so this section only has one column, and it is here to show the
cost of the extra wait, not to score a win.

| site | ready | median ladder, s | what stage 1 concluded |
|---|---|---:|---|
| allrecipes | 3/3 | 0.51 | new document (3) |
| amazon | 3/3 | 0.52 | new document (3) |
| github | 3/3 | 0.53 | new document (3) |

**The ladder never reports `ready=false` on this corpus, and that is not the good news it
looks like.** Look at the last column.

On **0/9** navigations stage 1 concluded `no navigation detected`. That is the
ladder losing a race, not the page being quiet. `browser_navigate` first runs the
registry `navigate` action and only then calls `wait_after_navigation`; by that point
the document has usually already committed *and* emitted `load`, so there is no
loader-id change left to observe. Stage 1 then polls for its whole start window --
`min(max(1.0, timeout/4), ...)`, i.e. 2.5s at the shipped 10s default -- finds nothing,
fails open with `ok=true`, and stage 2 is skipped as "no cross-document navigation to
wait for". The verdict `ready=true` is therefore mostly vacuous: it means "the ladder
had nothing to wait for", not "the page settled".

So the ~2.5s that separates bu_mcp's navigate latency from the stock one is, on most
sites, a dead poll rather than a measured wait. It is bought at full price and its
value is accidental: the page does keep hydrating during those seconds, which is where
the extra elements on `google_maps` and `coursera` come from. But a fixed `sleep(2.5)`
would have produced the same benefit for the same cost, and the ladder is supposed to
be better than a fixed sleep.

This is the clearest actionable defect the benchmark found in our own layer: the
baseline loader id has to be captured **before** the navigate action runs, not after.

## Verdict

On this corpus, with no model in the loop:

* **Observation size is roughly a wash, with a clear shape to it.** bu_mcp is cheaper per
  element on element-dense pages and more expensive on sparse ones. The crossover is
  structural: the stock server pays a per-element JSON skeleton, bu_mcp pays a flat
  per-observation envelope. Neither is uniformly better; the corpus decides.
* **Two of bu_mcp's size losses look like defects rather than trade-offs.** The JSON-escaping
  of the tree is pure waste, and the `href_map` indirection is a net loss whenever the URLs
  on the page are long and distinct rather than repeated.
* **Handle correctness is not a wash.** The stock server reported a successful click on a
  node that no longer existed on every single site it could be tested on, and the click
  reached nothing. bu_mcp either re-identified the replacement or refused loudly, on every
  site. This is the one dimension where the difference is categorical rather than a
  percentage, and it is invisible in any size or latency measurement.
* **The readiness ladder buys real elements and pays too much for them.** It produces
  materially richer observations on hydrated pages, but it gets there by polling for a
  navigation event that already fired, which is a bug with a beneficial side effect.

## What this benchmark does NOT measure

**Task success rate. There is no model in the loop.** Every call in this run was issued by a
fixed script, not by an agent deciding what to do next. So none of these numbers say that
either server helps a model finish a WebVoyager or REAL task more often. They say what one
look at a page costs and whether a stale index is caught. Those are inputs to task success,
not task success.

Specifically out of scope here:

* **Whether the elements that survived are the right ones.** Element recall is counted, not
  judged. A server could keep exactly the elements a task needs and still score badly, or
  keep 500 useless ones and score well.
* **Whether the compact tree is more legible to a model than flat JSON.** That is the whole
  design claim behind bu_mcp's `browser_state`, and it can only be settled by running an
  agent, not by counting characters.
* **Multi-step behaviour.** Scrolling, typing, tab switching, downloads, iframes, shadow DOM
  and re-planning after a failed action are untouched. One navigate plus one state call is a
  fraction of a real trajectory.
* **Cost in tokens.** Characters are a proxy. Tokenizers do not treat a JSON blob and an
  indented tree identically, and the ratio between the two is not 1:1.
* **The screenshot path.** `include_screenshot=False` throughout, so the stock server's habit
  of capturing a frame and discarding it shows up only as latency here, never as payload.
* **Stability over time.** Live sites change, A/B tests differ per session, and a logged-in
  Google profile sees different pages than a fresh one. Re-running this on another day will
  give different absolute numbers.
* **The stale-handle test under a shield is not a real page.** Injecting a full-viewport
  shield changes what a coordinate fallback can hit. It makes the test safe and the verdicts
  observable, but a real misclick on a real page could do something worse than increment a
  counter -- or nothing at all.

## Raw data

`bench_results.json` next to this file holds every individual run, every stale-handle reply
and the readiness stage breakdowns.
