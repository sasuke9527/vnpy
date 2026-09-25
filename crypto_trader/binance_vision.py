"""
Download Binance USDT-M futures history from ``data.binance.vision`` into the
vnpy database and the funding file that ``backtest.py`` consumes.

Why this exists next to ``download_data.py``: the REST hosts
(``fapi.binance.com``) are geo-blocked from some networks (HTTP 451), while
the public archive bucket is a plain CDN that needs no API key and is not
blocked.  Monthly zips cover every listed USDT-M perpetual back to its
listing date; daily zips fill the current month.

CLI examples (run from anywhere; the script chdirs into ``crypto_trader``):

    python binance_vision.py --symbol ETHUSDT --start 2023-01 --funding
    python binance_vision.py --symbol DOGEUSDT --symbol 1000PEPEUSDT --start 2024-01
    python binance_vision.py --list-symbols PEPE,DOGE,WIF

Data lands in ``.vntrader/database.db`` as ``<SYMBOL>_SWAP_BINANCE`` on
``Exchange.GLOBAL`` (the scheme vnpy_binance emits), and funding in
``.vntrader/funding_<SYMBOL>.json`` as ``[[fundingTime_ms, rate], ...]``.
Downloaded zips are cached under ``crypto_trader/data/vision`` (git-ignored).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import zipfile
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

UTC = timezone.utc

# settings must be importable before vnpy is imported (it chdirs for us).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from settings import PROJ, ensure_trader_dir, symbol_for  # noqa: E402

BASE_URL = "https://data.binance.vision/data/futures/um"
LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
CACHE_DIR = PROJ / "data" / "vision"
KLINE_COLUMNS = (
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
)
INTERVALS = {"1m": "MINUTE", "1h": "HOUR", "1d": "DAILY"}
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


# ---------------------------------------------------------------------------
# URL helpers (pure)
# ---------------------------------------------------------------------------

def month_range(start: str, end: str) -> list[str]:
    """Inclusive list of ``YYYY-MM`` strings from ``start`` to ``end``."""
    if not (_MONTH_RE.match(start) and _MONTH_RE.match(end)):
        raise ValueError("months must be YYYY-MM")
    y, m = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    out: list[str] = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def monthly_kline_url(symbol: str, interval: str, ym: str) -> str:
    return f"{BASE_URL}/monthly/klines/{symbol}/{interval}/{symbol}-{interval}-{ym}.zip"


def daily_kline_url(symbol: str, interval: str, day: str) -> str:
    return f"{BASE_URL}/daily/klines/{symbol}/{interval}/{symbol}-{interval}-{day}.zip"


def monthly_funding_url(symbol: str, ym: str) -> str:
    return f"{BASE_URL}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{ym}.zip"


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------

def parse_kline_csv(text: str) -> Iterator[tuple[int, float, float, float, float, float, float]]:
    """
    Yield ``(open_time_ms, open, high, low, close, volume, quote_volume)``
    from an archive CSV, with or without the header row.
    """
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row or row[0] == "open_time" or not row[0].isdigit():
            continue
        yield (
            int(row[0]), float(row[1]), float(row[2]), float(row[3]),
            float(row[4]), float(row[5]), float(row[7]) if len(row) > 7 else 0.0,
        )


def parse_funding_csv(text: str) -> list[list[float]]:
    """``calc_time,funding_interval_hours,last_funding_rate`` -> ``[[ms, rate], ...]``."""
    out: list[list[float]] = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0].isdigit():
            continue
        out.append([int(row[0]), float(row[-1])])
    return out


def unzip_single_csv(blob: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".csv"))
        return zf.read(name).decode("utf-8")


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch(url: str, cache_path: Path | None = None, retries: int = 4, timeout: int = 120) -> bytes | None:
    """GET ``url`` (cached on disk); ``None`` on 404. Retries transient errors."""
    if cache_path and cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path.read_bytes()
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=timeout)
        except requests.RequestException as exc:  # network hiccup
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
            print(f"  retry {attempt + 1} after {type(exc).__name__}: {url}")
            continue
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            if attempt == retries - 1:
                raise RuntimeError(f"HTTP {resp.status_code} for {url}")
            time.sleep(2 ** attempt)
            continue
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(resp.content)
        return resp.content
    return None


def list_symbols(pattern: str = "") -> list[str]:
    """All USDT-M symbols in the archive; optional comma-separated substrings."""
    wanted = [p.strip().upper() for p in pattern.split(",") if p.strip()]
    prefix = "data/futures/um/monthly/klines/"
    marker = ""
    out: list[str] = []
    while True:
        url = f"{LIST_URL}?delimiter=/&prefix={prefix}&max-keys=1000" + (f"&marker={marker}" if marker else "")
        text = requests.get(url, timeout=60).text
        prefixes = re.findall(rf"<Prefix>{re.escape(prefix)}([^/<]+)/</Prefix>", text)
        out.extend(prefixes)
        if "<IsTruncated>true</IsTruncated>" not in text or not prefixes:
            break
        marker = f"{prefix}{prefixes[-1]}/"
    if wanted:
        out = [s for s in out if any(w in s for w in wanted)]
    return sorted(set(out))


# ---------------------------------------------------------------------------
# vnpy database sink (imports vnpy lazily, after chdir_project)
# ---------------------------------------------------------------------------

def rows_to_bars(rows: Iterator[tuple[int, float, float, float, float, float, float]], vnpy_symbol: str, interval: str) -> list[Any]:
    from vnpy.trader.constant import Exchange, Interval
    from vnpy.trader.object import BarData

    vn_interval = Interval[INTERVALS[interval]]
    bars: list[Any] = []
    for open_ms, o, h, low, c, vol, quote in rows:
        bars.append(BarData(
            symbol=vnpy_symbol,
            exchange=Exchange.GLOBAL,
            datetime=datetime.fromtimestamp(open_ms / 1000, tz=UTC),
            interval=vn_interval,
            volume=vol,
            turnover=quote,
            open_interest=0.0,
            open_price=o,
            high_price=h,
            low_price=low,
            close_price=c,
            gateway_name="VISION",
        ))
    return bars


def save_bars(bars: list[Any]) -> int:
    if not bars:
        return 0
    from vnpy.trader.database import get_database

    get_database().save_bar_data(bars)
    return len(bars)


def download_klines(symbol: str, interval: str, start: str, end: str | None = None, cache_dir: Path = CACHE_DIR) -> int:
    """
    Monthly zips from ``start`` (YYYY-MM) through the last available month,
    then daily zips for the remainder up to yesterday (UTC).  Returns bars saved.
    """
    today = datetime.now(tz=UTC).date()
    end_month = end or f"{today.year:04d}-{today.month:02d}"
    vnpy_symbol = symbol_for("binance_linear", symbol)
    total = 0
    last_month_ok: str | None = None

    for ym in month_range(start, end_month):
        url = monthly_kline_url(symbol, interval, ym)
        blob = fetch(url, cache_dir / symbol / interval / f"{ym}.zip")
        if blob is None:
            print(f"  {symbol} {interval} {ym}: monthly file missing")
            continue
        n = save_bars(rows_to_bars(parse_kline_csv(unzip_single_csv(blob)), vnpy_symbol, interval))
        total += n
        last_month_ok = ym
        print(f"  {symbol} {interval} {ym}: {n} bars")

    # Daily fill after the last monthly file (or from ``start`` when no
    # monthly file exists yet, e.g. when start is the current month).
    if last_month_ok is None:
        y, m = int(start[:4]), int(start[5:7])
    else:
        y, m = int(last_month_ok[:4]), int(last_month_ok[5:7])
        m += 1
        if m > 12:
            y, m = y + 1, 1
    day = datetime(y, m, 1, tzinfo=UTC).date()
    misses = 0
    while day < today and misses < 3:
        d = day.isoformat()
        blob = fetch(daily_kline_url(symbol, interval, d), cache_dir / symbol / interval / f"{d}.zip")
        if blob is None:
            misses += 1
        else:
            misses = 0
            n = save_bars(rows_to_bars(parse_kline_csv(unzip_single_csv(blob)), vnpy_symbol, interval))
            total += n
            print(f"  {symbol} {interval} {d}: {n} bars")
        day += timedelta(days=1)
    return total


def download_funding(symbol: str, start: str, end: str | None = None, cache_dir: Path = CACHE_DIR,
                     trader_dir: Path | None = None) -> int:
    """Merge monthly funding files into ``<trader_dir>/funding_<SYMBOL>.json``."""
    today = datetime.now(tz=UTC).date()
    end_month = end or f"{today.year:04d}-{today.month:02d}"
    path = (trader_dir or Path.cwd() / ".vntrader") / f"funding_{symbol}.json"
    merged: dict[int, float] = {}
    if path.exists():
        for ms, rate in json.loads(path.read_text()):
            merged[int(ms)] = float(rate)
    for ym in month_range(start, end_month):
        blob = fetch(monthly_funding_url(symbol, ym), cache_dir / symbol / "funding" / f"{ym}.zip")
        if blob is None:
            continue
        for ms, rate in parse_funding_csv(unzip_single_csv(blob)):
            merged[int(ms)] = rate
    rows = sorted([[ms, rate] for ms, rate in merged.items()])
    path.write_text(json.dumps(rows))
    print(f"  {symbol} funding: {len(rows)} stamps -> {path.name}")
    return len(rows)


def print_overview(symbol: str) -> None:
    from vnpy.trader.database import get_database

    vnpy_symbol = symbol_for("binance_linear", symbol)
    for o in get_database().get_bar_overview():
        if o.symbol == vnpy_symbol:
            print(f"  DB {o.symbol} {o.interval.value}: {o.count} bars {o.start} -> {o.end}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", action="append", default=[], help="Binance symbol, repeatable (ETHUSDT)")
    parser.add_argument("--interval", default="1m", choices=sorted(INTERVALS))
    parser.add_argument("--start", default="2023-01", help="first month YYYY-MM")
    parser.add_argument("--end", default=None, help="last month YYYY-MM (default: current month)")
    parser.add_argument("--funding", action="store_true", help="also download funding rates")
    parser.add_argument("--no-klines", action="store_true", help="skip klines (funding only)")
    parser.add_argument("--list-symbols", default=None, metavar="SUBSTR[,SUBSTR]", help="list archive symbols and exit")
    parser.add_argument("--trader-dir", default=None, metavar="PATH",
                        help="alternative .vntrader folder (default: crypto_trader/.vntrader)")
    args = parser.parse_args(argv)

    if args.list_symbols is not None:
        for s in list_symbols(args.list_symbols):
            print(s)
        return 0
    if not args.symbol:
        parser.error("--symbol is required")

    trader_dir = ensure_trader_dir(Path(args.trader_dir).resolve() if args.trader_dir else None)
    os.chdir(trader_dir.parent)  # vnpy resolves cwd/.vntrader at import time
    for symbol in args.symbol:
        symbol = symbol.upper()
        print(f"== {symbol}")
        if not args.no_klines:
            n = download_klines(symbol, args.interval, args.start, args.end)
            print(f"  saved {n} bars")
        if args.funding:
            download_funding(symbol, args.start, args.end, trader_dir=trader_dir)
        print_overview(symbol)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
