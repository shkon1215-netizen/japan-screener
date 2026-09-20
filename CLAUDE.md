# CLAUDE.md — Japan (TSE) Valuation Screener

Screens Prime + Standard + Growth for stocks ≥20% below industry-peer median on
PER, PBR, EV/EBITDA. Gates: market cap ≥ USD 600M, median daily traded value
≥ USD 4M. Third market after Korea and the UK; `screener.py` ported unchanged.

## Run order

```bash
python test_japan.py      # offline logic check — must pass, no network needed
python check_setup.py     # tests all nine live calls individually
python main_jp.py -v      # full run, 8–12 min cold, ~1 min warm

# fundamentals source: auto probes kabutan and falls back to yfinance.
# kabutan is blocked from datacenter IPs, so CI must name yfinance explicitly.
python main_jp.py --source yfinance

# each run rewrites jp_dashboard.html in place — open it, re-run, reload
python serve.py           # 127.0.0.1:8765, opens a browser, Refresh button works
python serve.py --min-roe 8       # extra args go to the run

# the Ito Review's floor rather than the user's 5% preference
python main_jp.py --min-roe 8

# quick run with no yfinance .info pass: PER and PBR only, no EV/EBITDA
python main_jp.py --no-ev --no-history
```

Always run `check_setup.py` before `main_jp.py`. Several sources here fail by
*succeeding*, and a full run is slow enough that discovering it afterwards is
expensive.

## Files

| File | Role |
|---|---|
| `screener.py` | Core engine, ported from Korea. `sanitize_metrics`, `compute_peer_benchmarks`, `score` are market-agnostic — do not edit here first. |
| `config_jp.py` | Thresholds, metric bounds, 種類株式 detection, the measured percentile table. |
| `providers_jp.py` | JPX roster + alerts, Yahoo Japan cross-section, two interchangeable fundamentals sources (kabutan, yfinance), yfinance prices and EV/EBITDA. |
| `japan_filters.py` | Japan share-class hygiene, ROE gate, absolute screen, own-history screen, TSE flags. |
| `main_jp.py` | CLI. Orders the gates so per-ticker calls run last. |
| `dashboard.py` | Generated — do not edit. Run `sync_dashboard_from_korea.py`. |
| `sync_dashboard_from_korea.py` | Korea's `dashboard.py` + 48 Japan substitutions. Every one must match exactly once. |
| `serve.py` | Local server behind the dashboard's Refresh button. |
| `build_site.py` | Assembles `site/` for GitHub Pages (noindex, no Refresh button). |
| `dashboard.cmd` | Double-click launcher. |
| `check_setup.py` | Pre-flight diagnostic, nine live checks. |
| `test_japan.py` | Offline tests with planted traps. Keep green. |

## Invariants — do not remove without understanding why

1. **種類株式 (five-character codes) are excluded.** TSE codes are four
   characters — four digits historically, alphanumeric since 2024 (キオクシア is
   `285A`). A *five*-character code is a class or preferred line sharing its
   first four characters with the common. There are exactly seven on the whole
   exchange and every one is a trap: `25935` 伊藤園第1種優先株式 has traded
   40–50% below `2593` for years because it carries no vote, and the other six
   are 社債型種類株式 — quasi-bonds with a fixed redemption, whose "PBR" is an
   artefact rather than a valuation. All seven sit in プライム（内国株式）, so
   nothing upstream removes them.

   This is Korea's 우선주 invariant with a different rule and a much smaller
   population. Rare does not mean harmless: they would flag as deep value on
   every single run.

2. **PER/PBR of 0 or negative must be treated as missing, never as cheap.**
   `METRIC_BOUNDS` enforces it, in history as well as today. Japan needs this
   at least as badly as Korea — 武田薬品 (4502) filed a loss in FY2026/03, and
   its PER computed from a negative EPS would sort to the top.

3. **Peer groups are `industry × board`, falling back to `industry`.** Measured
   on the gated universe: median PBR is 1.51 on Prime, 2.69 on Standard, **5.83
   on Growth**. Growth is to Prime roughly what KOSDAQ is to KOSPI, so pooling
   them makes every Growth name look expensive and every Prime name cheap.

4. **Peer counts exclude the stock itself**; the benchmark is a *winsorized
   median*, not a mean.

5. **`avg_discount` averages across all metrics with data**, including the ones
   that failed.

6. **EV/EBITDA is suppressed for financials.** Here that is exact rather than a
   guess: JPX's 33業種 names them — 銀行業, 証券、商品先物取引業, 保険業,
   その他金融業 — where Korea had to match the substring "Financial" in a vendor
   sector string.

7. **Slow calls run last.** The JPX roster, the Yahoo market-cap ranking and one
   batched price download are all cross-sectional and cut 3,707 listings to 661
   before anything runs per ticker. Do not reorder.

   Japan deviates from Korea in *where the liquidity gate sits*, and it is
   deliberate: no free source publishes a Japanese cross-sectional traded-value
   series (Yahoo's turnover ranking is a single session). So liquidity is
   computed from `yf.download`, which is batched — ~20 requests for the whole
   universe — and still runs before the per-ticker work. The gate binds hard:
   **994 names clear USD 600m but only 661 clear USD 4m a day.**

8. **The ROE floor runs after scoring, never before.** It narrows `passes`; it
   does not touch `avg_discount`. Gating earlier would change the peer medians
   themselves. Missing ROE fails the gate. Default 5%, `--min-roe 0` disables.

9. **Three independent screens.** Relative (peer median), absolute
   (`apply_absolute_screen`) and own-history (`apply_history_screen`) are scored
   separately and unioned into `passes_any`; `screen` lists every one a name
   cleared. None gates another.

10. **Financials clear the absolute screen without EV/EBITDA.** Invariant 6
    suppresses it for them, so a strict both-metrics rule would exclude every
    bank, insurer and broker — the corner of the market trading furthest below
    book. `abs_financials_pbr_only` (default on) lets them qualify on PBR + ROE;
    those rows carry `abs_via_carveout`.

11. **Each fundamentals source declares whether its per-share figures are
    restated for splits, and `build_valuation_history` reads that rather than
    assuming.** The two sources differ, and the difference is invisible in the
    numbers themselves:

    | | EPS | BPS |
    |---|---|---|
    | kabutan | restated (修正1株益) | **as filed** |
    | yfinance | restated | restated |

    Getting it wrong does not raise; it produces a plausible multiple that is
    simply the wrong one. Both directions were hit during this build. See the
    next invariant for the kabutan case; for yfinance, treating its already-
    restated figures as filed inflated every historical multiple and took the
    own-history screen from 0 passes to 7 in the top 70 names — a wrong answer
    that looked like a finding.

    The check that says both are now right: run the same tickers through both
    sources and compare. 日立 (6501), which split 5:1, comes back as PER
    10.475 / 21.889 / 25.71 / 25.035 from yfinance against kabutan's 10.595 /
    21.962 / 25.825 / 25.249, and identical PBR. Two independent providers
    agreeing to a rounding is the evidence; either alone is just a number.

12. **kabutan's EPS is split-adjusted and its BPS is not.** The columns are
    labelled 修正1株益 and 修正1株配 — *restated* — but the balance-sheet column
    is plain 1株純資産, as filed. Yahoo's price series is split-adjusted
    regardless of `auto_adjust`, because splits are applied to the chart data
    itself.

    So the PER history lines up by accident and the PBR history does not.
    日立 (6501) split five for one in June 2024; its filed 2024/03 BPS of 6,155
    against a post-split price gives a PBR of **0.45, against 2.71 the next
    year**. A six-fold artefact inside a three-point median *is* the answer, not
    noise. `build_valuation_history` divides each year's BPS by the splits
    recorded after that year end; the split events arrive batched with the price
    panel and cost nothing. `test_japan.py` holds this down with the real
    figures.

13. **Forecasts are carried and never screened on.** Japanese convention is
    forward: the PER a Japanese investor quotes is 会社予想PER, struck on the
    company's own guidance. That guidance is a formal disclosure rather than an
    analyst consensus, so it is worth having — but it is still management's
    opinion of its own future. Every screen runs on the last *filed* year;
    `fwd_eps`, `fwd_dps`, `forward_pe` and `div_yield_fwd` ride along for
    context. Same rule as Korea's `isConsensus: "Y"`, different justification.

## Two fundamentals sources, and why that is not a choice

`--source auto` (default) probes kabutan with three tickers and falls back.

| | kabutan | yfinance |
|---|---|---|
| reachable from CI | **no** — HTTP 405 | yes |
| filed years | 4 EPS / 3 BPS | **5 of all three statements** |
| company guidance (会社予想) | **yes** | no |
| filed DPS | yes | trailing rate from `.info` |

**kabutan answers HTTP 405 to every request from a GitHub runner** — 659 of 659
on the first CI run, with an English WAF page. A runner-side probe found
minkabu at 403 and irbank's HTML site at 403 too; only irbank's CSV host stays
open, and it rate-limits too hard to use (see below). These are deliberate
blocks on datacenter traffic rather than rate limits, so they are **routed
around, not worked around** — the workflow names `--source yfinance` and says
why in the file.

yfinance is not a downgrade for the screen itself. It carries five filed
columns where kabutan carries four, so the own-history screen is *deeper*, and
the two agree on the reconstructed history to within a rounding. What is lost
is the company's own 会社予想. yfinance's `forwardEps` is **analyst consensus**,
which is a different thing, and it is not substituted for guidance — under this
source the forecast columns on the published page are simply empty.

**How closely the two agree, measured on twelve large names:** BPS median
difference **0.0%** (eleven of twelve identical to five significant figures),
EPS median difference **1.2%**. But the tails matter and they are not random:

| | kabutan | yfinance | diff |
|---|---|---|---|
| 三菱ＵＦＪ (8306) EPS | 213.2 | 165.0 | **−22.6%** |
| デンソー (6902) BPS | 2,040 | 1,767 | **−13.4%** |

yfinance normalises Japanese bank and insurer statements poorly, and picks a
different equity line where minority interests or treasury stock are large. So
kabutan stays the default wherever it is reachable, and **the published page
should be read as slightly noisier than a local run, with the noise
concentrated in financials.**

Consequence worth remembering: **the local dashboard and the published one are
not built from the same source.** Expect the counts to differ — the first
published run found 123 relative / 22 absolute / 9 history against a local
kabutan run's 110 / 18 / 7, from a combination of the per-share differences
above and yfinance's extra filed year. A forecast column that is populated
locally and blank on Pages is the same cause, not a bug.

## Known gaps (ranked by value of fixing)

1. **The own-history screen sees four filed years of PER and three of PBR.**
   That is what kabutan publishes free; older years are behind their
   subscription. `hist_min_years = 3` is therefore the floor rather than a
   comfortable minimum — 643 of 661 names have exactly three usable PBR points.
   A source with deeper free history would strengthen this screen more than any
   other single change.
2. **No historical EV/EBITDA exists free for Japan**, so the own-history screen
   runs on two metrics rather than Korea's three. Neither kabutan nor irbank
   publishes a depreciation line, so EBITDA cannot be rebuilt from filings.
   Current EV/EBITDA comes from yfinance and carries the same reliability
   caveat it does on the Korea build.
3. **The TSE publishes its capital-cost disclosure list as PDFs only.** JPX
   names the companies that have actually disclosed a "action to implement
   management conscious of cost of capital and stock price" plan, monthly, but
   only as PDF attachments under `/equities/follow-up/`. So `tse_focus` flags
   the cohort the exchange is *pressing* (PBR < 1 or ROE < 8%) and cannot say
   who has already responded. Parsing those PDFs would close it.
4. **親子上場 (listed subsidiaries of listed parents) are not identified.**
   This is Japan's most distinctive structural discount after 種類株式 — a
   majority-owned listed subsidiary trades below fair value for float and
   minority-squeeze reasons rather than business ones — and nothing here finds
   them. It needs major-shareholder data, which no free cross-sectional source
   provides. Holdcos are flagged by name; their subsidiaries are not.
5. **`pbr_bottom20_industry` is a current cross-section only**, like Korea's.
6. No forward estimates beyond company guidance; trailing multiples only.

## Likely first failures

- **irbank will look like the obvious source and must not be used at scale.**
  It publishes a genuinely excellent per-company CSV
  (`f.irbank.net/files/{code}/fy-data-all.csv`) with four to five filed years of
  P&L, balance sheet, cash flow and dividends. It also sheds load hard, and it
  does so with **HTTP 200 carrying a page titled 表示制限中**, or a 302 to the
  same. Twelve sequential requests two seconds apart earned a cooling-off
  period. Caching one of those responses poisons the cache for a full TTL and
  parsing one as a CSV yields an empty frame indistinguishable from a company
  that files no accounts. `_is_restricted` and `Throttled` are kept in
  `providers_jp.py` for that shape even though irbank is no longer used.

- **kabutan is fine.** Measured: 24 requests with no delay at all, 7.3s, zero
  rejections. 661 names take under two minutes. If it ever starts refusing,
  `_get_text` already treats the refusal as a failure rather than as data.

- **yfinance `.info` is the one that throttles silently** — the dict comes back
  with fields *missing*, not an error. `JapanEnricher` probes once
  single-threaded before opening the pool, caches successes, paces at two
  workers, and raises if under 50% of the universe priced. Measured at 661
  names: 100% priced in about five minutes. `--no-ev` screens without it.

- **JPX moved the roster from `.xls` to `.xlsx`.** `_roster_url()` reads the
  index page rather than hardcoding the path, and falls back to the known URL.

- **Yahoo Japan's 取引値 column is the price with the session date glued on** —
  `"3,02509/18"` is 3,025 on 09/18, not 302,509. `_price()` strips the date
  first. If market caps ever come back ~100x too large, look here.

- **pandas 3.x** — pin `pandas<3`.

## What Japan gives you that Korea and the UK did not

Korea's known gap #1 was that industry classification came from yfinance, and it
was called the highest-value upgrade available. **In Japan that gap does not
exist.** `data_j.xlsx` is the exchange's own file and carries:

- the official **33業種** code (and a 17業種 roll-up), so peer groups and the
  financials rule are both exact rather than inferred
- the market segment, so Prime / Standard / Growth needs no derivation
- the TOPIX scale category (Core30 / Large70 / Mid400 / Small 1 / Small 2)
- **ETFs, ETNs, REITs, infrastructure funds, TOKYO PRO Market, foreign listings
  and 出資証券 each filed under their own 市場・商品区分**, so they never enter
  the universe. Korea had to strip these by name; the UK had to reverse-engineer
  investment trusts out of a trade-body register.

The 監理・整理銘柄 list is also published **by code**, so the distress filter is
exact. Korea's equivalent matches on company name and can be defeated by a
rename — its known gap #3. Here it cannot.

## Calibration, measured rather than inherited

Across the 661 gated names, 2026-09-19:

| | p10 | p25 | median | p75 | p90 |
|---|---|---|---|---|---|
| PER | 10.50 | 14.19 | **18.05** | 25.73 | 37.87 |
| PBR | 0.76 | 1.06 | **1.58** | 2.70 | 4.61 |
| EV/EBITDA | 6.05 | 7.61 | **9.90** | 13.20 | 18.07 |
| ROE % | 1.90 | 5.44 | **8.73** | 12.83 | 19.32 |
| Yield % | 0.65 | 1.36 | **2.02** | 2.75 | 3.54 |

Korea's absolute-screen levels survive that test, and for once that is a
measured result rather than an assumption: **PBR < 1.0 passes 20.3% of the
universe and EV/EBITDA < 8 passes 24.4%**, so the two valuation tests are about
equally strict and both sit near p25. The UK needed re-cutting precisely because
they were not — there PBR < 1 was p10 against a yield floor at p69, which is one
strict test with three no-ops attached.

`PBR < 1.0` has a second claim here that it has in no other market: it is the
**Tokyo Stock Exchange's own criterion**, the line its March 2023 request was
drawn at. Calibration and local convention agree.

**Two numbers did change from Korea, both toward Japanese practice:**

- **Cost of equity 8%, not 10%.** The 2014 Ito Review set 8% ROE as the minimum
  a listed company should clear to exceed its cost of capital, and the TSE's
  whole below-book campaign is built on that arithmetic. It also makes the two
  tests cohere: at CoE 8%, fair PBR is exactly 1.0 when ROE is 8%. At 10% only
  49 names (7.4%) clear the fair-PBR test; at 8%, 106 (16.0%).
- **The own-history screen requires both metrics, not two of three.** With only
  PER and PBR available, allowing one to fail is no test at all — and PBR is
  specifically what catches a one-off gain flattering PER.

The ROE floor (5%) and dividend floor (2%) are **preferences, not valuation
levels**, and are left at the user's stated values. They are not recalibrated
per market.

Which test actually binds, measured: the `PBR < 1 & EV/EBITDA < 8` core does
most of the work (dropping it takes the absolute screen from 18 to 82), the
fair-PBR test costs 11 names, the dividend floor 5, the ROE floor 1. No test is
a no-op and none is doing all the work.

## Results, first full run (2026-09-19)

```
3,707 domestic lines
  -7    種類株式
  -49   監理・整理銘柄
  994   cleared USD 600m
  661   cleared USD 4m/day        <- the liquidity gate binds hard in Japan
  144   cheap vs peers
  110   ...and ROE >= 5%
   18   cheap outright             (4 via the financials carve-out)
    7   cheap vs own history       (5 of which no other screen found)
  124   passing at least one screen
```

Overlap: 100 relative only, 9 absolute only, 5 history only, 8 relative +
absolute, 1 relative + history, 1 all three.

The own-history screen at 2-of-2 and 30% finds 7. At 1-of-2 it finds 36
(`--hist-min-metrics 1`); at 2-of-2 and 20%, 15. Seven is small but it is doing
its job — five of them clear neither other screen.

## One screen, not one per board

Korea runs KOSPI and KOSDAQ as separate runs with separate dashboards because
they are genuinely different markets. Japan does not, and the reason is
arithmetic: with a USD 600m floor the universe is **618 Prime, 25 Standard, 18
Growth**. Standard and Growth are far too thin to form their own peer cohorts,
let alone their own pages. The board is a peer dimension and a filter column
here, not a separate run.

Note this is *not* the same as saying the board does not matter — invariant 3
shows it matters a great deal for the peer medians. It is only the *page* layout
that collapses to one.

## Why the Refresh button needs serve.py

Unchanged from Korea, and worth restating because it is easy to re-litigate: a
refresh means fetching JPX, Yahoo Japan and kabutan and redoing the peer maths
in pandas. Nothing in a browser can do that — a page opened over `file://`
cannot start a process, and a published Artifact is sandboxed. So
`dashboard.py` probes `api/status` on load: answered, it shows the button;
unanswered, it shows the command line. The same HTML is therefore correct off
disk, behind `serve.py`, and published.

**`serve.py` re-renders on every request from an imported `dashboard` module.**
Python caches that import, so after running `sync_dashboard_from_korea.py` you
must restart the server or you will spend a while debugging a page that is
correct on disk and stale in the browser. It happened during this build.

## Adjustable thresholds, and a precision bug worth knowing about

The dashboard re-evaluates all three screens in the browser, and every input
they need travels with each row. `evaluate()` mirrors `apply_roe_gate`,
`apply_absolute_screen` and `apply_history_screen`.

**If the client-side verdict at default settings disagrees with the Python
funnel, that is a real bug — they compute the same thing twice.** It did here,
by one name. Korea's `_records` rounds the payload to two decimals, and the page
re-applies thresholds to the *rounded* value: 三菱ＨＣキャピタル's PBR of
**0.99948 becomes 1.00** and stops being "below 1", so the browser reported 17
absolute passes where Python said 18. The sync now ships six decimals for every
re-thresholded field. Display is unaffected — the table formats with `toFixed`
at render time.

**This is not Japan-specific.** Korea and the UK ship the same rounding and will
disagree with their own funnels wherever a name lands on a boundary. Worth
fixing there.

Verified after the fix, browser against Python: relative 110/110, absolute
18/18, history 7/7, any 124/124, PBR<1 134/134, EV<8 161/161, fair 106/106,
yield 336/336, ROE 508/508.

Two things remain deliberately not adjustable, because they cannot be recomputed
from the shipped rows: **peer medians** (fixed when the run built its cohorts)
and **market cap below the run's floor** (those rows were gated out before
scoring and are simply absent).

## Interpretation

Sort by `avg_discount`, then read `roe_pct` immediately. Low PBR + high ROE is a
possible mispricing; low PBR + low ROE is arithmetic — a company not earning its
cost of capital, correctly priced. That is the entire premise of the TSE's 2023
request, and at an 8% cost of equity the fair-PBR column says which case a name
is in.

Two Japan-specific readings:

- **A name can read expensive against its own history while being cheap.**
  Guidance is a formal disclosure, so the price already reflects the current
  year while this screen still measures the last filing. Check `forward_pe`
  against `trailing_pe` before concluding a company has re-rated.
- **`net_cash_to_mcap` is context, not a test.** It is shown because a large
  minority of TSE companies hold net cash worth a real fraction of their own
  market value, which is what makes a low PBR in Japan mean something different
  from a low PBR elsewhere. It is not part of any screen; adding it would be a
  decision to take, not a default.

Japanese value is a crowded trade — the TSE campaign has been running since 2023
and below-book names have been picked over. Assume the obvious ones are found.

Research tool, not investment advice.
