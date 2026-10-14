"""NEXUS research-only market scanner; no automatic trade entries."""
import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
OUT = Path('output')
OUT.mkdir(exist_ok=True)
BATCH = int(os.getenv('NEXUS_BATCH', '50'))
LIMIT = int(os.getenv('NEXUS_LIMIT', '0'))
SLEEP = float(os.getenv('NEXUS_SLEEP', '3'))
SOURCES = {'NASDAQ': 'https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt',
           'OTHER': 'https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt'}


def get_universe():
    pieces, stats = [], {}
    for market, url in SOURCES.items():
        response = requests.get(url, timeout=45, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        df = pd.read_csv(io.StringIO(response.text), sep='|', dtype=str)
        df = df[~df.iloc[:, 0].str.contains('File Creation Time', na=False)]
        stats[market + '_raw'] = len(df)
        df = df[(df['Test Issue'] == 'N') & (df['ETF'] == 'N')]
        if market == 'NASDAQ':
            df = df.rename(columns={'Symbol': 'symbol', 'Security Name': 'name'})
            df['exchange'] = 'NASDAQ'
        else:
            df = df[df['Exchange'].isin(['N', 'A'])]
            df = df.rename(columns={'ACT Symbol': 'symbol', 'Security Name': 'name'})
            df['exchange'] = df['Exchange'].map({'N': 'NYSE', 'A': 'NYSE American'})
        pieces.append(df[['symbol', 'name', 'exchange']])
    df = pd.concat(pieces, ignore_index=True).drop_duplicates('symbol')
    excluded = r'(?i)warrant|rights?($|\s)|units?($|\s)|preferred|preference|depositary shares|depositary receipts|american depositary|\badr\b|\betf\b|\betn\b|exchange traded|notes due|debenture|trust preferred'
    mask = df['name'].str.contains(excluded, regex=True, na=False)
    stats['excluded_by_name'] = int(mask.sum())
    df = df.loc[~mask].copy()
    stats['eligible_approx'] = len(df)
    if LIMIT > 0:
        df = df.head(LIMIT)
    df['yf_symbol'] = df['symbol'].str.replace('.', '-', regex=False)
    stats['attempted'] = len(df)
    df.to_csv(OUT / 'universe.csv', index=False)
    return df, stats


def evaluate(stock, prices):
    prices = prices.dropna(subset=['Close', 'Low', 'High', 'Volume'])
    if len(prices) < 55:
        raise ValueError('insufficient_history')
    c = prices['Close'].astype(float)
    v = prices['Volume'].astype(float)
    last = float(c.iloc[-1])
    low = float(prices['Low'].tail(252).min())
    high = float(prices['High'].tail(252).max())
    if min(last, low, high) <= 0:
        raise ValueError('invalid_prices')
    vol_avg = float(v.iloc[-21:-1].mean())
    vol_ratio = float(v.iloc[-1] / vol_avg) if vol_avg > 0 else 0
    ret5 = float(last / c.iloc[-6] - 1)
    ret20 = float(last / c.iloc[-21] - 1)
    rebound = last / low - 1
    liquidity = float((c * v).tail(20).mean())
    ma20 = float(c.tail(20).mean())
    previous_ma20 = float(c.iloc[-21:-1].mean())
    near_low = rebound <= .35
    early = near_low and last > float(c.iloc[-2]) and ret5 > 0 and vol_ratio >= 1.2
    confirmed = near_low and last > ma20 and ma20 > previous_ma20 and ret20 > .05
    signal = '反発確認' if confirmed else ('反発初動' if early else 'none')
    return dict(symbol=stock.symbol, name=stock.name, exchange=stock.exchange,
                price_date=str(prices.index[-1].date()), close=round(last, 4),
                low52=round(low, 4), high52=round(high, 4),
                rebound_pct=round(rebound * 100, 2), drawdown_pct=round((last / high - 1) * 100, 2),
                ret5_pct=round(ret5 * 100, 2), ret20_pct=round(ret20 * 100, 2),
                volume_ratio=round(vol_ratio, 2), avg_dollar_volume_20=round(liquidity),
                signal=signal, liquidity_flag=liquidity >= 2_000_000,
                fundamentals='未確認', entry_status='未判定', virtual_fill=False)


def main():
    started = datetime.now(timezone.utc)
    stocks, stats = get_universe()
    scored, failed = [], []
    for start in range(0, len(stocks), BATCH):
        block = stocks.iloc[start:start + BATCH]
        tickers = block['yf_symbol'].tolist()
        try:
            data = yf.download(tickers, period='1y', interval='1d', auto_adjust=True,
                               group_by='ticker', progress=False, threads=False)
        except Exception as error:
            failed.extend({'symbol': s, 'reason': 'batch_download:' + str(error)[:120]} for s in block['symbol'])
            continue
        for stock in block.itertuples(index=False):
            try:
                prices = data[stock.yf_symbol] if isinstance(data.columns, pd.MultiIndex) else data
                if prices.empty:
                    raise ValueError('empty_prices')
                scored.append(evaluate(stock, prices))
            except Exception as error:
                failed.append({'symbol': stock.symbol, 'reason': str(error)[:120]})
        logging.info('Attempted %d/%d, usable %d, failed %d', min(start+BATCH, len(stocks)), len(stocks), len(scored), len(failed))
        time.sleep(SLEEP)
    pd.DataFrame(scored, columns=['symbol', 'name', 'exchange', 'price_date', 'close', 'low52', 'high52', 'rebound_pct', 'drawdown_pct', 'ret5_pct', 'ret20_pct', 'volume_ratio', 'avg_dollar_volume_20', 'signal', 'liquidity_flag', 'fundamentals', 'entry_status', 'virtual_fill']).to_csv(OUT / 'all_scored.csv', index=False)
    df = pd.DataFrame(scored)
    candidates = df[(df.signal != 'none') & df.liquidity_flag].sort_values('volume_ratio', ascending=False) if len(df) else df
    candidates.to_csv(OUT / 'candidates.csv', index=False)
    pd.DataFrame(failed, columns=['symbol', 'reason']).to_csv(OUT / 'failures.csv', index=False)
    report = {'started_utc': started.isoformat(), 'started_jst': started.astimezone(ZoneInfo('Asia/Tokyo')).isoformat(),
              'reference_time_jst': '23:45', 'universe': stats, 'usable': len(scored),
              'failed_or_insufficient': len(failed), 'candidates': len(candidates),
              'confirmed': int((candidates.signal == '反発確認').sum()) if len(candidates) else 0,
              'early': int((candidates.signal == '反発初動').sum()) if len(candidates) else 0,
              'warning': 'Name-based common-stock exclusion is approximate. Free daily prices may be delayed, unavailable or rate-limited. No fundamentals verification or hypothetical fills.'}
    (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not scored:
        raise RuntimeError('No usable prices; inspect failures.csv')


if __name__ == '__main__':
    main()
