"""NEXUS v2.3: use completed prior-session daily bars; never records hypothetical fills."""
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
OUT = Path("output")
OUT.mkdir(exist_ok=True)
BATCH = int(os.getenv("NEXUS_BATCH", "40"))
LIMIT = int(os.getenv("NEXUS_LIMIT", "0"))
SLEEP = float(os.getenv("NEXUS_SLEEP", "2.5"))
SOURCES = {
    "NASDAQ": "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt",
    "OTHER": "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt",
}
# Conservative name-based exclusion: classification remains approximate.
NON_COMMON = re.compile(
    r"\b(?:warrants?|rights?|units?|preferred|preference|depositary|ADR|ADS|"
    r"ETF|ETN|ETP|closed.end|exchange.traded|debentures?|bonds?|notes?|"
    r"fund|portfolio|income trust|trust units?|acquisition rights)\b", re.I
)
COLUMNS = [
    "symbol", "name", "exchange", "price_date", "close", "low52", "high52",
    "rebound_pct", "drawdown_pct", "ret5_pct", "ret20_pct", "volume_ratio",
    "avg_dollar_volume_20", "ma20", "ma50", "signal", "priority",
    "liquidity_flag", "price_flag", "history_days", "full_52week_history", "fundamentals", "entry_status", "virtual_fill",
]


def universe():
    frames, counts = [], {}
    for market, url in SOURCES.items():
        response = requests.get(url, timeout=45, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        df = pd.read_csv(io.StringIO(response.text), sep="|", dtype=str)
        df = df[~df.iloc[:, 0].str.contains("File Creation Time", na=False)]
        counts[market + "_raw"] = len(df)
        df = df[(df["Test Issue"] == "N") & (df["ETF"] == "N")]
        if market == "NASDAQ":
            df = df.rename(columns={"Symbol": "symbol", "Security Name": "name"})
            df["exchange"] = "NASDAQ"
        else:
            df = df[df["Exchange"].isin(["N", "A"])]
            df = df.rename(columns={"ACT Symbol": "symbol", "Security Name": "name"})
            df["exchange"] = df["Exchange"].map({"N": "NYSE", "A": "NYSE American"})
        frames.append(df[["symbol", "name", "exchange"]])
    df = pd.concat(frames, ignore_index=True).drop_duplicates("symbol")
    excluded = df["name"].fillna("").map(lambda name: bool(NON_COMMON.search(name)))
    counts["excluded_by_name"] = int(excluded.sum())
    df = df[~excluded].copy()
    counts["eligible_approx"] = len(df)
    if LIMIT > 0:
        df = df.head(LIMIT).copy()
    df["yf_symbol"] = df["symbol"].str.replace(".", "-", regex=False)
    counts["attempted"] = len(df)
    df.to_csv(OUT / "universe.csv", index=False)
    return df, counts


def evaluate(stock, prices):
    # Yahoo's current-session daily candle has partial volume before the close.
    # Exclude all bars dated today in New York, even if a market has already closed.
    # This deliberately produces a previous-session research scan, NOT a 23:45 fill.
    ny_today = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
    dates = pd.to_datetime(prices.index)
    if dates.tz is not None:
        dates = dates.tz_convert("America/New_York")
    prices = prices.loc[[day.date() < ny_today for day in dates]]
    prices = prices.dropna(subset=["Close", "Low", "High", "Volume"])
    if len(prices) < 55:
        raise ValueError("insufficient_history")
    close = prices["Close"].astype(float)
    volume = prices["Volume"].astype(float)
    last = float(close.iloc[-1])
    low = float(prices["Low"].tail(252).min())
    high = float(prices["High"].tail(252).max())
    if min(last, low, high) <= 0:
        raise ValueError("invalid_prices")
    average_volume = float(volume.iloc[-21:-1].mean())
    volume_ratio = float(volume.iloc[-1] / average_volume) if average_volume > 0 else 0.0
    ret5 = float(last / close.iloc[-6] - 1)
    ret20 = float(last / close.iloc[-21] - 1)
    rebound = last / low - 1
    drawdown = last / high - 1
    liquidity = float((close * volume).tail(20).mean())
    ma20 = float(close.tail(20).mean())
    ma50 = float(close.tail(50).mean())
    previous_ma20 = float(close.iloc[-21:-1].mean())
    # Candidate signals only. Financial quality, event risks and R:R require review.
    near_low = rebound <= 0.45 and drawdown <= -0.18
    turning = last > float(close.iloc[-2]) and ret5 > 0
    early = near_low and turning and (last <= ma20 * 1.06 or ret20 <= 0.05)
    confirmed = near_low and last > ma20 and ma20 > previous_ma20 and ret20 > 0.05
    signal = "åçºç¢ºèª" if confirmed else ("åçºåå" if early else "none")
    priority = round(min(volume_ratio, 4) * 10 + min(max(ret5, 0), 0.2) * 100
                     + max(0, 0.45 - rebound) * 20, 2)
    return {
        "symbol": stock.symbol, "name": stock.name, "exchange": stock.exchange,
        "price_date": str(prices.index[-1].date()), "close": round(last, 4),
        "low52": round(low, 4), "high52": round(high, 4),
        "rebound_pct": round(rebound * 100, 2),
        "drawdown_pct": round(drawdown * 100, 2),
        "ret5_pct": round(ret5 * 100, 2), "ret20_pct": round(ret20 * 100, 2),
        "volume_ratio": round(volume_ratio, 2),
        "avg_dollar_volume_20": round(liquidity),
        "ma20": round(ma20, 4), "ma50": round(ma50, 4),
        "signal": signal, "priority": priority,
        "liquidity_flag": liquidity >= 2_000_000,
        "price_flag": last >= 2,
        "history_days": len(prices),
        "full_52week_history": len(prices) >= 240,
        "fundamentals": "æªç¢ºèª", "entry_status": "æªå¤å®",
        "virtual_fill": False,
    }


def fetch_prices(tickers):
    error = "empty_download"
    for attempt in range(3):
        try:
            data = yf.download(
                tickers, period="1y", interval="1d", auto_adjust=True,
                group_by="ticker", progress=False, threads=False,
            )
            if data is not None and not data.empty:
                return data
        except Exception as exc:
            error = str(exc)[:100]
        time.sleep(2 * (attempt + 1))
    raise ValueError("download_failed:" + error)


def main():
    started = datetime.now(timezone.utc)
    stocks, counts = universe()
    scored, failed = [], []
    for index in range(0, len(stocks), BATCH):
        block = stocks.iloc[index:index + BATCH]
        try:
            batch = fetch_prices(block["yf_symbol"].tolist())
        except Exception as exc:
            logging.warning("Batch download failed; retrying singles: %s", exc)
            batch = None
        for stock in block.itertuples(index=False):
            try:
                prices = None
                if batch is not None:
                    try:
                        prices = batch[stock.yf_symbol] if isinstance(batch.columns, pd.MultiIndex) else batch
                    except KeyError:
                        pass
                if prices is None or prices.empty or prices["Close"].dropna().empty:
                    single = fetch_prices([stock.yf_symbol])
                    prices = single[stock.yf_symbol] if isinstance(single.columns, pd.MultiIndex) else single
                scored.append(evaluate(stock, prices))
            except Exception as exc:
                failed.append({"symbol": stock.symbol, "reason": str(exc)[:120]})
        logging.info("Attempted %d/%d usable=%d unavailable=%d",
                     min(index + BATCH, len(stocks)), len(stocks), len(scored), len(failed))
        time.sleep(SLEEP)

    data = pd.DataFrame(scored, columns=COLUMNS)
    data.to_csv(OUT / "all_scored.csv", index=False)
    candidates = data[
        (data["signal"] != "none") & data["liquidity_flag"] & data["price_flag"]
    ].sort_values("priority", ascending=False)
    candidates.to_csv(OUT / "candidates.csv", index=False)
    pd.DataFrame(failed, columns=["symbol", "reason"]).to_csv(OUT / "failures.csv", index=False)
    ny = started.astimezone(ZoneInfo("America/New_York"))
    report = {
        "version": "2.3", "started_utc": started.isoformat(),
        "started_jst": started.astimezone(ZoneInfo("Asia/Tokyo")).isoformat(),
        "reference_time_jst": "23:45",
        "price_basis": "Completed prior-session daily bars only; current New York calendar day excluded",
        "minutes_after_us_open_at_reference": 75 if ny.dst().total_seconds() else 15,
        "universe": counts, "usable": len(scored), "failed_or_insufficient": len(failed),
        "candidates": len(candidates),
        "confirmed": int((candidates["signal"] == "åçºç¢ºèª").sum()),
        "early": int((candidates["signal"] == "åçºåå").sum()),
        "warning": "Approximate common-share filter; may exclude valid stocks or include non-common instruments. Daily bars for the current NY date are excluded; signals lag at least one session. Free daily prices may be delayed. Financial review and R:R are unverified. No virtual trades recorded.",
    }
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not scored:
        raise RuntimeError("No usable price series")


if __name__ == "__main__":
    main()
