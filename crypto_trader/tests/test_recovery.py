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

Scenarios: in sync (adopt saved stop), exchange flat (clear, confirmed twice
when no position row exists at all), exchange holds a different size (adopt),
two strategies on the symbol with a mismatch (DESYNC halt, no orders), a
restart with a missing ``stop_price``, a same-process re-init (watchdog
path), an over-fill that flips the position, float residue after partial
fills, the live exit chase with asynchronous cancels, protective orders
under the order-rate limiter, a failed warmup and the S2 reconcile path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from vnpy.trader.constant import Direction, Exchange, Interval, Offset, Status
from vnpy.trader.object import AccountData, BarData, ContractData, OrderData, PositionData, TickData, TradeData
from vnpy_ctastrategy.base import STOPORDER_PREFIX, EngineType, StopOrder, StopOrderStatus

import run_live
from risk import RECONCILE_SECS, STATE_FILENAME, reconcile
from strategies.donchian_trend_h1 import DonchianTrendH1
from strategies.squeeze_break_15m import SqueezeBreak15M
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
        self.account_ready = True      # False = the account query has not answered yet
        self.positions: list[PositionData] = []
        self.notifications: list[tuple[str, str]] = []
        self.orders: dict[str, OrderData] = {}

    def set_position(self, volume: float, price: float) -> None:
        """
        Scripted NET position (signed volume) as the Binance gateway reports it.
        ``volume == 0`` leaves NO row (``/fapi/v3/positionRisk`` lists only open
        positions); use ``set_zero_row`` for an explicit zero-volume push.
        """
        self.positions = []
        if volume:
            self.positions.append(PositionData(symbol=SYMBOL, exchange=Exchange.GLOBAL, direction=Direction.NET,
                                               volume=volume, price=price, pnl=0.0, gateway_name=GATEWAY))

    def set_zero_row(self) -> None:
        """Explicit zero-volume row (ACCOUNT_UPDATE after the position was closed)."""
        self.positions = [PositionData(symbol=SYMBOL, exchange=Exchange.GLOBAL, direction=Direction.NET,
                                       volume=0.0, price=0.0, pnl=0.0, gateway_name=GATEWAY)]

    def get_contract(self, vt_symbol: str) -> ContractData | None:
        return self.contract if vt_symbol == VT_SYMBOL else None

    def get_order(self, vt_orderid: str) -> OrderData | None:
        return self.orders.get(vt_orderid)

    def get_account(self, vt_accountid: str) -> AccountData | None:
        if vt_accountid != f"{GATEWAY}.USDT" or not self.account_ready:
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
        self.strategies: dict[str, Any] = {}
        self.synced = 0
        self._n = 0
        self.fail_load_bar = False

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
        if self.fail_load_bar:
            raise RuntimeError("history query failed")
        return list(self.warmup)

    @property
    def live_stops(self) -> list[StopOrder]:
        return list(self.stop_orders.values())

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
    engine.strategies[s.strategy_name] = s
    s.on_init()
    s.inited = True
    for name, value in persisted.items():
        assert name in s.variables, f"{name} is not a persisted variable"
        setattr(s, name, value)
    s.on_start()
    s.trading = True
    return s


def reinit(s: DonchianTrendH1) -> None:
    """
    The watchdog path: the SAME instance is re-initialised after a callback
    exception (``CtaEngine.call_strategy_func`` cleared ``trading``/``inited``
    without cancelling anything), exactly like ``_init_strategy`` +
    ``start_strategy`` do it.
    """
    s.trading = False
    s.inited = False
    s.on_init()
    s.inited = True
    s.on_start()
    s.trading = True


def fill(s: DonchianTrendH1, direction: Direction, volume: float, price: float, dt: datetime) -> None:
    """Engine-style fill: ``pos`` is updated with plain float arithmetic BEFORE ``on_trade``."""
    s.pos += volume if direction == Direction.LONG else -volume
    s.on_trade(TradeData(symbol=SYMBOL, exchange=Exchange.GLOBAL, orderid="o", tradeid=f"t{dt.timestamp()}",
                         direction=direction, offset=Offset.CLOSE, price=price, volume=volume, datetime=dt,
                         gateway_name=GATEWAY))


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
    """An explicit zero-volume row (the gateway saw the close) clears immediately."""
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.set_zero_row()             # the stop filled while we were down
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


def test_missing_position_row_clears_only_after_confirmation(warmup_bars: list[BarData]) -> None:
    """
    No PositionData row at all is ambiguous (Binance positionRisk v3 lists
    only open positions; a late/failed reply looks identical): the position
    is kept and protected first, and cleared only when a second reading
    >= RECONCILE_SECS later still shows no row.
    """
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.set_position(0.0, 0.0)      # -> no row
    s = restart(engine, stale_state(0.016, 3000.0, 2940.0))
    s.on_tick(make_tick(2950.0))
    assert s.pos == 0.016 and s.stop_price == 2940.0, "never cleared on a single 'no row' reading"
    assert len(engine.stop_sends) == 1 and s.stop_orderid in engine.stop_orders
    assert any("clear pending confirmation" in m for m in engine.logs)
    assert not any("reconcile clear" in m for m in engine.logs)

    s.on_tick(make_tick(2951.0, FIRST_TICK_DT + timedelta(seconds=RECONCILE_SECS + 1)))
    assert s.pos == 0.0 and s.stop_price == 0.0 and s.stop_orderid == ""
    assert not engine.live_stops, "the stale stop is cancelled with the cleared position"
    assert any("reconcile clear" in m for m in engine.logs)
    assert not [o for o in engine.sent if not o["stop"]]


def test_reconcile_deferred_until_account_received(warmup_bars: list[BarData]) -> None:
    """Before the account snapshot exists nothing is adopted or cleared; the local position is protected."""
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.account_ready = False
    engine.main_engine.set_zero_row()
    s = restart(engine, stale_state(0.016, 3000.0, 2940.0))
    s.on_tick(make_tick(2950.0))
    assert s.pos == 0.016 and len(engine.stop_sends) == 1
    assert any("reconcile deferred" in m for m in engine.logs)
    engine.main_engine.account_ready = True
    s.on_tick(make_tick(2951.0, FIRST_TICK_DT + timedelta(seconds=RECONCILE_SECS + 1)))
    assert s.pos == 0.0 and not engine.live_stops


def test_reinit_same_instance_leaves_exactly_one_live_stop(warmup_bars: list[BarData]) -> None:
    """
    Watchdog re-init (finding: double stop -> position flip).  After the
    re-init only one protective stop may exist in the engine, and
    ``run_live.cancel_strategy_orders`` must drop the old one on the same fake.
    """
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.set_position(0.016, 3000.0)
    s = restart(engine, stale_state(0.016, 3000.0, 2940.0))
    s.on_tick(make_tick(3010.0))
    first = s.stop_orderid
    assert first and len(engine.live_stops) == 1

    reinit(s)
    s.on_tick(make_tick(3011.0, FIRST_TICK_DT + timedelta(seconds=1)))
    live = engine.live_stops
    assert len(live) == 1, f"expected exactly one live stop after re-init, got {live}"
    assert live[0].volume == 0.016 and live[0].direction == Direction.SHORT
    assert first in engine.cancelled and s.stop_orderid == live[0].stop_orderid

    # the runner's pre-init cancel does the same at engine level (trading=False)
    s.trading = False
    n = run_live.cancel_strategy_orders(engine, s.strategy_name)  # type: ignore[arg-type]
    assert n == 1 and not engine.live_stops


def test_overfill_flip_is_closed_with_a_cover_order(warmup_bars: list[BarData]) -> None:
    """A SELL larger than the long position flips it short: the reversed lot gets a stop and is exited at once."""
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.set_position(0.016, 3000.0)
    s = restart(engine, stale_state(0.016, 3000.0, 2940.0))
    s.on_tick(make_tick(3010.0))
    fill(s, Direction.SHORT, 0.032, 2939.0, FIRST_TICK_DT + timedelta(seconds=2))
    assert s.pos == -0.016
    assert len(s.trade_log) == 1 and s.trade_log[0]["direction"] == 1
    assert s._exit_reason == "UNWANTED_FLIP"
    covers = [o for o in engine.sent if not o["stop"] and o["direction"] == Direction.LONG]
    assert covers and covers[-1]["volume"] == 0.016 and covers[-1]["offset"] == Offset.CLOSE
    assert s.exit_orderid == covers[-1]["id"]
    assert not [so for so in engine.live_stops if so.direction == Direction.SHORT], "no SELL stop may survive a flip"
    assert s.entry_price == 2939.0 and s.stop_price > 2939.0


def test_float_residue_after_partial_fills_books_the_round_trip(warmup_bars: list[BarData]) -> None:
    """0.005 + 0.045 - 0.05 leaves pos = -6.9e-18 in the engine: the strategy must treat it as flat."""
    engine = FakeCtaEngine(warmup_bars)
    s = restart(engine, dict(equity=50.0))
    s.on_tick(make_tick(3000.0))
    t = FIRST_TICK_DT + timedelta(seconds=1)
    fill(s, Direction.LONG, 0.005, 3000.0, t)
    fill(s, Direction.LONG, 0.045, 3000.0, t + timedelta(seconds=1))
    assert s.pos == 0.05 and s.stop_orderid and engine.live_stops[-1].volume == 0.05
    fill(s, Direction.SHORT, 0.05, 3060.0, t + timedelta(seconds=2))
    assert s.pos == 0.0 and s._is_flat()
    assert len(s.trade_log) == 1 and s.trades_today == 1
    assert s.trade_log[0]["pnl"] == pytest.approx(0.05 * 60 - 0.0005 * 3000 * 0.05 - 0.0005 * 3060 * 0.05)
    assert s.realized_pnl == pytest.approx(3.0)                       # gross; fees live in fees_paid
    assert s.fees_paid == pytest.approx(0.0005 * (3000 * 0.05 + 3060 * 0.05))
    assert not engine.live_stops and s._exit_reason == "" and s.stop_orderid == ""
    assert VT_SYMBOL not in s.guard.shared.locks
    # no zero-quantity order is ever sent, and nothing more on later ticks
    n_sent = len(engine.sent)
    s.on_tick(make_tick(3061.0, t + timedelta(seconds=30)))
    assert len(engine.sent) == n_sent
    assert all(o["volume"] >= STEP for o in engine.sent)


def test_chase_counts_one_step_per_pending_cancel(warmup_bars: list[BarData]) -> None:
    """Live cancels are asynchronous: 20 ticks while the cancel is pending advance the chase by ONE step."""
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.set_position(0.016, 3000.0)
    s = restart(engine, stale_state(0.016, 3000.0, 2940.0))
    s.on_tick(make_tick(3000.0))
    s._request_exit("REGIME", 3000.0, FIRST_TICK_DT.timestamp())
    exit_id = s.exit_orderid
    assert exit_id and exit_id.startswith("FAKE") and s.stop_orderid == ""
    for i in range(1, 21):
        s.on_tick(make_tick(2999.0, FIRST_TICK_DT + timedelta(seconds=0.5 * i)))   # 0.5 .. 10.0 s
    assert s._chase_count == 1
    assert engine.cancelled.count(exit_id) == 2, "one cancel at 5 s, one re-issue at 10 s (lost-cancel guard)"
    assert s.exit_orderid == exit_id, "nothing is resent while the cancel is unconfirmed"
    # confirmation arrives -> resend one step worse, still counted once
    s.on_order(OrderData(symbol=SYMBOL, exchange=Exchange.GLOBAL, orderid=exit_id.split(".")[-1], status=Status.CANCELLED,
                         direction=Direction.SHORT, price=2997.0, volume=0.016, gateway_name="FAKE"))
    s.on_tick(make_tick(2999.0, FIRST_TICK_DT + timedelta(seconds=10.5)))
    limits = [o for o in engine.sent if not o["stop"]]
    assert len(limits) == 2 and s._chase_count == 1
    assert limits[-1]["price"] == pytest.approx(round(2999.0 * (1 - 0.002), 2))


def test_protective_orders_are_never_rate_limited(warmup_bars: list[BarData]) -> None:
    """Six sends in the last minute: a trail move still re-arms the stop and an exit is still sent."""
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.set_position(0.016, 3000.0)
    s = restart(engine, stale_state(0.016, 3000.0, 2940.0))
    s.on_tick(make_tick(3000.0))
    ts = FIRST_TICK_DT.timestamp()
    for i in range(6):
        s.guard.note_order_sent(ts + i)
    assert not s.guard.check_order("entry", Direction.LONG, 3100.0, 0.016, 50.0, ts=ts + 7, is_stop=True,
                                   ref_close=3000.0)[0], "entries ARE rate limited"
    s.stop_price = 2950.0
    s._arm_stop(ts + 7)
    assert s.stop_orderid and engine.stop_orders[s.stop_orderid].price == 2950.0
    assert len(engine.live_stops) == 1
    s._request_exit("REGIME", 3000.0, ts + 8)
    assert s.exit_orderid and s.stop_orderid == ""
    assert not engine.live_stops


def test_clear_cancels_resting_exit_and_reject_forces_reconcile(warmup_bars: list[BarData]) -> None:
    """
    A resting server exit is cancelled on 'clear' (it would open a reverse
    position), and a REJECTED exit backs off and forces a reconcile instead
    of being resent on every tick.
    """
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.set_position(0.016, 3000.0)
    s = restart(engine, stale_state(0.016, 3000.0, 2940.0))
    s.on_tick(make_tick(3000.0))
    ts = FIRST_TICK_DT.timestamp()
    s._request_exit("REGIME", 3000.0, ts)
    exit_id = s.exit_orderid
    engine.main_engine.set_zero_row()               # closed manually while the exit rests
    s._reconcile(3000.0, ts + 1, force=True)
    assert exit_id in engine.cancelled and s.pos == 0.0 and s.exit_orderid == "" and s._exit_reason == ""

    # second scenario: the exchange rejects the exit (reduce-only on a gone position)
    engine2 = FakeCtaEngine(warmup_bars)
    engine2.main_engine.set_position(0.016, 3000.0)
    s2 = restart(engine2, stale_state(0.016, 3000.0, 2940.0))
    s2.on_tick(make_tick(3000.0))
    s2._request_exit("REGIME", 3000.0, ts)
    exit_id = s2.exit_orderid
    engine2.main_engine.set_zero_row()
    s2.on_tick(make_tick(3000.0, FIRST_TICK_DT + timedelta(seconds=1)))
    s2.on_order(OrderData(symbol=SYMBOL, exchange=Exchange.GLOBAL, orderid=exit_id.split(".")[-1],
                          status=Status.REJECTED, direction=Direction.SHORT, price=2997.0, volume=0.016,
                          gateway_name="FAKE"))
    n_limits = len([o for o in engine2.sent if not o["stop"]])
    for i in range(2, 11):
        s2.on_tick(make_tick(3000.0, FIRST_TICK_DT + timedelta(seconds=i)))
    assert len([o for o in engine2.sent if not o["stop"]]) == n_limits, "no resend during the back-off"
    assert s2.stop_orderid and len(engine2.live_stops) == 1, "protected (local stop) while backing off"
    s2.on_tick(make_tick(3000.0, FIRST_TICK_DT + timedelta(seconds=12)))   # forced reconcile is due
    assert s2.pos == 0.0 and s2._exit_reason == "" and not engine2.live_stops
    assert len([o for o in engine2.sent if not o["stop"]]) == n_limits


def test_failed_warmup_halts_and_is_not_startable(warmup_bars: list[BarData]) -> None:
    """load_bar raising inside on_init: EXCEPTION halt survives the variable restore; warmup_ok is False."""
    engine = FakeCtaEngine(warmup_bars)
    engine.fail_load_bar = True
    s = restart(engine, dict(equity=50.0, halted=False, halt_reason=""))   # the engine restores unhalted values
    assert s.warmup_ok is False and s.atr == 0.0
    assert s.halted and s.halt_reason == "EXCEPTION"
    assert any("re-applied halt EXCEPTION" in m for m in engine.logs)
    # the runner refuses to start such a strategy
    s.inited = True
    s.trading = False
    cta = engine
    cta.init_strategy = lambda name: _Done()  # type: ignore[attr-defined]
    cta.start_strategy = lambda name: setattr(s, "trading", True)  # type: ignore[attr-defined]
    assert run_live.init_and_start(cta, s.strategy_name) is False  # type: ignore[arg-type]
    assert not s.trading and not s.inited


class _Done:
    def result(self, timeout: float | None = None) -> None:
        return None


def test_s2_reconcile_cancels_stale_stop_before_rearming(warmup_bars: list[BarData]) -> None:
    """SqueezeBreak15M: adopt re-arms ONE stop for the new size; clear leaves no stop behind."""
    engine = FakeCtaEngine(warmup_bars)
    engine.main_engine.set_position(0.016, 3000.0)
    s2 = SqueezeBreak15M(engine, "s2_eth", VT_SYMBOL, {"risk_dial": "normal", "capital": 50.0, "warmup_days": 5})
    engine.strategies[s2.strategy_name] = s2
    s2.on_init()
    s2.inited = True
    for name, value in dict(pos=0.016, entry_price=3000.0, stop_price=2940.0, highest_since_entry=3000.0,
                            lowest_since_entry=2940.0, equity=50.0, r_value=0.96).items():
        setattr(s2, name, value)
    s2.on_start()
    s2.trading = True
    s2.on_tick(make_tick(3000.0))
    first = s2.stop_orderid
    assert first and len(engine.live_stops) == 1 and engine.live_stops[0].volume == 0.016

    engine.main_engine.set_position(0.010, 3000.0)
    s2.on_tick(make_tick(3000.0, FIRST_TICK_DT + timedelta(seconds=RECONCILE_SECS + 1)))
    assert s2.pos == 0.010
    live = engine.live_stops
    assert len(live) == 1 and live[0].volume == 0.010 and first in engine.cancelled

    engine.main_engine.set_zero_row()
    s2.on_tick(make_tick(3000.0, FIRST_TICK_DT + timedelta(seconds=2 * RECONCILE_SECS + 2)))
    assert s2.pos == 0.0 and not engine.live_stops and s2.stop_orderid == ""


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
    assert exp.highest_since_entry == max(3000.0, 3005.0, last)
    assert s.highest_since_entry == 3013.0, "bar high of the first 1m bar is applied after the adopt"
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


def test_desync_with_two_strategies_halts_without_orders(warmup_bars: list[BarData],
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from vnpy.trader.setting import SETTINGS

    monkeypatch.setitem(SETTINGS, "email.username", "bot@example.com")
    monkeypatch.setitem(SETTINGS, "email.receiver", "owner@example.com")
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


def test_desync_without_notification_channel_only_logs(warmup_bars: list[BarData]) -> None:
    """No email / wechat configured: the halt is logged, send_notification is never called (it would hang close())."""
    engine = FakeCtaEngine(warmup_bars, n_strategies=2)
    engine.main_engine.set_position(0.020, 3005.0)
    s = restart(engine, stale_state(0.016, 3000.0, 2940.0))
    s.on_tick(make_tick(3010.0))
    assert s.halted and s.halt_reason == "DESYNC"
    assert not engine.main_engine.notifications
    assert any("HALT DESYNC" in m for m in engine.logs)


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
