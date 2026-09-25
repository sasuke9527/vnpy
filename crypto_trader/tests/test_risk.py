"""Tests for risk.py (SPEC section 4) using a FakeStrategy, no engine."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from vnpy.trader.constant import Direction, Exchange, Interval, Status
from vnpy.trader.object import BarData, OrderData, TickData
from vnpy_ctastrategy.base import EngineType

import risk
from risk import (
    ReconcileResult,
    RiskDecision,
    RiskGuard,
    SharedRiskState,
    get_equity,
    reconcile,
)

VT_SYMBOL = "ETHUSDT_SWAP_BINANCE.GLOBAL"
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

DIAL = dict(risk_pct_s1=0.020, max_leverage=3.0, gross_leverage=4.0, daily_loss_pct=0.06,
            max_dd_halt=0.25, max_consec_losses=4, cooldown_hours=8, max_trades_day_s1=3)


class FakeStrategy:
    """Mirrors the persisted variables / risk parameters of the spec."""

    def __init__(self, name: str = "s1_eth", engine_type: EngineType = EngineType.BACKTESTING) -> None:
        self.strategy_name = name
        self.vt_symbol = VT_SYMBOL
        self.engine_type = engine_type
        self.logs: list[str] = []
        self.synced = 0
        self.trading = True
        # contract facts
        self.size = 1.0
        self.pricetick = 0.01
        self.min_volume = 0.001
        # risk parameters (normally applied from DIALS in on_init)
        self.risk_dial = "normal"
        self.daily_loss_pct = 0.06
        self.max_dd_halt = 0.25
        self.max_consec_losses = 4
        self.cooldown_hours = 8.0
        self.max_trades_day = 3
        self.max_leverage = 3.0
        self.gross_leverage = 4.0
        self.min_notional = 20.0
        self.capital = 50.0
        # persisted variables
        self.pos = 0.0
        self.equity = 50.0
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.entry_price = 0.0
        self.stop_price = 0.0
        self.highest_since_entry = 0.0
        self.lowest_since_entry = 0.0
        self.entry_bar_ts = 0.0
        self.bars_in_trade = 0
        self.last_1m_dt = ""
        self.day_key = ""
        self.day_start_equity = 0.0
        self.peak_equity = 0.0
        self.consec_losses = 0
        self.cooldown_until = 0.0
        self.trades_today = 0
        self.reject_count = 0
        self.halted = False
        self.halt_reason = ""
        self.entry_orderid = ""
        self.stop_orderid = ""
        self.exit_orderid = ""
        self.last_reconcile_ts = 0.0

    def get_engine_type(self) -> EngineType:
        return self.engine_type

    def write_log(self, msg: str) -> None:
        self.logs.append(msg)

    def sync_data(self) -> None:
        self.synced += 1

    def get_size(self) -> float:
        return self.size

    def get_pricetick(self) -> float:
        return self.pricetick


def bar(dt: datetime, close: float = 3000.0) -> BarData:
    return BarData(symbol="ETHUSDT_SWAP_BINANCE", exchange=Exchange.GLOBAL, datetime=dt,
                   interval=Interval.MINUTE, open_price=close, high_price=close,
                   low_price=close, close_price=close, gateway_name="TEST")


def tick(dt: datetime, last: float = 3000.0) -> TickData:
    return TickData(symbol="ETHUSDT_SWAP_BINANCE", exchange=Exchange.GLOBAL, datetime=dt,
                    last_price=last, gateway_name="TEST")


def order(status: Status) -> OrderData:
    return OrderData(symbol="ETHUSDT_SWAP_BINANCE", exchange=Exchange.GLOBAL, orderid="1",
                     status=status, gateway_name="TEST")


@pytest.fixture()
def vntrader(tmp_path: Path) -> Path:
    d = tmp_path / ".vntrader"
    d.mkdir()
    return d


def make_guard(strategy: FakeStrategy | None = None, state_dir: Path | None = None,
               shared: SharedRiskState | None = None) -> tuple[FakeStrategy, RiskGuard]:
    s = strategy or FakeStrategy()
    g = RiskGuard(s, dials=DIAL, state_dir=state_dir, shared=shared)
    return s, g


# --------------------------------------------------------------------------- #
# day roll
# --------------------------------------------------------------------------- #

def test_day_roll_resets_counters_and_daily_loss(vntrader: Path) -> None:
    s, g = make_guard(state_dir=vntrader)
    d = g.pre_bar(bar(T0), equity_mtm=50.0, balance=50.0)
    assert isinstance(d, RiskDecision) and d.allow_entry
    assert s.day_key == "2026-03-01"
    assert s.day_start_equity == 50.0
    assert g.shared.day_start_equity == 50.0

    s.trades_today = 3
    g.pre_bar(bar(T0 + timedelta(hours=1)), equity_mtm=46.0, balance=46.0)
    assert s.trades_today == 3  # same day: not reset
    assert s.halted and s.halt_reason == "DAILY_LOSS"

    d = g.pre_bar(bar(T0 + timedelta(days=1)), equity_mtm=46.0, balance=46.0)
    assert s.day_key == "2026-03-02"
    assert s.trades_today == 0
    assert s.day_start_equity == 46.0
    assert not s.halted and s.halt_reason == ""
    assert not g.shared.halted
    assert d.allow_entry


def test_day_key_uses_utc_date() -> None:
    dt = datetime(2026, 3, 1, 23, 30, tzinfo=timezone(timedelta(hours=-8)))  # 07:30 UTC next day
    assert risk.day_key_of(dt) == "2026-03-02"


# --------------------------------------------------------------------------- #
# daily loss
# --------------------------------------------------------------------------- #

def test_daily_loss_blocks_entries_and_flattens_only_losers(vntrader: Path) -> None:
    s, g = make_guard(state_dir=vntrader)
    g.pre_bar(bar(T0, 3000), 100.0, 100.0)
    # losing position: pos long 0.02 from 3000, close 2900 -> -2 unrealized
    s.pos = 0.02
    s.entry_price = 3000.0
    d = g.pre_bar(bar(T0 + timedelta(minutes=15), 2900), equity_mtm=93.0, balance=95.0)
    assert not d.allow_entry
    assert d.cancel_entries
    assert d.must_flatten
    assert "DAILY_LOSS" in d.reason
    # a profitable position keeps its trailing stop (no flatten)
    s.entry_price = 2800.0
    d = g.pre_bar(bar(T0 + timedelta(minutes=30), 2900), equity_mtm=93.0, balance=95.0)
    assert not d.allow_entry and not d.must_flatten
    # explicit unrealized pnl argument is honoured
    d = g.pre_bar(bar(T0 + timedelta(minutes=45), 2900), 93.0, 95.0, unrealized_pnl=-0.5)
    assert d.must_flatten
    # still blocked at the exact threshold, no flatten when flat
    s.pos = 0.0
    d = g.pre_bar(bar(T0 + timedelta(hours=1), 2900), equity_mtm=94.0, balance=94.0)
    assert not d.allow_entry and not d.must_flatten


def test_daily_loss_is_account_wide_via_shared_state(vntrader: Path) -> None:
    shared = SharedRiskState(vntrader / "risk_state.json")
    s1, g1 = make_guard(FakeStrategy("s1"), state_dir=vntrader, shared=shared)
    s2, g2 = make_guard(FakeStrategy("s2"), state_dir=vntrader, shared=shared)
    g1.pre_bar(bar(T0), 100.0, 100.0)
    g2.pre_bar(bar(T0), 100.0, 100.0)
    g1.pre_bar(bar(T0 + timedelta(minutes=15)), 93.0, 93.0)
    d2 = g2.pre_bar(bar(T0 + timedelta(minutes=15)), 93.0, 93.0)
    assert not d2.allow_entry and "DAILY_LOSS" in d2.reason
    # file was written atomically and contains the shared flag
    data = json.loads((vntrader / "risk_state.json").read_text())
    assert data["halted"] and data["halt_reason"] == "DAILY_LOSS"
    assert not (vntrader / "risk_state.json.tmp").exists()


# --------------------------------------------------------------------------- #
# drawdown halt
# --------------------------------------------------------------------------- #

def test_drawdown_halt_requires_resume(vntrader: Path) -> None:
    s, g = make_guard(FakeStrategy(engine_type=EngineType.LIVE), state_dir=vntrader)
    g.pre_bar(bar(T0), 100.0, 100.0)
    assert s.peak_equity == 100.0
    g.pre_bar(bar(T0 + timedelta(days=1)), 120.0, 120.0)
    assert g.shared.peak_equity == 120.0
    s.pos = 0.01
    s.entry_price = 3000.0
    d = g.pre_bar(bar(T0 + timedelta(days=2), 3100), equity_mtm=90.0, balance=90.0)
    assert d.must_flatten and not d.allow_entry and d.reason == "HALTED:DRAWDOWN"
    assert s.halted and s.halt_reason == "DRAWDOWN"
    assert g.shared.halted and g.shared.halt_reason == "DRAWDOWN"

    # a new day and recovering equity do not clear a drawdown halt
    s.pos = 0.0
    d = g.pre_bar(bar(T0 + timedelta(days=3)), equity_mtm=110.0, balance=110.0)
    assert not d.allow_entry and s.halted and s.halt_reason == "DRAWDOWN"

    # RESUME clears it, deletes the flag and re-bases the peak
    (vntrader / "RESUME").write_text("")
    d = g.pre_bar(bar(T0 + timedelta(days=3, hours=1)), equity_mtm=110.0, balance=110.0)
    assert d.allow_entry
    assert not s.halted and not g.shared.halted
    assert not (vntrader / "RESUME").exists()
    assert g.shared.peak_equity == 110.0


# --------------------------------------------------------------------------- #
# consecutive losses, cooldown, streak multiplier
# --------------------------------------------------------------------------- #

def test_streak_multiplier_and_cooldown(vntrader: Path) -> None:
    s, g = make_guard(state_dir=vntrader)
    g.pre_bar(bar(T0), 50.0, 50.0)
    ts0 = risk.ts_of(T0)
    assert g.streak_mult() == 1.0
    g.on_trade_closed(-1.0, ts0)
    assert s.consec_losses == 1 and s.trades_today == 1 and g.streak_mult() == 1.0
    g.on_trade_closed(-1.0, ts0)
    assert g.streak_mult() == 1.0                         # 0.5^0
    g.on_trade_closed(-1.0, ts0)
    assert g.streak_mult() == 0.5                         # 0.5^1
    assert s.cooldown_until == 0.0
    g.on_trade_closed(-1.0, ts0)                          # 4th loss -> cooldown
    assert g.streak_mult() == 0.25
    assert s.cooldown_until == pytest.approx(ts0 + 8 * 3600)
    g.on_trade_closed(-1.0, ts0)
    assert g.streak_mult() == 0.25                        # floor
    assert s.trades_today == 5

    s.trades_today = 0
    d = g.pre_bar(bar(T0 + timedelta(hours=1)), 48.0, 48.0)
    assert not d.allow_entry and d.reason == "COOLDOWN" and not d.must_flatten
    d = g.pre_bar(bar(T0 + timedelta(hours=9)), 48.0, 48.0)
    assert d.allow_entry

    g.on_trade_closed(+2.0, ts0)                          # win resets the streak
    assert s.consec_losses == 0 and g.streak_mult() == 1.0
    assert g.shared.consec_losses_global == 0


def test_trades_per_day_limit(vntrader: Path) -> None:
    s, g = make_guard(state_dir=vntrader)
    g.pre_bar(bar(T0), 50.0, 50.0)
    s.trades_today = 3
    d = g.pre_bar(bar(T0 + timedelta(minutes=15)), 50.0, 50.0)
    assert not d.allow_entry and d.reason == "TRADES_DAY"
    d = g.pre_bar(bar(T0 + timedelta(days=1)), 50.0, 50.0)
    assert d.allow_entry


# --------------------------------------------------------------------------- #
# reject counter
# --------------------------------------------------------------------------- #

def test_reject_counter_halts_at_three_and_resets_on_accept(vntrader: Path) -> None:
    s, g = make_guard(FakeStrategy(engine_type=EngineType.LIVE), state_dir=vntrader)
    g.pre_bar(bar(T0), 50.0, 50.0)
    g.on_order(order(Status.REJECTED))
    g.on_order(order(Status.REJECTED))
    assert s.reject_count == 2 and not s.halted
    g.on_order(order(Status.NOTTRADED))
    assert s.reject_count == 0
    g.on_order(order(Status.REJECTED))
    g.on_order(order(Status.REJECTED))
    g.on_order(order(Status.REJECTED))
    assert s.reject_count == 3
    assert s.halted and s.halt_reason == "REJECTS"
    assert not g.shared.halted                     # per-strategy halt only
    d = g.pre_bar(bar(T0 + timedelta(minutes=15)), 50.0, 50.0)
    assert not d.allow_entry and d.reason == "HALTED:REJECTS" and not d.must_flatten
    (vntrader / "RESUME").write_text("")
    d = g.pre_bar(bar(T0 + timedelta(minutes=30)), 50.0, 50.0)
    assert d.allow_entry and s.reject_count == 0


# --------------------------------------------------------------------------- #
# flag files
# --------------------------------------------------------------------------- #

def test_kill_pause_resume_flags(vntrader: Path) -> None:
    s, g = make_guard(FakeStrategy(engine_type=EngineType.LIVE), state_dir=vntrader)
    g.pre_bar(bar(T0), 50.0, 50.0)

    (vntrader / "PAUSE").write_text("")
    s.pos = 0.01
    s.entry_price = 3000.0
    d = g.pre_bar(bar(T0 + timedelta(minutes=15)), 50.0, 50.0)
    assert not d.allow_entry and d.cancel_entries and not d.must_flatten and d.reason == "PAUSE"
    assert not s.halted
    (vntrader / "PAUSE").unlink()
    d = g.pre_bar(bar(T0 + timedelta(minutes=30)), 50.0, 50.0)
    assert d.allow_entry

    (vntrader / "KILL").write_text("")
    d = g.pre_bar(bar(T0 + timedelta(minutes=45)), 50.0, 50.0)
    assert d.must_flatten and not d.allow_entry and d.reason == "KILL"
    assert s.halted and s.halt_reason == "KILL"
    assert g.shared.halted and g.shared.halt_reason == "KILL"
    # KILL stays in force while the file exists
    s.pos = 0.0
    d = g.pre_bar(bar(T0 + timedelta(hours=1)), 50.0, 50.0)
    assert not d.allow_entry and not d.must_flatten

    (vntrader / "RESUME").write_text("")
    d = g.pre_bar(bar(T0 + timedelta(hours=2)), 50.0, 50.0)
    assert d.allow_entry and not s.halted and not g.shared.halted
    assert not (vntrader / "RESUME").exists()
    assert not (vntrader / "KILL").exists()


def test_resume_never_clears_coarse_lots(vntrader: Path) -> None:
    s, g = make_guard(FakeStrategy(engine_type=EngineType.LIVE), state_dir=vntrader)
    s.halted = True
    s.halt_reason = "COARSE_LOTS"
    (vntrader / "RESUME").write_text("")
    d = g.pre_bar(bar(T0), 50.0, 50.0)
    assert not d.allow_entry and s.halted and s.halt_reason == "COARSE_LOTS"


def test_backtest_guard_ignores_and_keeps_operator_flags(vntrader: Path) -> None:
    """A backtest sharing the live .vntrader must neither obey nor consume KILL / PAUSE / RESUME."""
    s, g = make_guard(state_dir=vntrader)          # BACKTESTING
    assert not g.live
    for flag in ("KILL", "PAUSE", "RESUME"):
        (vntrader / flag).write_text("")
    s.halted, s.halt_reason = True, "REJECTS"
    d = g.pre_bar(bar(T0), 50.0, 50.0)
    assert not d.must_flatten and d.reason == "HALTED:REJECTS"   # RESUME not applied, KILL not applied
    assert s.halted and s.halt_reason == "REJECTS"
    for flag in ("KILL", "PAUSE", "RESUME"):
        assert (vntrader / flag).exists(), f"{flag} consumed by a backtest"
    assert g.check_flags() == (False, False)


def test_tick_is_throttled_to_five_seconds(vntrader: Path) -> None:
    s, g = make_guard(FakeStrategy(engine_type=EngineType.LIVE), state_dir=vntrader)
    assert g.live
    d = g.tick(tick(T0))
    assert d is not None and d.allow_entry
    (vntrader / "PAUSE").write_text("")
    assert g.tick(tick(T0 + timedelta(seconds=3))) is None
    d = g.tick(tick(T0 + timedelta(seconds=5)))
    assert d is not None and d.reason == "PAUSE"
    d = g.tick(tick(T0 + timedelta(seconds=11)), equity_mtm=50.0, balance=50.0)
    assert d is not None and s.day_key == "2026-03-01"


# --------------------------------------------------------------------------- #
# reconcile
# --------------------------------------------------------------------------- #

def test_reconcile_pure_outcomes() -> None:
    ok = reconcile(0.016, 0.016, step=0.001)
    assert ok.outcome == "ok" and ok.pos == 0.016
    assert reconcile(0.0164, 0.016, step=0.001).outcome == "ok"     # within step/2

    adopt = reconcile(0.02, 0.0, step=0.001, n_strategies_on_symbol=1,
                      exchange_entry_price=2950.0, last_price=3000.0, saved_highest=2990.0)
    assert isinstance(adopt, ReconcileResult)
    assert adopt.outcome == "adopt" and adopt.pos == 0.02
    assert adopt.entry_price == 2950.0 and adopt.highest_since_entry == 3000.0
    assert adopt.lowest_since_entry == 2950.0 and adopt.recompute_stop

    clear = reconcile(0.0, 0.02, step=0.001)
    assert clear.outcome == "clear" and clear.pos == 0.0 and clear.entry_price == 0.0

    desync = reconcile(0.02, 0.0, step=0.001, n_strategies_on_symbol=2)
    assert desync.outcome == "desync" and desync.pos == 0.0


def test_reconcile_applied_to_strategy(vntrader: Path) -> None:
    s, g = make_guard(state_dir=vntrader)
    s.pos = 0.016
    s.entry_price = 3000.0
    s.stop_price = 2940.0
    s.stop_orderid = "STOP.1"
    s.highest_since_entry = 3050.0
    res = g.reconcile(0.0, ts=100.0)
    assert res.outcome == "clear"
    assert s.pos == 0.0 and s.entry_price == 0.0 and s.stop_price == 0.0
    assert s.stop_orderid == "" and s.highest_since_entry == 0.0
    assert s.last_reconcile_ts == 100.0 and s.synced >= 1

    res = g.reconcile(-0.03, exchange_entry_price=3100.0, last_price=3080.0, ts=200.0)
    assert res.outcome == "adopt"
    assert s.pos == -0.03 and s.entry_price == 3100.0
    assert s.lowest_since_entry == 3080.0 and s.highest_since_entry == 3100.0
    assert s.stop_price == 0.0 and s.stop_orderid == ""   # strategy recomputes from ATR

    res = g.reconcile(0.05, n_strategies_on_symbol=2, ts=300.0)
    assert res.outcome == "desync"
    assert s.pos == -0.03                                  # untouched
    assert s.halted and s.halt_reason == "DESYNC"


# --------------------------------------------------------------------------- #
# check_order
# --------------------------------------------------------------------------- #

def test_check_order_rejections(vntrader: Path) -> None:
    s, g = make_guard(FakeStrategy(engine_type=EngineType.LIVE), state_dir=vntrader)
    g.pre_bar(bar(T0), 50.0, 50.0)
    ok, why = g.check_order("entry", Direction.LONG, 3000.0, 0.016, balance=50.0, last_price=3000.0)
    assert ok, why
    assert not g.check_order("entry", Direction.LONG, 3000.0, 0.0, 50.0)[0]
    # min notional: 0.005 * 3000 = 15 < 20
    ok, why = g.check_order("entry", Direction.LONG, 3000.0, 0.005, 50.0)
    assert not ok and "min_notional" in why
    # leverage: 0.06 * 3000 = 180 > 0.98*50*3 = 147
    ok, why = g.check_order("entry", Direction.LONG, 3000.0, 0.06, 50.0)
    assert not ok and "max_leverage" in why
    # price distance (live, limit orders only)
    ok, why = g.check_order("exit", Direction.SHORT, 3100.0, 0.016, 50.0, last_price=3000.0)
    assert not ok and "from last" in why
    assert g.check_order("entry", Direction.LONG, 3100.0, 0.016, 50.0, last_price=3000.0,
                         is_stop=True, ref_close=3000.0)[0]
    # wrong-side stops
    ok, why = g.check_order("entry", Direction.LONG, 2990.0, 0.016, 50.0, is_stop=True, ref_close=3000.0)
    assert not ok and "not above" in why
    ok, why = g.check_order("stop", Direction.SHORT, 3010.0, 0.016, 50.0, is_stop=True, ref_close=3000.0)
    assert not ok and "not below" in why
    # price not on tick
    ok, why = g.check_order("entry", Direction.LONG, 3000.005, 0.016, 50.0)
    assert not ok and "pricetick" in why
    # duplicate intent
    s.entry_orderid = "STOP.7"
    ok, why = g.check_order("entry", Direction.LONG, 3000.0, 0.016, 50.0)
    assert not ok and "duplicate" in why
    s.entry_orderid = ""
    # orders per minute
    ts = risk.ts_of(T0)
    for i in range(6):
        g.note_order_sent(ts + i)
    ok, why = g.check_order("entry", Direction.LONG, 3000.0, 0.016, 50.0, ts=ts + 10)
    assert not ok and "rate" in why
    assert g.check_order("entry", Direction.LONG, 3000.0, 0.016, 50.0, ts=ts + 61)[0]


def test_check_order_backtest_skips_price_distance(vntrader: Path) -> None:
    s, g = make_guard(state_dir=vntrader)
    assert g.check_order("exit", Direction.SHORT, 3200.0, 0.016, 50.0, last_price=3000.0)[0]


# --------------------------------------------------------------------------- #
# gross cap and symbol locks
# --------------------------------------------------------------------------- #

def test_free_notional_and_symbol_locks(vntrader: Path) -> None:
    shared = SharedRiskState(vntrader / "risk_state.json")
    s1, g1 = make_guard(FakeStrategy("s1"), state_dir=vntrader, shared=shared)
    s2, g2 = make_guard(FakeStrategy("s2"), state_dir=vntrader, shared=shared)
    assert g1.free_notional(50.0) == 200.0
    assert g1.acquire_lock(48.0)
    s1.entry_orderid = "STOP.1"
    assert g2.free_notional(50.0) == 152.0
    assert g1.free_notional(50.0) == 200.0            # own notional does not count
    assert not g2.acquire_lock(10.0)                  # same symbol locked by s1
    d2 = g2.pre_bar(bar(T0), 50.0, 50.0)
    assert not d2.allow_entry and d2.reason == "LOCKED:s1"
    # lock persists across a re-read of the file
    reread = SharedRiskState(vntrader / "risk_state.json")
    assert reread.locks[VT_SYMBOL] == "s1" and reread.open_notional["s1"] == 48.0
    # released automatically when flat and no entry order pending
    s1.entry_orderid = ""
    g1.pre_bar(bar(T0), 50.0, 50.0)
    assert VT_SYMBOL not in shared.locks and "s1" not in shared.open_notional
    assert g2.free_notional(50.0) == 200.0


# --------------------------------------------------------------------------- #
# get_equity
# --------------------------------------------------------------------------- #

def test_get_equity_backtest_and_fallback() -> None:
    s = FakeStrategy()
    s.realized_pnl = 3.0
    s.fees_paid = 0.5
    s.pos = 0.02
    s.entry_price = 3000.0
    balance, mtm = get_equity(s, close=3100.0)
    assert balance == pytest.approx(52.5)
    assert mtm == pytest.approx(52.5 + 0.02 * 100)
    assert s.equity == pytest.approx(52.5)

    class Broken(FakeStrategy):
        def get_engine_type(self) -> EngineType:
            raise RuntimeError("no engine")

    b = Broken()
    b.equity = 42.0
    assert get_equity(b) == (42.0, 42.0)
    assert any("get_equity failed" in m for m in b.logs)


def test_get_equity_live_uses_wallet_and_position_pnl() -> None:
    class Pos:
        def __init__(self, vt_symbol: str, pnl: float) -> None:
            self.vt_symbol = vt_symbol
            self.pnl = pnl
            self.gateway_name = "BINANCE_LINEAR"

    class Acct:
        balance = 55.0

    class Contract:
        gateway_name = "BINANCE_LINEAR"

    class MainEngine:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def get_contract(self, vt_symbol: str) -> Any:
            return Contract()

        def get_account(self, vt_accountid: str) -> Any:
            self.asked.append(vt_accountid)
            return Acct()

        def get_all_positions(self) -> list[Pos]:
            return [Pos(VT_SYMBOL, -1.5), Pos("BTCUSDT_SWAP_BINANCE.GLOBAL", 9.0)]

    class CtaEngine:
        main_engine = MainEngine()

    s = FakeStrategy(engine_type=EngineType.LIVE)
    s.cta_engine = CtaEngine()  # type: ignore[attr-defined]
    balance, mtm = get_equity(s)
    assert balance == 55.0 and mtm == 53.5
    assert s.cta_engine.main_engine.asked == ["BINANCE_LINEAR.USDT"]  # type: ignore[attr-defined]

    # startup race: account missing -> persisted equity
    class NoAcct(MainEngine):
        def get_account(self, vt_accountid: str) -> Any:
            return None

    s.equity = 48.0
    s.cta_engine.main_engine = NoAcct()  # type: ignore[attr-defined]
    balance, mtm = get_equity(s)
    assert balance == 48.0 and mtm == 46.5


def test_get_equity_okx_derives_wallet_from_eq() -> None:
    """OKX AccountData.balance is ``eq`` (already includes upl): the wallet is eq - sum(upl), never double counted."""
    class Pos:
        def __init__(self, vt_symbol: str, pnl: float) -> None:
            self.vt_symbol = vt_symbol
            self.pnl = pnl
            self.gateway_name = "OKX"

    class Acct:
        balance = 47.0          # eq = 50 wallet - 3 upl

    class Contract:
        gateway_name = "OKX"

    class MainEngine:
        def get_contract(self, vt_symbol: str) -> Any:
            return Contract()

        def get_account(self, vt_accountid: str) -> Any:
            assert vt_accountid == "OKX.USDT"
            return Acct()

        def get_all_positions(self) -> list[Pos]:
            return [Pos("ETHUSDT_SWAP_OKX.GLOBAL", -3.0)]

    class CtaEngine:
        main_engine = MainEngine()

    s = FakeStrategy(engine_type=EngineType.LIVE)
    s.vt_symbol = "ETHUSDT_SWAP_OKX.GLOBAL"
    s.cta_engine = CtaEngine()  # type: ignore[attr-defined]
    balance, mtm = get_equity(s)
    assert balance == pytest.approx(50.0) and mtm == pytest.approx(47.0)


# --------------------------------------------------------------------------- #
# reconcile_live: unknown vs flat, quiet periods
# --------------------------------------------------------------------------- #

class _LiveMain:
    def __init__(self) -> None:
        self.account = True
        self.positions: list[Any] = []

    def get_contract(self, vt_symbol: str) -> Any:
        class C:
            gateway_name = "BINANCE_LINEAR"
        return C()

    def get_account(self, vt_accountid: str) -> Any:
        return object() if self.account else None

    def get_all_positions(self) -> list[Any]:
        return list(self.positions)


class _Pos:
    def __init__(self, volume: float, price: float = 3000.0) -> None:
        self.vt_symbol = VT_SYMBOL
        self.direction = Direction.NET
        self.volume = volume
        self.price = price
        self.pnl = 0.0
        self.gateway_name = "BINANCE_LINEAR"


def _live_guard(vntrader: Path) -> tuple[FakeStrategy, RiskGuard, _LiveMain]:
    s = FakeStrategy(engine_type=EngineType.LIVE)
    main = _LiveMain()

    class Cta:
        main_engine = main
        symbol_strategy_map = {VT_SYMBOL: [s]}

    s.cta_engine = Cta()  # type: ignore[attr-defined]
    g = RiskGuard(s, dials=DIAL, state_dir=vntrader)
    return s, g, main


def test_exchange_net_position_none_without_row(vntrader: Path) -> None:
    s, g, main = _live_guard(vntrader)
    assert risk.exchange_net_position(s) is None
    main.positions = [_Pos(0.0)]
    assert risk.exchange_net_position(s) == (0.0, 0.0)
    main.positions = [_Pos(-0.02, 2990.0)]
    assert risk.exchange_net_position(s) == (-0.02, 2990.0)
    assert risk.account_ready(s)
    main.account = False
    assert not risk.account_ready(s)


def test_reconcile_live_waits_for_account_and_confirms_missing_row(vntrader: Path) -> None:
    s, g, main = _live_guard(vntrader)
    s.pos, s.entry_price, s.stop_price = 0.016, 3000.0, 2940.0
    main.account = False
    assert g.reconcile_live(3000.0, ts=1000.0, force=True) is None
    assert s.pos == 0.016 and any("deferred" in m for m in s.logs)
    main.account = True
    # first "no row" reading: pending, nothing changes
    assert g.reconcile_live(3000.0, ts=1001.0, force=True) is None
    assert s.pos == 0.016 and any("pending confirmation" in m for m in s.logs)
    # too early for the confirmation
    assert g.reconcile_live(3000.0, ts=1001.0 + risk.RECONCILE_SECS - 1, force=True) is None
    assert s.pos == 0.016
    res = g.reconcile_live(3000.0, ts=1001.0 + risk.RECONCILE_SECS, force=True)
    assert res is not None and res.outcome == "clear" and s.pos == 0.0
    # a local flat strategy with no row is simply ok
    res = g.reconcile_live(3000.0, ts=2000.0, force=True)
    assert res is not None and res.outcome == "ok"


def test_reconcile_live_explicit_zero_row_clears_and_quiet_period(vntrader: Path) -> None:
    s, g, main = _live_guard(vntrader)
    s.pos = 0.016
    main.positions = [_Pos(0.016)]
    res = g.reconcile_live(3000.0, ts=1000.0, force=True)
    assert res is not None and res.outcome == "ok"
    main.positions = [_Pos(0.0)]
    t1 = 1000.0 + risk.RECONCILE_SECS + 1              # past the 60 s throttle
    g.note_fill(t1 - 1.0)                              # a fill 1 s ago -> quiet for RECONCILE_QUIET_SECS
    assert g.reconcile_live(3000.0, ts=t1, force=False) is None, "quiet period"
    t2 = t1 + risk.RECONCILE_QUIET_SECS
    s.exit_orderid = "BINANCE_LINEAR.7"
    assert g.reconcile_live(3000.0, ts=t2, force=False) is None, "server order resting"
    s.exit_orderid = ""
    res = g.reconcile_live(3000.0, ts=t2, force=False)
    assert res is not None and res.outcome == "clear" and s.pos == 0.0


def test_is_flat_tolerates_float_residue() -> None:
    s, g = make_guard()
    s.pos = -6.938893903907228e-18
    assert g.is_flat()
    s.pos = 0.001
    assert not g.is_flat()
    assert g._release_lock_if_flat() is False       # nothing locked


# --------------------------------------------------------------------------- #
# exception wrapper and dial fallback
# --------------------------------------------------------------------------- #

def test_safe_call_halts_once_then_propagates(vntrader: Path) -> None:
    s, g = make_guard(state_dir=vntrader)
    s.last_1m_dt = "2026-03-01T12:00:00"

    def boom() -> None:
        raise ValueError("x")

    assert g.safe_call(boom) is None
    assert s.halted and s.halt_reason == "EXCEPTION"
    with pytest.raises(ValueError):
        g.safe_call(boom)
    s.last_1m_dt = "2026-03-01T12:01:00"
    assert g.safe_call(boom) is None                    # new bar: swallowed again


def test_exception_halt_auto_clears_after_a_clean_bar(vntrader: Path) -> None:
    """SPEC 4.9: the EXCEPTION halt is 'for the current bar'; a later clean bar clears it (hard halts stay)."""
    s, g = make_guard(state_dir=vntrader)
    s.last_1m_dt = "2026-03-01T12:00:00"

    def boom() -> None:
        raise ValueError("x")

    def fine() -> int:
        return 1

    assert g.safe_call(boom) is None
    assert s.halted and s.halt_reason == "EXCEPTION"
    assert g.safe_call(fine) == 1
    assert s.halted, "same bar: still halted"
    s.last_1m_dt = "2026-03-01T12:01:00"
    assert g.safe_call(fine) == 1
    assert not s.halted and s.halt_reason == ""
    assert any("auto-cleared" in m for m in s.logs)
    # a hard halt is never auto-cleared
    s.halted, s.halt_reason = True, "REJECTS"
    s.last_1m_dt = "2026-03-01T12:02:00"
    g.safe_call(boom)                                   # sets _exc_bar_key; REJECTS is not downgraded
    s.last_1m_dt = "2026-03-01T12:03:00"
    g.safe_call(fine)
    assert s.halted and s.halt_reason == "REJECTS"


def test_halt_before_start_is_reapplied_on_start() -> None:
    """A halt raised while trading is False (on_init) is remembered and re-applied after the variable restore."""
    s, g = make_guard()
    s.trading = False
    assert g.halt("EXCEPTION")
    assert g.pending_halt == "EXCEPTION"
    s.halted, s.halt_reason = False, ""                 # what CtaEngine._init_strategy restores
    assert g.reapply_pending_halt()
    assert s.halted and s.halt_reason == "EXCEPTION" and g.pending_halt == ""
    assert not g.reapply_pending_halt()


def test_limits_fall_back_to_dials_without_settings() -> None:
    s = FakeStrategy()
    del s.max_trades_day
    del s.cooldown_hours
    g = RiskGuard(s, dials=dict(max_trades_day_s1=7, cooldown_hours=2), state_dir=Path("."))
    assert g.limits.max_trades_day == 7 and g.limits.cooldown_hours == 2.0
    assert g.limits.max_leverage == 3.0                 # from the strategy attribute
    assert isinstance(risk.load_dials("normal"), dict)  # never raises
