"""
Historical data downloader for crypto_trader (spec section 7, "Data").

Pulls klines, funding-rate history and exchange filters from the PUBLIC
REST endpoints of Binance USDT-M futures and OKX with plain ``requests``
(no gateway login, no API key, no vnpy datafeed licence) and stores them
where ``backtest.py`` / ``run_live.py`` expect them:

* bars      -> ``.vntrader/database.db`` via ``get_database().save_bar_data``
               (upsert on ``symbol, exchange, interval, datetime``) using the
               exact symbol scheme the live gateways emit:
               ``ETHUSDT_SWAP_BINANCE`` / ``ETHUSDT_SWAP_OKX`` on ``Exchange.GLOBAL``
* funding   -> ``.vntrader/funding_<BASE>.json`` as ``[[fundingTime_ms, rate], ...]``
* filters   -> ``.vntrader/exchange_filters.json`` as
               ``{name: {tickSize, stepSize, minQty, minNotional[, ctVal]}}``

CLI (run from anywhere; the script chdirs into ``crypto_trader`` before the
first vnpy import so ``.vntrader`` resolves to the project folder)::

    python download_data.py --exchange binance_linear --symbol ETHUSDT --interval 1m --start 2023-01-01
    python download_data.py --exchange binance_linear --symbol ETHUSDT --start 2023-01-01 --funding --filters
    python download_data.py --exchange okx --symbol ETHUSDT --interval 1h --start 2024-01-01 --end 2024-06-30
    python download_data.py --exchange binance_linear --symbol ETHUSDT --filters --skip-bars

Endpoints (all public, no auth):

* Binance ``GET /fapi/v1/klines``          limit 1500, oldest first, paginate by ``startTime``
* Binance ``GET /fapi/v1/fundingRate``     limit 1000, oldest first, paginate by ``startTime``
* Binance ``GET /fapi/v1/exchangeInfo``    PRICE_FILTER / LOT_SIZE / MIN_NOTIONAL per symbol
* OKX     ``GET /api/v5/market/history-candles``     limit 100, NEWEST first, paginate with ``after``
* OKX     ``GET /api/v5/public/funding-rate-history`` limit 100, newest first, paginate with ``after``
* OKX     ``GET /api/v5/public/instruments?instType=SWAP``  ctVal / tickSz / lotSz / minSz

The ``parse_*`` functions are pure (network-free) and are what
``tests/test_download_parse.py`` exercises with literal sample rows.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

PROJ: Path = Path(__file__).resolve().parent

# vnpy fixes TRADER_DIR at first import from cwd/.vntrader, so when run as a
# script we must chdir into the project (and create .vntrader) *before* the
# vnpy imports below.  Under pytest, tests/conftest.py has already done the
# equivalent in a temp dir, and importing this module must not chdir again.
if __name__ == "__main__":  # pragma: no cover - exercised by the CLI only
    (PROJ / ".vntrader").mkdir(parents=True, exist_ok=True)
    os.chdir(PROJ)
    if str(PROJ) not in sys.path:
        sys.path.insert(0, str(PROJ))

from vnpy.trader.constant import Exchange, Interval  # noqa: E402
from vnpy.trader.database import DB_TZ, get_database  # noqa: E402
from vnpy.trader.object import BarData  # noqa: E402
from vnpy.trader.utility import get_file_path  # noqa: E402

log = logging.getLogger("download_data")

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Constants (mirroring the gateways)
# ---------------------------------------------------------------------------

EXCHANGES: tuple[str, ...] = ("binance_linear", "okx")
GATEWAY_NAMES: dict[str, str] = {"binance_linear": "BINANCE_LINEAR", "okx": "OKX"}
SYMBOL_SUFFIX: dict[str, str] = {"binance_linear": "_SWAP_BINANCE", "okx": "_SWAP_OKX"}

BINANCE_HOST: str = "https://fapi.binance.com"      # linear_gateway.py REAL_REST_HOST
OKX_HOST: str = "https://www.okx.com"               # okx_gateway.py REAL_REST_HOST

#: vnpy Interval -> exchange interval string (linear_gateway.py:97-101, okx_gateway.py:84-88)
BINANCE_INTERVAL: dict[Interval, str] = {Interval.MINUTE: "1m", Interval.HOUR: "1h", Interval.DAILY: "1d"}
OKX_INTERVAL: dict[Interval, str] = {Interval.MINUTE: "1m", Interval.HOUR: "1H", Interval.DAILY: "1D"}
INTERVAL_DELTA: dict[Interval, timedelta] = {
    Interval.MINUTE: timedelta(minutes=1),
    Interval.HOUR: timedelta(hours=1),
    Interval.DAILY: timedelta(days=1),
}

BINANCE_KLINE_LIMIT: int = 1500
BINANCE_FUNDING_LIMIT: int = 1000
OKX_CANDLE_LIMIT: int = 100
OKX_FUNDING_LIMIT: int = 100

#: Binance IP budget is 2400 weight / minute; a 1500-row klines call costs 10.
BINANCE_WEIGHT_BUDGET: int = 2400
BINANCE_WEIGHT_SOFT_LIMIT: int = 2000
BINANCE_PAGE_SLEEP: float = 0.3
#: OKX: 20 requests / 2 s per IP for history-candles.
OKX_PAGE_SLEEP: float = 0.12

REQUEST_TIMEOUT: float = 30.0
MAX_RETRIES: int = 6
SAVE_CHUNK: int = 10_000

_QUOTES: tuple[str, ...] = ("USDT", "USDC", "USD")


# ---------------------------------------------------------------------------
# Symbol helpers (kept local so this module works without settings.py)
# ---------------------------------------------------------------------------

def normalise_base(symbol: str) -> str:
    """
    Reduce any accepted spelling to the Binance-style base id ``ETHUSDT``:
    ``ETHUSDT``, ``ETHUSDT_SWAP_BINANCE``, ``ETHUSDT_SWAP_OKX``,
    ``ETH-USDT-SWAP``, ``ETHUSDT_SWAP_BINANCE.GLOBAL``.
    """
    s = symbol.strip().upper().split(".")[0]
    for suffix in SYMBOL_SUFFIX.values():
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    if "-" in s:
        parts = [p for p in s.split("-") if p and p != "SWAP"]
        s = "".join(parts)
    if not s:
        raise ValueError(f"cannot interpret symbol {symbol!r}")
    return s


def split_base_quote(base: str) -> tuple[str, str]:
    """``ETHUSDT`` -> ``("ETH", "USDT")``."""
    base = base.upper()
    for quote in _QUOTES:
        if base.endswith(quote) and len(base) > len(quote):
            return base[: -len(quote)], quote
    raise ValueError(f"cannot split {base!r} into base/quote (known quotes {_QUOTES})")


def vnpy_symbol(exchange: str, base: str) -> str:
    """The symbol stored in the DB / used live: ``ETHUSDT_SWAP_BINANCE`` or ``ETHUSDT_SWAP_OKX``."""
    if exchange not in SYMBOL_SUFFIX:
        raise ValueError(f"unknown exchange {exchange!r}; expected one of {EXCHANGES}")
    return normalise_base(base) + SYMBOL_SUFFIX[exchange]


def native_name(exchange: str, base: str) -> str:
    """The exchange-native id (``contract.name``): Binance ``ETHUSDT``, OKX ``ETH-USDT-SWAP``."""
    b = normalise_base(base)
    if exchange == "binance_linear":
        return b
    if exchange == "okx":
        coin, quote = split_base_quote(b)
        return f"{coin}-{quote}-SWAP"
    raise ValueError(f"unknown exchange {exchange!r}; expected one of {EXCHANGES}")


# ---------------------------------------------------------------------------
# Pure parsers (network-free; covered by tests/test_download_parse.py)
# ---------------------------------------------------------------------------

def ms_to_datetime(ms: int | float | str) -> datetime:
    """Epoch milliseconds -> tz-aware datetime in ``DB_TZ``."""
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).astimezone(DB_TZ)


def parse_binance_kline_row(row: Sequence[Any], symbol: str, interval: Interval,
                            gateway_name: str = "BINANCE_LINEAR") -> BarData:
    """
    One ``/fapi/v1/klines`` row -> ``BarData``.  Row layout (12 fields):
    ``[openTime, open, high, low, close, volume, closeTime, quoteVolume,
    trades, takerBuyBase, takerBuyQuote, ignore]``.  Field mapping is the
    same as ``vnpy_binance.linear_gateway.RestApi.query_history``.
    ``symbol`` is the vnpy symbol (``ETHUSDT_SWAP_BINANCE``).
    """
    bar = BarData(
        symbol=symbol,
        exchange=Exchange.GLOBAL,
        datetime=ms_to_datetime(row[0]),
        interval=interval,
        volume=float(row[5]),
        turnover=float(row[7]),
        open_price=float(row[1]),
        high_price=float(row[2]),
        low_price=float(row[3]),
        close_price=float(row[4]),
        gateway_name=gateway_name,
    )
    if len(row) >= 11:
        bar.extra = {
            "trade_count": int(row[8]),
            "active_volume": float(row[9]),
            "active_turnover": float(row[10]),
        }
    return bar


def parse_okx_candle_row(row: Sequence[Any], symbol: str, interval: Interval,
                         gateway_name: str = "OKX") -> BarData:
    """
    One ``/api/v5/market/history-candles`` row -> ``BarData``.  Row layout
    (9 strings): ``[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]``.
    As in ``vnpy_okx.okx_gateway.RestApi.query_history``: ``volume`` = vol
    (contracts for SWAP) and ``turnover`` = volCcy (base-coin volume).
    ``symbol`` is the vnpy symbol (``ETHUSDT_SWAP_OKX``).
    """
    ts, op, hp, lp, cp, volume, vol_ccy = row[0], row[1], row[2], row[3], row[4], row[5], row[6]
    return BarData(
        symbol=symbol,
        exchange=Exchange.GLOBAL,
        datetime=ms_to_datetime(ts),
        interval=interval,
        volume=float(volume),
        turnover=float(vol_ccy),
        open_price=float(op),
        high_price=float(hp),
        low_price=float(lp),
        close_price=float(cp),
        gateway_name=gateway_name,
    )


def okx_candle_closed(row: Sequence[Any]) -> bool:
    """OKX ``confirm`` field: ``"1"`` = closed candle, ``"0"`` = still forming."""
    return len(row) < 9 or str(row[8]) == "1"


def drop_unclosed_binance_rows(rows: Iterable[Sequence[Any]], now_ms: int | None = None) -> list[Sequence[Any]]:
    """
    Keep only klines whose ``closeTime`` (field 6) is already in the past.
    The live gateway unconditionally pops the last kline; using the close
    time is equivalent for a live download and does not drop a valid
    candle when the requested range ends in the past.
    """
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return [row for row in rows if int(row[6]) < now]


def parse_funding_rows(rows: Iterable[Mapping[str, Any]]) -> list[list[float]]:
    """
    Funding history rows (Binance ``/fapi/v1/fundingRate`` or OKX
    ``/api/v5/public/funding-rate-history``; both carry ``fundingTime`` and
    ``fundingRate``) -> ``[[fundingTime_ms, rate], ...]`` sorted ascending,
    de-duplicated by time.  Rows with an empty rate are skipped.
    """
    by_time: dict[int, float] = {}
    for row in rows:
        raw_rate = row.get("fundingRate")
        raw_time = row.get("fundingTime")
        if raw_rate in (None, "") or raw_time in (None, ""):
            continue
        by_time[int(raw_time)] = float(raw_rate)
    return [[ms, by_time[ms]] for ms in sorted(by_time)]


def _filter_map(filters: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(f.get("filterType")): f for f in filters if isinstance(f, Mapping)}


def parse_binance_filters(info: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    """
    ``/fapi/v1/exchangeInfo`` (the whole packet or its ``symbols`` list) ->
    ``{symbol: {tickSize, stepSize, minQty, minNotional}}`` keyed by the
    native symbol (``ETHUSDT``).  Entries lacking PRICE_FILTER or LOT_SIZE
    are skipped; ``minNotional`` comes from the MIN_NOTIONAL filter
    (``notional`` on USDT-M, ``minNotional`` on spot) and is omitted when
    the exchange does not publish it.
    """
    symbols: Iterable[Mapping[str, Any]]
    if isinstance(info, Mapping):
        symbols = info.get("symbols") or []
    else:
        symbols = info
    result: dict[str, dict[str, float]] = {}
    for d in symbols:
        name = d.get("symbol")
        if not name:
            continue
        fm = _filter_map(d.get("filters") or [])
        price = fm.get("PRICE_FILTER")
        lot = fm.get("LOT_SIZE")
        if not price or not lot:
            continue
        row: dict[str, float] = {
            "tickSize": float(price.get("tickSize") or 0.0),
            "stepSize": float(lot.get("stepSize") or lot.get("minQty") or 0.0),
            "minQty": float(lot.get("minQty") or 0.0),
        }
        notional = fm.get("MIN_NOTIONAL")
        if notional:
            raw = notional.get("notional", notional.get("minNotional"))
            if raw not in (None, ""):
                row["minNotional"] = float(raw)
        result[str(name)] = row
    return result


def parse_okx_instruments(packet: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    """
    ``/api/v5/public/instruments?instType=SWAP`` (the whole packet or its
    ``data`` list) -> ``{instId: {tickSize, stepSize, minQty, ctVal}}``
    keyed by the native instId (``ETH-USDT-SWAP``): ``tickSize`` = tickSz,
    ``stepSize`` = lotSz, ``minQty`` = minSz, ``ctVal`` = contract value in
    base coin (what the gateway uses as ``ContractData.size``).  OKX has no
    minimum-notional filter (the minimum is one contract), so ``minNotional``
    is not written and callers fall back to their table.
    """
    items: Iterable[Mapping[str, Any]]
    if isinstance(packet, Mapping):
        items = packet.get("data") or []
    else:
        items = packet
    result: dict[str, dict[str, float]] = {}
    for d in items:
        inst_id = d.get("instId")
        if not inst_id:
            continue
        result[str(inst_id)] = {
            "tickSize": float(d.get("tickSz") or 0.0),
            "stepSize": float(d.get("lotSz") or 0.0),
            "minQty": float(d.get("minSz") or 0.0),
            "ctVal": float(d.get("ctVal") or 1.0),
        }
    return result


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

def proxies_from_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """``CT_PROXY_HOST`` / ``CT_PROXY_PORT`` -> requests ``proxies`` dict (empty when unset)."""
    source: Mapping[str, str] = os.environ if env is None else env
    host = (source.get("CT_PROXY_HOST") or "").strip()
    raw_port = (source.get("CT_PROXY_PORT") or "0").strip() or "0"
    try:
        port = int(float(raw_port))
    except ValueError:
        port = 0
    if not host or port <= 0:
        return {}
    url = host if "://" in host else f"http://{host}:{port}"
    return {"http": url, "https": url}


def build_session(env: Mapping[str, str] | None = None) -> requests.Session:
    """A ``requests.Session`` with the proxy (if configured) and a UA header."""
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "crypto_trader-download/1.0"})
    proxies = proxies_from_env(env)
    if proxies:
        session.proxies.update(proxies)
        log.info("using proxy %s", proxies["https"])
    return session


class BinanceWeightGuard:
    """Sleeps when the ``X-MBX-USED-WEIGHT-1M`` header approaches the IP budget."""

    def __init__(self, soft_limit: int = BINANCE_WEIGHT_SOFT_LIMIT) -> None:
        self.soft_limit = soft_limit
        self.used: int = 0

    def update(self, headers: Mapping[str, str]) -> None:
        raw = headers.get("X-MBX-USED-WEIGHT-1M") or headers.get("x-mbx-used-weight-1m")
        if raw:
            try:
                self.used = int(raw)
            except ValueError:
                pass

    def wait_if_needed(self) -> float:
        """Return the seconds slept (0 when under the soft limit)."""
        if self.used < self.soft_limit:
            return 0.0
        pause = 61 - (time.time() % 60)
        log.warning("binance weight %d >= %d; sleeping %.0fs for the window to reset", self.used, self.soft_limit, pause)
        time.sleep(pause)
        self.used = 0
        return pause


def http_get(session: requests.Session, url: str, params: Mapping[str, Any],
             weight: BinanceWeightGuard | None = None, retries: int = MAX_RETRIES) -> requests.Response:
    """
    GET with retries: honours ``Retry-After`` on 429/418, backs off on 5xx
    and connection errors, raises on other non-2xx codes (451 = geo-block).
    """
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = session.get(url, params=dict(params), timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            last_exc = exc
            wait = min(2.0 ** attempt, 30.0)
            log.warning("%s: %s; retry in %.0fs", url, exc, wait)
            time.sleep(wait)
            continue
        if weight is not None:
            weight.update(resp.headers)
        if resp.status_code in (418, 429):
            wait = float(resp.headers.get("Retry-After") or 5)
            log.warning("%s: HTTP %d (rate limited); sleeping %.0fs", url, resp.status_code, wait)
            time.sleep(wait)
            continue
        if resp.status_code == 451:
            raise RuntimeError(
                f"{url}: HTTP 451 - endpoint geo-blocked from this network. "
                "Use a proxy (CT_PROXY_HOST/CT_PROXY_PORT) or binance_vision.py."
            )
        if 500 <= resp.status_code < 600:
            wait = min(2.0 ** attempt, 30.0)
            log.warning("%s: HTTP %d; retry in %.0fs", url, resp.status_code, wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"{url}: giving up after {retries} attempts ({last_exc})")


def _okx_data(resp: requests.Response, url: str) -> list[Any]:
    packet = resp.json()
    if str(packet.get("code")) != "0":
        raise RuntimeError(f"OKX error {packet.get('code')}: {packet.get('msg')} ({url})")
    data = packet.get("data") or []
    return list(data)


# ---------------------------------------------------------------------------
# Fetchers (network; parameterised by ``session`` so tests can stub them)
# ---------------------------------------------------------------------------

def fetch_binance_klines(session: requests.Session, name: str, interval: Interval,
                         start: datetime, end: datetime, host: str = BINANCE_HOST,
                         limit: int = BINANCE_KLINE_LIMIT, page_sleep: float = BINANCE_PAGE_SLEEP,
                         now_ms: int | None = None) -> list[Sequence[Any]]:
    """
    Paginate ``/fapi/v1/klines`` forward by ``startTime`` from ``start`` to
    ``end`` (inclusive open times) and return the closed rows, oldest first.
    """
    rows_all: list[Sequence[Any]] = []
    delta_ms = int(INTERVAL_DELTA[interval].total_seconds() * 1000)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    guard = BinanceWeightGuard()
    url = host + "/fapi/v1/klines"

    while start_ms <= end_ms:
        guard.wait_if_needed()
        params: dict[str, Any] = {
            "symbol": name, "interval": BINANCE_INTERVAL[interval],
            "startTime": start_ms, "endTime": end_ms, "limit": limit,
        }
        rows = http_get(session, url, params, weight=guard).json()
        if not rows:
            break
        rows_all.extend(rows)
        log.info("binance %s %s: +%d rows (%s .. %s) weight1m=%d", name, BINANCE_INTERVAL[interval], len(rows),
                 ms_to_datetime(rows[0][0]), ms_to_datetime(rows[-1][0]), guard.used)
        if len(rows) < limit:
            break
        start_ms = int(rows[-1][0]) + delta_ms
        time.sleep(page_sleep)

    return drop_unclosed_binance_rows(rows_all, now_ms)


def fetch_binance_funding(session: requests.Session, name: str, start: datetime, end: datetime,
                          host: str = BINANCE_HOST, limit: int = BINANCE_FUNDING_LIMIT,
                          page_sleep: float = BINANCE_PAGE_SLEEP) -> list[dict[str, Any]]:
    """Paginate ``/fapi/v1/fundingRate`` forward by ``startTime``; raw rows, oldest first."""
    rows_all: list[dict[str, Any]] = []
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    guard = BinanceWeightGuard()
    url = host + "/fapi/v1/fundingRate"

    while start_ms <= end_ms:
        guard.wait_if_needed()
        params: dict[str, Any] = {"symbol": name, "startTime": start_ms, "endTime": end_ms, "limit": limit}
        rows = http_get(session, url, params, weight=guard).json()
        if not rows:
            break
        rows_all.extend(rows)
        log.info("binance funding %s: +%d rows (.. %s)", name, len(rows), ms_to_datetime(rows[-1]["fundingTime"]))
        if len(rows) < limit:
            break
        start_ms = int(rows[-1]["fundingTime"]) + 1
        time.sleep(page_sleep)
    return rows_all


def fetch_binance_exchange_info(session: requests.Session, host: str = BINANCE_HOST) -> dict[str, Any]:
    """``/fapi/v1/exchangeInfo`` packet (all USDT-M symbols)."""
    resp = http_get(session, host + "/fapi/v1/exchangeInfo", {})
    packet: dict[str, Any] = resp.json()
    return packet


def fetch_okx_candles(session: requests.Session, inst_id: str, interval: Interval,
                      start: datetime, end: datetime, host: str = OKX_HOST,
                      limit: int = OKX_CANDLE_LIMIT, page_sleep: float = OKX_PAGE_SLEEP) -> list[Sequence[Any]]:
    """
    Paginate ``/api/v5/market/history-candles`` backwards with ``after``
    (rows with ts < after, newest first) until the oldest row is <= start.
    Returns closed candles (``confirm == "1"``) inside [start, end], oldest first.
    """
    buf: dict[int, Sequence[Any]] = {}
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    after = str(end_ms + 1)   # exclusive bound: include a candle opening exactly at ``end``
    url = host + "/api/v5/market/history-candles"

    while True:
        params: dict[str, Any] = {"instId": inst_id, "bar": OKX_INTERVAL[interval], "limit": str(limit), "after": after}
        data = _okx_data(http_get(session, url, params), url)
        if not data:
            break
        for row in data:
            if okx_candle_closed(row):
                buf[int(row[0])] = row
        oldest = int(data[-1][0])
        log.info("okx %s %s: +%d rows (%s .. %s)", inst_id, OKX_INTERVAL[interval], len(data),
                 ms_to_datetime(oldest), ms_to_datetime(data[0][0]))
        if oldest <= start_ms:
            break
        after = str(oldest)
        time.sleep(page_sleep)

    return [buf[k] for k in sorted(buf) if start_ms <= k <= end_ms]


def fetch_okx_funding(session: requests.Session, inst_id: str, start: datetime, end: datetime,
                      host: str = OKX_HOST, limit: int = OKX_FUNDING_LIMIT,
                      page_sleep: float = OKX_PAGE_SLEEP) -> list[dict[str, Any]]:
    """Paginate ``/api/v5/public/funding-rate-history`` backwards with ``after``; raw rows."""
    rows_all: list[dict[str, Any]] = []
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    after = str(end_ms + 1)
    url = host + "/api/v5/public/funding-rate-history"

    while True:
        params: dict[str, Any] = {"instId": inst_id, "limit": str(limit), "after": after}
        data = _okx_data(http_get(session, url, params), url)
        if not data:
            break
        rows_all.extend(data)
        oldest = min(int(row["fundingTime"]) for row in data)
        log.info("okx funding %s: +%d rows (%s ..)", inst_id, len(data), ms_to_datetime(oldest))
        if oldest <= start_ms or len(data) < limit:
            break
        after = str(oldest)
        time.sleep(page_sleep)
    return [row for row in rows_all if start_ms <= int(row["fundingTime"]) <= end_ms]


def fetch_okx_instruments(session: requests.Session, host: str = OKX_HOST, inst_type: str = "SWAP") -> list[dict[str, Any]]:
    """``/api/v5/public/instruments?instType=SWAP`` data list."""
    url = host + "/api/v5/public/instruments"
    return _okx_data(http_get(session, url, {"instType": inst_type}), url)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def clear_bar_cache() -> None:
    """``vnpy_ctastrategy.backtesting.load_bar_data`` is ``lru_cache``d; clear it after a save."""
    try:
        from vnpy_ctastrategy.backtesting import load_bar_data, load_tick_data
    except Exception:  # pragma: no cover - optional dependency
        return
    load_bar_data.cache_clear()
    load_tick_data.cache_clear()


def save_bars(bars: Sequence[BarData], chunk: int = SAVE_CHUNK) -> int:
    """
    Upsert bars into the vnpy database in chunks and clear the backtester's
    bar cache.  NOTE: ``save_bar_data`` mutates the BarData objects in
    place (naive datetime, enum values -> strings); do not reuse them.
    Returns the number of bars saved.
    """
    if not bars:
        return 0
    database = get_database()
    total = 0
    for i in range(0, len(bars), chunk):
        part = list(bars[i:i + chunk])
        database.save_bar_data(part)
        total += len(part)
        log.info("saved %d / %d bars", total, len(bars))
    clear_bar_cache()
    return total


def write_json_atomic(path: Path, payload: Any) -> Path:
    """Write JSON via a temp file + rename so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=None, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)
    return path


def funding_file(base: str) -> Path:
    """``.vntrader/funding_<BASE>.json`` (BASE = ``ETHUSDT``) in vnpy's current trader dir."""
    return Path(get_file_path(f"funding_{normalise_base(base)}.json"))


def filters_file() -> Path:
    """``.vntrader/exchange_filters.json`` in vnpy's current trader dir."""
    return Path(get_file_path("exchange_filters.json"))


def save_funding(base: str, rows: Sequence[Sequence[float]], path: Path | None = None) -> Path:
    """
    Merge ``[[ms, rate], ...]`` into ``funding_<BASE>.json`` (existing
    stamps are kept so partial downloads accumulate) and return the path.
    """
    target = path or funding_file(base)
    merged: dict[int, float] = {}
    if target.exists():
        try:
            for ms, rate in json.loads(target.read_text(encoding="utf-8")):
                merged[int(ms)] = float(rate)
        except (OSError, ValueError, TypeError):
            log.warning("could not read existing %s; overwriting", target)
            merged = {}
    for ms, rate in rows:
        merged[int(ms)] = float(rate)
    payload = [[ms, merged[ms]] for ms in sorted(merged)]
    write_json_atomic(target, payload)
    log.info("wrote %d funding rows to %s", len(payload), target)
    return target


def save_filters(filters: Mapping[str, Mapping[str, float]], path: Path | None = None) -> Path:
    """
    Merge ``{name: {...}}`` into ``exchange_filters.json`` (rows for other
    names / the other exchange are preserved) and return the path.
    """
    target = path or filters_file()
    merged: dict[str, dict[str, float]] = {}
    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                merged = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            log.warning("could not read existing %s; overwriting", target)
    for name, row in filters.items():
        merged[str(name)] = {str(k): float(v) for k, v in row.items()}
    write_json_atomic(target, merged)
    log.info("wrote %d filter rows to %s", len(merged), target)
    return target


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def download_bars(exchange: str, base: str, interval: Interval, start: datetime, end: datetime,
                  session: requests.Session, save: bool = True) -> list[BarData]:
    """
    Download klines for one perpetual swap and (optionally) save them.
    Returns the parsed BarData list (mutated by the save when ``save``).
    """
    symbol = vnpy_symbol(exchange, base)
    name = native_name(exchange, base)
    gateway = GATEWAY_NAMES[exchange]
    if exchange == "binance_linear":
        rows = fetch_binance_klines(session, name, interval, start, end)
        bars = [parse_binance_kline_row(r, symbol, interval, gateway) for r in rows]
    else:
        rows = fetch_okx_candles(session, name, interval, start, end)
        bars = [parse_okx_candle_row(r, symbol, interval, gateway) for r in rows]
    span = f" [{bars[0].datetime} .. {bars[-1].datetime}]" if bars else ""
    log.info("%s.GLOBAL %s: %d bars%s", symbol, interval.value, len(bars), span)
    if save:
        save_bars(bars)
    return bars


def download_funding(exchange: str, base: str, start: datetime, end: datetime,
                     session: requests.Session, save: bool = True) -> list[list[float]]:
    """Download funding-rate history and (optionally) merge it into ``funding_<BASE>.json``."""
    name = native_name(exchange, base)
    if exchange == "binance_linear":
        raw = fetch_binance_funding(session, name, start, end)
    else:
        raw = fetch_okx_funding(session, name, start, end)
    rows = parse_funding_rows(raw)
    log.info("%s funding %s: %d stamps", exchange, name, len(rows))
    if save:
        save_funding(base, rows)
    return rows


def download_filters(exchange: str, session: requests.Session, save: bool = True) -> dict[str, dict[str, float]]:
    """Download exchange filters / instrument specs and (optionally) merge them into ``exchange_filters.json``."""
    if exchange == "binance_linear":
        filters = parse_binance_filters(fetch_binance_exchange_info(session))
    else:
        filters = parse_okx_instruments(fetch_okx_instruments(session))
    log.info("%s: %d instrument filter rows", exchange, len(filters))
    if save:
        save_filters(filters)
    return filters


def parse_date(text: str, end_of_day: bool = False) -> datetime:
    """``YYYY-MM-DD`` (UTC) -> tz-aware datetime; ``end_of_day`` gives 23:59:59.999."""
    dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
    if end_of_day:
        dt = dt + timedelta(days=1) - timedelta(milliseconds=1)
    return dt


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download klines / funding / exchange filters from public REST into the vnpy database.",
    )
    p.add_argument("--exchange", choices=list(EXCHANGES), required=True, help="binance_linear | okx")
    p.add_argument("--symbol", default="ETHUSDT", help="ETHUSDT (also accepts ETH-USDT-SWAP / ETHUSDT_SWAP_BINANCE)")
    p.add_argument("--interval", choices=["1m", "1h", "d"], default="1m", help="vnpy Interval value (default 1m)")
    p.add_argument("--start", default=None, help="YYYY-MM-DD (UTC); required for bars and funding")
    p.add_argument("--end", default=None, help="YYYY-MM-DD (UTC, inclusive); default now")
    p.add_argument("--funding", action="store_true", help="also download funding-rate history")
    p.add_argument("--filters", action="store_true", help="also download exchange filters (exchangeInfo / instruments)")
    p.add_argument("--skip-bars", action="store_true", help="do not download klines (use with --funding / --filters)")
    p.add_argument("--no-save", action="store_true", help="fetch and report only; write nothing")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    want_bars = not args.skip_bars
    if (want_bars or args.funding) and not args.start:
        build_parser().error("--start is required for bars and funding")

    session = build_session()
    save = not args.no_save
    exchange: str = args.exchange
    base = normalise_base(args.symbol)
    log.info("trader dir: %s", Path(get_file_path("database.db")).parent)

    if args.filters:
        download_filters(exchange, session, save=save)

    if want_bars or args.funding:
        start = parse_date(args.start)
        end = parse_date(args.end, end_of_day=True) if args.end else datetime.now(UTC)
        if start >= end:
            raise SystemExit("--start must be before --end")
        if want_bars:
            download_bars(exchange, base, Interval(args.interval), start, end, session, save=save)
        if args.funding:
            download_funding(exchange, base, start, end, session, save=save)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
