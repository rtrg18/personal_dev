"""
portfolio_sync.py
-----------------
Reads positions from positions.json, fetches live market data via yfinance,
computes valuations in SGD, and writes portfolio.json for Rainmeter to read.

Requirements:
    pip install yfinance

Recommended: run via Windows Task Scheduler every 10-15 minutes during market hours.
"""

import json
import os
import logging
from datetime import datetime, timezone
from pathlib import Path

import yfinance as yf

# ---------------------------------------------------------------------------
# CONFIG — adjust paths if needed
# ---------------------------------------------------------------------------
BASE_DIR       = Path(__file__).parent
POSITIONS_FILE = BASE_DIR / "positions.json"
OUTPUT_FILE    = BASE_DIR / "portfolio.json"
LOG_FILE       = BASE_DIR / "portfolio_sync.log"

# Fallback FX rates used only if yfinance cannot fetch live rates.
# Update these occasionally as a rough safety net.
FALLBACK_FX = {
    "USD": 1.34,
    "HKD": 0.172,
    "SGD": 1.0,
}

# How many trading days to use for historical volatility calculation (annualised).
VOLATILITY_DAYS = 30

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FX HELPERS
# ---------------------------------------------------------------------------
FX_TICKERS = {
    "USD": "USDSGD=X",
    "HKD": "HKDSGD=X",
}

def fetch_fx_rates() -> dict[str, float]:
    """Return a dict of {currency: rate_to_SGD}. Falls back gracefully."""
    rates = {"SGD": 1.0}
    for currency, ticker in FX_TICKERS.items():
        try:
            data = yf.Ticker(ticker).fast_info
            rate = data.last_price
            if rate and rate > 0:
                rates[currency] = float(rate)
                log.info(f"FX {currency}/SGD = {rate:.4f}")
            else:
                raise ValueError("No price returned")
        except Exception as e:
            fallback = FALLBACK_FX.get(currency, 1.0)
            rates[currency] = fallback
            log.warning(f"FX fetch failed for {currency} ({e}). Using fallback {fallback}")
    return rates


def to_sgd(amount: float, currency: str, rates: dict[str, float]) -> float:
    return amount * rates.get(currency, FALLBACK_FX.get(currency, 1.0))


# ---------------------------------------------------------------------------
# MARKET DATA HELPERS
# ---------------------------------------------------------------------------
def fetch_ticker_data(ticker_symbol: str) -> dict:
    """
    Fetch price, 52-week high, previous close, sector, and 30-day volatility
    for a single ticker. Returns a dict with None for any field that fails.
    """
    result = {
        "last_price":    None,
        "prev_close":    None,
        "week52_high":   None,
        "volatility_30d": None,
        "sector":        None,
        "industry":      None,
        "currency":      None,
        "name":          None,
        "fetch_error":   None,
    }

    try:
        t = yf.Ticker(ticker_symbol)
        info = t.info

        # Last price — prefer regularMarketPrice, fall back to previousClose
        result["last_price"]  = info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose")
        result["prev_close"]  = info.get("previousClose") or info.get("regularMarketPreviousClose")
        result["week52_high"] = info.get("fiftyTwoWeekHigh")
        result["sector"]      = info.get("sector")
        result["industry"]    = info.get("industry")
        result["currency"]    = info.get("currency")
        result["name"]        = info.get("shortName") or info.get("longName") or ticker_symbol

        # 30-day historical volatility (annualised)
        hist = t.history(period=f"{VOLATILITY_DAYS + 5}d")
        if len(hist) >= 5:
            returns = hist["Close"].pct_change().dropna()
            if len(returns) >= 5:
                import math
                vol = float(returns.tail(VOLATILITY_DAYS).std() * math.sqrt(252))
                result["volatility_30d"] = round(vol * 100, 2)  # expressed as %

        log.info(f"Fetched {ticker_symbol}: price={result['last_price']}, sector={result['sector']}")

    except Exception as e:
        result["fetch_error"] = str(e)
        log.error(f"Failed to fetch {ticker_symbol}: {e}")

    return result


# ---------------------------------------------------------------------------
# CORE AGGREGATION
# ---------------------------------------------------------------------------
def build_portfolio(positions: list[dict], fx_rates: dict[str, float]) -> dict:
    """
    Merge position data with live market data.
    Returns the full portfolio dict ready to be written as JSON.
    """
    sectors: dict[str, list] = {}
    total_cost_sgd   = 0.0
    total_value_sgd  = 0.0
    total_pnl_sgd    = 0.0

    for pos in positions:
        ticker_symbol = pos["ticker"]
        qty           = float(pos["quantity"])
        avg_cost      = float(pos["avg_cost"])
        pos_currency  = pos.get("currency", "SGD").upper()
        brokerage     = pos.get("brokerage", "Unknown")
        acquired_date = pos.get("acquired_date", "")

        market = fetch_ticker_data(ticker_symbol)

        # Determine the price currency — trust yfinance over positions.json
        # (e.g. for .SI tickers yfinance reports SGD automatically)
        price_currency = (market["currency"] or pos_currency).upper()

        last_price  = market["last_price"]
        prev_close  = market["prev_close"]
        week52_high = market["week52_high"]
        volatility  = market["volatility_30d"]

        # --- Compute values ---
        if last_price is not None:
            cost_local      = qty * avg_cost
            mkt_value_local = qty * last_price
            pnl_local       = mkt_value_local - cost_local
            pnl_pct         = (pnl_local / cost_local * 100) if cost_local else 0.0

            cost_sgd      = to_sgd(cost_local,      price_currency, fx_rates)
            mkt_value_sgd = to_sgd(mkt_value_local, price_currency, fx_rates)
            pnl_sgd       = to_sgd(pnl_local,       price_currency, fx_rates)

            day_chg_pct = (
                round((last_price - prev_close) / prev_close * 100, 2)
                if prev_close and prev_close > 0 else None
            )
            pct_from_52wk_high = (
                round((last_price - week52_high) / week52_high * 100, 2)
                if week52_high and week52_high > 0 else None
            )
        else:
            # Price fetch failed — show cost basis only, null out computed fields
            cost_sgd      = to_sgd(qty * avg_cost, price_currency, fx_rates)
            mkt_value_sgd = None
            pnl_sgd       = None
            pnl_pct       = None
            day_chg_pct   = None
            pct_from_52wk_high = None

        total_cost_sgd  += cost_sgd
        if mkt_value_sgd is not None:
            total_value_sgd += mkt_value_sgd
        if pnl_sgd is not None:
            total_pnl_sgd += pnl_sgd

        # --- Sector grouping ---
        sector = market["sector"] or "Other / Unclassified"

        entry = {
            "ticker":              ticker_symbol,
            "name":                market["name"] or ticker_symbol,
            "brokerage":           brokerage,
            "industry":            market["industry"],
            "quantity":            qty,
            "avg_cost":            avg_cost,
            "avg_cost_currency":   pos_currency,
            "acquired_date":       acquired_date,

            # Live price data
            "last_price":          round(last_price, 4)  if last_price  is not None else None,
            "last_price_currency": price_currency,
            "prev_close":          round(prev_close, 4)  if prev_close  is not None else None,
            "week52_high":         round(week52_high, 4) if week52_high is not None else None,
            "pct_from_52wk_high":  pct_from_52wk_high,
            "volatility_30d_pct":  volatility,
            "day_change_pct":      day_chg_pct,

            # SGD valuations
            "cost_basis_sgd":      round(cost_sgd, 2),
            "mkt_value_sgd":       round(mkt_value_sgd, 2) if mkt_value_sgd is not None else None,
            "unrealised_pnl_sgd":  round(pnl_sgd, 2)       if pnl_sgd       is not None else None,
            "unrealised_pnl_pct":  round(pnl_pct, 2)       if pnl_pct       is not None else None,

            "fetch_error":         market["fetch_error"],
        }

        sectors.setdefault(sector, []).append(entry)

    # Sort positions within each sector by market value descending
    for sector in sectors:
        sectors[sector].sort(
            key=lambda x: x["mkt_value_sgd"] if x["mkt_value_sgd"] is not None else 0,
            reverse=True,
        )

    # Sort sectors by total market value descending
    sectors_sorted = dict(
        sorted(
            sectors.items(),
            key=lambda kv: sum(p["mkt_value_sgd"] or 0 for p in kv[1]),
            reverse=True,
        )
    )

    total_pnl_pct = (total_pnl_sgd / total_cost_sgd * 100) if total_cost_sgd else 0.0

    return {
        "meta": {
            "last_updated":        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "last_updated_local":  datetime.now().strftime("%Y-%m-%d %H:%M"),
            "fx_rates":            fx_rates,
            "positions_file":      str(POSITIONS_FILE),
        },
        "summary": {
            "total_cost_sgd":          round(total_cost_sgd, 2),
            "total_mkt_value_sgd":     round(total_value_sgd, 2),
            "total_unrealised_pnl_sgd": round(total_pnl_sgd, 2),
            "total_unrealised_pnl_pct": round(total_pnl_pct, 2),
            "num_positions":           len(positions),
            "num_sectors":             len(sectors_sorted),
        },
        "by_sector": sectors_sorted,
    }


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------
def main():
    log.info("=== portfolio_sync starting ===")

    # Load positions
    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            positions = json.load(f)
        log.info(f"Loaded {len(positions)} positions from {POSITIONS_FILE}")
    except Exception as e:
        log.error(f"Could not load positions.json: {e}")
        return

    # Fetch FX rates
    fx_rates = fetch_fx_rates()

    # Build portfolio
    portfolio = build_portfolio(positions, fx_rates)

    # Write output atomically (write to temp file first, then rename)
    tmp_file = OUTPUT_FILE.with_suffix(".tmp")
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(portfolio, f, indent=2, ensure_ascii=False)
        os.replace(tmp_file, OUTPUT_FILE)
        log.info(f"portfolio.json written to {OUTPUT_FILE}")
    except Exception as e:
        log.error(f"Failed to write portfolio.json: {e}")
        if tmp_file.exists():
            tmp_file.unlink()

    log.info("=== portfolio_sync complete ===")


if __name__ == "__main__":
    main()