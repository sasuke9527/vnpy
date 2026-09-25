"""
Tests for ``download_data.py``: pure parsers on literal exchange sample
rows, pagination logic against a stubbed ``requests.Session`` (no network),
symbol naming, and the save path into the hermetic sqlite DB set up by
``tests/conftest.py``.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import download_data as dd
from vnpy.trader.constant import Exchange, Interval

UTC = timezone.utc
T0 = 1735689600000  # 2025-01-01 00:00:00 UTC
MIN_MS = 60_000

# ---------------------------------------------------------------------------
# Literal sample rows (shapes copied from the exchange API docs)
# ---------------------------------------------------------------------------

BINANCE_KLINE = [
    T0, "3337.71", "3339.00", "3335.20", "3338.50", "123.456",
    T0 + MIN_MS - 1, "412345.67", 1234, "60.1", "200600.5", "0",
]
OKX_CANDLE = ["1735689600000", "3337.7", "3339", "3335.2", "3338.5", "1500", "150", "500700.5", "1"]
OKX_CANDLE_OPEN = ["1735689660000", "3338.5", "3340", "3338", "3339.9", "10", "1", "3339.9", "0"]

BINANCE_FUNDING = [
    {"symbol": "ETHUSDT", "fundingTime": T0 + 8 * 3600_000, "fundingRate": "-0.00012000", "markPrice": "3350.1"},
    {"symbol": "ETHUSDT", "fundingTime": T0, "fundingRate": "0.00010000", "markPrice": "3337.9"},
    {"symbol": "ETHUSDT", "fundingTime": T0, "fundingRate": "0.00010000", "markPrice": "3337.9"},  # duplicate
    {"symbol": "ETHUSDT", "fundingTime": T0 + 16 * 3600_000, "fundingRate": "", "markPrice": "3360"},  # empty rate
]
OKX_FUNDING = [
    {"instId": "ETH-USDT-SWAP", "fundingRate": "0.0000875", "fundingTime": "1735718400000",
     "realizedRate": "0.00008", "method": "current_period"},
    {"instId": "ETH-USDT-SWAP", "fundingRate": "-0.0001", "fundingTime": "1735689600000",
     "realizedRate": "-0.0001", "method": "current_period"},
]

BINANCE_EXCHANGE_INFO: dict[str, Any] = {
    "timezone": "UTC",
    "symbols": [
        {
            "symbol": "ETHUSDT", "contractType": "PERPETUAL", "status": "TRADING",
            "filters": [
                {"filterType": "PRICE_FILTER", "minPrice": "39.86", "maxPrice": "306177", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "maxQty": "10000", "minQty": "0.001", "stepSize": "0.001"},
                {"filterType": "MARKET_LOT_SIZE", "maxQty": "2000", "minQty": "0.001", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "20"},
            ],
        },
        {
            "symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING",
            "filters": [
                {"filterType": "PRICE_FILTER", "minPrice": "556.8", "maxPrice": "4529764", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "maxQty": "1000", "minQty": "0.001", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "100"},
            ],
        },
        {
            "symbol": "XRPUSDT", "contractType": "PERPETUAL", "status": "TRADING",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                {"filterType": "LOT_SIZE", "minQty": "0.1", "stepSize": "0.1"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        },
        {"symbol": "BROKEN", "contractType": "PERPETUAL", "filters": []},   # no filters -> skipped
    ],
}
OKX_INSTRUMENTS: dict[str, Any] = {
    "code": "0", "msg": "",
    "data": [
        {"instType": "SWAP", "instId": "ETH-USDT-SWAP", "ctVal": "0.1", "ctValCcy": "ETH", "tickSz": "0.01",
         "lotSz": "1", "minSz": "1", "state": "live", "ctType": "linear"},
        {"instType": "SWAP", "instId": "XRP-USDT-SWAP", "ctVal": "100", "ctValCcy": "XRP", "tickSz": "0.0001",
         "lotSz": "1", "minSz": "1", "state": "live", "ctType": "linear"},
    ],
}


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------

def test_parse_binance_kline_row() -> None:
    bar = dd.parse_binance_kline_row(BINANCE_KLINE, "ETHUSDT_SWAP_BINANCE", Interval.MINUTE)
    assert bar.symbol == "ETHUSDT_SWAP_BINANCE"
    assert bar.exchange is Exchange.GLOBAL
    assert bar.vt_symbol == "ETHUSDT_SWAP_BINANCE.GLOBAL"
    assert bar.interval is Interval.MINUTE
    assert bar.datetime.tzinfo is not None
    assert bar.datetime == datetime(2025, 1, 1, tzinfo=UTC)
    assert bar.open_price == 3337.71
    assert bar.high_price == 3339.0
    assert bar.low_price == 3335.2
    assert bar.close_price == 3338.5
    assert bar.volume == 123.456
    assert bar.turnover == 412345.67
    assert bar.gateway_name == "BINANCE_LINEAR"
    assert bar.extra == {"trade_count": 1234, "active_volume": 60.1, "active_turnover": 200600.5}


def test_parse_okx_candle_row() -> None:
    bar = dd.parse_okx_candle_row(OKX_CANDLE, "ETHUSDT_SWAP_OKX", Interval.HOUR)
    assert bar.symbol == "ETHUSDT_SWAP_OKX"
    assert bar.vt_symbol == "ETHUSDT_SWAP_OKX.GLOBAL"
    assert bar.exchange is Exchange.GLOBAL
    assert bar.interval is Interval.HOUR
    assert bar.datetime == datetime(2025, 1, 1, tzinfo=UTC)
    assert bar.datetime.tzinfo is not None
    assert (bar.open_price, bar.high_price, bar.low_price, bar.close_price) == (3337.7, 3339.0, 3335.2, 3338.5)
    assert bar.volume == 1500.0        # contracts
    assert bar.turnover == 150.0       # volCcy, as vnpy_okx maps it
    assert bar.gateway_name == "OKX"
    assert dd.okx_candle_closed(OKX_CANDLE)
    assert not dd.okx_candle_closed(OKX_CANDLE_OPEN)


def test_datetime_is_db_tz_aware() -> None:
    from vnpy.trader.database import DB_TZ

    dt = dd.ms_to_datetime("1735689600000")
    assert dt.tzinfo is not None
    assert dt.utcoffset() == datetime(2025, 1, 1, tzinfo=UTC).astimezone(DB_TZ).utcoffset()
    assert dt == datetime(2025, 1, 1, tzinfo=UTC)


def test_drop_unclosed_binance_rows() -> None:
    rows = [BINANCE_KLINE, [T0 + MIN_MS, "1", "1", "1", "1", "0", T0 + 2 * MIN_MS - 1, "0", 0, "0", "0", "0"]]
    kept = dd.drop_unclosed_binance_rows(rows, now_ms=T0 + MIN_MS + 30_000)   # second candle still open
    assert kept == [BINANCE_KLINE]
    assert dd.drop_unclosed_binance_rows(rows, now_ms=T0 + 10 * MIN_MS) == rows


def test_parse_funding_rows_binance_sorted_deduped() -> None:
    out = dd.parse_funding_rows(BINANCE_FUNDING)
    assert out == [[T0, 0.0001], [T0 + 8 * 3600_000, -0.00012]]
    assert all(isinstance(ms, int) and isinstance(r, float) for ms, r in out)
    assert json.loads(json.dumps(out)) == out


def test_parse_funding_rows_okx() -> None:
    out = dd.parse_funding_rows(OKX_FUNDING)
    assert out == [[1735689600000, -0.0001], [1735718400000, 0.0000875]]


def test_parse_binance_filters() -> None:
    f = dd.parse_binance_filters(BINANCE_EXCHANGE_INFO)
    assert set(f) == {"ETHUSDT", "BTCUSDT", "XRPUSDT"}
    assert f["ETHUSDT"] == {"tickSize": 0.01, "stepSize": 0.001, "minQty": 0.001, "minNotional": 20.0}
    assert f["BTCUSDT"] == {"tickSize": 0.1, "stepSize": 0.001, "minQty": 0.001, "minNotional": 100.0}
    assert f["XRPUSDT"]["minNotional"] == 5.0
    # the symbols list alone is accepted too
    assert dd.parse_binance_filters(BINANCE_EXCHANGE_INFO["symbols"]) == f


def test_parse_okx_instruments() -> None:
    f = dd.parse_okx_instruments(OKX_INSTRUMENTS)
    assert f["ETH-USDT-SWAP"] == {"tickSize": 0.01, "stepSize": 1.0, "minQty": 1.0, "ctVal": 0.1}
    assert f["XRP-USDT-SWAP"]["ctVal"] == 100.0
    assert "minNotional" not in f["ETH-USDT-SWAP"]
    assert dd.parse_okx_instruments(OKX_INSTRUMENTS["data"]) == f


# ---------------------------------------------------------------------------
# Symbol naming
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["ETHUSDT", "ethusdt", "ETH-USDT-SWAP", "ETHUSDT_SWAP_BINANCE",
                                 "ETHUSDT_SWAP_OKX", "ETHUSDT_SWAP_BINANCE.GLOBAL"])
def test_normalise_base(raw: str) -> None:
    assert dd.normalise_base(raw) == "ETHUSDT"


def test_symbol_scheme_matches_gateways() -> None:
    assert dd.vnpy_symbol("binance_linear", "ETHUSDT") == "ETHUSDT_SWAP_BINANCE"
    assert dd.vnpy_symbol("okx", "ETH-USDT-SWAP") == "ETHUSDT_SWAP_OKX"
    assert dd.native_name("binance_linear", "ETHUSDT") == "ETHUSDT"
    assert dd.native_name("okx", "ETHUSDT") == "ETH-USDT-SWAP"
    with pytest.raises(ValueError):
        dd.vnpy_symbol("bybit", "ETHUSDT")


def test_proxies_from_env() -> None:
    assert dd.proxies_from_env({"CT_PROXY_HOST": "", "CT_PROXY_PORT": "0"}) == {}
    assert dd.proxies_from_env({"CT_PROXY_HOST": "127.0.0.1", "CT_PROXY_PORT": "7890"}) == {
        "http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890",
    }
    assert dd.proxies_from_env({"CT_PROXY_HOST": "127.0.0.1"}) == {}


# ---------------------------------------------------------------------------
# Pagination against a stubbed session (no network)
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload: Any, headers: Mapping[str, str] | None = None, status: int = 200) -> None:
        self._payload = payload
        self.headers: dict[str, str] = dict(headers or {})
        self.status_code = status

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _kline_row(ms: int) -> list[Any]:
    return [ms, "1", "2", "0.5", "1.5", "10", ms + MIN_MS - 1, "15", 3, "5", "7.5", "0"]


class _BinanceSession:
    """Serves 1m klines for [T0, T0 + n_bars) in pages of ``limit``."""

    def __init__(self, n_bars: int) -> None:
        self.n_bars = n_bars
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any], timeout: float) -> _Resp:
        self.calls.append(dict(params))
        if url.endswith("/fapi/v1/klines"):
            limit = int(params["limit"])
            rows = [_kline_row(ms) for ms in range(int(params["startTime"]), T0 + self.n_bars * MIN_MS, MIN_MS)
                    if ms <= int(params["endTime"])][:limit]
            return _Resp(rows, {"X-MBX-USED-WEIGHT-1M": "10"})
        if url.endswith("/fapi/v1/fundingRate"):
            limit = int(params["limit"])
            stamps = [T0 + i * 8 * 3600_000 for i in range(5)]
            frows = [{"symbol": params["symbol"], "fundingTime": s, "fundingRate": "0.0001"}
                     for s in stamps if int(params["startTime"]) <= s <= int(params["endTime"])][:limit]
            return _Resp(frows)
        raise AssertionError(url)


def test_fetch_binance_klines_paginates_and_drops_open_candle() -> None:
    n = 3500
    session = _BinanceSession(n)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime.fromtimestamp((T0 + (n - 1) * MIN_MS) / 1000, tz=UTC)
    rows = dd.fetch_binance_klines(session, "ETHUSDT", Interval.MINUTE, start, end,  # type: ignore[arg-type]
                                   page_sleep=0.0, now_ms=T0 + (n - 1) * MIN_MS + 30_000)
    assert len(session.calls) == 3
    assert session.calls[0] == {"symbol": "ETHUSDT", "interval": "1m", "startTime": T0,
                                "endTime": T0 + (n - 1) * MIN_MS, "limit": 1500}
    assert session.calls[1]["startTime"] == T0 + 1500 * MIN_MS
    assert len(rows) == n - 1                     # last candle still open -> dropped
    assert rows[0][0] == T0 and rows[-1][0] == T0 + (n - 2) * MIN_MS


def test_fetch_binance_funding_paginates() -> None:
    session = _BinanceSession(0)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime(2025, 1, 3, tzinfo=UTC)
    raw = dd.fetch_binance_funding(session, "ETHUSDT", start, end, limit=2, page_sleep=0.0)  # type: ignore[arg-type]
    assert len(session.calls) == 3
    assert session.calls[1]["startTime"] == T0 + 8 * 3600_000 + 1
    assert dd.parse_funding_rows(raw) == [[T0 + i * 8 * 3600_000, 0.0001] for i in range(5)]


class _OkxSession:
    """Serves 1m candles for [T0, T0 + n_bars), newest first, ``after`` exclusive; last one unconfirmed."""

    def __init__(self, n_bars: int) -> None:
        self.n_bars = n_bars
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any], timeout: float) -> _Resp:
        self.calls.append(dict(params))
        assert url.endswith("/api/v5/market/history-candles")
        after = int(params["after"])
        limit = int(params["limit"])
        newest = T0 + (self.n_bars - 1) * MIN_MS
        data: list[list[str]] = []
        ms = min(after - 1, newest)
        ms -= ms % MIN_MS
        while ms >= T0 and len(data) < limit:
            confirm = "0" if ms == newest else "1"
            data.append([str(ms), "1", "2", "0.5", "1.5", "100", "10", "15", confirm])
            ms -= MIN_MS
        return _Resp({"code": "0", "msg": "", "data": data})


def test_fetch_okx_candles_pages_backwards_and_keeps_closed_only() -> None:
    n = 250
    session = _OkxSession(n)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    end = datetime.fromtimestamp((T0 + (n - 1) * MIN_MS) / 1000, tz=UTC)
    rows = dd.fetch_okx_candles(session, "ETH-USDT-SWAP", Interval.MINUTE, start, end, page_sleep=0.0)  # type: ignore[arg-type]
    assert session.calls[0] == {"instId": "ETH-USDT-SWAP", "bar": "1m", "limit": "100",
                                "after": str(T0 + (n - 1) * MIN_MS + 1)}
    assert len(session.calls) == 3
    assert [int(c["after"]) for c in session.calls[1:]] == [T0 + 150 * MIN_MS, T0 + 50 * MIN_MS]
    assert len(rows) == n - 1                                  # unconfirmed newest candle dropped
    assert [int(r[0]) for r in rows] == [T0 + i * MIN_MS for i in range(n - 1)]   # ascending


# ---------------------------------------------------------------------------
# Persistence into the hermetic test DB / trader dir
# ---------------------------------------------------------------------------

def test_save_bars_roundtrip(db: Any, trader_dir: Path) -> None:
    rows = [_kline_row(T0 + i * MIN_MS) for i in range(120)]
    bars = [dd.parse_binance_kline_row(r, "DLTEST_SWAP_BINANCE", Interval.MINUTE) for r in rows]
    assert dd.save_bars(bars, chunk=50) == 120
    loaded = db.load_bar_data("DLTEST_SWAP_BINANCE", Exchange.GLOBAL, Interval.MINUTE,
                              datetime(2025, 1, 1), datetime(2025, 1, 2))
    assert len(loaded) == 120
    assert loaded[0].datetime == datetime(2025, 1, 1, tzinfo=UTC)
    assert loaded[-1].datetime == datetime(2025, 1, 1, 1, 59, tzinfo=UTC)
    assert loaded[0].vt_symbol == "DLTEST_SWAP_BINANCE.GLOBAL"
    # idempotent upsert
    bars2 = [dd.parse_binance_kline_row(r, "DLTEST_SWAP_BINANCE", Interval.MINUTE) for r in rows]
    dd.save_bars(bars2)
    assert len(db.load_bar_data("DLTEST_SWAP_BINANCE", Exchange.GLOBAL, Interval.MINUTE,
                                datetime(2025, 1, 1), datetime(2025, 1, 2))) == 120


def test_save_funding_and_filters_merge(trader_dir: Path) -> None:
    fpath = dd.funding_file("ETHUSDT")
    assert fpath == trader_dir / "funding_ETHUSDT.json"
    dd.save_funding("ETHUSDT", [[T0, 0.0001]])
    dd.save_funding("ETHUSDT", [[T0 + 8 * 3600_000, -0.0002], [T0, 0.0003]])   # later download wins on collision
    assert json.loads(fpath.read_text()) == [[T0, 0.0003], [T0 + 8 * 3600_000, -0.0002]]

    xpath = dd.filters_file()
    assert xpath == trader_dir / "exchange_filters.json"
    dd.save_filters(dd.parse_binance_filters(BINANCE_EXCHANGE_INFO))
    dd.save_filters(dd.parse_okx_instruments(OKX_INSTRUMENTS))
    saved = json.loads(xpath.read_text())
    assert saved["ETHUSDT"]["minNotional"] == 20.0          # binance rows survive the okx merge
    assert saved["ETH-USDT-SWAP"]["ctVal"] == 0.1

    # settings.load_exchange_filters reads the same shape
    import settings

    filters = settings.load_exchange_filters(xpath)
    assert settings.min_notional_for("ETHUSDT", filters) == 20.0
    assert settings.min_notional_for("BTCUSDT", filters) == 100.0


def test_cli_help_and_parse_date() -> None:
    with pytest.raises(SystemExit) as exc:
        dd.main(["--help"])
    assert exc.value.code == 0
    assert dd.parse_date("2025-01-01") == datetime(2025, 1, 1, tzinfo=UTC)
    assert dd.parse_date("2025-01-01", end_of_day=True) == datetime(2025, 1, 1, 23, 59, 59, 999000, tzinfo=UTC)
