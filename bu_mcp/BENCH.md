# bu_mcp vs stock browser-use MCP: observation cost and handle correctness

Run: 2026-09-03 12:55:12 -> 2026-09-03 13:01:06 (main pass)  
Chrome: `Chrome/152.0.7977.65` headless, CDP `http://127.0.0.1:9222`  
Python: `/Users/draiqws/browser-use/.venv/bin/python`  
Repeats per site per server: 3 (median reported)  
Regenerate: `/Users/draiqws/browser-use/.venv/bin/python -m bu_mcp.bench`

This is a **re-run of the whole corpus after the three defects the first run exposed were
fixed** (commit `87207752c`: the navigate/baseline race, the JSON-escape tax on the tree,
the `href_map` placeholder rule). The methodology is byte-for-byte the one used before --
same 16 sites, 3 repeats, medians, alternating server order, cold pass separated from warm --
so the "before" columns below are directly comparable. The previous run is
2026-09-02 22:57:00 -> 23:05:00.

Two measurements are new in this report and were not in the previous one: the **hydration
stage** (cost and yield, section "Hydration"), and a **side observation about tab flags**
(section "Side observation").

## Methodology

Two MCP servers, driven over real stdio JSON-RPC by `bu_mcp/bench.py`, both attached to
the same already-running headless Chrome:

| | command | state tool | navigate | click |
|---|---|---|---|---|
| **stock** | `python -m browser_use.mcp` | `browser_get_state` | `browser_navigate` | `browser_click` |
| **ours** | `PYTHONPATH=/Users/draiqws/browser-use BU_MCP_CDP_URL=http://127.0.0.1:9222 python -m bu_mcp.server` | `browser_state` | `browser_navigate` | `browser_click` |

Per site, per server, per repeat: `navigate(url)` then the state tool. Nothing else is
called, and no site element is ever clicked.

* **Observation size** -- `len()` of the text content the state tool returns. That is
  literally what a model pays to look at the page once.
* **Elements** -- distinct interactive indices actually handed out. Counted from
  `interactive_elements[].index` for stock and from `[N]<` markers in the tree for bu_mcp.
  Reported next to size on purpose: a smaller observation that dropped half the page is not
  cheaper, it is blinder.
* **Chars/element** (`cpe`) -- size divided by elements. The headline ratio. Its blind spot
  is spelled out in the "Where the characters go" section: it charges bu_mcp for page text
  that the stock server simply does not carry.
* **Latency** -- wall clock of the MCP call, client side, including JSON-RPC round trip.

Bias controls (unchanged from the first run):

* 3 repeats per cell, **median** reported (not mean: one blocked reload would dominate).
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

Foreign-tab check after the run: OK, none lost, none changed. Harness errors: none.

**One harness change was needed for this run.** `measure_once` copied only
`ready/elapsed/stages` out of the `waiting` dict, so `navigated` -- the single field that
answers "did the ladder actually see a navigation?" -- never reached `bench_results.json`,
and the previous report had to infer it from free-text stage details. Two fields are now
persisted, `navigated` and `hydrated`. Nothing else in the harness was touched, and no
measurement path changed.

Also worth recording, because the task description assumed otherwise: `bench_results.json`
is **not** in git -- `.gitignore:28` (`*.json`) excludes it. The previous run's file existed
only in the working tree; it was copied aside before this run and the "before" columns come
from that copy.

## Corpus

13 live sites straight out of the WebVoyager task set, 2 deterministic replicas from
REAL / realevals.xyz, and one control page.

| site | url | source | outcome (stock / bu_mcp) |
|---|---|---|---|
| wikipedia | `https://en.wikipedia.org/wiki/Main_Page` | control | ok / ok |
| allrecipes | `https://www.allrecipes.com/` | webvoyager | ok / ok |
| amazon | `https://www.amazon.com/` | webvoyager | ok / ok |
| apple | `https://www.apple.com/` | webvoyager | ok / ok |
| arxiv | `https://arxiv.org/` | webvoyager | ok / ok |
| github | `https://github.com/` | webvoyager | ok / ok |
| espn | `https://www.espn.com/` | webvoyager | **empty on the cold pass** / ok |
| coursera | `https://www.coursera.org/` | webvoyager | ok / ok |
| cambridge_dict | `https://dictionary.cambridge.org/` | webvoyager | ok / ok |
| bbc_news | `https://www.bbc.com/news` | webvoyager | ok / ok |
| huggingface | `https://huggingface.co/` | webvoyager | ok / ok |
| wolframalpha | `https://www.wolframalpha.com/` | webvoyager | ok / ok |
| google_maps | `https://www.google.com/maps` | webvoyager | ok / ok |
| google_flights | `https://www.google.com/travel/flights` | webvoyager | ok / ok |
| real_omnizon | `https://real-omnizon.vercel.app/` | real | ok / ok |
| real_udriver | `https://real-udriver.vercel.app/` | real | ok / ok |

95 of 96 runs came back `ok`. No site served an anti-automation interstitial to either
server. That is a property of this run, not a claim about these sites: the profile is a
real logged-in Chrome with normal history, which is exactly the setup that does not trip
bot detection.

The single non-`ok` cell is the same one as in the previous run and reproduces exactly:
**espn, stock, cold pass -- 693 chars, 0 interactive elements, title `espn.com`.** The stock
`navigate` returns as soon as the CDP command does, so on the first, uncached visit it hands
the model the redirect shell. Warm repeats give it 35 elements. bu_mcp returned 31 elements
on all three repeats including the cold one. This is a latency/readiness trade-off showing
up as a correctness outcome: the faster navigate is faster because it sometimes returns
before there is a page.

Outcome codes: `ok` = state returned with interactive elements; `blocked` = anti-automation
marker in the text; `empty` = loaded, zero interactive elements; `nav_error` / `state_error`
/ `timeout` = the tool call itself failed. Blocked and empty cells are kept in the table and
excluded from the aggregate cost numbers.

## Observation cost per site

Medians over 3 repeats. `cpe` = chars per interactive element. `cpe before` is the same
ratio from the 2026-09-02 run, for the same site, same methodology.

| site | stock chars | ours chars | size | stock el | ours el | element recall | stock cpe | ours cpe | **cpe now** | *cpe before* |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| wikipedia | 12419 | 11851 | 0.95x | 85 | 89 | 1.05x | 146.1 | 133.2 | **0.91x** | *1.05x* |
| allrecipes | 1186 | 910 | 0.77x | 4 | 4 | 1.00x | 296.5 | 227.5 | **0.77x** | *1.51x* |
| amazon | 18687 | 17676 | 0.95x | 101 | 111 | 1.10x | 185.0 | 159.2 | **0.86x** | *1.08x* |
| apple | 2362 | 1881 | 0.80x | 19 | 18 | 0.95x | 124.3 | 104.5 | **0.84x** | *1.23x* |
| arxiv | 27496 | 16808 | 0.61x | 217 | 217 | 1.00x | 126.7 | 77.5 | **0.61x** | *0.67x* |
| github | 2389 | 2444 | 1.02x | 19 | 19 | 1.00x | 125.7 | 128.6 | **1.02x** | *1.43x* |
| espn | 6266 | 6051 | 0.97x | 35 | 31 | 0.89x | 179.0 | 195.2 | **1.09x** | *1.25x* |
| coursera | 4166 | 12787 | 3.07x | 36 | 166 | 4.61x | 115.7 | 77.0 | **0.67x** | *0.76x* |
| cambridge_dict | 3718 | 4117 | 1.11x | 28 | 28 | 1.00x | 132.8 | 147.0 | **1.11x** | *1.41x* |
| bbc_news | 3541 | 6936 | 1.96x | 27 | 34 | 1.26x | 131.1 | 204.0 | **1.56x** | *1.76x* |
| huggingface | 2474 | 1742 | 0.70x | 19 | 19 | 1.00x | 130.2 | 91.7 | **0.70x** | *1.07x* |
| wolframalpha | 10544 | 6269 | 0.59x | 84 | 85 | 1.01x | 125.5 | 73.8 | **0.59x** | *0.71x* |
| google_maps | 1277 | 2808 | 2.20x | 8 | 47 | 5.88x | 159.6 | 59.7 | **0.37x** | *0.54x* |
| google_flights | 7024 | 5755 | 0.82x | 71 | 71 | 1.00x | 98.9 | 81.1 | **0.82x** | *1.04x* |
| real_omnizon | 8812 | 6181 | 0.70x | 109 | 109 | 1.00x | 80.8 | 56.7 | **0.70x** | *0.85x* |
| real_udriver | 2040 | 1639 | 0.80x | 17 | 17 | 1.00x | 120.0 | 96.4 | **0.80x** | *1.28x* |

`size` and `cpe` ratios are ours/stock: below 1.00x means bu_mcp is cheaper. `element recall`
is ours/stock: below 1.00x means bu_mcp handed the model fewer clickable things.

Per-site change, old ratio -> new ratio:

| site | before | now | delta | site | before | now | delta |
|---|---:|---:|---:|---|---:|---:|---:|
| wikipedia | 1.05x | 0.91x | -0.14 | cambridge_dict | 1.41x | 1.11x | -0.31 |
| allrecipes | 1.51x | 0.77x | -0.74 | bbc_news | 1.76x | 1.56x | -0.21 |
| amazon | 1.08x | 0.86x | -0.22 | huggingface | 1.07x | 0.70x | -0.37 |
| apple | 1.23x | 0.84x | -0.39 | wolframalpha | 0.71x | 0.59x | -0.12 |
| arxiv | 0.67x | 0.61x | -0.05 | google_maps | 0.54x | 0.37x | -0.16 |
| github | 1.43x | 1.02x | -0.40 | google_flights | 1.04x | 0.82x | -0.22 |
| espn | 1.25x | 1.09x | -0.16 | real_omnizon | 0.85x | 0.70x | -0.14 |
| coursera | 0.76x | 0.67x | -0.09 | real_udriver | 1.28x | 0.80x | -0.47 |

**Every one of the 16 sites moved in bu_mcp's favour. None regressed.**

## Summary

Over the 16 sites where both servers returned a usable state:

| metric | stock | bu_mcp | ours/stock | *before* |
|---|---:|---:|---:|---:|
| total chars across the corpus | 114,401 | 105,855 | **0.93x** | *1.12x* |
| total elements across the corpus | 879 | 1,065 | **1.21x** | *1.22x* |
| corpus-wide chars per element | 130.1 | 99.4 | **0.76x** | *0.92x* |
| median per-site chars | 3,942 | 5,903 | 1.50x | *1.80x* |
| median per-site cpe | 128.4 | 100.5 | -- | -- |
| median of the per-site cpe ratios | -- | -- | **0.81x** | *1.07x* |
| median per-site element recall | -- | -- | 1.00x | *1.00x* |

bu_mcp has the lower chars/element on **12 of 16** sites (was 5 of 16):
wikipedia, allrecipes, amazon, apple, arxiv, coursera, huggingface, wolframalpha,
google_maps, google_flights, real_omnizon, real_udriver.

It still **loses** on 4: github (1.02x, effectively a tie), espn (1.09x),
cambridge_dict (1.11x), bbc_news (1.56x).

### Did the verdict on observation size change? Yes.

The previous report's verdict was "**a wash**": corpus-wide 0.92x in our favour but the
median site at 1.07x against us, i.e. bu_mcp was cheaper only where pages were element-dense
and more expensive on the typical page. That verdict no longer holds. Both numbers now point
the same way -- corpus 0.76x, median site 0.81x -- and the win/loss count inverted from 5:11
to 12:4. **On this corpus bu_mcp is now the cheaper observation, not a wash.**

The flip happened on 7 sites that crossed from "more expensive" to "cheaper":
**wikipedia (1.05 -> 0.91), allrecipes (1.51 -> 0.77), amazon (1.08 -> 0.86),
apple (1.23 -> 0.84), huggingface (1.07 -> 0.70), google_flights (1.04 -> 0.82),
real_udriver (1.28 -> 0.80)**. Two more moved a long way without quite crossing --
github (1.43 -> 1.02) and cambridge_dict (1.41 -> 1.11) -- and the five sites that were
already wins got cheaper still (google_maps 0.54 -> 0.37, real_omnizon 0.85 -> 0.70,
wolframalpha 0.71 -> 0.59, coursera 0.76 -> 0.67, arxiv 0.67 -> 0.61).

The cause is visible in the composition table below and is the same everywhere: the envelope
shrank from ~1,300 chars to ~400-500, and the JSON-escape tax on the tree went to zero by
construction. On a sparse page like allrecipes (4 elements) that fixed overhead **was** the
observation; removing it moves the ratio by half. That is also why the improvement is so
uniform across sites: a flat per-observation cost was removed, so the smaller the page, the
larger the relative gain.

### Why the three spot checks understated it

The spot checks (allrecipes 0.75x, github 1.05x, amazon 0.90x) were **accurate per site** --
the full run gives 0.77x, 1.02x, 0.86x for the same three, within run-to-run noise. They
misled about the corpus for a different reason: those three happened to be a pessimistic
sample. github is one of only four remaining losses, and allrecipes and amazon are mid-table.
Their median is 0.90x. The sites carrying the corpus number -- arxiv 0.61x, wolframalpha
0.59x, real_omnizon 0.70x, coursera 0.67x, google_maps 0.37x -- were all outside the sample,
and those are exactly the element-dense pages where the stock server's per-element JSON
skeleton (60-74 chars per element, every element, every observation) dominates. Sampling
three sites, two of them sparse, hid the mechanism that produces the corpus-wide number.

So the direction of the extrapolation was right and its magnitude was wrong in the
conservative direction: the true effect is bigger than the three sites suggested.

## Where the characters go

One navigate + one state per site per server, single sample (not a median), recorded by
`bench.py --sample`. `before` columns are the same measurement from the previous run.

| site | stock: per-element JSON skeleton | stock: fixed cost/element | ours: envelope | ours: escape tax | *escape before* | ours: href_map | *href_map before* |
|---|---:|---:|---:|---:|---:|---:|---:|
| wikipedia | 6232 | 73.3 | 468 | 0 | *313* | 0 | *0* |
| allrecipes | 292 | 73.0 | 396 | 0 | *18* | 0 | *0* |
| amazon | 7747 | 68.6 | 401 | 0 | *453* | 0 | *10202* |
| apple | 1265 | 66.6 | 395 | 0 | *49* | 0 | *0* |
| arxiv | 15991 | 73.7 | 384 | 0 | *681* | 0 | *0* |
| github | 1270 | 66.8 | 439 | 0 | *94* | 0 | *0* |
| espn | 2464 | 70.4 | 450 | 0 | *178* | 0 | *0* |
| coursera | 2315 | 64.3 | 457 | 0 | *1031* | 262 | *0* |
| cambridge_dict | 1908 | 68.1 | 486 | 0 | *213* | 0 | *0* |
| bbc_news | 1824 | 67.6 | 485 | 0 | *215* | 0 | *0* |
| huggingface | 1325 | 69.7 | 403 | 0 | *77* | 0 | *0* |
| wolframalpha | 5921 | 70.5 | 448 | 0 | *419* | 0 | *0* |
| google_maps | 503 | 62.9 | 393 | 0 | *136* | 0 | *0* |
| google_flights | 4534 | 63.9 | 495 | 0 | *624* | 0 | *0* |
| real_omnizon | 6434 | 59.0 | 477 | 0 | *368* | 0 | *0* |
| real_udriver | 1105 | 65.0 | 438 | 0 | *85* | 0 | *0* |
| **total** | **61,130** | -- | **7,015** | **0** | *4,954* | **262** | *10,202* |

Three things this table settles:

1. **The escape tax is gone, by construction, not by tuning.** The tree is now a separate MCP
   text block instead of a string field inside pretty-printed JSON, so its newlines and tabs
   are not re-encoded as two-character escapes. 4,954 characters across the corpus, 1,031 of
   them on coursera alone, now cost zero.
2. **The envelope went from 21,165 to 7,015 characters across 16 observations** -- from ~1,300
   to ~440 each. This is the single largest contributor to the flip, and it is the reason
   sparse pages improved the most.
3. **`href_map` is now applied where it pays.** Before, amazon spent 10,202 characters on a
   placeholder map and still had to print the URLs; now the map appears only on coursera
   (262 chars, where URLs actually repeat) and amazon carries its hrefs inline for a net
   -16% on that site's observation. This is why `href_inline` for amazon went from 1,085 to
   9,501 while the total still dropped from 20,969 to 17,660.

And one thing it explains rather than settles -- **why bbc_news is still 1.56x against us.**
On that page bu_mcp's tree is 99 lines, of which 34 carry an index and **4,006 of ~6,400
characters sit in lines with no index at all**: headlines, standfirsts, timestamps, image alt
text. The stock server's 27 elements are mostly `{"index": 54, "tag": "a", "text": "",
"href": "/news/articles/c17jqp0xzpzo"}` -- 54 characters of element text *in total* across all
27 elements. So the "cheaper" observation on bbc is a list of anchors with no indication of
where any of them lead. `chars/element` scores that as a win for stock. Whether it is one
cannot be decided by counting characters -- it needs a model in the loop, which this benchmark
does not have. The same effect, weaker, is behind cambridge_dict and espn.

## Latency

Seconds, median. Navigation latency is dominated by the network and by how long each server
waits before returning, not by serialization.

| site | nav stock | nav ours | nav cold stock | nav cold ours | nav warm stock | nav warm ours | state stock | state ours |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| wikipedia | 0.36 | 1.09 | 1.02 | 1.01 | 0.36 | 1.17 | 0.22 | 0.30 |
| allrecipes | 0.23 | 1.03 | 0.23 | 1.03 | 0.38 | 1.05 | 0.05 | 0.02 |
| amazon | 1.36 | 2.29 | 1.89 | 2.07 | 1.31 | 2.32 | 0.52 | 0.37 |
| apple | 0.83 | 2.21 | 0.48 | 3.28 | 0.99 | 1.80 | 0.27 | 0.16 |
| arxiv | 0.56 | 0.95 | 0.56 | 0.84 | 0.61 | 1.02 | 0.13 | 0.11 |
| github | 1.75 | 2.87 | 0.79 | 3.84 | 2.05 | 2.47 | 0.48 | 0.23 |
| espn | 8.33 | 9.42 | 2.03 | 17.92 | 8.51 | 9.37 | 0.28 | 0.26 |
| coursera | 1.58 | 2.27 | 1.60 | 3.31 | 1.45 | 2.22 | 0.50 | 0.35 |
| cambridge_dict | 1.12 | 2.21 | 8.79 | 8.94 | 1.12 | 1.95 | 0.21 | 0.15 |
| bbc_news | 0.78 | 2.40 | 0.60 | 4.86 | 0.79 | 2.25 | 0.40 | 0.16 |
| huggingface | 1.67 | 1.41 | 1.77 | 1.32 | 1.18 | 1.57 | 0.18 | 0.17 |
| wolframalpha | 0.85 | 2.02 | 0.82 | 2.02 | 0.93 | 2.07 | 0.11 | 0.14 |
| google_maps | 0.87 | 2.48 | 0.73 | 2.37 | 0.99 | 3.07 | 0.14 | 0.09 |
| google_flights | 1.84 | 2.50 | 1.84 | 1.91 | 1.91 | 3.05 | 0.25 | 0.29 |
| real_omnizon | 0.71 | 1.01 | 1.44 | 1.01 | 0.56 | 1.10 | 0.18 | 0.06 |
| real_udriver | 0.39 | 1.00 | 0.32 | 1.19 | 0.42 | 0.94 | 0.07 | 0.03 |

Corpus medians: navigate 0.94s stock / 2.07s ours (**was 1.15s / 4.02s**); state 0.22s stock /
0.17s ours. Cold and warm are within noise of each other for both servers on both calls.

**The state call is still faster on bu_mcp: 13/16 sites**, median ratio 0.69x (was 16/16 at
0.59x). The cause is unchanged and checkable in the source: `browser_use/mcp/server.py:888`
calls `get_browser_state_summary()` with no arguments, and that signature defaults to
`include_screenshot=True` (`browser_use/browser/session.py:1591`). The frame is captured every
time and discarded at `server.py:928` unless the *tool* argument asks for it -- and this
benchmark always passes false. bu_mcp never takes the screenshot at all. The three sites where
we are now slower (wikipedia, wolframalpha, google_flights) are within 0.05s and are pages
where our tree build is doing more work than the stock element list.

**Navigation is still slower on bu_mcp, but the gap fell from +2.84s to +0.86s per site
(median).** Of the remaining gap, ~0.52s is the hydration stage (below) and the rest is that
`browser_navigate` returns after the load event rather than after the CDP command. The espn
cold cell (17.9s vs 2.0s) is the extreme case and the same one that produced the stock
server's empty observation: stock returned early from a page that was still redirecting.

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

All click counters are installed over CDP (`document.addEventListener` in capture phase plus
a listener bound to the probe node object), never as inline `onclick=` attributes -- several
sites here ship a CSP that kills inline handlers, which would silently zero every counter.

Verdicts: `reidentified` (clicked the live replacement -- best), `refused` (loud
`isError=true` -- acceptable), `clicked-detached` (reported success, handler ran on a node
that is no longer in the document -- worst), `silent-wrong-click`, `silent-noop`.

**Nothing changed here, in either direction.** All 64 attempts (16 sites x 2 servers x 2
variants) reproduce the previous run exactly:

| server | variant | reidentified | refused | clicked-detached | silent-wrong-click | silent-noop |
|---|---|---:|---:|---:|---:|---:|
| stock | recreated | 0 | 0 | **16** | 0 | 0 |
| stock | removed | 0 | 0 | **16** | 0 | 0 |
| ours | recreated | **16** | 0 | 0 | 0 | 0 |
| ours | removed | 0 | **16** | 0 | 0 | 0 |

Per site the pattern is uniform: stock `clicked-detached` / `clicked-detached`, ours
`reidentified` / `refused`, on all 16 sites.

**16/16 silent false successes for the stock server, unchanged.** On the "removed" variant --
where the node is gone and the only correct answer is a loud refusal -- stock replies
`Clicked element 110503` with `isError=false` while the click counters show `probe=0,
shield=0, sink=0, other=0, probeDirect=1`: the handler ran on the detached node object, the
event never entered the document, and the page saw nothing. bu_mcp fails the call with a
`STALE ELEMENT HANDLE` message that names every re-identification level it tried
(backendNodeId, xpath, accessible name) on 16/16, and `stale_flag` is set on all 16.

This remains the one dimension where the difference is categorical rather than a percentage,
and it is invisible in any size or latency measurement.

## Page readiness: is a navigation actually recognised?

`bu_mcp.waiting.wait_after_navigation` runs a fail-open ladder and returns a `ready` verdict.
The stock server has no equivalent, so this section has one column.

The previous run's headline defect was that the baseline `loaderId` was captured *after* the
navigate action, so by the time the ladder looked, the document had already committed and
there was no change left to observe. **That is fixed and the fix is confirmed on the whole
corpus:**

| | before (2026-09-02) | now |
|---|---|---|
| navigations where stage 1 saw a real navigation | **2 / 48** (both `same-document`, both google_maps) | **48 / 48** (all `new document: loaderId X -> Y`) |
| `navigated` field true | not persisted by the harness | 48 / 48 |
| stage 1 (`navigation_start`) median cost | ~2.5s of dead polling | **0.001s** |
| stage 2 (`lifecycle_load`) | skipped, "no cross-document navigation to wait for" | runs, resolves in 0.000s (the `load` event is already in the buffer) |
| `ready=true` | vacuous ("nothing to wait for") | 48/48, and now means "document committed, load seen, DOM stopped moving" |

The task brief remembered this as "3 of 48"; the previous report's own table says 46/48
concluded `no navigation detected` and 2/48 saw a `same-document` navigation, so the real
before-number is **2/48**. Either way it is now 48/48.

The 2.5s that used to separate our navigate from the stock one was, as the previous report
suspected, a dead poll. It is gone: stage 1 costs a millisecond, and what remains of the
latency gap is the hydration budget, which is now an explicit line item rather than a side
effect.

Per-site ladder breakdown (medians over 3 repeats, `ours` only):

| site | ready | navigated | hydrated | nav_start s | lifecycle_load s | hydration s | ladder total s |
|---|---|---|---|---:|---:|---:|---:|
| wikipedia | 3/3 | 3/3 | 3/3 | 0.001 | 0.000 | 0.514 | 0.516 |
| allrecipes | 3/3 | 3/3 | 3/3 | 0.000 | 0.000 | 0.506 | 0.507 |
| amazon | 3/3 | 3/3 | 3/3 | 0.001 | 0.000 | 0.519 | 0.520 |
| apple | 3/3 | 3/3 | 3/3 | 0.001 | 0.000 | 0.621 | 0.622 |
| arxiv | 3/3 | 3/3 | 3/3 | 0.001 | 0.000 | 0.512 | 0.514 |
| github | 3/3 | 3/3 | 3/3 | 0.001 | 0.000 | 0.525 | 0.527 |
| espn | 3/3 | 3/3 | 3/3 | 0.001 | 0.000 | 0.517 | 0.518 |
| coursera | 3/3 | 3/3 | 3/3 | 0.001 | 0.000 | 0.524 | 0.524 |
| cambridge_dict | 3/3 | 3/3 | 3/3 | 0.001 | 0.000 | 0.516 | 0.517 |
| bbc_news | 3/3 | 3/3 | 3/3 | 0.001 | 0.000 | 0.518 | 0.519 |
| huggingface | 3/3 | 3/3 | 3/3 | 0.001 | 0.000 | 0.516 | 0.517 |
| wolframalpha | 3/3 | 3/3 | 3/3 | 0.001 | 0.000 | 0.518 | 0.519 |
| google_maps | 3/3 | 3/3 | 3/3 | 0.002 | 0.000 | **1.533** | 1.534 |
| google_flights | 3/3 | 3/3 | 3/3 | 0.001 | 0.000 | 0.522 | 0.522 |
| real_omnizon | 3/3 | 3/3 | 3/3 | 0.001 | 0.000 | 0.513 | 0.515 |
| real_udriver | 3/3 | 3/3 | 3/3 | 0.001 | 0.000 | 0.509 | 0.511 |

No hydration stage failed anywhere in the run (0 of 144 stage records with `ok=false`).

## Hydration: what it costs and what it buys

New in this report. The hydration stage (`BU_MCP_HYDRATE_TIMEOUT`, default 3s) runs
`wait_for_page_ready` after the navigation has completed: loading indicators disappear ->
network quiet -> `MutationObserver` sees a 300ms quiet window. It is the deliberate
replacement for the accidental 2.5s wait that the old race used to produce.

**Cost** is the `hydration s` column above: a median of **0.518s** per navigation, 29.1s
across all 48 navigations of the main run. The floor is structural -- the `mutation_quiet`
stage needs its 300ms quiet window before it can conclude anything, and the other two stages
resolve almost instantly (`no visible loading indicators`, `network quiet,
readyState=complete` on 48/48). Only google_maps costs materially more (1.53s).

**Yield** was measured separately, with a dedicated A/B: for each site, `navigate(hydrate=0)`
vs `navigate()` (3s budget), 2 repeats each, order alternated, always arriving from a neutral
page so the previous variant's hydration cannot be inherited.

| site | el hydrate=0 | el hydrate=3s | delta | chars hydrate=0 | chars hydrate=3s | hydration s |
|---|---:|---:|---:|---:|---:|---:|
| wikipedia | 89 | 89 | +0 | 11768 | 11769 | 0.52 |
| allrecipes | 4 | 4 | +0 | 844 | 844 | 0.51 |
| amazon | 111 | 111 | +0 | 17760 | 17750 | 0.52 |
| apple | 18 | 18 | 0 | 1838 | 1830 | 0.57 |
| arxiv | 217 | 217 | +0 | 16748 | 16749 | 0.52 |
| github | 19 | 19 | +0 | 2348 | 2398 | 0.53 |
| espn | 31 | 31 | +0 | 5988 | 5987 | 0.52 |
| **coursera** | 101 | **166** | **+65** | 8065 | 12736 | 0.76 |
| cambridge_dict | 28 | 28 | +0 | 4042 | 4042 | 0.51 |
| bbc_news | 34 | 34 | +0 | 6876 | 6878 | 0.52 |
| huggingface | 19 | 19 | +0 | 1672 | 1677 | 0.62 |
| wolframalpha | 85 | 85 | +0 | 6171 | 6198 | 0.51 |
| **google_maps** | 8 | **47** | **+39** | 975 | 2744 | 1.49 |
| google_flights | 71 | 71 | +0 | 5576 | 5574 | 0.51 |
| real_omnizon | 109 | 109 | +0 | 6107 | 6107 | 0.51 |
| real_udriver | 17 | 17 | +0 | 1578 | 1580 | 0.51 |
| **total** | **961.5** | **1065** | **+10.8%** | | | **9.6s** |

Read this honestly: **the hydration stage pays for itself on 2 sites out of 16 and is dead
weight on the other 14.** On google_maps it is the difference between 8 handles and 47 -- the
whole map UI -- and on coursera between a shell and 166 handles. Everywhere else it costs
~0.5s and returns zero extra elements; the largest non-zero movement elsewhere is +50
characters on github, which is page churn, not hydration.

Two caveats that cut against reading this as a clean win:

* **coursera is bimodal.** Of the two `hydrate=0` samples, one returned 166 elements anyway
  and the other 36; the median in the table (101) is the average of the two. So on coursera
  hydration converts a coin flip into a certainty rather than adding elements that were never
  there. google_maps is not bimodal: 8 in both `hydrate=0` samples, 47 in both `hydrate=3s`
  samples.
* **The composition pass, a single un-repeated sample taken later the same session, caught
  google_maps at 8 elements with hydration enabled** (1,044 chars, the un-hydrated shape).
  So the +39 is not guaranteed on every visit even with the stage running.

Against the alternative it replaced, though, the trade is clearly better than before: the old
code paid ~2.5s on *every* navigation for the same benefit; the ladder pays 0.5s on the 14
sites that do not need it and 0.8-1.5s on the two that do, and it reports which happened
(`hydrated`).

## Side observation: two tabs reported as `current` at the same time

Not a benchmark metric; recorded because it was raised and it reproduces on demand. Left
unfixed on purpose.

`bu_mcp/state.py:502` decides which tab is the current one by value comparison:

```python
'current': tab.url == state.url and tab.title == state.title,
```

There is no tie-break, so **any two tabs sharing a URL and title are both flagged**. Forced
repro (own tabs only, closed afterwards): open `https://example.com/`, then open it again with
`new_tab=True`, then call `browser_state` --

```
current=None  url='https://myaccount.google.com/'
current=None  url='https://mail.google.com/mail/u/0/#spam'
current=True  url='https://example.com/'
current=True  url='https://example.com/'
>>> tabs marked current: 2 of 4
```

It is not a contrived condition. It happened incidentally during this very benchmark: while
both servers were parked on `https://www.bbc.com/news` in their own tabs, our own state
output listed `{"tab_id":"B4AA",...,"current":true},{"tab_id":"639D",...,"current":true}` --
two "current" tabs in a single observation a model would have to act on. Any workflow that
opens the same page twice (a duplicated tab, two search results from one origin, a retry in a
new tab) hits it.

The authoritative answer is available and not used: `session.agent_focus_target_id` is what
the rest of bu_mcp resolves against, and each tab object already carries `target_id` (the
serializer even shortens it into `tab_id` for the header). Comparing ids instead of
(url, title) would make the flag exact and would also stop depending on `title`, which is
frequently empty.

## Verdict

On this corpus, with no model in the loop:

* **Observation size is no longer a wash: bu_mcp is cheaper.** Corpus-wide 0.76x
  chars/element (was 0.92x), median site 0.81x (was 1.07x), cheaper on 12/16 sites (was 5/16),
  with equal or better element recall on 14/16. All 16 sites improved; none regressed. The
  cause is a removed flat overhead (envelope ~1,300 -> ~440 chars, escape tax 4,954 -> 0) plus
  an `href_map` rule that now only fires when it pays.
* **The remaining four losses are structural, not defects.** github is a tie at 1.02x. On
  bbc_news, cambridge_dict and espn our tree carries the page's visible text (4,006 of 6,400
  characters on bbc are non-indexed lines) while the stock server ships anchors with empty
  `text` fields. `chars/element` charges us for that text and credits stock for omitting it.
  Which is actually better for a model is outside what this benchmark can answer.
* **Handle correctness is unchanged and still categorical.** 16/16 silent false successes for
  the stock server on both stale variants; 16/16 correct behaviour from bu_mcp (re-identify
  or refuse loudly).
* **The readiness ladder now does what it claimed.** 48/48 navigations recognised (was 2/48),
  stage 1 costs 0.001s instead of 2.5s of dead polling, and `ready=true` is a statement about
  the page rather than about having nothing to wait for. Navigate latency overhead dropped
  from +2.84s to +0.86s per site.
* **Hydration is honest but narrow.** ~0.52s per navigation, +10.8% elements corpus-wide,
  and all of that comes from 2 of 16 sites. On the other 14 it is a pure 0.5s tax. It is
  strictly better than the accidental 2.5s it replaced, and it is now measurable and
  switchable (`hydrate=0`), which is the actual improvement.
* **The state call stays cheaper on latency** (13/16, median 0.69x) for the same reason as
  before: the stock server captures a screenshot on every state call and throws it away.

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
  agent, not by counting characters. This run makes the point sharper, not weaker: on
  bbc_news the two servers disagree about whether an observation should contain the page's
  text at all, and `chars/element` cannot arbitrate that.
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

`bench_results.json` next to this file holds every individual run, every stale-handle reply,
the readiness stage breakdowns and the composition sample. It is gitignored, so it exists
only in the working tree.

The hydration A/B is not part of `bench.py` (the harness was deliberately left alone) and was
run as a standalone script against the same servers and the same tab-pinning helpers; its
per-run rows are the table in the "Hydration" section.
