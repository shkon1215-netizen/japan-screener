"""Data sources for the Japan (TSE) screener.

Four free sources, each doing the one thing it is best at:

  1. JPX          data_j.xlsx - the roster, the board, and the official
                  33業種 classification. This is the disclosure-portal leg of
                  the pattern, and Japan's version is unusually good: it names
                  every listed line, separates ETF/ETN, REIT, PRO Market and
                  foreign listings into their own 市場・商品区分 values, and
                  carries the exchange's own industry code. Korea's biggest
                  known gap - industry classification coming from a vendor -
                  simply does not exist here.
  2. Yahoo! JP    the market-capitalisation ranking, 50 rows a page, sorted
                  descending. Paging it until market cap falls through the
                  floor is a complete cross-section of everything that can
                  possibly pass, for ~20 requests, and it carries price and
                  shares outstanding too. This is the cheap gate that keeps
                  the per-ticker work bounded (invariant 7).
  3. kabutan      one page per surviving ticker: four filed years of P&L with
                  EPS and DPS, three of balance sheet with BPS, three of cash
                  flow, and ROE. Fast and, measured, untroubled by 24 requests
                  back to back.
  4. yfinance     two different endpoints doing two different jobs. `download`
                  is batched and cheap: one call per sixty tickers gives the
                  daily volumes the liquidity gate needs and the monthly
                  closes the own-history screen strikes its multiples on.
                  `.info` is per ticker and is the only call here that can
                  throttle silently; it is used for EV/EBITDA, cash and debt.

Rate-limit discipline, which is the whole reason this file is shaped the way
it is. Three separate sources here fail by SUCCEEDING: Yahoo's `.info` returns
a dict with the fields simply absent, irbank answers 200 with a page reading
表示制限中, and Naver (on the Korea build) served a full-looking document with
no table in it. None of them raises. Downstream, all three are
indistinguishable from "this company has no such data", which turns a fetch
failure into a plausible, complete-looking, wrong screen. So: probe once
single-threaded before opening a pool, detect the soft rejection explicitly
and never cache it, cache successes so a re-run fills gaps instead of starting
over, and refuse to screen a universe that came back less than half priced.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

import config_jp as C

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"}

JPX_INDEX = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
JPX_ROSTER = ("https://www.jpx.co.jp/markets/statistics-equities/misc/"
              "tvdivq0000001vg2-att/data_j.xlsx")
JPX_SUPERVISION = "https://www.jpx.co.jp/listing/market-alerts/supervision/index.html"
YJ_RANK = "https://finance.yahoo.co.jp/stocks/ranking/{kind}?market=all&page={page}"
FX_URL = "https://api.frankfurter.app/latest?from=USD&to=JPY"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
# Bump a namespace's version whenever the SHAPE of what it stores changes -
# a new field, a renamed one, a value that used to be a list and is now a
# string. Without this the old shape outlives the fix by a full TTL, and the
# symptom is a column that is quietly empty rather than an error. It happened
# twice during this build: fin_years stayed a Python list repr, and dividends
# came back all-NaN because the cached .info records predated the field.
CACHE_SCHEMA = {
    "kb": 2,     # kabutan: fin_years became a comma-joined string
    "yf": 2,     # yfinance .info: gained dividend_rate
    "yfs": 2,    # yfinance statements: split-adjustment flags corrected
}


class Cache:
    """Disk cache keyed by (namespace, key), with the namespace's shape version
    stored alongside the value.

    Exists for the reason the UK build discovered: when a per-ticker source
    throttles, successive runs should FILL GAPS rather than start over. A run
    that got 300 of 800 names leaves 300 on disk; the next run fetches 500.
    """

    def __init__(self, root: str, ttl_hours: int = 20):
        self.root = root
        self.ttl = timedelta(hours=ttl_hours)
        os.makedirs(root, exist_ok=True)

    def _path(self, ns: str, key: str) -> str:
        d = os.path.join(self.root, ns)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, re.sub(r"[^0-9A-Za-z_.-]", "_", str(key)) + ".json")

    def get(self, ns: str, key: str):
        p = self._path(ns, key)
        try:
            if datetime.now() - datetime.fromtimestamp(os.path.getmtime(p)) > self.ttl:
                return None
            with open(p, encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(blob, dict) or "_v" not in blob:
            return None                      # pre-versioning entry: refetch
        if blob.get("_v") != CACHE_SCHEMA.get(ns, 1):
            return None                      # shape changed under it
        return blob.get("d")

    def put(self, ns: str, key: str, value) -> None:
        try:
            with open(self._path(ns, key), "w", encoding="utf-8") as fh:
                json.dump({"_v": CACHE_SCHEMA.get(ns, 1), "d": value},
                          fh, ensure_ascii=False)
        except (OSError, TypeError):
            pass


# ---------------------------------------------------------------------------
# small parsing helpers
# ---------------------------------------------------------------------------
_NUM = re.compile(r"-?[\d,]+(?:\.\d+)?")
_BLANK = ("", "-", "−", "－", "―", "—", "nan", "None", "NaN")


def _num(v) -> float:
    """Figures arrive as '14,594,987,460株', '44,149,837百万円', '-', '△10'."""
    if v is None:
        return np.nan
    s = str(v).strip()
    if s in _BLANK:
        return np.nan
    m = _NUM.search(s.replace(",", "").replace("△", "-"))
    if not m:
        return np.nan
    try:
        return float(m.group(0))
    except ValueError:
        return np.nan


def _price(v) -> float:
    """Yahoo's 取引値 column is the price with the session date glued on:
    '3,02509/18' is 3,025 on 09/18, not 302,509. Strip the date first."""
    s = re.sub(r"\d{2}/\d{2}$", "", str(v).strip())
    return _num(s)


_CODE_RE = re.compile(r"([0-9A-Z]{4,5})(?=東証|札証|名証|福証)")


def _code_from_label(v) -> str:
    """'トヨタ自動車(株)7203東証PRM掲示板' -> '7203'.

    Anchored on the exchange name rather than on position, because a company
    name can itself end in digits and the newer codes contain letters
    (キオクシア is 285A)."""
    m = _CODE_RE.search(str(v).replace("　", ""))
    return m.group(1) if m else ""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


# irbank sheds load by answering HTTP 200 with a page titled 表示制限中
# ("display restricted, high load") instead of the data. This is the same
# shape of failure as Naver's client-rendered page in Sept 2026 and Yahoo's
# field-stripped .info: a success status carrying nothing. Caching one of
# these would poison the cache for a full TTL, and parsing one as a CSV
# yields an empty frame that is indistinguishable from a company with no
# filings. So it is detected explicitly, never cached, and retried.
_RESTRICTED = ("表示制限中", "高負荷")


def _is_restricted(text: str) -> bool:
    head = text[:1500]
    return any(tok in head for tok in _RESTRICTED)


class Throttled(RuntimeError):
    pass


_THROTTLE_STATUS = (301, 302, 307, 308, 429, 503)

# A per-ticker fetch that returns nothing is the least informative failure in
# this file: "fundamentals: 0 of 660" is true whether the host refused us, went
# down, or changed its markup. So the first few rejections are logged with
# their status, once per host, and the run says which it was. This was not
# hypothetical - the first CI run failed exactly this way and the log could not
# distinguish a block from a parse error.
_SEEN_STATUS: dict = {}


def _note_status(url: str, status: int, detail: str = "") -> None:
    host = url.split("/")[2] if "//" in url else url
    key = (host, status)
    _SEEN_STATUS[key] = _SEEN_STATUS.get(key, 0) + 1
    if _SEEN_STATUS[key] <= 2:
        log.warning("%s answered HTTP %d%s (%s)", host, status,
                    (" - " + detail) if detail else "", url)


def fetch_failure_summary() -> str:
    if not _SEEN_STATUS:
        return ""
    return ", ".join("%s HTTP %d x%d" % (h, s, n)
                     for (h, s), n in sorted(_SEEN_STATUS.items()))


def _get_text(s: requests.Session, url: str, timeout: int = 30,
              tries: int = 3, backoff: float = 2.0) -> str:
    """GET that treats a soft rejection as the failure it is.

    Two shapes, both seen on irbank: HTTP 200 carrying the 表示制限中 page, and
    a redirect to it. Requests follows redirects, so a 3xx surfacing here at
    all means the chain did not resolve - which is a rejection, not data.
    Either way the caller gets Throttled and the result is never cached.
    """
    seen_throttle = False
    for attempt in range(tries):
        try:
            r = s.get(url, timeout=timeout)
        except requests.RequestException:
            time.sleep(backoff * (attempt + 1))
            continue
        text = r.content.decode("utf-8-sig", errors="replace")
        if r.status_code in _THROTTLE_STATUS or _is_restricted(text):
            seen_throttle = True
            time.sleep(backoff * (attempt + 1))
            continue
        if r.status_code != 200:
            _note_status(url, r.status_code, text.strip()[:80].replace("\n", " "))
            return ""
        return text
    if seen_throttle:
        raise Throttled(url)
    return ""


def _last(vals):
    """The most recent finite value in a series that runs oldest-first."""
    for v in reversed(list(vals)):
        if v is not None and np.isfinite(v):
            return float(v)
    return np.nan


def _cagr(vals) -> float:
    """Compound rate across the span actually covered.

    Undefined when EITHER end of the span is zero or negative: a fractional
    root of a negative ratio is a complex number, not a growth rate, and a
    ratio of two negatives is a positive that means nothing. Reported as
    missing rather than as a number with a meaningless sign. The yearly
    figures always ship alongside it, so a turnaround is still visible in the
    sparkline even when the rate is not - and Japan has plenty of them.
    """
    ok = [float(v) for v in vals if v is not None and np.isfinite(v)]
    if len(ok) < 2 or ok[0] <= 0 or ok[-1] <= 0:
        return np.nan
    n = len(ok) - 1
    return float((ok[-1] / ok[0]) ** (1.0 / n) - 1.0) * 100.0


def _report(what: str, rows, codes, throttled) -> None:
    """One place to say how complete a per-ticker fetch actually was.

    Without this the funnel cannot tell "this company files no accounts" from
    "the source shed our request", and those are opposite facts: the first is
    a reason to exclude a name, the second is a reason to distrust the whole
    run. The cache means a re-run fills the gap rather than starting over.
    """
    n, total = len(rows), len(codes)
    log.info("%s: %d of %d%s", what, n, total,
             (" (%d throttled)" % len(throttled)) if throttled else "")
    if throttled and len(throttled) > 0.02 * max(total, 1):
        log.warning("source shed %d of %d %s requests. Those names are MISSING, "
                    "not empty - re-run to fill them from cache.",
                    len(throttled), total, what)


# ---------------------------------------------------------------------------
# 1. JPX: roster and alert lists
# ---------------------------------------------------------------------------
class JPXProvider:
    def __init__(self, cfg: C.ScreenConfig):
        self.cfg = cfg
        self.cache = Cache(cfg.cache_dir, cfg.cache_ttl_hours)
        self.s = _session()

    def _roster_url(self) -> str:
        """The attachment path is stable but the extension moved from .xls to
        .xlsx, so read the index page rather than hardcoding it, and fall back
        to the known URL only if that fails."""
        try:
            r = self.s.get(JPX_INDEX, timeout=30)
            r.raise_for_status()
            m = re.search(r'href="([^"]+data_j\.xlsx?)"', r.text)
            if m:
                return "https://www.jpx.co.jp" + m.group(1)
        except requests.RequestException as e:
            log.warning("JPX index page unreadable (%s); using the known URL", e)
        return JPX_ROSTER

    def listing_roster(self) -> pd.DataFrame:
        url = self._roster_url()
        log.info("roster: %s", url)
        r = self.s.get(url, timeout=90)
        r.raise_for_status()
        raw = pd.read_excel(io.BytesIO(r.content), dtype={"コード": str})
        raw["コード"] = raw["コード"].astype(str).str.strip()

        seg = raw["市場・商品区分"].astype(str).str.strip()
        board = seg.map(C.SEGMENT_TO_BOARD)
        # Everything without a board is not a domestic operating company:
        # ETF・ETN, REIT and infrastructure funds, TOKYO PRO Market, foreign
        # listings, 出資証券. Korea had to strip these by name; here the
        # exchange has already done it, and done it authoritatively.
        keep = board.notna() & board.isin(self.cfg.boards)
        df = pd.DataFrame({
            "ticker": raw["コード"],
            "name": raw["銘柄名"].astype(str).str.strip(),
            "board": board,
            "segment": seg,
            "industry": raw["33業種区分"].astype(str).str.strip(),
            "industry17": raw["17業種区分"].astype(str).str.strip(),
            "topix_scale": raw["規模区分"].astype(str).str.strip(),
        })[keep].reset_index(drop=True)
        df["roster_date"] = str(raw["日付"].iloc[0]) if len(raw) else ""
        for c in ("industry", "industry17", "topix_scale"):
            df[c] = df[c].replace({"-": "", "－": ""})
        log.info("roster: %d domestic lines (%s)", len(df),
                 ", ".join("%s %d" % (b, int((df["board"] == b).sum()))
                           for b in self.cfg.boards))
        return df

    def supervised_codes(self) -> set:
        """監理銘柄 / 整理銘柄 - Japan's 관리종목 analogue, and a cleaner one.

        監理銘柄 flags a company whose listing may be cancelled (the delisting
        criteria are in play); 整理銘柄 flags one where the decision is made
        and the line is in its final month of trading. Both still trade, both
        carry a live price, and both would otherwise screen as spectacularly
        cheap - a company on its way off the exchange is not a value stock.

        JPX publishes them by CODE, so unlike KIND's name-only 관리종목 list
        this match cannot be defeated by a company renaming itself. Korea's
        known gap #3 does not carry over.
        """
        try:
            r = self.s.get(JPX_SUPERVISION, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("supervision list unreachable (%s) - NOT applied", e)
            return set()
        # JPX serves UTF-8 but does not always say so, and requests then guesses
        # Latin-1. Codes are ASCII either way, but the company names in the log
        # line below would be mojibake.
        r.encoding = "utf-8"
        codes = set()
        try:
            # Three tables: 監理銘柄（審査中）, 監理銘柄（確認中）, 整理銘柄. Read the
            # コード column only - scanning every column would also sweep up
            # anything else on the page that happens to look like a code.
            for t in pd.read_html(io.StringIO(r.text)):
                cols = [c for c in t.columns if "コード" in str(c)]
                for col in cols:
                    s = t[col].astype(str).str.strip()
                    codes |= set(s[s.str.fullmatch(r"[0-9A-Z]{4,5}")].tolist())
        except ValueError:
            pass
        log.info("監理・整理銘柄: %d codes", len(codes))
        return codes


# ---------------------------------------------------------------------------
# 2. Yahoo! Finance Japan: the cross-section
# ---------------------------------------------------------------------------
class YahooJPRanking:
    """Cross-sectional market cap and turnover, descending, 50 rows a page.

    The point of a descending sort is that the screen only ever cares about
    the top of the list, so paging can STOP once the floor is crossed. The
    whole cross-section for a USD 600m universe is about twenty requests -
    the cheap gate invariant 7 asks for, before any per-ticker work.
    """

    def __init__(self, cfg: C.ScreenConfig):
        self.cfg = cfg
        self.s = _session()

    def _page(self, kind: str, page: int) -> pd.DataFrame:
        r = self.s.get(YJ_RANK.format(kind=kind, page=page), timeout=30)
        r.raise_for_status()
        try:
            return pd.read_html(io.StringIO(r.text))[0]
        except (ValueError, IndexError):
            return pd.DataFrame()

    @staticmethod
    def _col(t, needle):
        for c in t.columns:
            if needle in str(c):
                return c
        return None

    def market_cap_snapshot(self, floor_jpy: float, max_pages: int = 90) -> pd.DataFrame:
        rows, asof, pages = [], "", 0
        for page in range(1, max_pages + 1):
            t = self._page("marketCapitalHigh", page)
            mcol = self._col(t, "時価総額") if not t.empty else None
            if t.empty or mcol is None:
                break
            pages = page
            label = self._col(t, "名称")
            pcol = self._col(t, "取引値")
            scol = self._col(t, "発行済")
            if not asof:
                m = re.search(r"(\d{2}/\d{2})$", str(t[pcol].iloc[0]).strip())
                asof = m.group(1) if m else ""
            last = np.nan
            for _, r in t.iterrows():
                mcap = _num(r[mcol]) * 1e6          # the column is 百万円
                rows.append({"ticker": _code_from_label(r[label]),
                             "close_jpy": _price(r[pcol]),
                             "shares_out": _num(r[scol]) if scol else np.nan,
                             "market_cap_local": mcap})
                last = mcap
            if np.isfinite(last) and last < floor_jpy:
                break
            time.sleep(self.cfg.request_delay)
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df = df[df["ticker"].ne("")].drop_duplicates("ticker")
        log.info("market cap cross-section: %d lines over %d pages, down to %.3g JPY",
                 len(df), pages, float(df["market_cap_local"].min()))
        df.attrs["asof"] = asof
        return df.reset_index(drop=True)

def jpy_to_usd() -> float:
    """USD per JPY - the small number (~0.0063), and it is MULTIPLIED.

    Frankfurter first, deliberately a different host from every other source
    here: on the UK build the FX call went through the same throttled host as
    the fundamentals, so it failed at exactly the moment it was needed and
    fell back to a constant that silently rescaled every market cap in the
    gate.

    Yahoo's JPY=X second. A single source turned out to be its own failure
    mode: on 2026-10-05 Frankfurter timed out once on a GitHub runner and the
    unhandled ReadTimeout failed the whole scheduled run, an hour after the
    same call had answered. Yahoo shares the fundamentals' throttle, which is
    why it is not first - but as a fallback it only has to answer once.

    Still no constant at the end. If both live sources fail this raises, and
    the run fails loudly rather than gating on a stale rate.
    """
    errors = []
    try:
        r = requests.get(FX_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        jpy = float(r.json()["rates"]["JPY"])
        if not 50 < jpy < 500:
            raise ValueError("implausible USD/JPY %r" % jpy)
        return 1.0 / jpy
    except Exception as e:                                    # noqa: BLE001
        errors.append("Frankfurter: %s" % (str(e)[:120] or type(e).__name__))
        log.warning("FX via Frankfurter failed (%s); trying Yahoo", errors[-1])

    try:
        import yfinance as yf
        h = yf.Ticker("JPY=X").history(period="5d")
        jpy = float(h["Close"].dropna().iloc[-1])
        if not 50 < jpy < 500:
            raise ValueError("implausible USD/JPY %r" % jpy)
        log.info("FX from Yahoo (JPY=X)")
        return 1.0 / jpy
    except Exception as e:                                    # noqa: BLE001
        errors.append("Yahoo: %s" % (str(e)[:120] or type(e).__name__))

    raise RuntimeError("no live USD/JPY rate - " + "; ".join(errors)
                       + ". Pass --fx to pin one by hand.")


# ---------------------------------------------------------------------------
# 3. kabutan: filed fundamentals, one request per ticker
# ---------------------------------------------------------------------------
# irbank was the first choice here and had to be abandoned: it publishes a
# beautiful per-company CSV (fy-data-all.csv) but sheds load hard, answering
# either HTTP 200 or a 302 with a page titled 表示制限中. Twelve sequential
# requests two seconds apart were enough to earn a cooling-off period, so it
# cannot serve a thousand-name universe. The detector for that failure is kept
# above, because the shape recurs - a success status carrying no data - and
# because it is what turned a silent wrong answer into a loud one.
#
# kabutan serves the same content and took 24 requests with no delay at all
# (7.3s, zero rejections). One page, /stock/finance?code=X, carries four
# tables the screen needs:
#
#   通期業績   4 filed years + 予 forecast: 売上高 営業益 経常益 最終益 EPS DPS
#   収益性     2 filed years + 予:          ROE ROA 営業利益率
#   財務       3 filed years + latest Q:    BPS 自己資本比率 総資産 自己資本
#   CF         3 filed years:               営業CF フリーCF 現金等残高
#
# Two Japan-specific traps live in those tables:
#
#   1. Every one of them carries a 予 (forecast) row, and kabutan's headline
#      PER and 利回り are struck on it. Japanese convention IS forward: the
#      figure a Japanese investor quotes is 会社予想PER, built on the company's
#      own guidance rather than an analyst's. That guidance is a formal
#      disclosure, not a consensus, so it is worth carrying - but it is still
#      the company's opinion of its own future, and a screen that reports it
#      as history is reporting management's forecast as fact. Forecast rows
#      are therefore parsed into fwd_* and never screened on.
#
#   2. The 財務 and 業績 tables mix annual rows ("I 2026.03") with interim and
#      quarterly ones ("I 26.04-06"). Only four-digit-year rows are annual.
#      Taking the last row blindly would compare a quarter with a year.
KABUTAN_FINANCE = "https://kabutan.jp/stock/finance?code={code}"

OKU = 1e8          # 億円 - the unit Japanese filings are read in
MAN = 1e6          # kabutan states balance-sheet and P&L figures in 百万円

_ANNUAL_RE = re.compile(r"(?<!\d)(\d{4})\.(\d{2})(?!\d)")
_FORECAST_TOK = "予"          # 予


def _norm(s) -> str:
    """kabutan headers arrive full-width and line-broken: 'ＲＯＥ', '修正 1株益',
    '１株 純資産'. NFKC folds the full-width forms to ASCII and stripping
    whitespace closes the <br>, so one spelling matches everywhere."""
    import unicodedata
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(s)))


def _pick_table(tables, must_have, annual_only=True):
    """The first table whose normalised columns contain every needle."""
    for t in tables:
        cols = {_norm(c) for c in t.columns}
        if not all(any(n in c for c in cols) for n in must_have):
            continue
        if annual_only:
            first = t[t.columns[0]].astype(str).map(_norm)
            if not first.map(lambda v: bool(_ANNUAL_RE.search(v))).any():
                continue
        return t
    return None


def _annual_rows(t):
    """(period, is_forecast, row) for the annual rows of a kabutan table.

    Period is 'YYYY.MM' - the fiscal year END, which is what dates the price
    used to strike that year's PER and PBR. 前期比 and quarterly rows drop out
    because they carry no four-digit year.
    """
    out = []
    key = t.columns[0]
    for _, r in t.iterrows():
        lab = _norm(r[key])
        m = _ANNUAL_RE.search(lab)
        if not m:
            continue
        out.append(("%s.%s" % (m.group(1), m.group(2)),
                    _FORECAST_TOK in lab, r))
    return out


def _cell(row, cols, needle):
    """Exact column match first, substring only as a fallback.

    kabutan's 財務 table carries both 自己資本 and 自己資本比率. A plain
    substring search finds the RATIO - 37.8 - where the caller wanted total
    equity, and 37.8 million yen of shareholders' equity for Toyota is wrong
    in a way that still looks like a number.
    """
    for c in cols:
        if _norm(c) == needle:
            return _num(row[c])
    for c in cols:
        if needle in _norm(c):
            return _num(row[c])
    return np.nan


def fetch_fundamentals(codes, cache: Cache, delay: float = 0.0,
                       workers: int = 3) -> pd.DataFrame:
    """Latest filed EPS / BPS / ROE / DPS plus the multi-year series.

    Every per-share figure comes from the SAME filing as the history, which is
    what keeps today's PER comparable with the company's own past PERs. Taking
    a vendor's TTM "now" against filed-year history compares two different
    things, and on the UK build that was a real bug rather than a nicety.
    """
    s = _session()
    rows, throttled = [], []
    codes = list(codes)

    def one(code):
        hit = cache.get("kb", code)
        if hit is None:
            try:
                text = _get_text(s, KABUTAN_FINANCE.format(code=code))
            except Throttled:
                throttled.append(code)
                return None
            if not text:
                return None
            if "決算期" not in text:
                # HTTP 200 with no 決算期 table: either the markup moved or we
                # are being served an interstitial. Both are silent failures,
                # so say which by logging a fingerprint of what did arrive.
                _note_status(KABUTAN_FINANCE.format(code=code), 200,
                             "no 決算期 in %d bytes" % len(text))
                return None
            try:
                tables = pd.read_html(io.StringIO(text))
            except ValueError:
                return None
            hit = _parse_kabutan(tables)
            if hit:
                cache.put("kb", code, hit)
            if delay:
                time.sleep(delay)
        if not hit:
            return None
        return dict(hit, ticker=code)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for rec in ex.map(one, codes):
            if rec:
                rows.append(rec)
    _report("fundamentals", rows, codes, throttled)
    df = pd.DataFrame(rows)
    if "fin_years" in df.columns:
        # Normalised here rather than in the parser so that records ALREADY in
        # the cache are fixed too. A shape change inside _parse_kabutan only
        # takes effect for names fetched after it, so the old shape otherwise
        # outlives the fix by a full cache TTL - and the symptom is cosmetic
        # enough (bracketed year labels in a tooltip) to survive a review.
        df["fin_years"] = df["fin_years"].map(
            lambda v: ",".join(str(x)[:4] for x in v)
            if isinstance(v, (list, tuple)) else str(v or ""))
    return df


def _parse_kabutan(tables) -> dict:
    pl = _pick_table(tables, ["決算期", "売上高",
                              "最終益", "1株益"])
    prof = _pick_table(tables, ["決算期", "ROE", "ROA"])
    bs = _pick_table(tables, ["決算期", "1株純資産",
                              "自己資本比率"])
    cf = _pick_table(tables, ["決算期", "営業CF"])
    if pl is None:
        return {}

    rec: dict = {}
    per, fc = [], {}
    rev, op, npr, eps, dps = [], [], [], [], []
    for period, is_fc, r in _annual_rows(pl):
        cols = pl.columns
        vals = {
            "rev": _cell(r, cols, "売上高"),
            "op": _cell(r, cols, "営業益"),
            "np": _cell(r, cols, "最終益"),
            "eps": _cell(r, cols, "1株益"),
            "dps": _cell(r, cols, "1株配"),
        }
        if is_fc:
            fc = {"fwd_eps": vals["eps"], "fwd_dps": vals["dps"],
                  "fwd_period": period}
            continue
        per.append(period)
        rev.append(vals["rev"])
        op.append(vals["op"])
        npr.append(vals["np"])
        eps.append(vals["eps"])
        dps.append(vals["dps"])
    rec.update(fc)

    roe = []
    if prof is not None:
        for period, is_fc, r in _annual_rows(prof):
            if is_fc:
                continue
            roe.append((period, _cell(r, prof.columns, "ROE")))

    bps_per, bps, equity = [], [], []
    if bs is not None:
        for period, is_fc, r in _annual_rows(bs):
            if is_fc:
                continue
            bps_per.append(period)
            bps.append(_cell(r, bs.columns, "1株純資産"))
            equity.append(_cell(r, bs.columns, "自己資本"))
        e = _last(equity)
        rec["equity_local"] = e * MAN if np.isfinite(e) else np.nan
        rec["debt_ratio"] = _last([_cell(r, bs.columns, "有利子負債倍率")
                                   for _, f, r in _annual_rows(bs) if not f])

    ocf = []
    if cf is not None:
        for period, is_fc, r in _annual_rows(cf):
            if is_fc:
                continue
            ocf.append(_cell(r, cf.columns, "営業CF"))
        rec["cash_local"] = _last([_cell(r, cf.columns, "現金等残高")
                                   for _, f, r in _annual_rows(cf) if not f]) * MAN

    rec["trailing_eps"] = _last(eps)
    rec["book_value_ps"] = _last(bps)
    rec["dps"] = _last(dps)
    rec["roe_reported"] = _last([v for _, v in roe])
    # Periods and values kept side by side, oldest first: the own-history
    # screen needs to know WHICH fiscal year each figure belongs to so it can
    # strike it against that year's closing price.
    rec["eps_periods"], rec["eps_vals"] = per, [_j(v) for v in eps]
    rec["bps_periods"], rec["bps_vals"] = bps_per, [_j(v) for v in bps]
    # kabutan labels its earnings column 修正1株益 - restated onto today's
    # share count - while the balance-sheet column is plain 1株純資産, as
    # filed. The two are on different bases and only the second needs
    # un-splitting; build_valuation_history reads these flags rather than
    # assuming, because the yfinance source has neither restated.
    rec["eps_adjusted"], rec["bps_adjusted"] = True, False
    # Comma-joined, not a list: this column survives a CSV round trip and the
    # dashboard splits it on "," for the growth tooltip. A Python list would
    # arrive there as "['2024.03', '2025.03']" and label the bars with brackets.
    rec["fin_years"] = ",".join(p[:4] for p in per[-3:])
    rec["fin_n"] = int(sum(1 for v in eps if np.isfinite(v)))

    n = 3
    for key, vals in (("rev", rev), ("op", op), ("ocf", ocf), ("np", npr)):
        tail = vals[-n:]
        for i in range(n):
            v = tail[i] if i < len(tail) else np.nan
            # kabutan states these in 百万円; the dashboard reads 億円.
            rec["%s_y%d" % (key, i + 1)] = (v * MAN / OKU
                                            if v is not None and np.isfinite(v)
                                            else np.nan)
        rec["%s_cagr" % key] = _cagr(vals)
    return rec


def _j(v):
    """JSON-safe: the cache round-trips through json, which has no NaN."""
    return None if v is None or not np.isfinite(v) else float(v)


# ---------------------------------------------------------------------------
# 3a. yfinance filed statements - the fallback, and the only one CI can use
# ---------------------------------------------------------------------------
# kabutan answers HTTP 405 to a GitHub runner - every request, with an English
# WAF page. minkabu answers 403 and irbank's HTML site 403; only its CSV host
# stays open. That is a deliberate block on datacenter traffic rather than a
# rate limit, so it is routed around rather than worked around.
#
# yfinance's statement endpoints do answer runners, and measured at 3 workers
# 660 names take under three minutes. What they give is in some ways BETTER
# than kabutan: five filed columns of income statement, balance sheet and cash
# flow from one fetch per ticker, where kabutan publishes four years of EPS and
# only three of BPS without a subscription.
#
# What is lost is specific and worth stating: kabutan carries the company's own
# 会社予想 - formal guidance, the basis of every PER quoted in Japan. yfinance
# has `forwardEps`, which is ANALYST CONSENSUS and a different thing entirely.
# Presenting one as the other would be exactly the error invariant 12 exists to
# prevent, so under this source the forecast columns are simply empty.
_YF_ROWS = {
    "rev": ("Total Revenue", "Operating Revenue"),
    "op": ("Operating Income", "Total Operating Income As Reported"),
    "np": ("Net Income Common Stockholders", "Net Income",
           "Net Income Including Noncontrolling Interests"),
    "equity": ("Stockholders Equity", "Total Equity Gross Minority Interest"),
    "shares": ("Ordinary Shares Number", "Share Issued"),
    "ocf": ("Operating Cash Flow",),
    "cash": ("Cash And Cash Equivalents",
             "Cash Cash Equivalents And Short Term Investments"),
}


def _yf_row(df, names):
    if df is None or getattr(df, "empty", True):
        return None
    for n in names:
        if n in df.index:
            return df.loc[n]
    return None


def fetch_fundamentals_yf(codes, cache: Cache, delay: float = 0.0,
                          workers: int = 3) -> pd.DataFrame:
    """Same schema as fetch_fundamentals, from yfinance's filed statements.

    Per-share figures are derived rather than taken: EPS is net income over
    that year's share count and BPS is shareholders' equity over the same,
    which keeps both on ONE definition across every year and every company.
    Using the filed share count (not today's) means these are as-reported and
    therefore NOT restated for splits - see `bps_adjusted` / `eps_adjusted`,
    which is what tells build_valuation_history how to line them up with a
    split-adjusted price series.

    Shares come from Ordinary Shares Number, which nets off treasury stock.
    That matters in Japan: Toyota holds about 11% of itself, and a PBR struck
    on issued shares reads 1.11 where the market quotes 0.96.
    """
    import yfinance as yf
    rows = []
    codes = list(codes)

    def one(code):
        hit = cache.get("yfs", code)
        if hit is None:
            try:
                t = yf.Ticker(code + ".T")
                inc, bs, cf = t.income_stmt, t.balance_sheet, t.cashflow
            except Exception:                                  # noqa: BLE001
                return None
            if inc is None or inc.empty:
                return None
            got = {k: _yf_row(inc if k in ("rev", "op", "np") else
                              bs if k in ("equity", "shares", "cash") else cf,
                              names)
                   for k, names in _YF_ROWS.items()}
            # Oldest first, to match every other series in this file.
            periods = ["%04d.%02d" % (c.year, c.month) for c in inc.columns][::-1]
            hit = {"periods": periods}
            for k, ser in got.items():
                if ser is None:
                    hit[k] = [None] * len(periods)
                    continue
                vals = [_num(v) for v in list(ser)][::-1]
                vals = (vals + [np.nan] * len(periods))[:len(periods)]
                hit[k] = [_j(v) for v in vals]
            cache.put("yfs", code, hit)
            if delay:
                time.sleep(delay)
        if not hit or not hit.get("periods"):
            return None

        per = hit["periods"]
        eq, sh = hit.get("equity") or [], hit.get("shares") or []
        npr = hit.get("np") or []

        def per_share(nums):
            out = []
            for i in range(len(per)):
                v = nums[i] if i < len(nums) else None
                s = sh[i] if i < len(sh) else None
                out.append(v / s if (v is not None and s and s > 0) else None)
            return out

        eps_vals = per_share(npr)
        bps_vals = per_share(eq)
        rec = {
            "ticker": code,
            "eps_periods": per, "eps_vals": eps_vals,
            "bps_periods": per, "bps_vals": bps_vals,
            # BOTH restated, unlike kabutan where only EPS is. Verified rather
            # than assumed, because assuming it cost a wrong answer: yfinance
            # reports 日立's 2024/03 share count as 4.633bn, which is the
            # POST-split basis - the filed figure was about 0.93bn before the
            # 5:1 in June 2024. Its Basic EPS is restated to match. So every
            # per-share figure derived here is already on today's basis and
            # must NOT be un-split, or the history inflates and every name
            # looks cheap against itself.
            "eps_adjusted": True, "bps_adjusted": True,
            "trailing_eps": _last([v for v in eps_vals if v is not None]),
            "book_value_ps": _last([v for v in bps_vals if v is not None]),
            "equity_local": _last([v for v in eq if v is not None]),
            "cash_local": _last([v for v in (hit.get("cash") or []) if v is not None]),
            # Derived from the same two filed lines the multiples use, so ROE
            # cannot disagree with the PBR it is read against.
            "roe_reported": np.nan,
            "dps": np.nan, "fwd_eps": np.nan, "fwd_dps": np.nan,
            "fin_years": ",".join(p[:4] for p in per[-3:]),
            "fin_n": int(sum(1 for v in eps_vals if v is not None)),
        }
        ni_l = _last([v for v in npr if v is not None])
        eq_l = rec["equity_local"]
        if np.isfinite(ni_l) and np.isfinite(eq_l) and eq_l > 0:
            rec["roe_reported"] = ni_l / eq_l * 100.0

        n = 3
        for key in ("rev", "op", "ocf", "np"):
            series = [v for v in (hit.get(key) or [])]
            tail = series[-n:]
            for i in range(n):
                v = tail[i] if i < len(tail) else None
                rec["%s_y%d" % (key, i + 1)] = (v / OKU if v is not None
                                                else np.nan)
            rec["%s_cagr" % key] = _cagr([v for v in series if v is not None])
        return rec

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for rec in ex.map(one, codes):
            if rec:
                rows.append(rec)
    _report("fundamentals (yfinance)", rows, codes, [])
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 3b. Prices: one batched call for the whole universe
# ---------------------------------------------------------------------------
def fetch_price_panel(codes, period: str, interval: str,
                      chunk: int = 60, actions: bool = False) -> pd.DataFrame:
    """Close (and volume) for every ticker at once.

    yfinance's per-ticker `.info` is the call that throttles; `download` is a
    different, batched endpoint and is cheap - 24 tickers of seven years of
    monthly closes came back in 0.7 seconds. Everything the screen needs from
    price history therefore costs about twenty requests for the whole
    universe, not one per name, which is why the liquidity gate can run before
    any per-ticker work and invariant 7 still holds.
    """
    import yfinance as yf
    codes = list(codes)
    frames = []
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        try:
            px = yf.download([c + ".T" for c in part], period=period,
                             interval=interval, progress=False,
                             auto_adjust=False, actions=actions, threads=True)
        except Exception as e:                                # noqa: BLE001
            log.warning("price chunk %d failed: %s", i // chunk, e)
            continue
        if px is None or px.empty:
            continue
        frames.append(px)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, axis=1)
    log.info("price panel %s/%s: %d columns", period, interval, out.shape[1])
    return out


def average_daily_value(panel: pd.DataFrame, codes) -> pd.DataFrame:
    """Median daily traded value in JPY over the panel's window.

    Median, not mean, for the same reason every benchmark here is a median:
    one index-rebalance day can be twenty times a name's normal volume, and a
    mean would let that single session carry an otherwise untradeable stock
    through the gate.
    """
    if panel.empty:
        return pd.DataFrame(columns=["ticker", "adv_local"])
    try:
        close, vol = panel["Close"], panel["Volume"]
    except KeyError:
        return pd.DataFrame(columns=["ticker", "adv_local"])
    rows = []
    for c in codes:
        sym = c + ".T"
        if sym not in close.columns or sym not in vol.columns:
            continue
        v = (pd.to_numeric(close[sym], errors="coerce")
             * pd.to_numeric(vol[sym], errors="coerce")).dropna()
        if v.empty:
            continue
        rows.append({"ticker": c, "adv_local": float(v.median())})
    return pd.DataFrame(rows)


def build_valuation_history(df: pd.DataFrame, monthly: pd.DataFrame,
                            years: int = 5) -> pd.DataFrame:
    """The company's own PER and PBR, one figure per filed fiscal year.

    Each year's multiple is that fiscal year's CLOSING price over that year's
    filed EPS and BPS - the same construction as today's, which is today's
    price over the latest filed EPS and BPS. Both ends of the comparison are
    struck the same way, on one provider's figures, which is the property that
    makes a discount to one's own history mean anything.

    Japan's free history is shallower than Korea's five years: kabutan
    publishes four filed years of EPS and three of BPS without a subscription,
    so PER typically gets four points and PBR three. That is why hist_min_years
    is 3 and why both metrics must clear the threshold rather than two of
    three - with only two metrics, allowing one to fail is no test at all.

    ---- The split trap, which is the reason this function is not two lines ---

    kabutan labels its earnings and dividend columns 修正1株益 and 修正1株配 -
    ADJUSTED per-share figures, restated onto today's share count. Its balance
    sheet column, 1株純資産, is NOT adjusted; it is the BPS as filed in that
    year. Yahoo's price series is split-adjusted whatever `auto_adjust` says,
    because splits are applied to the chart data itself.

    So PER lines up by accident - adjusted price over adjusted EPS - and PBR
    does not. 日立 (6501) split five for one in June 2024, and its filed
    2024/03 BPS of 6,155 struck against a post-split price gives a PBR of
    0.45, against 2.71 the following year. Nothing about the company changed.
    A six-fold artefact sitting in a three-point median is not noise, it is
    the answer.

    The fix is to put BPS on the same basis as everything else: divide each
    year's filed BPS by the splits that happened AFTER that year end. Those
    events arrive batched with the price panel, so they cost nothing.
    """
    out = df.copy()
    if df.empty or monthly.empty or "Close" not in monthly:
        out["hist_years"] = ""
        for c in ("hist_per", "hist_pbr"):
            out[c] = [[] for _ in range(len(out))]
        return out
    close = monthly["Close"]
    splits = monthly["Stock Splits"] if "Stock Splits" in monthly else None
    idx = pd.to_datetime(close.index)

    def _period_ts(period):
        try:
            y, m = period.split(".")
            return int(y), int(m)
        except ValueError:
            return None

    def price_at(sym, period):
        """Close of the month the fiscal year ended in."""
        ym = _period_ts(period)
        if ym is None or sym not in close.columns:
            return np.nan
        hit = (idx.year == ym[0]) & (idx.month == ym[1])
        if not hit.any():
            return np.nan
        v = pd.to_numeric(close[sym][hit], errors="coerce").dropna()
        return float(v.iloc[-1]) if len(v) else np.nan

    def split_factor_after(sym, period):
        """Product of the split ratios recorded after this fiscal year end."""
        ym = _period_ts(period)
        if splits is None or ym is None or sym not in splits.columns:
            return 1.0
        s = pd.to_numeric(splits[sym], errors="coerce").fillna(0.0)
        after = (idx.year > ym[0]) | ((idx.year == ym[0]) & (idx.month > ym[1]))
        vals = [v for v in s[after].tolist() if v and v > 0]
        f = 1.0
        for v in vals:
            f *= float(v)
        return f or 1.0

    hy, hper, hpbr = [], [], []
    for _, r in df.iterrows():
        sym = str(r["ticker"]) + ".T"
        eps_p = list(r.get("eps_periods") or [])
        eps_v = list(r.get("eps_vals") or [])
        bps_map = dict(zip(list(r.get("bps_periods") or []),
                           list(r.get("bps_vals") or [])))
        yrs, pers, pbrs = [], [], []
        eps_adj = bool(r.get("eps_adjusted", True))
        bps_adj = bool(r.get("bps_adjusted", False))
        for p, e in list(zip(eps_p, eps_v))[-years:]:
            px = price_at(sym, p)
            yrs.append(p[:4])
            # The price is split-adjusted, always. A per-share figure that is
            # NOT restated has to be divided by the splits since that year
            # before the two can be compared; one that is already restated
            # must be left alone. Getting this backwards is silent: the
            # multiple stays a plausible number, it is just the wrong one.
            f = split_factor_after(sym, p)
            e_use = e if (e is None or eps_adj) else e / f
            pers.append(round(px / e_use, 3)
                        if (np.isfinite(px) and e_use and e_use > 0) else None)
            b = bps_map.get(p)
            b_use = b if (b is None or bps_adj) else (b / f if f else b)
            pbrs.append(round(px / b_use, 3)
                        if (np.isfinite(px) and b_use and b_use > 0) else None)
        hy.append(yrs)
        hper.append(pers)
        hpbr.append(pbrs)
    # Years comma-joined for the same reason as fin_years; the two multiple
    # series stay as lists because apply_history_screen consumes them in memory
    # and expands them into hist_*_y1..y5 columns before anything is written.
    out["hist_years"] = [",".join(y) for y in hy]
    out["hist_per"], out["hist_pbr"] = hper, hpbr
    return out

# ---------------------------------------------------------------------------
# 4. yfinance: EV/EBITDA, average volume, cash and debt
# ---------------------------------------------------------------------------
class JapanEnricher:
    """The one source here that can lie by omission.

    A throttled `.info` is a dict with the fields simply absent - not an
    error, not an exception. Downstream that is indistinguishable from a bank
    that genuinely has no EV/EBITDA, so a half-throttled run produces a
    complete-looking, plausible, wrong screen. Four defences, all of which
    earned their place on the UK build:

      1. probe once, single-threaded, before opening the pool - the limit is
         global, so a throttled run otherwise fires a thousand doomed requests
      2. cache successes, so the next run fills gaps instead of starting over
      3. two workers with a delay; slower finishes sooner
      4. refuse to screen a universe that came back less than half priced
    """

    FIELDS = ("enterpriseToEbitda", "averageVolume", "totalCash", "totalDebt",
              "sector", "industry", "enterpriseValue", "ebitda")

    def __init__(self, cfg: C.ScreenConfig, cache: Cache):
        self.cfg = cfg
        self.cache = cache

    @staticmethod
    def _info(symbol: str) -> dict:
        import yfinance as yf
        try:
            return yf.Ticker(symbol).info or {}
        except Exception:                                  # noqa: BLE001
            return {}

    def probe(self, tries: int = 3, backoff: float = 20.0) -> bool:
        """One known-good, heavily covered name. If Toyota comes back without
        a market cap, the session is throttled and nothing else will work.

        Retried with a long backoff because the block is usually transient and
        the alternative is failing a whole run. Observed: two CI runs ten
        minutes apart, the first fine and the second answered
        `401 Unauthorized` for every symbol - the limit is per source IP and
        recovers on its own. Backing off here costs a minute; not backing off
        costs the publish.
        """
        for attempt in range(tries):
            if bool(self._info("7203.T").get("marketCap")):
                log.info("yfinance probe: ok%s",
                         " (after %d retries)" % attempt if attempt else "")
                return True
            if attempt < tries - 1:
                log.warning("yfinance probe: throttled, waiting %.0fs",
                            backoff * (attempt + 1))
                time.sleep(backoff * (attempt + 1))
        log.info("yfinance probe: THROTTLED or blocked")
        return False

    def enrich(self, codes) -> pd.DataFrame:
        codes = list(codes)
        todo, rows = [], []
        for c in codes:
            hit = self.cache.get("yf", c)
            if hit is None:
                todo.append(c)
            else:
                rows.append(hit)
        log.info("enrich: %d cached, %d to fetch", len(rows), len(todo))

        if todo and not self.probe():
            log.error("yfinance is not answering - EV/EBITDA and ADV will be "
                      "missing for every uncached name")
            todo = []

        def one(code):
            info = self._info("%s.T" % code)
            if not info.get("marketCap"):
                return None
            rec = {"ticker": code}
            rec["ev_to_ebitda"] = _num(info.get("enterpriseToEbitda"))
            rec["avg_volume"] = _num(info.get("averageVolume"))
            rec["total_cash"] = _num(info.get("totalCash"))
            rec["total_debt"] = _num(info.get("totalDebt"))
            rec["yf_sector"] = str(info.get("sector") or "")
            # Trailing cash actually paid per share. Only used when the
            # fundamentals source could not supply a filed DPS, which is the
            # case under --source yfinance.
            rec["dividend_rate"] = _num(info.get("dividendRate"))
            self.cache.put("yf", code, rec)
            time.sleep(self.cfg.request_delay)
            return rec

        if todo:
            with ThreadPoolExecutor(max_workers=self.cfg.max_workers) as ex:
                for rec in ex.map(one, todo):
                    if rec:
                        rows.append(rec)

        df = pd.DataFrame(rows)
        got = len(df)
        frac = got / max(len(codes), 1)
        log.info("enrich: %d of %d priced (%.0f%%)", got, len(codes), frac * 100)
        if codes and frac < self.cfg.min_priced_fraction:
            raise RuntimeError(
                "only %d of %d names enriched (%.0f%%). Yahoo is throttling; "
                "re-run in a few minutes - the cache keeps what already "
                "arrived - or pass --no-ev to screen on PER and PBR alone."
                % (got, len(codes), frac * 100))
        return df
