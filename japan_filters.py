"""Japan-specific filters layered on top of the generic screener.

The shape is Korea's; the content is not. Every market has share classes and
vehicles whose cheapness is structural rather than an opportunity, and finding
them is the part that does not port. Japan's set turned out to be small,
sharply defined, and entirely resolvable from exchange data - which is the
opposite of the UK, where the equivalent class (investment trusts) is large
and invisible to every heuristic.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

import config_jp as K

log = logging.getLogger(__name__)


def _fin_mask(df: pd.DataFrame) -> pd.Series:
    """Banks, brokers, insurers and other financials, by 33業種.

    Korea had to match the substring "Financial" in a vendor sector string.
    Here the exchange states it, so invariant 6 stops being a guess.
    """
    ind = df.get("industry", pd.Series("", index=df.index)).fillna("")
    return ind.map(K.is_financial)


def tag_share_classes(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["is_class_share"] = df["ticker"].map(K.is_class_share)
    df["common_line"] = df["ticker"].map(K.common_line_of)
    df["is_holdco"] = df["name"].map(K.is_holdco)
    return df


def apply_japan_filters(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple:
    """Remove share classes whose discount is structural, not an opportunity.

    Japan's list is short because JPX has already done most of the work: the
    roster's 市場・商品区分 separates ETFs and ETNs, REITs and infrastructure
    funds, TOKYO PRO Market and foreign listings into their own categories, so
    providers_jp never admits them. Korea had to strip all of that by name,
    and the UK had to reverse-engineer it from a trade-body register.

    What is left is 種類株式: seven five-character lines that sit in プライム
    alongside ordinary equity. 伊藤園's 第1種優先株式 (25935) is the Japanese
    textbook case - no vote, a dividend premium, and a persistent 40-50%
    discount to the common that has nothing to do with the business being
    cheap. The other six are 社債型種類株式, bond-type class shares with a
    fixed redemption; a PBR struck on one of those is not a valuation at all.
    """
    df = tag_share_classes(df)
    stats = {"listings": len(df)}

    if cfg.exclude_class_shares:
        n = int(df["is_class_share"].sum())
        if n:
            log.info("種類株式 removed: %s",
                     ", ".join(df.loc[df["is_class_share"], "name"].head(10)))
        df = df[~df["is_class_share"]]
        stats["dropped_class_share"] = n

    if cfg.exclude_holdcos:
        n = int(df["is_holdco"].sum())
        df = df[~df["is_holdco"]]
        stats["dropped_holdco"] = n
    else:
        stats["flagged_holdco"] = int(df["is_holdco"].sum())

    stats["after_japan_filters"] = len(df)
    return df.copy(), stats


def apply_supervision_filter(df: pd.DataFrame, codes: set) -> tuple:
    """Drop 監理銘柄 and 整理銘柄.

    監理 means the delisting criteria are in play; 整理 means the decision is
    made and the line has weeks left to trade. Both still quote, and a company
    on its way off the exchange prices like a bargain for a reason that is not
    value. Korea's equivalent list matches on company NAME and so can be
    defeated by a rename; JPX publishes codes, so this one is exact.

    Runs on the roster, before the size gate, so the funnel shows it and so it
    keeps protecting when --min-mcap is lowered.
    """
    if not codes:
        return df, {"dropped_supervised": 0}
    df = df.copy()
    hit = df["ticker"].astype(str).str.strip().isin(codes)
    n = int(hit.sum())
    if n:
        log.info("監理・整理銘柄 removed: %s", ", ".join(df.loc[hit, "name"].head(10)))
    return df[~hit].copy(), {"dropped_supervised": n}


def add_quality_context(df: pd.DataFrame) -> pd.DataFrame:
    """Cheap context that separates value from value trap.

    ROE is derived from the same filed EPS and BPS the PER and PBR are struck
    on, rather than taken from a vendor field, so it cannot disagree with the
    multiples being screened. kabutan also publishes its own ROE; that is kept
    alongside as roe_reported and used only when the derived figure is
    unavailable, because a company with negative book value has an ROE that is
    arithmetically defined and economically meaningless.
    """
    df = df.copy()
    eps = pd.to_numeric(df.get("trailing_eps"), errors="coerce")
    bps = pd.to_numeric(df.get("book_value_ps"), errors="coerce")
    derived = np.where((bps > 0) & eps.notna(), eps / bps * 100.0, np.nan)
    rep = pd.to_numeric(df.get("roe_reported"), errors="coerce")
    df["roe_pct"] = pd.Series(derived, index=df.index).fillna(rep)
    df["div_yield"] = pd.to_numeric(df.get("div_yield"), errors="coerce")
    df["pays_dividend"] = df["div_yield"].fillna(0) > 0
    df["net_cash_to_mcap"] = _net_cash_ratio(df)
    return df


def _net_cash_ratio(df: pd.DataFrame) -> pd.Series:
    """(cash - interest-bearing debt) / market cap.

    Not a screening metric - it is context, and it is here because it is the
    single most Japan-specific number on the page. A large minority of TSE
    companies hold net cash worth a substantial fraction of their own market
    value, which is exactly what the TSE's 2023 request was aimed at and what
    makes a low PBR here mean something different from a low PBR elsewhere.
    Add it to the screen only if asked; it is shown, not tested.
    """
    cash = pd.to_numeric(df.get("total_cash"), errors="coerce")
    debt = pd.to_numeric(df.get("total_debt"), errors="coerce")
    mcap = pd.to_numeric(df.get("market_cap_local"), errors="coerce")
    return ((cash - debt) / mcap).where(mcap > 0)


def add_valueup_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Flag the cohort the Tokyo Stock Exchange itself is pressing.

    In March 2023 the TSE asked every Prime and Standard company to disclose
    an "action to implement management that is conscious of cost of capital
    and stock price", aimed squarely at companies trading below book or
    earning less than their cost of capital. It is the closest analogue to
    Korea's Value-Up programme and it is the live catalyst attached to a
    screen almost identical to this one.

    Two caveats, both real:

      - `pbr_bottom20_industry` is a CURRENT cross-section, like Korea's. It
        is a useful ranking, not the exchange's criterion.
      - JPX publishes the list of companies that have actually DISCLOSED a
        plan, but only as PDFs, so this cannot tell a company under pressure
        from one that has already responded. See known gaps in CLAUDE.md.
    """
    df = df.copy()
    rank = df.get("price_to_book_pct_rank")
    df["pbr_bottom20_industry"] = (rank <= 0.20) if rank is not None else False
    pbr = pd.to_numeric(df.get("price_to_book"), errors="coerce")
    roe = pd.to_numeric(df.get("roe_pct"), errors="coerce")
    df["pbr_below_1"] = pbr < 1.0
    # The TSE's own framing: below book, or not earning the Ito Review's 8%.
    df["tse_focus"] = (pbr < 1.0) | (roe < K.ScreenConfig.roe_ito_pct)
    return df


def apply_roe_gate(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple:
    """Require the screen's survivors to actually earn something.

    Runs AFTER scoring, because it narrows `passes` rather than replacing it.
    Nothing about avg_discount changes - invariant 5 still averages every
    metric with data. Gating earlier would change the peer medians themselves,
    and the cohort a stock is measured against has to include the low-ROE
    names, because that is what makes it representative.

    Missing ROE fails. A company whose EPS or BPS could not be resolved is not
    a company we can call profitable.

    The tiers are set on the user's standing preference (5% floor, double
    digits preferred). Japan's own benchmark is the Ito Review's 8%, which the
    dashboard marks but does not enforce - `--min-roe 8` does that.
    """
    df = df.copy()
    roe = pd.to_numeric(df.get("roe_pct"), errors="coerce")
    df["roe_ok"] = roe.notna() & (roe >= cfg.min_roe_pct)
    df["roe_tier"] = np.select(
        [roe >= 15.0, roe >= cfg.roe_good_pct, roe >= cfg.min_roe_pct],
        ["strong", "good", "marginal"], default="fail")

    before = int(df["passes"].sum())
    df["passes"] = df["passes"] & df["roe_ok"]
    after = int(df["passes"].sum())
    stats = {"dropped_roe_below_%g" % cfg.min_roe_pct: before - after,
             "passing_after_roe": after}
    return df.sort_values(["passes", "avg_discount"], ascending=[False, False]), stats


def apply_absolute_screen(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple:
    """Absolute cheapness, independent of the peer comparison.

    The relative screen asks what the neighbours trade at. That misses a
    cohort which is cheap outright while sitting in an industry where
    everything is cheap - and in Japan that describes whole sections of the
    market: 銀行業, 卸売業, 鉄鋼, 建設業 all trade as a block.

    The fair-PBR test is the only one here that says WHY a low PBR is wrong
    rather than merely noting that it is low, and in Japan it is not an
    imported idea: PBR < ROE / cost of equity is the arithmetic behind the
    TSE's own 2023 request. At the Ito Review's 8% cost of equity, fair PBR is
    exactly 1.0 when ROE is 8%, so the two tests agree with each other instead
    of pulling in different directions.

    Financials have no EV/EBITDA (invariant 6), so a strict both-metrics rule
    would exclude every bank, insurer and broker - which in Japan is precisely
    the corner trading furthest below book. `abs_financials_pbr_only` lets
    them qualify on PBR and ROE, and marks those rows so the weaker bar stays
    visible.
    """
    df = df.copy()
    pbr = pd.to_numeric(df.get("price_to_book"), errors="coerce")
    ev = pd.to_numeric(df.get("ev_to_ebitda"), errors="coerce")
    roe = pd.to_numeric(df.get("roe_pct"), errors="coerce")
    dy = pd.to_numeric(df.get("div_yield"), errors="coerce")
    fin = _fin_mask(df)

    df["abs_pbr_ok"] = pbr.notna() & (pbr < cfg.abs_max_pbr)
    df["abs_ev_ok"] = ev.notna() & (ev < cfg.abs_max_ev_ebitda)
    df["abs_ev_missing"] = ev.isna()
    df["abs_roe_ok"] = roe.notna() & (roe >= cfg.min_roe_pct)

    fair_pbr = roe / cfg.abs_cost_of_equity_pct
    df["abs_fair_pbr"] = fair_pbr
    df["abs_pbr_vs_roe_ok"] = pbr.notna() & fair_pbr.notna() & (pbr < fair_pbr)
    # Missing yield fails: unknown is not the same as paid.
    df["abs_div_ok"] = dy.notna() & (dy >= cfg.abs_min_div_yield)

    core = df["abs_pbr_ok"] & df["abs_ev_ok"]
    if cfg.abs_financials_pbr_only:
        core = core | (fin & df["abs_pbr_ok"])
        df["abs_via_carveout"] = fin & df["abs_pbr_ok"] & ~df["abs_ev_ok"]
    else:
        df["abs_via_carveout"] = False

    if cfg.abs_require_roe:
        core = core & df["abs_roe_ok"]
    if cfg.abs_require_pbr_vs_roe:
        core = core & df["abs_pbr_vs_roe_ok"]
    if cfg.abs_min_div_yield > 0:
        core = core & df["abs_div_ok"]
    df["abs_passes"] = core

    rel = df["passes"].astype(bool)
    absp = df["abs_passes"].astype(bool)
    df["screen"] = np.select([rel & absp, rel & ~absp, ~rel & absp],
                             ["both", "relative", "absolute"], default="")
    df["passes_any"] = rel | absp

    stats = {
        "abs_pbr_under_%g" % cfg.abs_max_pbr: int(df["abs_pbr_ok"].sum()),
        "abs_ev_under_%g" % cfg.abs_max_ev_ebitda: int(df["abs_ev_ok"].sum()),
        "abs_pbr_below_fair": int(df["abs_pbr_vs_roe_ok"].sum()),
        "abs_div_over_%gpct" % cfg.abs_min_div_yield: int(df["abs_div_ok"].sum()),
        "abs_passing": int(absp.sum()),
        "abs_via_financial_carveout": int((absp & df["abs_via_carveout"]).sum()),
        "abs_new_vs_relative": int((absp & ~rel).sum()),
        "passing_either_screen": int(df["passes_any"].sum()),
    }
    return df.sort_values(["passes_any", "avg_discount"], ascending=[False, False]), stats


# Korea screens three metrics against their own history. Japan screens two:
# no free source publishes a Japanese company's historical EV/EBITDA, and
# reconstructing one would mean inventing the depreciation line that neither
# kabutan nor irbank carries.
HIST_METRICS = (("per", "trailing_pe"), ("pbr", "price_to_book"))
HIST_SLOTS = 5


def _as_list(v) -> list:
    return list(v) if isinstance(v, (list, tuple, np.ndarray)) else []


def apply_history_screen(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple:
    """Cheap against the company's own filed history - the third screen.

    The relative screen asks whether a name is cheap against its industry; the
    absolute screen whether it is cheap outright. This asks whether it is
    cheap against ITSELF, which catches what both miss: a company that has
    always traded at a premium and has just de-rated, or one sitting in a
    sector that re-rated as a whole.

    Held to the same rules as the other two. The benchmark is a median
    (invariant 4) - one freak year would drag a mean far enough to make an
    ordinary year look cheap. A non-positive or out-of-bounds multiple is
    missing, never cheap, in history as well as today (invariant 2), so a loss
    year drops out of the benchmark rather than distorting it. Fewer than
    hist_min_years usable years means no benchmark: that is noise, not history.

    BOTH metrics must clear the threshold, where Korea requires two of three.
    With only two metrics available, allowing one to fail would let a single
    one-off gain carry a name through on PER alone - and a one-off is exactly
    what PBR is there to catch, because it does not move book value much.

    Trailing multiples cut both ways here, and Japan gives the effect extra
    force: guidance is a formal disclosure, so prices move on the forecast
    while this screen is still measuring the last filing. A company whose
    earnings have just surged reads EXPENSIVE against its own history until
    the next filing catches up. That is a real limitation, not a bug, and the
    forecast figures travel with each row so the reader can see it.
    """
    df = df.copy()
    disc_cols = []
    for key, cur_col in HIST_METRICS:
        lo, hi = K.METRIC_BOUNDS[cur_col]
        vals = df.get("hist_%s" % key, pd.Series([[]] * len(df), index=df.index)).map(_as_list)
        for i in range(HIST_SLOTS):
            df["hist_%s_y%d" % (key, i + 1)] = vals.map(
                lambda v, i=i: v[i] if i < len(v) else np.nan)

        def bench(v, lo=lo, hi=hi):
            ok = [float(x) for x in v
                  if x is not None and np.isfinite(float(x)) and lo <= float(x) <= hi]
            return float(np.median(ok)) if len(ok) >= cfg.hist_min_years else np.nan

        med = vals.map(bench)
        cur = pd.to_numeric(df.get(cur_col), errors="coerce")
        cur = cur.where((cur >= lo) & (cur <= hi))
        df["hist_%s_med" % key] = med
        df["hist_%s_disc" % key] = (med - cur) / med
        disc_cols.append("hist_%s_disc" % key)

    discs = df[disc_cols]
    df["hist_n_valid"] = discs.notna().sum(axis=1)
    df["hist_n_pass"] = (discs >= cfg.hist_min_discount).sum(axis=1)
    # Averages every metric with data, including the ones that failed - the
    # same rule as avg_discount (invariant 5).
    df["hist_avg_disc"] = discs.mean(axis=1, skipna=True)

    roe_ok = df.get("roe_ok", pd.Series(True, index=df.index)).fillna(False).astype(bool)
    hp = (df["hist_n_pass"] >= cfg.hist_min_metrics) & (df["hist_n_valid"] >= cfg.hist_min_metrics)
    if cfg.hist_require_roe:
        hp = hp & roe_ok
    df["hist_passes"] = hp

    rel = df["passes"].astype(bool)
    absp = df.get("abs_passes", pd.Series(False, index=df.index)).astype(bool)
    parts = pd.DataFrame({"relative": rel, "absolute": absp, "history": hp})
    df["screen"] = parts.apply(lambda r: " + ".join(k for k, v in r.items() if v), axis=1)
    df["passes_any"] = rel | absp | hp

    stats = {
        "hist_with_benchmark": int((df["hist_n_valid"] > 0).sum()),
        "hist_passing_%d%%" % round(cfg.hist_min_discount * 100): int(hp.sum()),
        "hist_new_vs_other_screens": int((hp & ~rel & ~absp).sum()),
        "passing_any_screen": int(df["passes_any"].sum()),
    }
    return df.sort_values(["passes_any", "avg_discount"], ascending=[False, False]), stats


def japan_output_columns(cfg: K.ScreenConfig) -> list:
    cols = ["ticker", "name", "board", "industry", "industry17", "topix_scale",
            "market_cap_usd", "adv_usd", "close_jpy"]
    for m in cfg.metrics:
        cols += [m, "%s_peer_median" % m, "%s_discount" % m,
                 "%s_peer_n" % m, "%s_pct_rank" % m]
    # Own filed history: the median benchmark, today's discount to it, and the
    # yearly values for the tooltip.
    cols += ["hist_years", "hist_n_valid", "hist_n_pass", "hist_avg_disc",
             "hist_passes"]
    for m in ("per", "pbr"):
        cols += ["hist_%s_med" % m, "hist_%s_disc" % m]
        cols += ["hist_%s_y%d" % (m, i) for i in range(1, HIST_SLOTS + 1)]
    # Three-year history, oldest first, in 億円, plus the compound rate.
    cols += ["fin_years", "fin_n"]
    for m in ("rev", "op", "ocf", "np"):
        cols += ["%s_y1" % m, "%s_y2" % m, "%s_y3" % m, "%s_cagr" % m]
    cols += ["screen", "passes_any", "abs_passes", "abs_pbr_ok", "abs_ev_ok",
             "abs_pbr_vs_roe_ok", "abs_div_ok", "abs_roe_ok", "abs_fair_pbr",
             "abs_via_carveout",
             "roe_pct", "roe_ok", "roe_tier", "div_yield", "pays_dividend",
             "fwd_eps", "fwd_dps", "forward_pe", "div_yield_fwd",
             "net_cash_to_mcap",
             "pbr_below_1", "pbr_bottom20_industry", "tse_focus", "is_holdco",
             "n_valid_metrics", "n_metrics_passing", "metrics_passing",
             "avg_discount", "median_pct_rank", "passes"]
    return cols
