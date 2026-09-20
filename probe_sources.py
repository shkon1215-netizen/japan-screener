"""Which Japanese data sources answer a GitHub runner? Throwaway diagnostic."""
import requests, time
H = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
     "Accept-Language": "ja,en;q=0.8"}
URLS = [
    ("kabutan finance", "https://kabutan.jp/stock/finance?code=7203"),
    ("minkabu stock",   "https://minkabu.jp/stock/7203"),
    ("minkabu yuho",    "https://minkabu.jp/stock/7203/settlement"),
    ("irbank page",     "https://irbank.net/7203"),
    ("irbank csv",      "https://f.irbank.net/files/7203/fy-data-all.csv"),
    ("yahoo jp quote",  "https://finance.yahoo.co.jp/quote/7203.T"),
    ("yahoo jp perf",   "https://finance.yahoo.co.jp/quote/7203.T/performance"),
    ("nikkei",          "https://www.nikkei.com/nkd/company/?scode=7203"),
    ("buffett-code",    "https://www.buffett-code.com/company/7203/"),
]
for name, u in URLS:
    try:
        r = requests.get(u, headers=H, timeout=25)
        t = r.content.decode("utf-8", errors="replace")
        marks = [k for k in ("決算期", "ROE", "1株純資産", "EPS", "BPS", "業績") if k in t]
        print(f"{name:16s} HTTP {r.status_code}  {len(r.content):>8,}b  marks={marks}")
    except Exception as e:
        print(f"{name:16s} ERR {type(e).__name__}: {e}")
    time.sleep(1)

print("\n--- yfinance fundamentals (library, works for prices already) ---")
import yfinance as yf
t0 = time.time()
tk = yf.Ticker("7203.T")
for attr in ("income_stmt", "balance_sheet", "cashflow"):
    try:
        df = getattr(tk, attr)
        cols = [str(c)[:10] for c in df.columns][:5] if df is not None else []
        idx = [i for i in (df.index if df is not None else []) if
               any(k in str(i) for k in ("Total Revenue", "Operating Income",
                                         "Net Income", "Basic EPS",
                                         "Stockholders Equity", "Ordinary Shares",
                                         "Operating Cash Flow"))]
        print(f"  {attr:14s} years={cols} useful_rows={idx}")
    except Exception as e:
        print(f"  {attr:14s} ERR {e}")
print("  elapsed %.1fs for one ticker" % (time.time() - t0))
info = tk.info or {}
print("  .info:", {k: info.get(k) for k in
      ("trailingEps", "bookValue", "returnOnEquity", "dividendRate",
       "trailingPE", "priceToBook", "marketCap")})
