"""
Mag7 data fetcher.

Runs every 15 minutes via GitHub Actions (market hours only).
- Always: Yahoo Finance v8 (price + RSI) — free, unlimited
- Once per hour: FMP stable API (PEG, ROE, margins, growth) — 250 calls/day limit
                 yfinance (analyst recommendations + price targets) — unlimited
"""

import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import yfinance as yf

API_KEY  = os.environ.get("FMP_API_KEY", "bDvRFJXzjs915DCcrFYoQ1BIsifPdSaB")
DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "mag7.json")
TICKERS   = ["MSFT", "AAPL", "NVDA", "AMZN", "GOOGL", "META", "TSLA"]
FMP_INTERVAL_HOURS = 1

NAMES = {
    "MSFT": "Microsoft", "AAPL": "Apple",    "NVDA": "NVIDIA",
    "AMZN": "Amazon",    "GOOGL": "Alphabet", "META": "Meta", "TSLA": "Tesla",
}
BETAS = {
    "MSFT": 0.90, "AAPL": 1.24, "NVDA": 1.97,
    "AMZN": 1.31, "GOOGL": 1.05, "META": 1.27, "TSLA": 2.31,
}


def fetch_url(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def calc_rsi(closes, period=14):
    clean = [c for c in (closes or []) if c is not None]
    if len(clean) < period + 1:
        return None
    changes = [clean[i + 1] - clean[i] for i in range(len(clean) - 1)]
    gains   = [max(c,  0) for c in changes]
    losses  = [max(-c, 0) for c in changes]
    avg_gain = sum(gains[:period])  / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i])  / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100
    return round(100 - (100 / (1 + avg_gain / avg_loss)))


# ── Yahoo Finance v8 (price + RSI) ────────────────────────────────────────────

def fetch_yahoo(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1mo"
    try:
        data   = fetch_url(url)
        result = data["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        price  = result["meta"].get("regularMarketPrice")
        return {"ticker": ticker, "price": price, "rsi": calc_rsi(closes)}
    except Exception as e:
        print(f"Yahoo error [{ticker}]: {e}")
        return {"ticker": ticker, "price": None, "rsi": None}


# ── yfinance (analyst recommendations + price targets) ────────────────────────

def fetch_analyst(ticker):
    try:
        t = yf.Ticker(ticker)

        recs = t.recommendations
        if recs is None or recs.empty:
            return None
        row = recs.iloc[0]
        buy  = int(row.get("strongBuy", 0)) + int(row.get("buy", 0))
        hold = int(row.get("hold", 0))
        sell = int(row.get("sell", 0)) + int(row.get("strongSell", 0))

        apt = t.analyst_price_targets or {}
        target = apt.get("mean")

        return {
            "buy":    buy,
            "hold":   hold,
            "sell":   sell,
            "target": round(float(target), 2) if target else None,
        }
    except Exception as e:
        print(f"yfinance analyst error [{ticker}]: {e}")
        return None


# ── FMP stable API ─────────────────────────────────────────────────────────────

def fetch_fmp_quotes():
    url = f"https://financialmodelingprep.com/stable/quote?symbol={','.join(TICKERS)}&apikey={API_KEY}"
    try:
        data = fetch_url(url)
        return {item["symbol"]: item for item in data} if isinstance(data, list) else {}
    except Exception as e:
        print(f"FMP quote error: {e}")
        return {}


def fetch_fmp_ratios(ticker):
    url = f"https://financialmodelingprep.com/stable/ratios?symbol={ticker}&apikey={API_KEY}"
    try:
        data = fetch_url(url)
        return {"ticker": ticker, "type": "ratios", "data": data[0] if isinstance(data, list) and data else {}}
    except Exception as e:
        print(f"FMP ratios error [{ticker}]: {e}")
        return {"ticker": ticker, "type": "ratios", "data": {}}


def fetch_fmp_metrics(ticker):
    url = f"https://financialmodelingprep.com/stable/key-metrics?symbol={ticker}&apikey={API_KEY}"
    try:
        data = fetch_url(url)
        return {"ticker": ticker, "type": "metrics", "data": data[0] if isinstance(data, list) and data else {}}
    except Exception as e:
        print(f"FMP metrics error [{ticker}]: {e}")
        return {"ticker": ticker, "type": "metrics", "data": {}}


def fetch_fmp_growth(ticker):
    url = f"https://financialmodelingprep.com/stable/financial-growth?symbol={ticker}&apikey={API_KEY}"
    try:
        data = fetch_url(url)
        return {"ticker": ticker, "type": "growth", "data": data[0] if isinstance(data, list) and data else {}}
    except Exception as e:
        print(f"FMP growth error [{ticker}]: {e}")
        return {"ticker": ticker, "type": "growth", "data": {}}


# ── Main ───────────────────────────────────────────────────────────────────────

def load_existing():
    try:
        with open(DATA_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {"lastFmpUpdate": None, "lastYahooUpdate": None, "lastAnalystUpdate": None, "stocks": []}


def should_update_fmp(existing):
    last = existing.get("lastFmpUpdate")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - last_dt >= timedelta(hours=FMP_INTERVAL_HOURS)
    except Exception:
        return True


def build_stock_map(existing):
    return {s["ticker"]: s for s in existing.get("stocks", [])}


def main():
    now       = datetime.now(timezone.utc)
    existing  = load_existing()
    stock_map = build_stock_map(existing)
    do_fmp    = should_update_fmp(existing)

    print(f"Run at {now.isoformat()} | FMP+analyst update: {do_fmp}")

    # ── Yahoo Finance v8 (always) — parallel ──────────────────────────────────
    yahoo_results = {}
    with ThreadPoolExecutor(max_workers=7) as ex:
        futures = {ex.submit(fetch_yahoo, t): t for t in TICKERS}
        for future in as_completed(futures):
            r = future.result()
            yahoo_results[r["ticker"]] = r

    # ── FMP + analyst (once per hour) — parallel ──────────────────────────────
    fmp_quotes    = {}
    fmp_by_ticker = {t: {"ratios": {}, "metrics": {}, "growth": {}} for t in TICKERS}
    analyst_results = {}

    if do_fmp:
        print("Fetching FMP + analyst data...")
        fmp_quotes = fetch_fmp_quotes()  # 1 batch call

        fmp_tasks = []
        for t in TICKERS:
            fmp_tasks.append(("ratios",  t))
            fmp_tasks.append(("metrics", t))
            fmp_tasks.append(("growth",  t))

        fn_map = {"ratios": fetch_fmp_ratios, "metrics": fetch_fmp_metrics, "growth": fetch_fmp_growth}

        with ThreadPoolExecutor(max_workers=10) as ex:
            fmp_futures  = {ex.submit(fn_map[kind], ticker): (kind, ticker) for kind, ticker in fmp_tasks}
            anal_futures = {ex.submit(fetch_analyst, ticker): ticker for ticker in TICKERS}

            for future in as_completed({**fmp_futures, **anal_futures}):
                if future in fmp_futures:
                    r = future.result()
                    fmp_by_ticker[r["ticker"]][r["type"]] = r["data"]
                else:
                    ticker = anal_futures[future]
                    result = future.result()
                    if result:
                        analyst_results[ticker] = result

    # ── Merge ─────────────────────────────────────────────────────────────────
    stocks = []
    for ticker in TICKERS:
        base    = stock_map.get(ticker, {})
        yahoo   = yahoo_results.get(ticker, {})
        ratios  = fmp_by_ticker[ticker]["ratios"]
        metrics = fmp_by_ticker[ticker]["metrics"]
        growth  = fmp_by_ticker[ticker]["growth"]
        quote   = fmp_quotes.get(ticker, {})
        analyst = analyst_results.get(ticker, {})

        def get(new_val, old_key, scale=1.0):
            if new_val is not None:
                try:
                    return round(float(new_val) * scale, 4)
                except Exception:
                    pass
            return base.get(old_key)

        price = yahoo.get("price") or quote.get("price") or base.get("price")

        stocks.append({
            "ticker":        ticker,
            "name":          NAMES[ticker],
            "price":         round(price, 2) if price else base.get("price"),
            "pe":            get(ratios.get("priceToEarningsRatio"),         "pe"),
            "peg":           get(ratios.get("priceToEarningsGrowthRatio"),   "peg"),
            "roe":           get(metrics.get("returnOnEquity"),               "roe",  100.0),
            "grossMargin":   get(ratios.get("grossProfitMargin"),             "grossMargin", 100.0),
            "revenueGrowth": get(growth.get("revenueGrowth"),                "revenueGrowth", 100.0),
            "rsi":           yahoo.get("rsi") or base.get("rsi"),
            "beta":          BETAS[ticker],
            "buy":           analyst.get("buy")    if analyst else base.get("buy"),
            "hold":          analyst.get("hold")   if analyst else base.get("hold"),
            "sell":          analyst.get("sell")   if analyst else base.get("sell"),
            "target":        analyst.get("target") if analyst else base.get("target"),
        })

    last_analyst = now.strftime("%Y-%m-%dT%H:%M:%SZ") if analyst_results else existing.get("lastAnalystUpdate")

    output = {
        "lastFmpUpdate":     now.strftime("%Y-%m-%dT%H:%M:%SZ") if do_fmp else existing.get("lastFmpUpdate"),
        "lastYahooUpdate":   now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lastAnalystUpdate": last_analyst,
        "generatedAt":       now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stocks":            stocks,
    }

    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Done. Wrote {len(stocks)} stocks. FMP+analyst updated: {do_fmp}")


if __name__ == "__main__":
    main()
