"""Offline validation of the Japan screener. No network required.

Run: python test_japan.py

The universe below is synthetic, but every planted case is a trap this build
actually hit, or one the Korea build hit and this one inherits. The two that
are Japan's own are the 種類株式 pair and the split-unadjusted BPS - both cost
real debugging time, and both produce a confident wrong answer rather than an
error, which is exactly the kind a test has to hold down.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import japan_filters as JF
import providers_jp as P
from config_jp import ScreenConfig
from screener import run_screen

rng = np.random.default_rng(7)

JPY_USD = 1.0 / 158.0
MCAP = 2.0e11          # ~USD 1.27bn, clears the gate
ADV = 2.0e9            # ~USD 12.7m, clears the gate


def base(**kw):
    r = dict(name="", board="PRIME", industry="機械",
             market_cap_local=MCAP, adv_local=ADV, close_jpy=5_000.0,
             trailing_pe=np.nan, price_to_book=np.nan, ev_to_ebitda=np.nan,
             trailing_eps=500.0, book_value_ps=4_000.0, div_yield=1.5)
    r.update(kw)
    return r


def make_universe() -> pd.DataFrame:
    rows = []
    # Two cohorts at deliberately different multiple levels, on different
    # boards - the board dimension has to keep them apart.
    specs = [
        ("機械", "PRIME", 12.0, 0.9, 6.5, 18),
        ("サービス業", "GROWTH", 34.0, 4.2, 24.0, 16),
    ]
    n = 0
    for industry, board, pe, pb, ev, count in specs:
        for _ in range(count):
            n += 1
            rows.append(base(
                ticker="%04d" % (1000 + n * 3), name="同業%d" % n,
                board=board, industry=industry,
                trailing_pe=pe * rng.uniform(0.93, 1.09),
                price_to_book=pb * rng.uniform(0.93, 1.09),
                ev_to_ebitda=ev * rng.uniform(0.93, 1.09),
                market_cap_local=MCAP * rng.uniform(0.9, 2.5),
                adv_local=ADV * rng.uniform(0.9, 2.5)))

    # --- planted cases --------------------------------------------------
    # PASS: genuinely cheap against PRIME 機械, earns its keep, pays out.
    rows.append(base(ticker="9001", name="本当に割安",
                     trailing_pe=7.5, price_to_book=0.55, ev_to_ebitda=3.9,
                     trailing_eps=660.0, book_value_ps=9_000.0, div_yield=4.2))
    # FAIL: the 種類株式 line of the same company. Five-character code, four
    # of them shared with the common. Cheaper on every metric for reasons that
    # have nothing to do with the business - exactly 伊藤園 25935 vs 2593.
    rows.append(base(ticker="90015", name="本当に割安第１種優先株式",
                     trailing_pe=4.5, price_to_book=0.33, ev_to_ebitda=3.9))
    # FAIL: 社債型種類株式 - a quasi-bond. Its "PBR" is an artefact.
    rows.append(base(ticker="90025", name="本当に割安第１回社債型種類株式",
                     trailing_pe=4.4, price_to_book=0.30, ev_to_ebitda=3.9))
    # FLAGGED, not dropped: a holdco at a persistent NAV discount.
    rows.append(base(ticker="9002", name="日本ホールディングス",
                     trailing_pe=6.0, price_to_book=0.30, ev_to_ebitda=3.4,
                     trailing_eps=300.0, book_value_ps=15_000.0, div_yield=2.0))
    # FAIL: a loss-maker whose multiples arrive as zero. Must read as MISSING,
    # never as cheap - the single biggest way to break a value screen.
    rows.append(base(ticker="9005", name="赤字会社",
                     trailing_pe=0.0, price_to_book=0.60, ev_to_ebitda=0.0,
                     trailing_eps=-200.0))
    # FAIL: cheap against PRIME machinery, ordinary for its own growth cohort.
    # 32x looks expensive next to 12x and cheap next to 34x; only the board
    # dimension tells them apart.
    rows.append(base(ticker="9006", name="成長株適正", board="GROWTH",
                     industry="サービス業",
                     trailing_pe=32.0, price_to_book=4.0, ev_to_ebitda=23.0))
    # PASS: genuinely cheap against its OWN growth cohort.
    rows.append(base(ticker="9007", name="成長株割安", board="GROWTH",
                     industry="サービス業",
                     trailing_pe=24.0, price_to_book=3.0, ev_to_ebitda=17.0))
    # FAIL: below the size floor.
    rows.append(base(ticker="9008", name="小型株", market_cap_local=4e10,
                     trailing_pe=7.5, price_to_book=0.55, ev_to_ebitda=3.9))
    # FAIL: thinly traded.
    rows.append(base(ticker="9009", name="低流動性", adv_local=1e8,
                     trailing_pe=7.5, price_to_book=0.55, ev_to_ebitda=3.9))
    # A bank: EV/EBITDA must be suppressed (invariant 6) and it must still be
    # able to clear the absolute screen on PBR and ROE.
    rows.append(base(ticker="9010", name="日本銀行持株", industry="銀行業",
                     trailing_pe=9.0, price_to_book=0.62, ev_to_ebitda=7.0,
                     trailing_eps=560.0, book_value_ps=8_000.0, div_yield=3.6))
    return pd.DataFrame(rows)


def check_split_adjustment(failures: list) -> None:
    """kabutan's BPS is filed, not restated; its EPS is restated. A 5:1 split
    therefore breaks the PBR history and leaves the PER history intact.

    日立 (6501) is the real case: filed 2024/03 BPS of 6,155 against a
    post-split price reads as a PBR of 0.45, against 2.71 the next year. This
    reproduces that shape and asserts the correction removes it.
    """
    idx = pd.to_datetime(["2024-03-01", "2024-06-01", "2025-03-01", "2026-03-01"])
    close = pd.DataFrame({"6501.T": [3000.0, 3100.0, 3300.0, 3600.0]}, index=idx)
    splits = pd.DataFrame({"6501.T": [0.0, 5.0, 0.0, 0.0]}, index=idx)
    monthly = pd.concat({"Close": close, "Stock Splits": splits}, axis=1)

    df = pd.DataFrame([{
        "ticker": "6501",
        "eps_periods": ["2024.03", "2025.03", "2026.03"],
        "eps_vals": [126.9, 133.9, 176.8],          # already restated
        "bps_periods": ["2024.03", "2025.03", "2026.03"],
        "bps_vals": [6155.38, 1277.25, 1459.71],    # NOT restated
    }])
    out = P.build_valuation_history(df, monthly)
    pbr = [v for v in out["hist_pbr"].iloc[0] if v is not None]
    print("\n=== split adjustment ===")
    print("  PBR history after correction: %s" % pbr)
    if len(pbr) != 3:
        failures.append("split_history_incomplete")
        return
    # Pre-split year must land in the same neighbourhood as the others, not an
    # order of magnitude below. Uncorrected it would be 3000/6155 = 0.49.
    if not 2.0 < pbr[0] < 3.0:
        failures.append("split_not_adjusted (first year %.2f)" % pbr[0])
    if max(pbr) / min(pbr) > 2.0:
        failures.append("split_artefact_remains")


def check_history_screen(failures: list) -> None:
    """Both metrics must clear, not one of two."""
    cfg = ScreenConfig()
    df = pd.DataFrame([
        # Cheap on PER and PBR: passes.
        dict(ticker="A", industry="機械", trailing_pe=7.0, price_to_book=0.7,
             hist_per=[14.0, 15.0, 13.0], hist_pbr=[1.4, 1.5, 1.3],
             roe_ok=True, passes=False, avg_discount=0.0),
        # Cheap on PER only - a one-off gain does exactly this, and PBR is
        # what is supposed to catch it. Must NOT pass.
        dict(ticker="B", industry="機械", trailing_pe=7.0, price_to_book=1.5,
             hist_per=[14.0, 15.0, 13.0], hist_pbr=[1.4, 1.5, 1.3],
             roe_ok=True, passes=False, avg_discount=0.0),
        # Cheap on both but only two usable years: no benchmark at all.
        dict(ticker="C", industry="機械", trailing_pe=7.0, price_to_book=0.7,
             hist_per=[14.0, 15.0], hist_pbr=[1.4, 1.5],
             roe_ok=True, passes=False, avg_discount=0.0),
    ])
    out, _ = JF.apply_history_screen(df, cfg)
    got = dict(zip(out["ticker"], out["hist_passes"]))
    print("\n=== own-history screen ===")
    for t, want in (("A", True), ("B", False), ("C", False)):
        ok = bool(got.get(t)) == want
        print("  %s %s expected=%s got=%s" % ("OK  " if ok else "FAIL", t, want,
                                              bool(got.get(t))))
        if not ok:
            failures.append("history_%s" % t)


def main() -> int:
    cfg = ScreenConfig()
    df = make_universe()

    df, jstats = JF.apply_japan_filters(df, cfg)
    res, stats = run_screen(df, JPY_USD, cfg)
    res = JF.add_quality_context(res)
    res = JF.add_valueup_flags(res)
    res, roestats = JF.apply_roe_gate(res, cfg)
    res, absstats = JF.apply_absolute_screen(res, cfg)

    print("=== funnel ===")
    for k, v in {**jstats, **stats, **roestats, **absstats}.items():
        print("  %-28s %s" % (k, v))

    idx = res.set_index("ticker")
    expected = {
        "9001": True, "9007": True,
        "90015": False, "90025": False, "9005": False, "9006": False,
        "9008": False, "9009": False,
    }

    print("\n=== planted cases ===")
    failures = []
    for t, want in expected.items():
        present = t in idx.index
        got = bool(idx.loc[t, "passes"]) if present else False
        ok = got == want
        why = "" if present else "  (removed before scoring)"
        print("  %s %-6s expected=%-5s got=%-5s%s"
              % ("OK  " if ok else "FAIL", t, want, got, why))
        if not ok:
            failures.append(t)

    # The holdco must survive as flagged rather than be silently dropped.
    if "9002" not in idx.index:
        failures.append("holdco_was_dropped")
    elif not bool(idx.loc["9002", "is_holdco"]):
        failures.append("holdco_not_flagged")
    else:
        print("  OK   9002   holdco retained and flagged (passes=%s)"
              % bool(idx.loc["9002", "passes"]))

    # Invariant 6: a bank has no EV/EBITDA, and must still be able to clear
    # the absolute screen through the carve-out.
    if "9010" not in idx.index:
        failures.append("bank_missing")
    else:
        bank = idx.loc["9010"]
        if pd.notna(bank["ev_to_ebitda"]):
            failures.append("bank_ev_not_suppressed")
        else:
            print("  OK   9010   EV/EBITDA suppressed for 銀行業")
        if not bool(bank["abs_passes"]):
            failures.append("bank_blocked_by_missing_ev")
        else:
            print("  OK   9010   bank clears the absolute screen on PBR + ROE")

    # Board separation: the two cohorts must not be pooled.
    pri = res[(res["board"] == "PRIME") & res["trailing_pe_peer_median"].notna()]
    grw = res[(res["board"] == "GROWTH") & res["trailing_pe_peer_median"].notna()]
    if not pri.empty and not grw.empty:
        pm, gm = pri["trailing_pe_peer_median"].median(), grw["trailing_pe_peer_median"].median()
        print("\n  PRIME 機械 peer PER: %.1f   GROWTH サービス業 peer PER: %.1f" % (pm, gm))
        if not gm > pm + 10:
            failures.append("board_separation")
    else:
        failures.append("board_cohorts_missing")

    check_split_adjustment(failures)
    check_history_screen(failures)

    # CAGR must refuse to invent a rate the data cannot support.
    print("\n=== CAGR guards ===")
    for label, series, want_nan in (
            ("positive span", [100.0, 120.0, 144.0], False),
            ("negative base", [-50.0, 20.0, 40.0], True),
            ("negative end", [100.0, 20.0, -40.0], True)):
        v = P._cagr(series)
        ok = np.isnan(v) == want_nan
        print("  %s %-14s -> %s" % ("OK  " if ok else "FAIL", label,
                                    "n/a" if np.isnan(v) else "%.1f%%" % v))
        if not ok:
            failures.append("cagr_%s" % label.replace(" ", "_"))

    print("\n=== passing ===")
    hits = res[res["passes_any"]][["name", "board", "industry", "trailing_pe",
                                   "price_to_book", "roe_pct", "div_yield",
                                   "avg_discount", "screen"]]
    print(hits.round(2).to_string(index=False) if not hits.empty else "  (none)")

    print("\n" + ("ALL CHECKS PASSED" if not failures else "FAILURES: %s" % failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
