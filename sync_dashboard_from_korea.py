"""Re-derive the Japan dashboard from the Korea build's current dashboard.py.

    python sync_dashboard_from_korea.py ../Korea/dashboard.py

The Japan dashboard is not a fork that drifts: it is Korea's dashboard.py with
a fixed list of Japan substitutions applied. When Korea gains a feature, copy
its dashboard.py over and run this.

Every substitution must match exactly once. Anything that no longer matches is
printed and the exit code is 1 - Korea's text moved, and that substitution
needs updating by hand rather than being silently skipped. Afterwards, grep
the result for Korean text, KOSPI and 억원 to catch anything new Korea added
that this list does not know about yet, and re-check that the browser's verdict
matches the Python funnel at default thresholds.

The substantive differences from Korea, as opposed to relabelling:

  - the own-history screen runs on TWO metrics, not three. No free source
    publishes a Japanese company's historical EV/EBITDA, so every "per, pbr,
    evx" triple here becomes a pair and evx_now disappears.
  - the growth panel's third series is operating cash flow, not EBITDA.
    kabutan publishes no depreciation line, and OCF is both free and, for a
    value screen, the more useful question.
  - Japanese text must break at any character. Korea's keep-all is correct for
    Hangul and wrong here: 株式会社三菱ＵＦＪフィナンシャル・グループ has no
    spaces to break at, so keep-all would push the table sideways.
"""
import io
import sys

if len(sys.argv) != 2:
    sys.exit("usage: python sync_dashboard_from_korea.py <path to Korea dashboard.py>")
SRC = sys.argv[1]
P = "dashboard.py"
s = io.open(SRC, encoding="utf-8").read()


PAIRS = [
    # ---- module docstring --------------------------------------------------
    ("Reads the CSV that main_kr.py writes", "Reads the CSV that main_jp.py writes"),
    ("re-run main_kr.py to refresh it in place.", "re-run main_jp.py to refresh it in place."),

    # ---- funnel ------------------------------------------------------------
    ('("after_korea_filters", "After share-class hygiene", None),',
     '("after_japan_filters", "After share-class hygiene", None),'),

    # ---- table columns -----------------------------------------------------
    ('("ticker", "Code", "l"), ("name", "Name", "l"), ("industry", "업종 industry", "l"),',
     '("ticker", "Code", "l"), ("name", "Name", "l"), ("industry", "33業種 industry", "l"),'),
    ('("rev_cagr", "Revenue 3y", ""), ("ebitda_cagr", "EBITDA 3y", ""),',
     '("rev_cagr", "Revenue 3y", ""), ("ocf_cagr", "Operating CF 3y", ""),'),
    ('("hist_avg_disc", "vs own 5y", ""),', '("hist_avg_disc", "vs own history", ""),'),

    # ---- precision of the re-thresholded fields ----------------------------
    # The page re-runs both screens in the browser, so any value a THRESHOLD is
    # applied to has to reach it at full precision. Rounded to 2dp,
    # 三菱ＨＣキャピタル's PBR of 0.99948 becomes 1.00 and stops being "below 1",
    # which is why the browser said 17 absolute passes where Python said 18 -
    # and a page that disagrees with its own funnel is worth nothing. The
    # displayed figures are unaffected: the table formats with toFixed at render
    # time, so these are still shown to two decimals.
    #
    # This is not Japan-specific. Korea and the UK ship the same rounding and
    # will disagree with their own funnels wherever a name lands on a boundary.
    ('''            "trailing_pe": _f(r.get("trailing_pe")),
            "price_to_book": _f(r.get("price_to_book")),
            "ev_to_ebitda": _f(r.get("ev_to_ebitda")),
            "roe_pct": _f(r.get("roe_pct"), 1),
            "div_yield": _f(r.get("div_yield")),''',
     '''            "trailing_pe": _f(r.get("trailing_pe"), 6),
            "price_to_book": _f(r.get("price_to_book"), 6),
            "ev_to_ebitda": _f(r.get("ev_to_ebitda"), 6),
            "roe_pct": _f(r.get("roe_pct"), 6),
            "div_yield": _f(r.get("div_yield"), 6),'''),

    # ---- row payload -------------------------------------------------------
    ('            "fin": bool(str(r.get("sector", "") or "").lower().find("financial") >= 0),',
     '            # 33業種 names the financials exactly, so this is not a\n'
     '            # substring guess the way a vendor sector string would be.\n'
     '            "fin": bool(str(r.get("industry", "") or "") in FINANCIAL_INDUSTRIES),'),
    ('            # Three-year history, oldest first, in 억원. The yearly values',
     '            # Three-year history, oldest first, in 億円. The yearly values'),
    ('            "ebitda": [_f(r.get(f"ebitda_y{i}"), 0) for i in (1, 2, 3)],',
     '            "ocf": [_f(r.get(f"ocf_y{i}"), 0) for i in (1, 2, 3)],'),
    ('            "ebitda_cagr": _f(r.get("ebitda_cagr"), 4),',
     '            "ocf_cagr": _f(r.get("ocf_cagr"), 4),'),
    ('            # Own five-year history. The medians and today\'s values let the\n'
     '            # page re-threshold the screen; the yearly values feed the tooltip.\n'
     '            "hist_years": str(r.get("hist_years", "") or ""),\n'
     '            "evx_now": _f(r.get("evx_now")),\n'
     '            "h_med": {k: _f(r.get(f"hist_{k}_med")) for k in ("per", "pbr", "evx")},\n'
     '            "h_ser": {k: [_f(r.get(f"hist_{k}_y{i}")) for i in range(1, 6)]\n'
     '                      for k in ("per", "pbr", "evx")},',
     '            # Own filed history, PER and PBR only - see the module\n'
     '            # docstring for why there is no EV/EBITDA here.\n'
     '            "hist_years": str(r.get("hist_years", "") or ""),\n'
     '            # Full precision for the same reason as the multiples above:\n'
     '            # the discount to this median is re-thresholded in the page.\n'
     '            "h_med": {k: _f(r.get(f"hist_{k}_med"), 6) for k in ("per", "pbr")},\n'
     '            "h_ser": {k: [_f(r.get(f"hist_{k}_y{i}")) for i in range(1, 6)]\n'
     '                      for k in ("per", "pbr")},'),
    ('            "pbr1": bool(r.get("pbr_below_1", False)),',
     '            "pbr1": bool(r.get("pbr_below_1", False)),\n'
     '            # The cohort the TSE\'s 2023 request is aimed at, and the\n'
     '            # company\'s own guidance - shown, never screened on.\n'
     '            "tse": bool(r.get("tse_focus", False)),\n'
     '            "fwd_pe": _f(r.get("forward_pe")),\n'
     '            "fwd_yield": _f(r.get("div_yield_fwd")),\n'
     '            "netcash": _f(r.get("net_cash_to_mcap"), 3),'),

    # ---- imports -----------------------------------------------------------
    ("import pandas as pd\n",
     "import pandas as pd\n\nfrom config_jp import FINANCIAL_INDUSTRIES\n"),

    # ---- drop labels -------------------------------------------------------
    ('''    for key, label in [("dropped_preferred", "우선주 preferred"),
                       ("dropped_reit", "리츠 REIT"),
                       ("dropped_spac", "스팩 SPAC")]:''',
     '''    for key, label in [("dropped_class_share", "種類株式 class shares"),
                       ("dropped_supervised", "監理・整理銘柄"),
                       ("dropped_holdco", "holding companies")]:'''),

    # ---- boards ------------------------------------------------------------
    ('''BOARD_FILES = {"KOSPI": "kr_dashboard.html",
               "KOSDAQ": "kq_dashboard.html",
               "BOTH": "krkq_dashboard.html"}''',
     '''# Japan runs as ONE screen, not one per board. With a USD 600m floor the
# split is 926 Prime, 57 Standard, 20 Growth - Standard and Growth are far too
# thin to form their own peer cohorts, let alone their own pages. The board is
# a column and a peer dimension here, not a separate run. This is the opposite
# of Korea, where KOSPI and KOSDAQ are genuinely different markets.
BOARD_FILES = {"ALL": "jp_dashboard.html",
               "PRIME": "jp_dashboard.html",
               "STANDARD": "jp_dashboard.html",
               "GROWTH": "jp_dashboard.html"}'''),
    ('''# its own. KOSPI keeps the original name - renaming a published artifact makes
# it unrecognisable to anyone who bookmarked it.
BOARD_TITLES = {"KOSDAQ": "KOSDAQ Discount Screen"}''',
     '''# One page, so nothing to retitle.
BOARD_TITLES = {}'''),

    # ---- FX field ----------------------------------------------------------
    ('"krw_per_usd": meta.get("krw_per_usd"),', '"jpy_per_usd": meta.get("jpy_per_usd"),'),

    # ---- titles ------------------------------------------------------------
    ('head.replace("<title>Korea Discount Screen</title>",',
     'head.replace("<title>Japan Discount Screen</title>",'),
    ('body.replace("<h1>Korea Discount Screen</h1>",', 'body.replace("<h1>Japan Discount Screen</h1>",'),
    ('_HEAD = """<title>Korea Discount Screen</title>', '_HEAD = """<title>Japan Discount Screen</title>'),
    # The <h1> appears twice - once as markup, once inside the board-retitling
    # call above - so the markup one is matched together with the eyebrow that
    # precedes it rather than on its own.
    ('<p class="eyebrow">KRX relative valuation</p>\n        <h1>Korea Discount Screen</h1>',
     '<p class="eyebrow">TSE relative valuation</p>\n        <h1>Japan Discount Screen</h1>'),

    # ---- fonts and line breaking -------------------------------------------
    ('family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+KR:wght@300;400;500;600;700&display=swap',
     'family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+JP:wght@300;400;500;600;700&display=swap'),
    ('--sans:"IBM Plex Sans KR", system-ui, -apple-system, "Segoe UI", "Malgun Gothic", sans-serif;',
     '--sans:"IBM Plex Sans JP", system-ui, -apple-system, "Segoe UI", '
     '"Hiragino Kaku Gothic ProN", "Yu Gothic", "Meiryo", sans-serif;'),
    ('''/* Korean breaks at any character by default, so 현대지에프홀딩스 shatters across
   four lines in a narrow column. keep-all breaks at word boundaries instead. */''',
     '''/* Japanese is the opposite case to Korean: it has no spaces, so breaking at
   any character is the CORRECT typography, and keep-all would push a name like
   三菱ＵＦＪフィナンシャル・グループ out of its column and the table sideways. */'''),
    ("  word-break:keep-all}", "  word-break:normal;overflow-wrap:anywhere}"),

    # ---- commands / search -------------------------------------------------
    ('<code id="cmd">python main_kr.py</code>', '<code id="cmd">python main_jp.py</code>'),
    ('placeholder="name, code or 업종"', 'placeholder="name, code or 業種"'),
    ('$("cmd").textContent = M.cmd || ("python main_kr.py --board "',
     '$("cmd").textContent = M.cmd || ("python main_jp.py --board "'),
    ('  ["Source", M.source === "naver" ? "KIND + Naver" : "KRX"],\n', ''),
    ('["USD/KRW", M.krw_per_usd],', '["USD/JPY", M.jpy_per_usd],'),

    # ---- history panel, selector and control labels ------------------------
    ('<h2>Cheap vs its own 5 years</h2>', '<h2>Cheap vs its own history</h2>'),
    ('<option value="history">Cheap vs own 5y</option>',
     '<option value="history">Cheap vs own history</option>'),
    ('<label for="t_hdisc">Below own 5y by at least %</label>',
     '<label for="t_hdisc">Below own history by at least %</label>'),
    ('`${rel.length} vs peers · ${absl.length} outright · ${hist.length} vs own 5y`',
     '`${rel.length} vs peers · ${absl.length} outright · ${hist.length} vs own history`'),
    ("const passTag = r.ev.hist ? ' <span class=\"tag\">5y low</span>' : \"\";",
     "const passTag = r.ev.hist ? ' <span class=\"tag\">own-history low</span>' : \"\";"),

    # ---- the two-metric own-history screen ---------------------------------
    ('''  // Own five-year history. Mirrors korea_filters.apply_history_screen: the
  // benchmark medians were built in Python (loss years and out-of-bounds
  // values already excluded), so only the threshold and count are live here.''',
     '''  // Own filed history. Mirrors japan_filters.apply_history_screen: the
  // benchmark medians were built in Python (loss years and out-of-bounds
  // values already excluded), so only the threshold and count are live here.
  // Two metrics, and both must clear - with only PER and PBR available,
  // letting one fail would be no test at all.'''),
    ('''/* Today's value against the five-year median, per metric. Null where there is
   no benchmark (fewer than the minimum usable years) or no valid value today -
   a missing value is never a cheap one (invariant 2). */
function histDiscounts(r) {
  const cur = {per: r.trailing_pe, pbr: r.price_to_book, evx: r.evx_now};
  const out = {};
  ["per", "pbr", "evx"].forEach(k => {''',
     '''/* Today's value against the filed-history median, per metric. Null where
   there is no benchmark (fewer than the minimum usable years) or no valid
   value today - a missing value is never a cheap one (invariant 2). */
function histDiscounts(r) {
  const cur = {per: r.trailing_pe, pbr: r.price_to_book};
  const out = {};
  ["per", "pbr"].forEach(k => {'''),
    ('''  const lab = {per: "PER", pbr: "PBR", evx: "EV/EBITDA"};
  const now = {per: r.trailing_pe, pbr: r.price_to_book, evx: r.evx_now};
  const tip = ["per", "pbr", "evx"].map(k => {''',
     '''  const lab = {per: "PER", pbr: "PBR"};
  const now = {per: r.trailing_pe, pbr: r.price_to_book};
  const tip = ["per", "pbr"].map(k => {'''),
    # The per-metric checklist under the own-history panel - the second place
    # the metric triple is written out.
    ('''  const lab = {per: "PER", pbr: "PBR", evx: "EV/EBITDA"};
  const lines = ["per", "pbr", "evx"].map(k => {''',
     '''  const lab = {per: "PER", pbr: "PBR"};
  const lines = ["per", "pbr"].map(k => {'''),

    # ---- live-refresh note -------------------------------------------------
    ('''   Only serve.py can actually re-run the screen: it shells out to main_kr.py,
   which scrapes KIND and Naver. A page opened straight off disk, or published
   as an Artifact, has no such backend - so the button is shown only once
   /api/status answers, and the command line is shown otherwise. */''',
     '''   Only serve.py can actually re-run the screen: it shells out to main_jp.py,
   which reads JPX, Yahoo Japan and kabutan. A page opened straight off disk,
   or published as an Artifact, has no such backend - so the button is shown
   only once /api/status answers, and the command line is shown otherwise. */'''),
    ('''/* One server can host several boards, so every call names its own. Absolute
   paths, because the page is served at /kospi or /kosdaq and a relative URL
   would resolve differently per board. */''',
     '''/* Kept from the Korea build, where one server hosts several boards. Japan is
   a single screen, so M.board is the segment filter rather than a separate
   run, and the query string is harmless. */'''),

    # ---- growth panel ------------------------------------------------------
    ('const GROWTH = {rev_cagr: "rev", ebitda_cagr: "ebitda", np_cagr: "np3"};',
     'const GROWTH = {rev_cagr: "rev", ocf_cagr: "ocf", np_cagr: "np3"};'),
    ('const GROWTH_LABEL = {rev_cagr: "Revenue", ebitda_cagr: "EBITDA", np_cagr: "Net profit"};',
     'const GROWTH_LABEL = {rev_cagr: "Revenue", ocf_cagr: "Operating cash flow", np_cagr: "Net profit"};'),
    ('const tip = GROWTH_LABEL[key] + " (억원)\\\\n"',
     'const tip = GROWTH_LABEL[key] + " (億円)\\\\n"'),

    # ---- prose -------------------------------------------------------------
    ('''    <p><b>Cheap vs its own five years</b> compares today's PER, PBR and EV/EBITDA
    with the median of the company's last five filed years. It catches what the
    other two miss - a company that always traded at a premium and has just
    de-rated. Loss years drop out of the benchmark rather than dragging it, and
    fewer than three usable years means no benchmark at all. <b>One caution:</b>
    these are trailing multiples, so when earnings are surging the latest filing
    lags the price and a stock reads <i>expensive</i> against its history until
    the next filing catches up. A one-off gain does the opposite.</p>''',
     '''    <p><b>Cheap vs its own history</b> compares today's PER and PBR with the
    median of the company's last filed years — four for PER, three for PBR,
    which is what kabutan publishes free. Both must clear the threshold, not
    one of two. Loss years drop out of the benchmark rather than dragging it.
    <b>One caution, and it bites harder in Japan than elsewhere:</b> these are
    trailing multiples struck on the last filing, while the price is already
    trading on the company's own guidance for the current year. A business whose
    earnings have just turned up therefore reads <i>expensive</i> against its own
    history until the next filing catches up. The forecast figures travel with
    every row so you can see which case you are looking at.</p>'''),
    ('''own 5 years</b> asks whether it is cheap against itself. Korea needs all
    three: a peer group where everything is expensive still produces "cheap"
    names, one where everything is cheap hides them, and neither notices a
    premium company that has quietly de-rated.''',
     '''own history</b> asks whether it is cheap against itself. Japan needs all
    three: 銀行業, 卸売業 and 鉄鋼 trade as blocks, so a peer comparison inside
    them finds relative bargains in a cheap crowd, while an absolute test alone
    would miss a quality name that has simply de-rated.'''),
    ('''    <p><b>Low PBR with low ROE is not a discount.</b> It is a company not earning
    its cost of capital, priced accordingly. Much of what gets called the Korea
    Discount is this. The ROE floor removes the worst of it, but a name just
    above the line is still worth checking by hand.</p>
    <p><b>업종 files holding companies under 기타 금융업.</b> That pools operating
    holdcos with bank holdcos in one peer group, and suppresses EV/EBITDA for
    both — enterprise value is meaningless for a bank, but not for an operating
    company. Holdcos are tagged so you can see which rows this touches.</p>''',
     '''    <p><b>Low PBR with low ROE is not a discount.</b> It is a company not earning
    its cost of capital, priced accordingly — which is the entire premise of the
    TSE's March 2023 request that companies below book disclose what they intend
    to do about it. The ROE floor removes the worst of it, but a name just above
    the line is still worth checking by hand. The fair-PBR test uses an 8% cost
    of equity, the Ito Review's figure, so fair value is exactly 1.0x book at 8%
    ROE.</p>
    <p><b>The exchange has already done the classification.</b> Industry is JPX's
    own 33業種 code, not a vendor's guess, and banks, brokers and insurers are
    named exactly rather than matched on a substring — so the rule that
    suppresses EV/EBITDA for financials is exact. ETFs, REITs, infrastructure
    funds, TOKYO PRO Market and foreign listings never enter the universe at
    all, because JPX files them under their own market categories.</p>'''),
]


missing = [a for a, _ in PAIRS if s.count(a) != 1]
for a in missing:
    print("NO MATCH (%d) %s" % (s.count(a), a.splitlines()[0][:100]))
for a, b in PAIRS:
    if s.count(a) == 1:
        s = s.replace(a, b)

io.open(P, "w", encoding="utf-8").write(s)
print("wrote %s (%d substitutions, %d failed)" % (P, len(PAIRS) - len(missing), len(missing)))

LEFTOVERS = ["KOSPI", "KOSDAQ", "억원", "업종 industry", "main_kr", "krw_per_usd",
             "우선주", "evx", "keep-all", "Korea Discount"]
for tok in LEFTOVERS:
    if tok in s:
        print("  leftover %-16s x%d" % (tok, s.count(tok)))
sys.exit(1 if missing else 0)
