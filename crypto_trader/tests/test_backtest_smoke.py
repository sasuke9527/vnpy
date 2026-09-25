"""
Backtest smoke test (SPEC section 1, ``tests/test_backtest_smoke.py``).

Both strategies are replayed through vnpy's ``BacktestingEngine`` on
synthetic 1-minute bars (``synth_data.generate_bars``, trend + chop regimes)
saved into the hermetic sqlite DB of ``tests/conftest.py``.  No network.

Checks:

* no traceback / exception in the engine output or the strategy logs;
* ``DonchianTrendH1`` trades at least once on one of the seeds {1, 2, 3};
* every order volume is an exact multiple of the 0.001 step and every entry
  order is worth at least the 20 USDT minimum notional when it is sent;
* whenever ``pos != 0`` at a bar close a protective stop is active (or a
  limit exit is in flight, which is the only moment the stop is withdrawn);
* the funding post-processing of ``backtest.py`` returns a statistics dict
  with the engine's keys;
* the risk guard works inside the backtester: with ``daily_loss_pct=0.001``
  the daily-loss halt fires and no new entry is filled for the rest of that
  UTC day.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import vnpy.trader.utility  # noqa: F401  (resolve TEMP_DIR before backtest.py's bootstrap)
from vnpy.trader.constant import Direction, Exchange, Interval, Offset
from vnpy.trader.object import BarData, OrderData
from vnpy_ctastrategy.backtesting import BacktestingEngine, load_bar_data
from vnpy_ctastrategy.base import STOPORDER_PREFIX
from vnpy_ctastrategy.template import CtaTemplate

import backtest as bt
from strategies.donchian_trend_h1 import DonchianTrendH1
from strategies.squeeze_break_15m import SqueezeBreak15M
from synth_data import generate_bars
from tests.conftest import SYNTH_DAYS, SYNTH_START, SYNTH_SYMBOL

VT_SYMBOL = f"{SYNTH_SYMBOL}.GLOBAL"
STEP = 0.001
MIN_NOTIONAL = 20.0
WARMUP_DAYS = 8
CAPITAL = 50.0
RATE = 0.0005
SLIPPAGE = 1.0
PRICETICK = 0.01

#: Extra seeds (start dates keep them apart in the shared DB; same symbol).
SEED_STARTS: dict[int, datetime] = {
    1: SYNTH_START,
    2: datetime(2026, 3, 1, tzinfo=timezone.utc),
    3: datetime(2026, 5, 1, tzinfo=timezone.utc),
}

STAT_KEYS = ("total_net_pnl", "end_balance", "max_ddpercent", "sharpe_ratio", "total_trade_count",
             "total_commission", "total_slippage", "total_return", "annual_return", "profit_days", "loss_days")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _save_seed(db: Any, seed: int) -> datetime:
    """Save ``SYNTH_DAYS`` of seed ``seed`` bars into the DB; return the start."""
    start = SEED_STARTS[seed]
    n = SYNTH_DAYS * 24 * 60
    bars = generate_bars(SYNTH_SYMBOL, Exchange.GLOBAL, Interval.MINUTE, start, n, seed=seed)
    db.save_bar_data(copy.deepcopy(bars))
    load_bar_data.cache_clear()
    return start


class Run:
    """One engine run with captured output, per-bar invariants and order snapshots."""

    def __init__(self, engine: BacktestingEngine, strategy: CtaTemplate) -> None:
        self.engine = engine
        self.strategy = strategy
        self.output: list[str] = []
        self.violations: list[str] = []
        self.bars_with_pos: int = 0
        self.stop_seen: int = 0
        self.orders_at_send: list[tuple[str, str, float, float, float]] = []   # (id, offset, price, vol, notional)

    @property
    def logs(self) -> list[str]:
        return list(self.engine.logs) + self.output


def _run(strategy_class: type[CtaTemplate], start: datetime, setting: dict[str, Any] | None = None,
         days: int = SYNTH_DAYS) -> Run:
    """Replay ``strategy_class`` from ``start + WARMUP_DAYS`` to ``start + days - 1``."""
    engine = BacktestingEngine()
    run_start = (start + timedelta(days=WARMUP_DAYS)).replace(tzinfo=None)
    run_end = (start + timedelta(days=days - 1)).replace(tzinfo=None)
    engine.set_parameters(vt_symbol=VT_SYMBOL, interval=Interval.MINUTE, start=run_start, end=run_end,
                          rate=RATE, slippage=SLIPPAGE, size=1, pricetick=PRICETICK, capital=CAPITAL,
                          annual_days=365)
    base: dict[str, Any] = {"capital": CAPITAL, "fee_rate": RATE, "risk_dial": "normal", "warmup_days": WARMUP_DAYS}
    base.update(setting or {})
    engine.add_strategy(strategy_class, base)
    run = Run(engine, engine.strategy)
    engine.output = run.output.append  # type: ignore[method-assign]

    # record every order at send time (volume / notional as the strategy sent it)
    orig_send: Callable[..., list[str]] = engine.send_order

    def send_order(strategy: CtaTemplate, direction: Any, offset: Any, price: float, volume: float,
                   stop: bool, lock: bool, net: bool) -> list[str]:
        ids = orig_send(strategy, direction, offset, price, volume, stop, lock, net)
        run.orders_at_send.append((ids[0], offset.value, float(price), float(volume), float(price) * float(volume)))
        return ids

    engine.send_order = send_order  # type: ignore[method-assign]

    # per-bar invariant: pos != 0 -> protective stop active, or a limit exit in flight
    orig_new_bar = engine.new_bar

    def new_bar(bar: BarData) -> None:
        orig_new_bar(bar)
        s = run.strategy
        if s.pos == 0:
            return
        run.bars_with_pos += 1
        stop_ok = bool(s.stop_orderid) and s.stop_orderid in engine.active_stop_orders
        exit_ok = bool(s.exit_orderid) and (s.exit_orderid in engine.active_limit_orders)
        if stop_ok:
            run.stop_seen += 1
        elif not exit_ok:
            run.violations.append(f"{bar.datetime} pos={s.pos} stop_orderid={s.stop_orderid!r} "
                                  f"exit_orderid={s.exit_orderid!r}")

    engine.new_bar = new_bar  # type: ignore[method-assign]

    engine.load_data()
    assert engine.history_data, "no synthetic bars loaded"
    engine.run_backtesting()
    return run


def _assert_clean(run: Run) -> None:
    bad = [line for line in run.logs if "Traceback" in line or "exception" in line.lower() or "触发异常" in line]
    assert not bad, "exception in logs:\n" + "\n".join(bad[:5])
    assert run.strategy.halt_reason != "EXCEPTION"


def _assert_orders(run: Run, all_orders_min_notional: bool = False) -> None:
    for oid, offset, _price, vol, notional in run.orders_at_send:
        units = vol / STEP
        assert abs(units - round(units)) < 1e-6, f"order {oid} volume {vol} is not a multiple of {STEP}"
        assert vol > 0
        if offset == Offset.OPEN.value or all_orders_min_notional:
            assert notional >= MIN_NOTIONAL - 1e-9, f"order {oid} ({offset}) notional {notional:.2f} < {MIN_NOTIONAL}"
    # engine-side records agree with what was sent
    for order in run.engine.get_all_orders():
        o: OrderData = order
        units = o.volume / STEP
        assert abs(units - round(units)) < 1e-6
    for so in run.engine.stop_orders.values():
        assert so.stop_orderid.startswith(STOPORDER_PREFIX)
        units = so.volume / STEP
        assert abs(units - round(units)) < 1e-6


def _funded_stats(run: Run) -> dict[str, Any]:
    df = run.engine.calculate_result()
    assert df is not None and not df.empty
    raw = run.engine.calculate_statistics(df, output=False)
    history = list(run.engine.history_data)
    trades = sorted(run.engine.get_all_trades(), key=lambda t: t.datetime)
    funding = bt.compute_funding(trades, history, 1.0, {}, 1.0, bt.FUNDING_FALLBACK_RATE)
    _df2, stats = bt.apply_funding(run.engine, df, funding)
    for key in STAT_KEYS:
        assert key in raw and key in stats, f"missing statistic {key}"
    if trades:
        assert funding.stamps_charged >= 0
        assert not math.isnan(float(stats["total_net_pnl"]))
    return stats


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def s1_run(synth_db: Any) -> Run:
    """DonchianTrendH1 on the first seed of {1, 2, 3} that produces a trade."""
    last: Run | None = None
    for seed in (1, 2, 3):
        start = SYNTH_START if seed == 1 else _save_seed(synth_db, seed)
        run = _run(DonchianTrendH1, start)
        _assert_clean(run)
        last = run
        if run.engine.get_all_trades():
            return run
    assert last is not None
    pytest.fail("DonchianTrendH1 produced no trade on seeds 1, 2 and 3")


def test_donchian_runs_clean_and_trades(s1_run: Run) -> None:
    _assert_clean(s1_run)
    trades = s1_run.engine.get_all_trades()
    assert trades, "expected at least one fill"
    assert s1_run.strategy.trade_log or s1_run.strategy.pos != 0
    assert not s1_run.strategy.halted or s1_run.strategy.halt_reason in ("DAILY_LOSS", "DRAWDOWN")


def test_donchian_order_sizes(s1_run: Run) -> None:
    _assert_orders(s1_run)
    opens = [o for o in s1_run.orders_at_send if o[1] == Offset.OPEN.value]
    assert opens, "no entry order was sent"


def test_donchian_stop_always_present(s1_run: Run) -> None:
    assert s1_run.bars_with_pos > 0
    assert s1_run.stop_seen > 0, "a protective stop was never active while in position"
    assert not s1_run.violations, "bars in position without stop/exit:\n" + "\n".join(s1_run.violations[:10])


def test_donchian_funded_statistics(s1_run: Run) -> None:
    stats = _funded_stats(s1_run)
    assert int(stats["total_trade_count"]) == len(s1_run.engine.get_all_trades())
    # every round trip in the strategy's own log carries a risk figure for the R statistics
    for row in s1_run.strategy.trade_log:
        assert row["risk_usd"] > 0
        assert set(row) >= {"entry_ts", "exit_ts", "pnl", "r", "reason"}


def test_squeeze_runs_clean(synth_db: Any) -> None:
    run = _run(SqueezeBreak15M, SYNTH_START, {"warmup_days": 5})
    _assert_clean(run)
    _assert_orders(run)
    assert not run.violations, "\n".join(run.violations[:10])
    assert not run.strategy.halted or run.strategy.halt_reason in ("DAILY_LOSS", "DRAWDOWN")
    if run.engine.get_all_trades():
        _funded_stats(run)
        for row in run.strategy.trade_log:
            assert row["r"] > 0


def test_daily_loss_halt_blocks_entries(synth_db: Any, s1_run: Run) -> None:
    """Inject a 0.1 % daily-loss dial: after the halt no entry fills for the rest of the UTC day."""
    start = (s1_run.engine.start - timedelta(days=WARMUP_DAYS)).replace(tzinfo=timezone.utc)
    dial = {"risk_dial": "custom", "risk_pct": 0.02, "max_leverage": 3.0, "gross_leverage": 4.0,
            "daily_loss_pct": 0.001, "max_dd_halt": 0.25, "max_consec_losses": 4, "cooldown_hours": 8.0,
            "max_trades_day": 3}
    run = _run(DonchianTrendH1, start, dial)
    _assert_clean(run)
    logs = run.engine.logs
    halts: list[datetime] = []
    for line in logs:
        if "daily loss:" in line:
            stamp = line.split("\t", 1)[0]
            halts.append(datetime.fromisoformat(stamp))
    assert halts, "daily-loss halt never fired with daily_loss_pct=0.001"
    assert any("HALT DAILY_LOSS" in line for line in logs)

    trades = sorted(run.engine.get_all_trades(), key=lambda t: t.datetime)
    pos = 0.0
    opens: list[datetime] = []
    for t in trades:
        signed = t.volume if t.direction == Direction.LONG else -t.volume
        if pos == 0 or (pos > 0) == (signed > 0):
            opens.append(t.datetime)
        pos += signed
    for h in halts:
        same_day_after = [o for o in opens if o.date() == h.date() and o > h]
        assert not same_day_after, f"entry filled after the daily-loss halt at {h}: {same_day_after}"
    # the halt is a soft one: it clears on the next UTC day
    assert run.strategy.halt_reason in ("", "DAILY_LOSS")
