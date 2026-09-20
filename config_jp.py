"""Configuration for the Japan (TSE) valuation screener.

Ported from the Korea build. The valuation maths is identical; what changes
here is every number that was calibrated against the Korean distribution, plus
the share-class rules, which are Japan's own.
"""
from __future__ import annotations

from dataclasses import dataclass

VALUATION_METRICS = ("trailing_pe", "price_to_book", "ev_to_ebitda")

METRIC_LABELS = {
    "trailing_pe": "PER",
    "price_to_book": "PBR",
    "ev_to_ebitda": "EV/EBITDA",
}

# Same role as Korea's: a multiple outside these bounds is MISSING, never
# cheap. Japan needs this at least as badly - roughly a fifth of TSE names
# post a loss in any given year, and a loss-maker's PER computed from a
# negative EPS would otherwise sort to the top of the screen.
METRIC_BOUNDS = {
    "trailing_pe": (1.0, 200.0),
    "price_to_book": (0.05, 30.0),
    "ev_to_ebitda": (0.5, 100.0),
}

# The four 33業種 codes that make enterprise value meaningless (invariant 6).
# Using the exchange's own classification instead of a vendor sector string
# makes this exact rather than a substring guess.
FINANCIAL_INDUSTRIES = ("銀行業", "証券、商品先物取引業", "保険業", "その他金融業")

# JPX 市場・商品区分 -> board label. Everything absent from this map is not a
# domestic operating company and never enters the universe: ETF・ETN,
# REIT・ベンチャーファンド・カントリーファンド・インフラファンド, PRO Market,
# 外国株式 and 出資証券.
SEGMENT_TO_BOARD = {
    "プライム（内国株式）": "PRIME",
    "スタンダード（内国株式）": "STANDARD",
    "グロース（内国株式）": "GROWTH",
}
BOARDS = ("PRIME", "STANDARD", "GROWTH")


@dataclass
class ScreenConfig:
    # --- Size / liquidity, specified in USD then converted at live FX ---
    min_market_cap_usd: float = 600_000_000     # ~JPY 95bn at 158
    min_adv_usd: float = 4_000_000              # ~JPY 630m
    adv_lookback_days: int = 60                 # trading days (yfinance 3m avg)

    # --- Valuation test ---
    discount_threshold: float = 0.20
    metrics: tuple[str, ...] = VALUATION_METRICS
    min_metrics_passing: int = 2
    min_valid_metrics: int = 2

    # --- Quality floor ---
    # 5% is the user's standing preference, carried over unchanged - it encodes
    # what they want from a holding, not what Japan costs, and preference
    # floors do not get recalibrated per market.
    #
    # Japan has its own famous number: the 2014 Ito Review set 8% ROE as the
    # minimum a listed company should clear to exceed its cost of capital, and
    # the TSE's 2023 request leans on the same threshold. `--min-roe 8` runs
    # the screen on that basis; the dashboard slider does it without a re-run.
    min_roe_pct: float = 5.0
    roe_good_pct: float = 10.0
    roe_ito_pct: float = 8.0        # shown as a reference line, not a gate

    # --- Absolute value screen ---
    # These levels are NOT preferences, and they are re-measured against the
    # local distribution rather than inherited, because a threshold only means
    # something against the distribution it was drawn from. Measured over the
    # 661 gated names on 2026-09-19:
    #
    #              p10    p25   median    p75    p90
    #   PER       10.50  14.19  18.05   25.73  37.87
    #   PBR        0.76   1.06   1.58    2.70   4.61
    #   EV/EBITDA  6.05   7.61   9.90   13.20  18.07
    #   ROE %      1.90   5.44   8.73   12.83  19.32
    #   Yield %    0.65   1.36   2.02    2.75   3.54
    #
    # Korea's numbers survive that test, and for once that is a measured
    # result rather than an assumption: PBR < 1.0 passes 20.3% of the Japanese
    # universe and EV/EBITDA < 8 passes 24.4%, so the two valuation tests are
    # about equally strict and both sit near p25. The UK needed re-cutting
    # precisely because they were not - there PBR < 1 was p10 against a yield
    # floor at p69, which is one strict test with three no-ops attached.
    #
    # PBR < 1.0 has a second claim here that it has in no other market: it is
    # the Tokyo Stock Exchange's own criterion, the line its March 2023
    # request was drawn at. Calibration and local convention agree.
    abs_max_pbr: float = 1.0
    abs_max_ev_ebitda: float = 8.0
    abs_require_roe: bool = True
    abs_financials_pbr_only: bool = True

    # Fair PBR ~ ROE / cost of equity. Korea uses 10%. Japan's cost of equity
    # is structurally lower, and the number the market itself argues about is
    # the Ito Review's 8% - the TSE's whole PBR-below-1 campaign is built on
    # "earn more than your cost of capital", with 8% the working figure. It
    # also makes the two tests cohere: at CoE 8%, fair PBR is 1.0 exactly when
    # ROE is 8%.
    abs_require_pbr_vs_roe: bool = True
    abs_cost_of_equity_pct: float = 8.0

    # Cash actually returned. Trailing, from the last filed 一株配当 - see
    # providers_jp for why the company forecast is carried but not screened on.
    abs_min_div_yield: float = 2.0

    # 監理銘柄 / 整理銘柄 - Japan's 관리종목 analogue. Unlike KIND's name-only
    # list, JPX publishes these by code, so the match is exact.
    exclude_supervised: bool = True

    # --- Historical-self screen ---
    # PER and PBR only. No free source publishes a Japanese company's
    # historical EV/EBITDA, so rather than manufacture one this screen runs on
    # two metrics and requires BOTH - see japan_filters.apply_history_screen.
    hist_metrics: tuple[str, ...] = ("per", "pbr")
    hist_min_discount: float = 0.30
    hist_min_metrics: int = 2
    hist_min_years: int = 3
    hist_require_roe: bool = True

    # --- Peer groups ---
    # industry = JPX 33業種区分, board = PRIME/STANDARD/GROWTH.
    peer_keys: tuple[str, ...] = ("industry", "board")
    fallback_peer_keys: tuple[str, ...] = ("industry",)
    min_peers: int = 5
    winsor_pct: float = 0.05

    # --- Japan-specific universe hygiene ---
    exclude_class_shares: bool = True   # 種類株式: 5-character codes
    flag_holdcos: bool = True           # ホールディングス / HD
    exclude_holdcos: bool = False

    exclude_sectors: tuple[str, ...] = ()
    boards: tuple[str, ...] = BOARDS

    # --- Fetching ---
    # Two workers, not six. The only per-ticker source that can throttle
    # silently is Yahoo, and a throttled .info comes back as a dict with the
    # fields missing rather than as an error.
    max_workers: int = 2            # yfinance .info - the throttling one
    kabutan_workers: int = 3        # measured fine at 3; kabutan does not throttle
    yf_stmt_workers: int = 3        # statement endpoints; 660 names in ~2.7 min
    request_delay: float = 0.35
    cache_dir: str = ".jp_cache"
    cache_ttl_hours: int = 20
    min_priced_fraction: float = 0.50   # refuse to screen a partial universe


# ---------------------------------------------------------------------------
# Share-class and vehicle detection
# ---------------------------------------------------------------------------
# TSE codes are 4 characters - historically four digits, alphanumeric since
# 2024 (キオクシア is 285A). A FIVE-character code is a 種類株式: a class or
# preferred line listed alongside its common stock, sharing the first four
# characters.
#
# There are only seven on the whole exchange, but every one of them is a trap:
#   25935  伊藤園第1種優先株式      - no vote; has traded 40-50% below 2593 for
#                                    years, which is the Japanese textbook case
#   50765  インフロニア            \
#   75505  ゼンショー               |  社債型種類株式 - bond-type class shares.
#   92015  日本航空                 |  These are quasi-debt with a fixed
#   92025  ＡＮＡ                   |  redemption; a "PBR" struck on them is not
#   94345  ソフトバンク第1回        |  a valuation, it is an artefact.
#   94346  ソフトバンク第2回       /
#
# All seven sit in プライム（内国株式）, so nothing upstream removes them.
def is_class_share(code: str) -> bool:
    return len(str(code).strip()) == 5


def common_line_of(code: str) -> str:
    """The common-stock code a class line belongs to."""
    c = str(code).strip()
    return c[:4] if len(c) == 5 else c


# Japan writes holding companies several ways; the full-width forms are what
# the JPX roster actually contains.
HOLDCO_TOKENS = ("ホールディングス", "ホールディング", "ＨＤ", "HOLDINGS", "HOLDCO",
                 "グループ本社")


def _has(name: str, tokens) -> bool:
    n = str(name).upper()
    return any(tok.upper() in n for tok in tokens)


def is_holdco(name: str) -> bool:
    return _has(name, HOLDCO_TOKENS)


def is_financial(industry: str) -> bool:
    return str(industry).strip() in FINANCIAL_INDUSTRIES
