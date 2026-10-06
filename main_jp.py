"""Japan (TSE) relative-valuation screener.

  python main_jp.py                          # Prime + Standard + Growth
  python main_jp.py --board PRIME            # Prime only
  python main_jp.py --min-roe 8              # the Ito Review's floor
  python main_jp.py --discount 0.15 --min-metrics 1

Order matters. The cheap cross-sectional calls - the JPX roster, the Yahoo
market-cap ranking, one batched price download - run first and cut 3,700
listings to about a thousand. Only then does anything run per ticker. That is
the difference between a ten-minute run and a two-hour one, and it is why the
gates are not in the order a reader might expect (invariant 7).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd

import japan_filters as JF
import providers_jp as P
from config_jp import BOARDS, ScreenConfig
from screener import run_screen


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="jp_screen_results.csv")
    p.add_argument("--board", choices=list(BOARDS) + ["ALL"], default="ALL")
    p.add_argument("--min-mcap", type=float, default=600e6, help="USD")
    p.add_argument("--min-adv", type=float, default=4e6, help="USD")
    p.add_argument("--discount", type=float, default=0.20)
    p.add_argument("--min-metrics", type=int, default=2)
    p.add_argument("--min-peers", type=int, default=5)
    p.add_argument("--peer-keys", default="industry,board")
    p.add_argument("--fx", type=float, help="JPY per USD (default: live - ECB via Frankfurter, then Yahoo)")
    p.add_argument("--include-class-shares", action="store_true",
                   help="keep 種類株式 - see japan_filters for why this is off")
    p.add_argument("--exclude-holdcos", action="store_true")
    p.add_argument("--skip-liquidity", action="store_true")
    p.add_argument("--min-roe", type=float, default=5.0,
                   help="ROE%% floor applied to survivors; 0 disables. "
                        "8 is the Ito Review's benchmark.")
    p.add_argument("--abs-pbr", type=float, default=1.0)
    p.add_argument("--abs-ev", type=float, default=8.0)
    p.add_argument("--abs-strict-financials", action="store_true",
                   help="require EV/EBITDA of financials too (they have none, "
                        "so none will pass)")
    p.add_argument("--no-abs-roe", action="store_true")
    p.add_argument("--no-abs-fair-pbr", action="store_true")
    p.add_argument("--coe", type=float, default=8.0,
                   help="cost of equity %% for the fair-PBR test")
    p.add_argument("--abs-min-div", type=float, default=2.0)
    p.add_argument("--source", choices=["auto", "kabutan", "yfinance"],
                   default="auto",
                   help="filed fundamentals. kabutan is richer but blocks "
                        "datacenter IPs, so CI must use yfinance; auto probes "
                        "and falls back.")
    p.add_argument("--no-ev", action="store_true",
                   help="skip the yfinance .info pass; screens on PER and PBR "
                        "alone and leaves EV/EBITDA missing everywhere")
    p.add_argument("--no-history", action="store_true",
                   help="skip the own-filed-history screen")
    p.add_argument("--hist-discount", type=float, default=0.30)
    p.add_argument("--hist-min-metrics", type=int, default=2)
    p.add_argument("--keep-supervised", action="store_true",
                   help="do not drop 監理銘柄 / 整理銘柄")
    p.add_argument("--dashboard", default="jp_dashboard.html",
                   help="self-contained HTML dashboard; pass '' to skip")
    p.add_argument("--all", action="store_true",
                   help="write every scored row, not just the passing ones")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def fetch_fundamentals(choice: str, tickers, cache, cfg, log):
    """Filed per-share figures, from whichever source this machine can reach.

    Two sources, and the choice is not about quality alone - it is about where
    the code is running. kabutan answers HTTP 405 to every request from a
    GitHub runner (659 of 659 on the first CI run), and minkabu and irbank's
    HTML site answer 403. That is a deliberate block on datacenter traffic, so
    CI cannot use them and it would be wrong to try.

      kabutan   preferred when reachable. Carries the company's own 会社予想,
                which is a formal disclosure and the basis of every PER quoted
                in Japan, plus a filed DPS. Four years of EPS, three of BPS.
      yfinance  works everywhere, including CI. Five filed columns of income
                statement, balance sheet and cash flow, so the own-history
                screen is actually DEEPER. No company guidance - yfinance's
                forwardEps is analyst consensus, which is a different thing
                and is not carried in its place (invariant 12).

    `auto` probes kabutan with three tickers and falls back rather than
    discovering the block 660 requests later.
    """
    tickers = list(tickers)
    if choice == "auto":
        probe = P.fetch_fundamentals(tickers[:3], cache, workers=1)
        choice = "kabutan" if len(probe) >= 2 else "yfinance"
        if choice == "yfinance":
            log.warning("kabutan is not answering (%s) - falling back to "
                        "yfinance. Forecast columns will be empty.",
                        P.fetch_failure_summary() or "no responses")
        log.info("fundamentals source auto-selected: %s", choice)

    log.info("fetching filed fundamentals for %d names from %s...",
             len(tickers), choice)
    if choice == "kabutan":
        fund = P.fetch_fundamentals(tickers, cache, delay=0.0,
                                    workers=cfg.kabutan_workers)
    else:
        fund = P.fetch_fundamentals_yf(tickers, cache, delay=0.0,
                                       workers=cfg.yf_stmt_workers)
    if fund.empty:
        log.error("no fundamentals came back. What the source actually said: %s",
                  P.fetch_failure_summary() or "nothing - no responses at all")
        return None, choice
    covered = len(fund) / max(len(tickers), 1)
    if covered < cfg.min_priced_fraction:
        log.error("only %d of %d names returned fundamentals (%.0f%%). "
                  "Screening a partial universe reports a plausible lie; "
                  "re-run to fill the gap from cache.",
                  len(fund), len(tickers), covered * 100)
        return None, choice
    return fund, choice


def main() -> int:
    a = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("jp")

    boards = tuple(BOARDS) if a.board == "ALL" else (a.board,)
    cfg = ScreenConfig(
        min_market_cap_usd=a.min_mcap,
        min_adv_usd=0.0 if a.skip_liquidity else a.min_adv,
        discount_threshold=a.discount,
        min_metrics_passing=a.min_metrics,
        min_peers=a.min_peers,
        min_roe_pct=a.min_roe,
        abs_max_pbr=a.abs_pbr,
        abs_max_ev_ebitda=a.abs_ev,
        abs_require_roe=not a.no_abs_roe,
        abs_financials_pbr_only=not a.abs_strict_financials,
        abs_require_pbr_vs_roe=not a.no_abs_fair_pbr,
        abs_cost_of_equity_pct=a.coe,
        abs_min_div_yield=a.abs_min_div,
        exclude_supervised=not a.keep_supervised,
        hist_min_discount=a.hist_discount,
        hist_min_metrics=a.hist_min_metrics,
        peer_keys=tuple(k.strip() for k in a.peer_keys.split(",") if k.strip()),
        exclude_class_shares=not a.include_class_shares,
        exclude_holdcos=a.exclude_holdcos,
        boards=boards,
    )

    jpx = P.JPXProvider(cfg)
    cache = jpx.cache

    # 1. roster + Japan share-class hygiene. No per-ticker calls at all.
    roster = jpx.listing_roster()
    if roster.empty:
        log.error("empty roster - JPX unreachable?")
        return 1
    roster, jstats = JF.apply_japan_filters(roster, cfg)
    if cfg.exclude_supervised:
        roster, sstats = JF.apply_supervision_filter(roster, jpx.supervised_codes())
        jstats.update(sstats)

    jpy_usd = (1.0 / a.fx) if a.fx else P.jpy_to_usd()
    log.info("FX: 1 USD = %.2f JPY", 1.0 / jpy_usd)
    floor_jpy = cfg.min_market_cap_usd / jpy_usd

    # 2. cross-sectional market cap: ~20 requests for the whole market,
    #    because the ranking is sorted and we stop at the floor.
    caps = P.YahooJPRanking(cfg).market_cap_snapshot(floor_jpy)
    if caps.empty:
        log.error("no market-cap cross-section - Yahoo Japan unreachable?")
        return 1
    asof = caps.attrs.get("asof", "")
    df = roster.merge(caps, on="ticker", how="inner")
    df["market_cap_usd"] = pd.to_numeric(df["market_cap_local"], errors="coerce") * jpy_usd
    pre = df[df["market_cap_usd"] >= cfg.min_market_cap_usd].copy()
    log.info("%d of %d listings cleared the size gate", len(pre), len(roster))
    jstats["cleared_size"] = len(pre)
    if pre.empty:
        print("Nothing cleared the size gate.")
        return 0

    # 3. liquidity, from one batched download rather than one call per name.
    #    Japan publishes no free cross-sectional traded-value SERIES - Yahoo's
    #    turnover ranking is a single session - so this is a real three-month
    #    median rather than a one-day proxy, and it still costs ~20 requests.
    if not a.skip_liquidity:
        daily = P.fetch_price_panel(pre["ticker"], "3mo", "1d")
        adv = P.average_daily_value(daily, pre["ticker"].tolist())
        pre = pre.merge(adv, on="ticker", how="left")
        pre["adv_usd"] = pd.to_numeric(pre["adv_local"], errors="coerce") * jpy_usd
        before = len(pre)
        pre = pre[pre["adv_usd"].notna() & (pre["adv_usd"] >= cfg.min_adv_usd)]
        log.info("%d of %d cleared the liquidity gate", len(pre), before)
    else:
        pre["adv_local"] = np.nan
        pre["adv_usd"] = np.nan
    jstats["cleared_size_liquidity"] = len(pre)
    if pre.empty:
        print("Nothing cleared the liquidity gate.")
        return 0

    # 4. per-ticker work starts HERE and nowhere earlier.
    fund, source = fetch_fundamentals(a.source, pre["ticker"], cache, cfg, log)
    if fund is None:
        return 1
    pre = pre.merge(fund, on="ticker", how="left")

    if not a.no_ev:
        log.info("fetching EV/EBITDA for %d names...", len(pre))
        try:
            enr = P.JapanEnricher(cfg, cache).enrich(pre["ticker"])
            if not enr.empty:
                pre = pre.merge(enr, on="ticker", how="left")
        except RuntimeError as e:
            log.error("%s", e)
            return 1
    for c in ("ev_to_ebitda", "total_cash", "total_debt"):
        if c not in pre.columns:
            pre[c] = np.nan

    # Split events arrive with the monthly panel, and today's per-share figures
    # need them BEFORE any multiple is struck - a split since the last filing
    # leaves EPS and BPS on the old share count. Fetched once, reused by the
    # history below. See P.restate_recent_splits.
    monthly = P.fetch_price_panel(pre["ticker"], "2y" if a.no_history else "8y",
                                  "1mo", actions=True)
    pre = P.restate_recent_splits(pre, monthly)
    notes = pre["split_note"].value_counts().to_dict()
    jstats["split_restated"] = int(notes.get("restated for split", 0)
                                   + notes.get("kabutan BPS restated", 0))
    jstats["split_basis_mismatch"] = int(notes.get("share basis mismatch", 0))

    # Today's multiples, struck on the SAME filed figures the history uses.
    # This is the property that makes "cheap against its own history" mean
    # anything: both ends of the comparison are built the same way.
    close = pd.to_numeric(pre["close_jpy"], errors="coerce")
    eps = pd.to_numeric(pre["trailing_eps"], errors="coerce")
    bps = pd.to_numeric(pre["book_value_ps"], errors="coerce")
    dps = pd.to_numeric(pre.get("dps"), errors="coerce")
    # Under --source yfinance there is no filed 一株配当, so fall back to the
    # trailing rate the .info pass already returned. Still trailing cash
    # actually paid, not a forecast, so the screen's meaning is unchanged.
    if "dividend_rate" in pre.columns:
        dps = dps.fillna(pd.to_numeric(pre["dividend_rate"], errors="coerce"))
    pre["trailing_pe"] = (close / eps).where(eps > 0)
    pre["price_to_book"] = (close / bps).where(bps > 0)
    pre["div_yield"] = (dps / close * 100.0).where(close > 0)
    # The company's own guidance, carried for context and never screened on.
    fdps = pd.to_numeric(pre.get("fwd_dps"), errors="coerce")
    feps = pd.to_numeric(pre.get("fwd_eps"), errors="coerce")
    pre["div_yield_fwd"] = (fdps / close * 100.0).where(close > 0)
    pre["forward_pe"] = (close / feps).where(feps > 0)

    if not a.no_history:
        log.info("reconstructing filed valuation history...")
        pre = P.build_valuation_history(pre, monthly)

    # 5. screen
    res, stats = run_screen(pre, jpy_usd, cfg)
    if res.empty:
        print("Nothing survived screening.")
        return 0
    res = JF.add_quality_context(res)
    res = JF.add_valueup_flags(res)
    if cfg.min_roe_pct > 0:
        res, roestats = JF.apply_roe_gate(res, cfg)
        stats = {**stats, **roestats}
    else:
        res["roe_ok"], res["roe_tier"] = True, ""

    res, absstats = JF.apply_absolute_screen(res, cfg)
    stats = {**stats, **absstats}

    if not a.no_history and "hist_per" in res.columns:
        res, hstats = JF.apply_history_screen(res, cfg)
        stats = {**stats, **hstats}

    funnel = {**jstats, **stats}
    print("\n--- funnel ---")
    for k, v in funnel.items():
        print("  %-28s %s" % (k, v))

    out = res if a.all else res[res.get("passes_any", res["passes"])]
    cols = [c for c in JF.japan_output_columns(cfg) if c in res.columns]
    out[cols].to_csv(a.out, index=False, encoding="utf-8-sig")

    hits = res[res.get("passes_any", res["passes"])]
    print("\n--- %d stock(s) cleared at least one screen ---" % len(hits))
    if not hits.empty:
        show = hits[["ticker", "name", "board", "industry", "market_cap_usd",
                     "trailing_pe", "price_to_book", "roe_pct", "div_yield",
                     "avg_discount", "screen"]].head(30).copy()
        show["mcap_$m"] = (show.pop("market_cap_usd") / 1e6).round(0).astype("Int64")
        show["avg_discount"] = show["avg_discount"].map(
            lambda x: "%.1f%%" % (x * 100) if pd.notna(x) else "")
        print(show.to_string(index=False))

    meta = {
        "asof": asof, "board": a.board, "source": source,
        "cmd": "python main_jp.py " + " ".join(sys.argv[1:]),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "jpy_per_usd": round(1.0 / jpy_usd, 2),
        "funnel": funnel,
        "thresholds": {
            "min_mcap_usd": cfg.min_market_cap_usd,
            "min_adv_usd": 0 if a.skip_liquidity else cfg.min_adv_usd,
            "skip_liquidity": bool(a.skip_liquidity),
            "discount": cfg.discount_threshold,
            "min_metrics": cfg.min_metrics_passing,
            "min_peers": cfg.min_peers,
            "min_valid_metrics": cfg.min_valid_metrics,
            "min_roe_pct": cfg.min_roe_pct,
            "roe_good_pct": cfg.roe_good_pct,
            "roe_ito_pct": cfg.roe_ito_pct,
            "abs_max_pbr": cfg.abs_max_pbr,
            "abs_max_ev_ebitda": cfg.abs_max_ev_ebitda,
            "abs_require_roe": cfg.abs_require_roe,
            "abs_financials_pbr_only": cfg.abs_financials_pbr_only,
            "abs_require_pbr_vs_roe": cfg.abs_require_pbr_vs_roe,
            "abs_cost_of_equity_pct": cfg.abs_cost_of_equity_pct,
            "abs_min_div_yield": cfg.abs_min_div_yield,
            "hist_min_discount": cfg.hist_min_discount,
            "hist_min_metrics": cfg.hist_min_metrics,
            "hist_min_years": cfg.hist_min_years,
        },
    }
    meta_path = os.path.splitext(a.out)[0] + "_meta.json"
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print("\nwrote %s and %s" % (a.out, meta_path))

    if a.dashboard:
        try:
            from dashboard import build_dashboard, sibling_boards
            build_dashboard(a.out, meta_path, a.dashboard,
                            boards=sibling_boards(a.board, a.dashboard))
            print("wrote %s   <- open this" % a.dashboard)
        except Exception as e:                                # noqa: BLE001
            log.error("dashboard build failed: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
