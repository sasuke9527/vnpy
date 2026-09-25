"""
Restart / recovery test (SPEC sections 4.8 and 8.4, ``tests/test_recovery.py``).

``DonchianTrendH1`` is driven against a minimal fake *live* ``CtaEngine``
(engine type LIVE, in-memory order book, scripted exchange positions).  The
persisted variables of a previous process (``pos``, ``entry_price``,
``stop_price``, stale order ids) are restored the way ``CtaEngine
._init_strategy`` does it - after ``on_init`` - and the first tick (with a
kline-stream bar in ``tick.extra``) is fed.  The expected outcome of every
scenario is computed with the pure ``risk.reconcile`` function and compared
with the strategy state; a protective stop (``stop=True`` order) must be
re-armed whenever the strategy ends up with a position.

Scenarios: in sync (adopt saved stop), exchange flat (clear), exchange holds
a different size (adopt), two strategies on the symbol with a mismatch
(DESYNC halt, no orders), and a restart with a missing ``stop_price``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from vnpy.trader.constant import Direction, Exchange, Interval, Offset
from vnpy.trader.object import AccountData, BarData, ContractData, PositionData, TickData
from vnpy_ctastrategy.base import STOPORDER_PREFIX, EngineType, StopOrder, StopOrderStatus

from risk import STATE_FILENAME, reconcile
from strategies.donchian_trend_h1 import DonchianTrendH1
from synth_data import generate_bars

SYMBOL = "ETHUSDT_SWAP_BINANCE"
VT_SYMBOL = f"{SYMBOL}.GLOBAL"
GATEWAY = "BINANCE_LINEAR"
STEP = 0.001
WARMUP_DAYS = 8
WARMUP_START = datetime(2026, 2, 1, tzinfo=timezone.utc)
FIRST_TICK_DT = WARMUP_START + timedelta(days=WARMUP_DAYS)


# ---------------------------------------------------------------------------
# fake live engine
# ---------------------------------------------------------------------------

class FakeMainEngine:
    """Contract, wallet and scripted positions of the exchange."""

    def __init__(self) -> None:
        self.contract = ContractData(symbol=SYMBOL, exchange=Exchange.GLOBAL, name="ETHUSDT", product=None,  # type: ignore[arg-type]
                                     size=1, pricetick=0.01, min_volume=STEP, gateway_name=GATEWAY)
        self.balance = 50.0
        self.positions: list[PositionData] = []
        self.notifications: list[tuple[str, str]] = []

    def set_position(self, volume: float, price: float) -> None:
        """Scripted NET position (signed volume) as the Binance gateway reports it."""
        self.positions = []
        if volume:
            self.positions.append(PositionData(symbol=SYMBOL, exchange=Exchange.GLOBAL, direction=Direction.NET,
                                               volume=volume, price=price, pnl=0.0, gateway_name=GATEWAY))

    def get_contract(self, vt_symbol: str) -> ContractData | None:
        return self.contract if vt_symbol == VT_SYMBOL else None

    def get_account(self, vt_accountid: str) -> AccountData | None:
        if vt_accountid != f"{GATEWAY}.USDT":
            return None
        return AccountData(accountid="USDT", balance=self.balance, frozen=0.0, gateway_name=GATEWAY)

    def get_all_positions(self) -> list[PositionData]:
        return list(self.positions)

    def send_notification(self, content: str, subject: str) -> None:
        self.notifications.append((subject, content))


class FakeCtaEngine:
    """
    The subset of ``vnpy_ctastrategy.engine.CtaEngine`` the strategy touches,
    with an in-memory order book: stop orders are local (``STOP.n``), limit
    orders get ``FAKE.n`` ids and stay active until cancelled.
    """

    def __init__(self, warmup: list[BarData], n_strategies: int = 1) -> None:
        self.engine_type = EngineType.LIVE
        self.main_engine = FakeMainEngine()
        self.warmup = warmup
        self.logs: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.stop_orders: dict[str, StopOrder] = {}
        self.strategy_orderid_map: dict[str, set[str]] = {}
        self.symbol_strategy_map: dict[str, list[Any]] = {VT_SYMBOL: [object() for _ in range(n_strategies)]}
        self.synced = 0
        self._n = 0

    # -- engine facts
    def get_engine_type(self) -> EngineType:
        return self.engine_type

    def get_pricetick(self, strategy: Any) -> float:
        return self.main_engine.contract.pricetick

    def get_size(self, strategy: Any) -> float:
        return self.main_engine.contract.size

    def write_log(self, msg: str, strategy: Any = None) -> None:
        self.logs.append(msg)

    def sync_strategy_data(self, strategy: Any) -> None:
        self.synced += 1

    def load_bar(self, vt_symbol: str, days: int, interval: Interval, callback: Any, use_database: bool) -> list[BarData]:
        return list(self.warmup)

    # -- orders
    def send_order(self, strategy: Any, direction: Direction, offset: Offset, price: float, volume: float,
                   stop: bool, lock: bool, net: bool) -> list[str]:
        self._n += 1
        oid = f"{STOPORDER_PREFIX}.{self._n}" if stop else f"FAKE.{self._n}"
        self.sent.append(dict(id=oid, direction=direction, offset=offset, price=price, volume=volume, stop=stop))
        ids = self.strategy_orderid_map.setdefault(strategy.strategy_name, set())
        ids.add(oid)
        if stop:
            so = StopOrder(vt_symbol=VT_SYMBOL, direction=direction, offset=offset, price=price, volume=volume,
                           stop_orderid=oid, strategy_name=strategy.strategy_name, datetime=datetime.now(timezone.utc))
            self.stop_orders[oid] = so
            strategy.on_stop_order(so)
        return [oid]

    def cancel_order(self, strategy: Any, vt_orderid: str) -> None:
        self.cancelled.append(vt_orderid)
        so = self.stop_orders.pop(vt_orderid, None)
        self.strategy_orderid_map.get(strategy.strategy_name, set()).discard(vt_orderid)
        if so is not None:
            so.status = StopOrderStatus.CANCELLED
            strategy.on_stop_order(so)

    def cancel_all(self, strategy: Any) -> None:
        for oid in list(self.strategy_orderid_map.get(strategy.strategy_name, set())):
            self.cancel_order(strategy, oid)

    @property
    def stop_sends(self) -> list[dict[str, Any]]:
        return [o for o in self.sent if o["stop"]]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def warmup_bars() -> list[BarData]:
    """8 days of 1m bars ending right before the first tick (seed 7)."""
    return generate_bars(SYMBOL, Exchange.GLOBAL, Interval.MINUTE, WARMUP_START, WARMUP_DAYS * 24 * 60, seed=7)


@pytest.fixture(autouse=True)
def clean_shared_state(trader_dir: Path) -> None:
    """Every scenario starts from an empty risk_state.json / flag files."""
    for name in (STATE_FILENAME, "KILL", "PAUSE", "RESUME"):
        try:
            (trader_dir / name).unlink()
        except FileNotFoundError:
            pass


def make_tick(last: float, dt: datetime = FIRST_TICK_DT) -> TickData:
    tick = TickData(symbol=SYMBOL, exchange=Exchange.GLOBAL, datetime=dt, last_price=last, bid_price_1=last - 0.01,
                    ask_price_1=last + 0.01, gateway_name=GATEWAY)
    tick.extra = {"bar": BarData(symbol="ETHUSDT", exchange=Exchange.GLOBAL, datetime=dt, interval=Interval.MINUTE,
                                 open_price=last, high_price=last + 1, low_price=last - 1, close_price=last,
                                 gateway_name=GATEWAY)}
    return tick


def restart(engine: FakeCtaEngine, persisted: dict[str, Any]) -> DonchianTrendH1:
    """
    Mirror ``CtaEngine._init_strategy`` + ``start_strategy``: construct,
    ``on_init`` (warmup, trading False), restore persisted variables, then
    ``on_start`` and ``trading = True``.  Returns the started strategy.
    """
    s = DonchianTrendH1(engine, "s1_eth", VT_SYMBOL, {"risk_dial": "normal", "capital": 50.0})
    s.on_init()
    s.inited = True
    for name, value in persisted.items():
        assert name in s.variables, f"{name} is not a persisted variable"
        setattr(s, name, value)
    s.on_start()
    s.trading = True
    return s


def stale_state(pos: float, entry: float, stop: float) -> dict[str, Any]:
    return dict(pos=pos, entry_price=entry, stop_price=stop, highest_since_entry=max(entry, stop),
                lowest_since_entry=min(entry, stop), entry_bar_ts=FIRST_TICK_DT.timestamp() - 7200.0,
                bars_in_trade=2, equity=50.0, entry_orderid="STOP.98", stop_orderid="STOP.99",
                exit_orderid="BINANCE_LINEAR.12345")


def expected(engine: FakeCtaEngine, local_pos: float, last: float, saved_hi: float, saved_lo: float) -> Any:
    """The outcome matrix from the pure function, with the same inputs the guard uses."""
    net = 0.0
    price = 0.0
    for p in engine.main_engine.get_all_positions():
        net += p.volume if p.direction != Direction.SHORT else -abs(p.volume)
        price = p.price
    n = len(engine.symbol_strategy_map[VT_SYMBOL])
    return reconcile(net, local_pos, STEP, n, price, last, saved_hi, saved_lo)


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_warmup_leaves_trade_state_untouched(warmup_bars: list[BarData]) -> None:
    engine = FakeCtaEngine(warmup_bars)
    s = restart(engine, stale_state(0.016, 3000.0, 2940.0))
    assert s.am.inited and s.atr > 0, "warmup should have filled the 1h ArrayManager"
    assert not engine.sent, "no order may be sent before the first tick"
    assert s.entry_orderid == "" and s.stop_orderid == "" and s.exit_orderid == "", "stale ids must be dropped on start"
    assert s.pos == 0.016 and s.entry_price == 3000.0 and s.stop_price == 2940.0


def test_in_sync_rearms_saved_stop(warmup_bars: list[BarData]) -> None:
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.set_position(0.016, 3000.0)
    s = restart(engine, stale_state(0.016, 3000.0, 2940.0))
    last = 3010.0
    exp = expected(engine, 0.016, last, s.highest_since_entry, s.lowest_since_entry)
    assert exp.outcome == "ok"

    s.on_tick(make_tick(last))
    assert s.pos == 0.016 and s.entry_price == 3000.0
    assert s.stop_price == 2940.0, "a saved stop price is kept, never lowered or recomputed"
    stops = engine.stop_sends
    assert len(stops) == 1, f"exactly one protective stop expected, got {stops}"
    so = stops[0]
    assert so["direction"] == Direction.SHORT and so["offset"] == Offset.CLOSE
    assert so["price"] == 2940.0 and so["volume"] == 0.016
    assert s.stop_orderid == so["id"] and so["id"] in engine.stop_orders
    assert s.highest_since_entry >= last
    assert not any(o for o in engine.sent if not o["stop"]), "no limit order on a clean restart"
    assert s.last_reconcile_ts == FIRST_TICK_DT.timestamp()
    assert not s.halted
    assert engine.synced > 0


def test_exchange_flat_clears_position(warmup_bars: list[BarData]) -> None:
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.set_position(0.0, 0.0)      # the stop filled while we were down
    s = restart(engine, stale_state(0.016, 3000.0, 2940.0))
    exp = expected(engine, 0.016, 2950.0, s.highest_since_entry, s.lowest_since_entry)
    assert exp.outcome == "clear" and exp.pos == 0.0

    s.on_tick(make_tick(2950.0))
    assert s.pos == 0.0
    assert s.entry_price == 0.0 and s.stop_price == 0.0 and s.bars_in_trade == 0
    assert s.stop_orderid == "" and s.entry_orderid == "" and s.exit_orderid == ""
    assert not engine.stop_sends, "no protective stop without a position"
    assert not engine.sent
    assert not s.halted
    assert any("reconcile clear" in m for m in engine.logs)


def test_exchange_size_differs_adopts_and_rearms(warmup_bars: list[BarData]) -> None:
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.set_position(0.020, 3005.0)   # partial fill / manual add while down
    s = restart(engine, stale_state(0.016, 3000.0, 2940.0))
    last = 3012.0
    exp = expected(engine, 0.016, last, s.highest_since_entry, s.lowest_since_entry)
    assert exp.outcome == "adopt" and exp.pos == 0.020 and exp.entry_price == 3005.0 and exp.recompute_stop

    s.on_tick(make_tick(last))
    assert s.pos == 0.020
    assert s.entry_price == 3005.0
    assert s.highest_since_entry == exp.highest_since_entry == max(3000.0, 3005.0, last)
    assert s.lowest_since_entry == exp.lowest_since_entry
    # stop recomputed from the current ATR below the adopted entry
    assert 0 < s.stop_price < 3005.0
    assert abs((3005.0 - s.stop_price) - max(s.stop_mult * s.atr, s._msp() * 3005.0)) < 0.011
    stops = engine.stop_sends
    assert len(stops) == 1
    assert stops[0]["volume"] == 0.020 and stops[0]["direction"] == Direction.SHORT
    assert stops[0]["price"] == s.stop_price
    assert s.stop_orderid == stops[0]["id"]
    assert not s.halted
    assert any("reconcile adopt" in m for m in engine.logs)


def test_short_adopted_from_flat(warmup_bars: list[BarData]) -> None:
    """Nothing persisted (fresh data file) but the exchange holds a short: adopt it and protect it."""
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.set_position(-0.030, 2990.0)
    s = restart(engine, dict(equity=50.0))
    exp = expected(engine, 0.0, 2980.0, 0.0, 0.0)
    assert exp.outcome == "adopt" and exp.pos == -0.030

    s.on_tick(make_tick(2980.0))
    assert s.pos == -0.030 and s.entry_price == 2990.0
    assert s.stop_price > 2990.0
    stops = engine.stop_sends
    assert len(stops) == 1
    assert stops[0]["direction"] == Direction.LONG and stops[0]["offset"] == Offset.CLOSE
    assert stops[0]["volume"] == 0.030 and stops[0]["price"] == s.stop_price


def test_desync_with_two_strategies_halts_without_orders(warmup_bars: list[BarData]) -> None:
    engine = FakeCtaEngine(warmup_bars, n_strategies=2)
    engine.main_engine.set_position(0.020, 3005.0)
    s = restart(engine, stale_state(0.016, 3000.0, 2940.0))
    exp = expected(engine, 0.016, 3010.0, s.highest_since_entry, s.lowest_since_entry)
    assert exp.outcome == "desync"

    s.on_tick(make_tick(3010.0))
    assert s.halted and s.halt_reason == "DESYNC"
    assert s.pos == 0.016, "never adopt when the symbol is shared"
    assert engine.main_engine.notifications, "DESYNC must notify"
    # the local position still gets its protective stop (never a naked position), nothing else
    stops = engine.stop_sends
    assert len(stops) == 1 and stops[0]["volume"] == 0.016
    assert not [o for o in engine.sent if not o["stop"]]

    # RESUME clears the DESYNC halt on a later (throttled) tick
    (Path(s.guard.state_dir) / "RESUME").write_text("1", encoding="utf-8")
    s.on_tick(make_tick(3011.0, FIRST_TICK_DT + timedelta(seconds=6)))
    assert not s.halted and s.halt_reason == ""


def test_missing_stop_price_is_recomputed(warmup_bars: list[BarData]) -> None:
    """Position persisted without a stop (crash between the fill and the sync): recompute and arm."""
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.set_position(0.016, 3000.0)
    state = stale_state(0.016, 3000.0, 0.0)
    state["stop_orderid"] = ""
    s = restart(engine, state)
    s.on_tick(make_tick(3001.0))
    assert s.pos == 0.016
    assert 0 < s.stop_price < 3000.0
    stops = engine.stop_sends
    assert len(stops) == 1 and stops[0]["price"] == s.stop_price and stops[0]["volume"] == 0.016


def test_stop_is_rearmed_on_next_bar_after_cancel(warmup_bars: list[BarData]) -> None:
    """If the protective stop disappears without a fill, the 1m bar check re-arms it."""
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.set_position(0.016, 3000.0)
    s = restart(engine, stale_state(0.016, 3000.0, 2940.0))
    s.on_tick(make_tick(3010.0))
    first = s.stop_orderid
    assert first
    engine.cancel_order(s, first)            # external cancel -> on_stop_order(CANCELLED)
    assert s.stop_orderid == ""
    s.on_tick(make_tick(3012.0, FIRST_TICK_DT + timedelta(minutes=1)))
    assert s.stop_orderid and s.stop_orderid != first
    assert len(engine.stop_sends) == 2 and engine.stop_sends[-1]["price"] == 2940.0


def test_kill_flag_at_startup_flattens(warmup_bars: list[BarData], trader_dir: Path) -> None:
    """SPEC 8.6: KILL present at startup -> strategy starts, flattens, halts."""
    (trader_dir / "KILL").write_text("1", encoding="utf-8")
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.set_position(0.016, 3000.0)
    s = restart(engine, stale_state(0.016, 3000.0, 2940.0))
    s.on_tick(make_tick(3010.0))
    assert s.halted and s.halt_reason == "KILL"
    limits = [o for o in engine.sent if not o["stop"]]
    assert limits, "KILL must send a limit exit"
    assert limits[-1]["direction"] == Direction.SHORT and limits[-1]["volume"] == 0.016
    assert limits[-1]["price"] < 3010.0
    assert s.exit_orderid == limits[-1]["id"]
    assert s.stop_orderid == "", "stop withdrawn while the limit exit is in flight"
