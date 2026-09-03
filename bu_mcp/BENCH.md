# bu_mcp vs stock browser-use MCP: observation cost and handle correctness

Run: 2026-09-02 22:57:00 -> 2026-09-02 23:05:00  
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
| wikipedia | `https://en.wikipedia.org/wiki/Main_Page` | control | ok / ok |
| allrecipes | `https://www.allrecipes.com/` | webvoyager | ok / ok |
| amazon | `https://www.amazon.com/` | webvoyager | ok / ok |
| apple | `https://www.apple.com/` | webvoyager | ok / ok |
| arxiv | `https://arxiv.org/` | webvoyager | ok / ok |
| github | `https://github.com/` | webvoyager | ok / ok |
| espn | `https://www.espn.com/` | webvoyager | ok / ok |
| coursera | `https://www.coursera.org/` | webvoyager | ok / ok |
| cambridge_dict | `https://dictionary.cambridge.org/` | webvoyager | ok / ok |
| bbc_news | `https://www.bbc.com/news` | webvoyager | ok / ok |
| huggingface | `https://huggingface.co/` | webvoyager | ok / ok |
| wolframalpha | `https://www.wolframalpha.com/` | webvoyager | ok / ok |
| google_maps | `https://www.google.com/maps` | webvoyager | ok / ok |
| google_flights | `https://www.google.com/travel/flights` | webvoyager | ok / ok |
| real_omnizon | `https://real-omnizon.vercel.app/` | real | ok / ok |
| real_udriver | `https://real-udriver.vercel.app/` | real | ok / ok |

**1 of 96 individual runs did not come back `ok`:**

* espn/stock/rep0 -> `empty` (693 chars, 0 elements)

These are kept in the record rather than dropped, and they do not enter the per-site
medians when a site was `ok` on the majority of its repeats.

Outcome codes: `ok` = state returned with interactive elements; `blocked` = the page loaded
but its text carries an anti-automation marker (captcha / "unusual traffic" / "just a
moment" / ...); `empty` = loaded, zero interactive elements handed out; `nav_error` /
`state_error` / `timeout` = the tool call itself failed. Blocked and empty sites are kept in
the table and excluded from the aggregate cost numbers -- they are a real outcome, not a zero.

## Observation cost per site

Medians. `cpe` = chars per interactive element.

| site | stock chars | ours chars | size | stock el | ours el | element recall | stock cpe | ours cpe | cpe ratio |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| wikipedia | 11057 | 12279 | 1.11x | 76 | 80 | 1.05x | 145.5 | 153.5 | 1.05x |
| allrecipes | 1186 | 1787 | 1.51x | 4 | 4 | 1.00x | 296.5 | 446.8 | 1.51x |
| amazon | 18927 | 21528 | 1.14x | 108 | 114 | 1.06x | 175.2 | 188.8 | 1.08x |
| apple | 2354 | 2751 | 1.17x | 19 | 18 | 0.95x | 123.9 | 152.8 | 1.23x |
| arxiv | 27497 | 18295 | 0.67x | 217 | 217 | 1.00x | 126.7 | 84.3 | 0.67x |
| github | 2389 | 3411 | 1.43x | 19 | 19 | 1.00x | 125.7 | 179.5 | 1.43x |
| espn | 5838 | 6879 | 1.18x | 34 | 32 | 0.94x | 171.7 | 215.0 | 1.25x |
| coursera | 4166 | 15083 | 3.62x | 36 | 172 | 4.78x | 115.7 | 87.7 | 0.76x |
| cambridge_dict | 3718 | 5255 | 1.41x | 28 | 28 | 1.00x | 132.8 | 187.7 | 1.41x |
| bbc_news | 3542 | 7870 | 2.22x | 27 | 34 | 1.26x | 131.2 | 231.5 | 1.76x |
| huggingface | 2474 | 2652 | 1.07x | 19 | 19 | 1.00x | 130.2 | 139.6 | 1.07x |
| wolframalpha | 10544 | 7713 | 0.73x | 84 | 87 | 1.04x | 125.5 | 88.7 | 0.71x |
| google_maps | 1277 | 4018 | 3.15x | 8 | 47 | 5.88x | 159.6 | 85.5 | 0.54x |
| google_flights | 7048 | 7296 | 1.04x | 71 | 71 | 1.00x | 99.3 | 102.8 | 1.04x |
| real_omnizon | 8812 | 7454 | 0.85x | 109 | 109 | 1.00x | 80.8 | 68.4 | 0.85x |
| real_udriver | 2040 | 2605 | 1.28x | 17 | 17 | 1.00x | 120.0 | 153.2 | 1.28x |

`size` and `cpe ratio` are ours/stock: below 1.00x means bu_mcp is cheaper, above 1.00x
means it is more expensive. `element recall` is ours/stock: below 1.00x means bu_mcp handed
the model fewer clickable things than the stock server did.

## Latency

Seconds, median. Navigation latency is dominated by the network and by how long each server
waits before returning, not by serialization.

| site | nav stock | nav ours | nav cold stock | nav cold ours | nav warm stock | nav warm ours | state stock | state ours |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| wikipedia | 0.40 | 3.61 | 0.35 | 3.14 | 0.52 | 3.66 | 0.25 | 0.20 |
| allrecipes | 0.24 | 3.04 | 0.22 | 3.29 | 0.58 | 2.92 | 0.07 | 0.02 |
| amazon | 1.71 | 4.66 | 2.55 | 4.66 | 1.66 | 4.72 | 0.50 | 0.36 |
| apple | 0.89 | 4.08 | 0.89 | 4.59 | 1.28 | 4.07 | 0.32 | 0.16 |
| arxiv | 0.43 | 3.12 | 0.43 | 3.10 | 0.47 | 3.14 | 0.12 | 0.10 |
| github | 1.14 | 4.30 | 0.97 | 4.30 | 1.44 | 4.21 | 0.72 | 0.18 |
| espn | 8.29 | 11.32 | 0.58 | 20.42 | 8.41 | 11.21 | 0.30 | 0.19 |
| coursera | 1.75 | 4.46 | 1.89 | 4.26 | 1.60 | 4.51 | 0.42 | 0.40 |
| cambridge_dict | 1.37 | 3.85 | 1.21 | 3.87 | 1.42 | 3.77 | 0.25 | 0.10 |
| bbc_news | 1.50 | 4.51 | 0.87 | 4.04 | 1.69 | 4.66 | 0.34 | 0.24 |
| huggingface | 1.61 | 3.93 | 1.61 | 3.54 | 1.44 | 4.25 | 0.26 | 0.15 |
| wolframalpha | 1.03 | 3.78 | 0.83 | 3.78 | 1.48 | 3.97 | 0.17 | 0.08 |
| google_maps | 1.42 | 4.34 | 1.78 | 4.56 | 1.29 | 4.11 | 0.22 | 0.08 |
| google_flights | 1.46 | 4.33 | 1.46 | 4.09 | 1.61 | 4.83 | 0.35 | 0.21 |
| real_omnizon | 0.62 | 3.42 | 0.70 | 3.31 | 0.58 | 3.52 | 0.12 | 0.06 |
| real_udriver | 0.48 | 3.06 | 0.48 | 3.06 | 0.63 | 3.12 | 0.08 | 0.06 |

**The state call is consistently faster on bu_mcp: 16/16 sites**, median ratio 0.59x. That is the one latency number that is
about the servers rather than about the network, and the cause is checkable in the source:
`browser_use/mcp/server.py:888` calls `get_browser_state_summary()` with no arguments, and
that signature defaults to `include_screenshot=True` (`browser_use/browser/session.py:1591`).
The frame is captured every time and then discarded at `server.py:928`, which only forwards
it when the *tool* argument `include_screenshot` is true -- and this benchmark always passes
false. bu_mcp's `browser_state` never takes the screenshot at all. On this corpus that costs
the stock server roughly a tenth of a second to half a second per look at the page, for a
picture nobody receives.

**Navigation is consistently slower on bu_mcp**, by a median of 2.83s. Most of
that is not a wait for the page -- see the readiness section, where it turns out to be a
poll for an event that already fired.

## Summary

Over the 16 sites where both servers returned a usable state:

| metric | stock | bu_mcp | ours/stock |
|---|---:|---:|---:|
| total chars across the corpus | 112,870 | 126,876 | 1.12x |
| total elements across the corpus | 876 | 1,068 | 1.22x |
| corpus-wide chars per element | 128.8 | 118.8 | 0.92x |
| median per-site chars | 3,942 | 7,088 | 1.17x |
| median per-site cpe | 128.4 | 153.0 | 1.07x |
| median per-site element recall | -- | -- | 1.00x |

bu_mcp has the lower chars/element on **5/16** sites (arxiv, coursera, wolframalpha, google_maps, real_omnizon).

It **loses** on 11: wikipedia, allrecipes, amazon, apple, github, espn, cambridge_dict, bbc_news, huggingface, google_flights, real_udriver.

**Do not read only the first table.** The two summary rows point in opposite directions
and both are true: corpus-wide chars-per-element favours bu_mcp (0.92x) while the
median site favours the stock server (1.07x). There is no contradiction. bu_mcp wins on
the pages with many interactive elements, and those pages contribute most of the
characters in the corpus total. It loses on the pages with few, and those are the
majority by count. Which number matters depends on what an agent actually looks at: if
the workload is dense listing and search-result pages, the corpus number is the relevant
one; if it is a long walk through sparse app screens, the median is.

On **coursera, google_maps** bu_mcp hands the model substantially more interactive elements than
the stock server does -- not because it serializes differently, but because it looked
later. Its `navigate` returns seconds after the stock one, and on a JavaScript-hydrated
page those seconds are the difference between a shell and a rendered app. The stock
server is not being frugal on these pages, it is being early. That is a cost the
chars-per-element ratio flatters rather than penalises, because a state call that
reports 8 elements on a page that has 47 looks cheap.


## Where the characters go

A second single-shot pass (`--sample`) records the size breakdown of one state payload per
site. Bytes. `meta` is everything outside the element payload (`url`, `title`, `tabs`,
`viewport`, `scroll`, JSON braces).

| site | n | stock total | skeleton | href | text | meta | ours total | tree | json-escape | href_map | meta |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| wikipedia | 76/80 | 11063 | 5507 | 3128 | 782 | 1646 | 12310 | 10678 | 313 | 0 | 1315 |
| allrecipes | 4/4 | 1215 | 292 | 106 | 72 | 745 | 1758 | 515 | 18 | 0 | 1221 |
| amazon | 113/112 | 19758 | 7634 | 9595 | 665 | 1864 | 20969 | 8989 | 453 | 10202 | 1323 |
| apple | 19/18 | 2356 | 1265 | 211 | 31 | 849 | 2747 | 1496 | 49 | 0 | 1198 |
| arxiv | 217/217 | 27499 | 15992 | 4618 | 3664 | 3225 | 18294 | 16423 | 681 | 0 | 1186 |
| github | 19/19 | 2326 | 1270 | 52 | 48 | 956 | 3463 | 2019 | 94 | 0 | 1346 |
| espn | 34/32 | 5842 | 2406 | 1494 | 758 | 1184 | 6879 | 5340 | 178 | 0 | 1357 |
| coursera | 36/172 | 4175 | 2315 | 537 | 165 | 1158 | 15085 | 12718 | 1031 | 0 | 1332 |
| cambridge_dict | 28/28 | 3718 | 1913 | 379 | 299 | 1127 | 5254 | 3633 | 213 | 0 | 1404 |
| bbc_news | 27/34 | 3526 | 1825 | 469 | 54 | 1178 | 7880 | 6205 | 215 | 0 | 1456 |
| huggingface | 19/19 | 2474 | 1325 | 131 | 73 | 945 | 2652 | 1355 | 77 | 0 | 1216 |
| wolframalpha | 84/87 | 10511 | 5921 | 2029 | 874 | 1687 | 7691 | 5956 | 419 | 0 | 1312 |
| google_maps | 8/47 | 1277 | 503 | 0 | 3 | 771 | 4018 | 2431 | 136 | 0 | 1447 |
| google_flights | 71/71 | 7065 | 4544 | 151 | 782 | 1588 | 7281 | 5246 | 624 | 0 | 1407 |
| real_omnizon | 109/109 | 8812 | 6434 | 1 | 454 | 1923 | 7454 | 5702 | 368 | 0 | 1380 |
| real_udriver | 17/17 | 2077 | 1105 | 10 | 39 | 923 | 2567 | 1213 | 85 | 0 | 1265 |

Three separate effects fall out of this table, and together they explain every row of the
cost table above.

**1. Stock pays a JSON skeleton per element; bu_mcp does not.** Each entry in
`interactive_elements` costs `{`, four quoted key names, commas and two levels of
pretty-print indent before any content. Measured:

* 68 bytes of pure JSON skeleton per element (median over the corpus),
  59 to 74 across sites.
* on `real_omnizon` the skeleton alone is 6,434 of 8,812 bytes = 73% of the whole observation.

bu_mcp's equivalent is one tab-indented line per element, which is why it wins outright on
the element-dense pages (arxiv, wolframalpha, real_omnizon).

**2. bu_mcp pays a flat envelope, and on sparse pages the envelope is the page.** Its
`metadata` column barely moves with page size. Compare:

* bu_mcp metadata: 1,186 to 1,456 bytes, median 1,328.
* on `allrecipes` the metadata (1,221) is **larger than the element tree itself** (515). The page has 4 interactive elements; there is nothing to amortise it over.
* on `real_udriver` the metadata (1,265) is **larger than the element tree itself** (1,213). The page has 17 interactive elements; there is nothing to amortise it over.

**3. The tree is a JSON string field, so every newline and tab is re-encoded.** bu_mcp
returns a text tree wrapped in pretty-printed JSON, which means each `\n` and `\t` costs two
characters instead of one on the wire. The `json-escape` column is that surcharge. It is
pure format overhead: the model gains nothing from it. This is the one number in the whole
report that looks like a straightforward bug rather than a trade-off -- the tree could be
returned as its own text content block next to a small JSON header and the surcharge would
go to zero.

**4. `href_map` placeholdering can backfire.** bu_mcp replaces long URLs with
`{{_<hash>}}` and moves the real URL into an `href_map`. That pays off when the same URL
repeats. On `amazon` it does not: the map costs 10,202 bytes on its own, on top of the
placeholders left in the tree, against 9,595 bytes of plain inline hrefs in the stock
payload. Distinct long URLs are exactly the case where the indirection loses.

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
| wikipedia | clicked-detached | reidentified | clicked-detached | refused (STALE) |
| allrecipes | clicked-detached | reidentified | clicked-detached | refused (STALE) |
| amazon | clicked-detached | reidentified | clicked-detached | refused (STALE) |
| apple | clicked-detached | reidentified | clicked-detached | refused (STALE) |
| arxiv | clicked-detached | reidentified | clicked-detached | refused (STALE) |
| github | clicked-detached | reidentified | clicked-detached | refused (STALE) |
| espn | clicked-detached | reidentified | clicked-detached | refused (STALE) |
| coursera | clicked-detached | reidentified | clicked-detached | refused (STALE) |
| cambridge_dict | clicked-detached | reidentified | clicked-detached | refused (STALE) |
| bbc_news | clicked-detached | reidentified | clicked-detached | refused (STALE) |
| huggingface | clicked-detached | reidentified | clicked-detached | refused (STALE) |
| wolframalpha | clicked-detached | reidentified | clicked-detached | refused (STALE) |
| google_maps | clicked-detached | reidentified | clicked-detached | refused (STALE) |
| google_flights | clicked-detached | reidentified | clicked-detached | refused (STALE) |
| real_omnizon | clicked-detached | reidentified | clicked-detached | refused (STALE) |
| real_udriver | clicked-detached | reidentified | clicked-detached | refused (STALE) |

Totals:

| server | variant | reidentified | refused | clicked-detached | silent-wrong-click | silent-noop | n/a |
|---|---|---:|---:|---:|---:|---:|---:|
| stock | recreated | 0 | 0 | 16 | 0 | 0 | 0 |
| stock | removed | 0 | 0 | 16 | 0 | 0 | 0 |
| ours | recreated | 16 | 0 | 0 | 0 | 0 | 0 |
| ours | removed | 0 | 16 | 0 | 0 | 0 | 0 |

**Price of the problem.** On the "removed" variant -- where the node is gone and the only
correct answer is a loud refusal -- the stock server reports success on 16/16 = 100%
of the sites it could be tested on, bu_mcp on 0/16 = 0%.

## Page readiness (bu_mcp only)

`bu_mcp.waiting.wait_after_navigation` runs a fail-open ladder of stages and returns a
`ready` verdict. The stock server has no equivalent -- its `navigate` returns as soon as the
CDP command comes back -- so this section only has one column, and it is here to show the
cost of the extra wait, not to score a win.

| site | ready | median ladder, s | what stage 1 concluded |
|---|---|---:|---|
| wikipedia | 3/3 | 2.55 | no navigation detected (3) |
| allrecipes | 3/3 | 2.52 | no navigation detected (3) |
| amazon | 3/3 | 2.55 | no navigation detected (3) |
| apple | 3/3 | 2.53 | no navigation detected (3) |
| arxiv | 3/3 | 2.53 | no navigation detected (3) |
| github | 3/3 | 2.54 | no navigation detected (3) |
| espn | 3/3 | 2.50 | no navigation detected (3) |
| coursera | 3/3 | 2.52 | no navigation detected (3) |
| cambridge_dict | 3/3 | 2.53 | no navigation detected (3) |
| bbc_news | 3/3 | 2.55 | no navigation detected (3) |
| huggingface | 3/3 | 2.53 | no navigation detected (3) |
| wolframalpha | 3/3 | 2.53 | no navigation detected (3) |
| google_maps | 3/3 | 2.31 | same-document navigation (2), no navigation detected (1) |
| google_flights | 3/3 | 2.56 | no navigation detected (3) |
| real_omnizon | 3/3 | 2.52 | no navigation detected (3) |
| real_udriver | 3/3 | 2.53 | no navigation detected (3) |

**The ladder never reports `ready=false` on this corpus, and that is not the good news it
looks like.** Look at the last column.

On **46/48** navigations stage 1 concluded `no navigation detected`. That is the
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
