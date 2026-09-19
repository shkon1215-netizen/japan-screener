"""Pre-flight diagnostic for the Japan screener.

    python check_setup.py

Tests every live call the screen depends on, one at a time, and prints what
came back. Run this before main_jp.py. A full run takes minutes and, more to
the point, several of these sources fail by SUCCEEDING - a renamed column, a
throttled page served with HTTP 200, a chart endpoint that answers with no
rows. Those produce a silent all-NaN merge downstream rather than an error,
and the screen then reports a confident wrong answer. This is where that gets
caught, while it is still cheap.
"""
from __future__ import annotations

import sys
import traceback

import pandas as pd

OK, BAD = "  ok  ", " FAIL "
results = []


def step(name):
    def deco(fn):
        def run():
            print("\n--- %s" % name)
            try:
                detail = fn()
                print("%s %s" % (OK, detail or ""))
                results.append((name, True))
            except Exception as e:                            # noqa: BLE001
                print("%s %s: %s" % (BAD, type(e).__name__, e))
                traceback.print_exc(limit=2)
                results.append((name, False))
        run.__name__ = fn.__name__
        return run
    return deco


@step("imports and config")
def t_imports():
    import config_jp
    import japan_filters                                       # noqa: F401
    import providers_jp                                        # noqa: F401
    import screener                                            # noqa: F401
    cfg = config_jp.ScreenConfig()
    return ("pandas %s | mcap floor $%.0fm | ADV floor $%.0fm | ROE %g%% | CoE %g%%"
            % (pd.__version__, cfg.min_market_cap_usd / 1e6,
               cfg.min_adv_usd / 1e6, cfg.min_roe_pct, cfg.abs_cost_of_equity_pct))


@step("FX (frankfurter, ECB rates)")
def t_fx():
    import providers_jp as P
    jpy_usd = P.jpy_to_usd()
    return "1 USD = %.2f JPY  (multiplied as %.8f USD per JPY)" % (1 / jpy_usd, jpy_usd)


@step("JPX roster (data_j.xlsx)")
def t_roster():
    import config_jp as C
    import providers_jp as P
    r = P.JPXProvider(C.ScreenConfig()).listing_roster()
    if r.empty:
        raise RuntimeError("roster came back empty")
    miss = [c for c in ("ticker", "name", "board", "industry") if c not in r.columns]
    if miss:
        raise RuntimeError("missing columns: %s" % miss)
    n_ind = r["industry"].replace("", pd.NA).nunique()
    five = int(r["ticker"].str.len().eq(5).sum())
    return ("%d domestic lines, %d industries, %s | %d five-character 種類株式"
            % (len(r), n_ind,
               ", ".join("%s %d" % (b, int((r["board"] == b).sum()))
                         for b in ("PRIME", "STANDARD", "GROWTH")), five))


@step("JPX 監理・整理銘柄")
def t_supervision():
    import config_jp as C
    import providers_jp as P
    codes = P.JPXProvider(C.ScreenConfig()).supervised_codes()
    if not codes:
        raise RuntimeError("no codes parsed - page layout may have changed. "
                           "This does NOT stop a run; it silently disables the "
                           "filter, so check it rather than ignoring it.")
    return "%d codes, e.g. %s" % (len(codes), ", ".join(sorted(codes)[:6]))


@step("Yahoo Japan market-cap ranking")
def t_caps():
    import config_jp as C
    import providers_jp as P
    cfg = C.ScreenConfig()
    # Only the top of the list - enough to prove the columns still parse.
    caps = P.YahooJPRanking(cfg).market_cap_snapshot(1e15, max_pages=1)
    if caps.empty:
        raise RuntimeError("no rows - the ranking page layout may have changed")
    bad = caps[caps["close_jpy"].isna() | caps["market_cap_local"].isna()]
    if len(bad):
        raise RuntimeError("%d rows lost price or market cap in parsing" % len(bad))
    top = caps.iloc[0]
    return ("%d rows, as of %s | top: %s close %.0f JPY, cap %.3g JPY"
            % (len(caps), caps.attrs.get("asof", "?"), top["ticker"],
               top["close_jpy"], top["market_cap_local"]))


@step("kabutan filed fundamentals")
def t_kabutan():
    import config_jp as C
    import providers_jp as P
    cfg = C.ScreenConfig()
    cache = P.Cache(cfg.cache_dir, cfg.cache_ttl_hours)
    f = P.fetch_fundamentals(["7203", "8306", "6501"], cache, workers=1)
    if len(f) < 3:
        raise RuntimeError("only %d of 3 returned - kabutan may be shedding load" % len(f))
    need = ("trailing_eps", "book_value_ps", "roe_reported", "dps",
            "eps_periods", "bps_periods")
    miss = [c for c in need if c not in f.columns or f[c].isna().all()]
    if miss:
        raise RuntimeError("empty or missing: %s" % miss)
    r = f[f["ticker"] == "7203"].iloc[0]
    return ("7203: EPS %.1f, BPS %.1f, ROE %.1f%%, DPS %.0f | %d filed years, "
            "forecast EPS %s (carried, never screened)"
            % (r["trailing_eps"], r["book_value_ps"], r["roe_reported"], r["dps"],
               len(r["eps_periods"]), r.get("fwd_eps")))


@step("yfinance batched prices (liquidity + history)")
def t_prices():
    import providers_jp as P
    daily = P.fetch_price_panel(["7203", "8306", "6501"], "3mo", "1d")
    if daily.empty:
        raise RuntimeError("no daily panel - Yahoo chart endpoint unreachable")
    adv = P.average_daily_value(daily, ["7203", "8306", "6501"])
    if adv.empty or adv["adv_local"].isna().all():
        raise RuntimeError("daily panel returned no traded value")
    monthly = P.fetch_price_panel(["6501"], "8y", "1mo", actions=True)
    has_splits = "Stock Splits" in monthly
    if not has_splits:
        raise RuntimeError("no split column - the PBR history would be wrong "
                           "across any split (see build_valuation_history)")
    return ("ADV 7203 = %.3g JPY/day | monthly panel %d rows, splits present"
            % (float(adv.set_index("ticker").loc["7203", "adv_local"]), len(monthly)))


@step("yfinance .info (EV/EBITDA) - the one that throttles")
def t_info():
    import config_jp as C
    import providers_jp as P
    cfg = C.ScreenConfig()
    enr = P.JapanEnricher(cfg, P.Cache(cfg.cache_dir, cfg.cache_ttl_hours))
    if not enr.probe():
        raise RuntimeError("probe failed - Yahoo is throttling this IP. Wait a "
                           "few minutes, or run with --no-ev to screen on PER "
                           "and PBR alone.")
    df = enr.enrich(["7203", "8306"])
    ev = df.set_index("ticker")["ev_to_ebitda"]
    # A bank having no EV/EBITDA is correct, not a failure (invariant 6).
    return ("7203 EV/EBITDA %s | 8306 (bank) %s - blank is correct for a bank"
            % (ev.get("7203"), ev.get("8306")))


@step("offline logic tests")
def t_offline():
    import subprocess
    p = subprocess.run([sys.executable, "test_japan.py"],
                       capture_output=True, text=True, encoding="utf-8")
    tail = (p.stdout or "").strip().splitlines()[-1:] or ["(no output)"]
    if p.returncode != 0:
        raise RuntimeError(tail[0])
    return tail[0]


def main() -> int:
    print("Japan screener pre-flight\n" + "=" * 60)
    for fn in (t_imports, t_fx, t_roster, t_supervision, t_caps, t_kabutan,
               t_prices, t_info, t_offline):
        fn()
    print("\n" + "=" * 60)
    bad = [n for n, ok in results if not ok]
    for n, ok in results:
        print("%s %s" % (OK if ok else BAD, n))
    if bad:
        print("\n%d check(s) failed: %s" % (len(bad), ", ".join(bad)))
        print("Fix these before running main_jp.py - a broken source usually "
              "shows up downstream as an all-NaN column, not as an error.")
        return 1
    print("\nAll checks passed. Run: python main_jp.py -v")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
