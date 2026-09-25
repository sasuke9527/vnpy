"""
Strategy 1 - DonchianTrendH1 (SPEC section 5).

1h Donchian breakout in the direction of an EMA/ADX regime, managed on 15m
bars, fed by 1m bars (BacktestingEngine, or the Binance kline stream in
live; ticks -> BarGenerator otherwise).  Entries are engine-local stop
orders at the breakout trigger, the protective stop is an engine-local stop
that is always present while a position is open, other exits are chased
limit orders that never give up until the position is flat.

All risk / order mechanics are delegated to ``risk.RiskGuard`` and
``sizing.calc_volume``; this file only holds the signal logic and the
order-state machine.  Persisted state is limited to ``variables`` (JSON
scalars); order ids are treated as stale after a restart.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from vnpy.trader.constant import Direction, Exchange, Interval, Status
from vnpy.trader.object import BarData, OrderData, TickData, TradeData
from vnpy.trader.utility import ArrayManager, BarGenerator, round_to
from vnpy_ctastrategy import CtaTemplate, StopOrder
from vnpy_ctastrategy.base import STOPORDER_PREFIX, EngineType, StopOrderStatus

from risk import RECONCILE_QUIET_SECS, RiskDecision, RiskGuard, RiskLimits, get_equity, guarded, ts_of
from settings import DIALS, base_from_vt_symbol, contract_name_for, exchange_from_vt_symbol, lot_info_for
from sizing import LotInfo, calc_volume, fee_rt, min_stop_pct

CHASE_SECS: float = 5.0        # re-price an unfilled limit exit after this long
CHASE_STEP: float = 0.001      # 0.1 % worse per chase step (6 steps) ...
CHASE_MAX: int = 6
CHASE_SLOW_SECS: float = 30.0  # ... then every 30 s at
CHASE_SLOW_PCT: float = 0.005  # 0.5 % through the book until flat
PANIC_SECS: float = 30.0
PANIC_TRIGGER: float = 0.002
PANIC_PCT: float = 0.003
ENTRY_LIMIT_PCT: float = 0.001
#: After an exit is REJECTED (reduce-only on a position that is gone, price
#: filter, ...) wait for the forced reconcile before retrying; the
#: protective stop is re-armed meanwhile so the position is never naked.
EXIT_REJECT_BACKOFF_SECS: float = RECONCILE_QUIET_SECS


class DonchianTrendH1(CtaTemplate):
    """1h Donchian breakout with EMA/ADX regime filter, 15m management."""

    author = "crypto_trader"

    # -- risk / fee parameters (dial keys overwritten by ``risk_dial``) ----
    risk_dial: str = "normal"
    risk_pct: float = 0.02
    max_leverage: float = 3.0
    gross_leverage: float = 4.0
    daily_loss_pct: float = 0.06
    max_dd_halt: float = 0.25
    max_consec_losses: int = 4
    cooldown_hours: float = 8.0
    max_trades_day: int = 3
    fee_rate: float = 0.0005
    slippage_pct: float = 0.0003
    fee_gate_mult: float = 3.0
    min_stop_pct: float = 0.004
    lot_tolerance: float = 1.5
    min_notional: float = 0.0          # 0 -> exchange_filters.json / fallback table
    # -- signal parameters --------------------------------------------------
    entry_window: int = 24
    ema_fast_n: int = 20
    ema_slow_n: int = 60
    atr_n: int = 14
    adx_n: int = 14
    adx_min: float = 15.0
    max_ext: float = 3.0
    entry_buf: float = 0.1
    chase_atr: float = 0.5
    stop_mult: float = 2.0
    trail_mult: float = 2.5
    time_stop_bars: int = 24
    mfe_min_atr: float = 1.0
    max_hold_bars: int = 240
    reentry_cooldown_bars: int = 4
    capital: float = 50.0
    warmup_days: int = 8
    use_kline_stream: bool = True

    parameters = [
        "risk_dial", "risk_pct", "max_leverage", "gross_leverage", "daily_loss_pct", "max_dd_halt",
        "max_consec_losses", "cooldown_hours", "max_trades_day", "fee_rate", "slippage_pct", "fee_gate_mult",
        "min_stop_pct", "lot_tolerance", "min_notional", "entry_window", "ema_fast_n", "ema_slow_n", "atr_n",
        "adx_n", "adx_min", "max_ext", "entry_buf", "chase_atr", "stop_mult", "trail_mult", "time_stop_bars",
        "mfe_min_atr", "max_hold_bars", "reentry_cooldown_bars", "capital", "warmup_days", "use_kline_stream",
    ]

    # -- persisted variables (JSON scalars only) ----------------------------
    equity: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    entry_price: float = 0.0
    stop_price: float = 0.0
    highest_since_entry: float = 0.0
    lowest_since_entry: float = 0.0
    entry_bar_ts: float = 0.0
    bars_in_trade: int = 0
    last_1m_dt: str = ""
    direction_blocked: int = 0
    block_until_ts: float = 0.0
    day_key: str = ""
    day_start_equity: float = 0.0
    peak_equity: float = 0.0
    consec_losses: int = 0
    cooldown_until: float = 0.0
    trades_today: int = 0
    reject_count: int = 0
    halted: bool = False
    halt_reason: str = ""
    entry_orderid: str = ""
    stop_orderid: str = ""
    exit_orderid: str = ""
    last_reconcile_ts: float = 0.0

    variables = [
        "equity", "realized_pnl", "fees_paid", "entry_price", "stop_price", "highest_since_entry",
        "lowest_since_entry", "entry_bar_ts", "bars_in_trade", "last_1m_dt", "direction_blocked",
        "block_until_ts", "day_key", "day_start_equity", "peak_equity", "consec_losses", "cooldown_until",
        "trades_today", "reject_count", "halted", "halt_reason", "entry_orderid", "stop_orderid",
        "exit_orderid", "last_reconcile_ts",
    ]

    def __init__(self, cta_engine: Any, strategy_name: str, vt_symbol: str, setting: dict) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.apply_dial()
        self.live: bool = self.get_engine_type() != EngineType.BACKTESTING
        self.guard: RiskGuard = RiskGuard(self, suffix="s1")
        # contract facts (filled in on_start)
        self.size: float = 0.0
        self.pricetick: float = 0.0
        self.min_volume: float = 0.0
        self.lot: LotInfo = LotInfo()
        self.kline_mode: bool = False
        # bars / indicators
        self._build_generators()
        self.last_close: float = 0.0
        self.atr: float = 0.0
        self.ema_fast_v: float = 0.0
        self.ema_slow_v: float = 0.0
        self.adx_v: float = 0.0
        self.signal_dir: int = 0
        self.trigger: float = 0.0
        self.entry_vol: float = 0.0
        # order-state machine (never persisted)
        self._armed_trigger: float = 0.0
        self._armed_vol: float = 0.0
        self._exit_reason: str = ""
        self._exit_from_stop: bool = False
        self._exit_sent_ts: float = 0.0
        self._chase_count: int = 0
        self._entry_risk_usd: float = 0.0
        self._trade_pnl: float = 0.0
        self._zero_size_trigger: float = 0.0
        self._now_ts: float = 0.0      # time of the latest 1m bar / tick (window bars carry their start time)
        self._first_tick: bool = True
        self._lots_checked: bool = False
        self._last_panic_ts: float = 0.0
        self._tick: TickData | None = None
        self._cancel_pending: str = ""   # exit order id whose cancel was sent but not yet confirmed
        self._exit_reject_ts: float = -1e18   # last exit REJECTED: back off before resending
        self._reconcile_due: float = 0.0      # forced reconcile after a fill / exit reject (live)
        self._trade_volume: float = 0.0  # volume closed in the current round trip (for slippage in R stats)
        self.warmup_ok: bool = False     # set at the end of on_init; the runner refuses to start without it
        self.trade_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # float-safe position helpers
    # ------------------------------------------------------------------ #
    def _step(self) -> float:
        return self.min_volume or self.lot.step or 0.001

    def _is_flat(self, pos: float | None = None) -> bool:
        """``pos`` (default: own) is zero within half a volume step (float residue after partial fills)."""
        value = float(self.pos) if pos is None else float(pos)
        return bool(abs(value) <= self._step() / 2.0)

    def _order_volume(self) -> float:
        """``abs(pos)`` rounded to the step; 0.0 when below one step (never send a zero-quantity order)."""
        vol = round_to(abs(self.pos), self._step())
        return vol if vol >= self._step() - 1e-12 else 0.0

    # ------------------------------------------------------------------ #
    # setup helpers
    # ------------------------------------------------------------------ #
    def apply_dial(self) -> None:
        """Overwrite the risk keys from ``settings.DIALS`` unless ``risk_dial == "custom"``."""
        if self.risk_dial == "custom":
            return
        dial = DIALS.get(self.risk_dial)
        if dial is None:
            self.write_log(f"unknown risk_dial {self.risk_dial!r}; keeping explicit parameters")
            return
        self.risk_pct = float(dial["risk_pct_s1"])
        self.max_leverage = float(dial["max_leverage"])
        self.gross_leverage = float(dial["gross_leverage"])
        self.daily_loss_pct = float(dial["daily_loss_pct"])
        self.max_dd_halt = float(dial["max_dd_halt"])
        self.max_consec_losses = int(dial["max_consec_losses"])
        self.cooldown_hours = float(dial["cooldown_hours"])
        self.max_trades_day = int(dial["max_trades_day_s1"])

    def _build_generators(self) -> None:
        noop = self._noop
        self.am: ArrayManager = ArrayManager(size=150)
        self.bg1: BarGenerator = BarGenerator(self.on_bar)
        self.bg_sig: BarGenerator = BarGenerator(noop, 1, self.on_hour_bar, Interval.HOUR)
        self.bg_mgmt: BarGenerator = BarGenerator(noop, 15, self.on_15m_bar, Interval.MINUTE)

    @staticmethod
    def _noop(bar: BarData) -> None:
        return

    def _setup_contract(self) -> None:
        """Read size/pricetick/step from the contract (live) or the engine + filters file."""
        name = ""
        gateway = ""
        if self.live:
            contract = self.cta_engine.main_engine.get_contract(self.vt_symbol)
            if contract:
                self.size, self.pricetick, self.min_volume = float(contract.size), float(contract.pricetick), float(contract.min_volume)
                name, gateway = str(contract.name), str(contract.gateway_name)
        else:
            self.size = float(self.get_size() or 1.0)
            self.pricetick = float(self.get_pricetick() or 0.0)
        if not name:
            try:
                name = contract_name_for(exchange_from_vt_symbol(self.vt_symbol), base_from_vt_symbol(self.vt_symbol))
            except ValueError:
                name = base_from_vt_symbol(self.vt_symbol)
        self.lot = lot_info_for(name, size=self.size or 1.0, pricetick=self.pricetick or None,
                                min_volume=self.min_volume or None)
        if self.min_notional > 0:
            self.lot = replace(self.lot, min_notional=float(self.min_notional))
        self.size, self.pricetick, self.min_volume = self.lot.size, self.lot.pricetick, self.lot.step
        self.kline_mode = self.live and bool(self.use_kline_stream) and gateway.upper().startswith("BINANCE")
        self.write_log(f"contract {name}: size={self.size} pricetick={self.pricetick} step={self.min_volume} "
                       f"min_notional={self.lot.min_notional} kline_mode={self.kline_mode}")

    def _balance(self, close: float) -> tuple[float, float]:
        """(balance, equity_mtm); falls back to ``capital`` when nothing is known yet."""
        balance, equity = get_equity(self, close)
        if balance <= 0:
            balance = equity = float(self.capital)
        return balance, equity

    def _check_lots(self, price: float) -> None:
        """SPEC section 3: refuse a symbol whose minimum lot is too coarse for the account."""
        if self._lots_checked or price <= 0:
            return
        self._lots_checked = True
        balance, _ = self._balance(price)
        n = self.lot.lots_max(balance, self.max_leverage, price)
        if n < 4:
            self.write_log(f"lot_notional {self.lot.lot_notional(price):.2f} USDT -> lots_max={n} < 4; refusing symbol")
            self.guard.halt("COARSE_LOTS")

    def _msp(self) -> float:
        return min_stop_pct(self.min_stop_pct, self.fee_gate_mult, fee_rt(self.fee_rate, self.slippage_pct))

    def _initial_stop(self, entry: float, direction: int) -> float:
        dist = max(self.stop_mult * self.atr, self._msp() * entry)
        return round_to(entry - dist if direction > 0 else entry + dist, self.pricetick or 0.01)

    # ------------------------------------------------------------------ #
    # lifecycle callbacks
    # ------------------------------------------------------------------ #
    @guarded
    def on_init(self) -> None:
        self.write_log("on_init")
        self.warmup_ok = False
        self.apply_dial()
        self.guard.limits = RiskLimits.from_strategy(self, suffix="s1")
        if self.equity <= 0:
            self.equity = float(self.capital)
        self._build_generators()
        self.load_bar(int(self.warmup_days), Interval.MINUTE)   # trading is False: no orders
        self.warmup_ok = bool(self.am.inited and self.atr > 0)
        if not self.warmup_ok:
            self.write_log(f"warmup incomplete: am.inited={self.am.inited} atr={self.atr}")

    @guarded
    def on_start(self) -> None:
        self._setup_contract()
        if self.live:
            # Ids are stale after a process restart (the runner cancelled the
            # server orphans) but NOT after an in-process re-init by the
            # watchdog: engine-local stops registered under this strategy
            # name would still fire.  Cancel everything the engine tracks.
            self._cancel_engine_orders()
            self.entry_orderid = self.stop_orderid = self.exit_orderid = ""
            self._cancel_pending = ""
            self._first_tick = True
        self.guard.reapply_pending_halt()   # e.g. an EXCEPTION halt raised inside on_init
        self._check_lots(self.last_close)
        self.write_log(f"on_start pos={self.pos} equity={self.equity} halted={self.halted}/{self.halt_reason} "
                       f"warmup_ok={self.warmup_ok}")

    def _cancel_engine_orders(self) -> None:
        """Cancel every order the CTA engine still tracks for this strategy (works while trading is False)."""
        try:
            tracked = self.cta_engine.strategy_orderid_map.get(self.strategy_name, set())
        except AttributeError:
            return
        for oid in list(tracked):
            self.write_log(f"cancelling order {oid} left from a previous run of this strategy")
            self.cta_engine.cancel_order(self, oid)

    @guarded
    def on_stop(self) -> None:
        if not self._is_flat():
            self.write_log(f"on_stop with open position {self.pos}: local stop dies with the process")
        self.sync_data()

    @guarded
    def on_tick(self, tick: TickData) -> None:
        if not tick.last_price:
            return
        ts = ts_of(tick.datetime)
        self._now_ts = ts
        self.last_close = float(tick.last_price)
        self._tick = tick
        if self._first_tick and self.trading:
            self._first_tick = False
            self._on_first_tick(tick, ts)
        if self.trading:
            # Reconcile BEFORE the 1m bar / chase logic so a position the
            # exchange no longer holds is cleared before anything resends for it.
            force = 0 < self._reconcile_due <= ts
            if force:
                self._reconcile_due = 0.0
            self._reconcile(tick.last_price, ts, force=force)
        bar = tick.extra.get("bar") if tick.extra else None
        if self.kline_mode:
            if bar is not None and bar.datetime.isoformat() != self.last_1m_dt:
                bar.symbol = self.vt_symbol.split(".")[0]
                bar.exchange = Exchange.GLOBAL
                bar.vt_symbol = self.vt_symbol
                self.on_bar(bar)
        else:
            self.bg1.update_tick(tick)
        if not self.trading:
            return
        balance, equity = self._balance(tick.last_price)
        decision = self.guard.tick(tick, equity, balance)
        if decision is not None:
            self._apply_decision(decision, tick.last_price, ts)
        if not self._is_flat():
            if self._exit_reason:
                self._chase_exit(ts, tick.last_price)
            else:
                self._panic_check(tick, ts)
                self._ensure_stop(ts)
        self.guard.heartbeat(ts)

    @guarded
    def on_bar(self, bar: BarData) -> None:
        """1m bar: signal generator first, then management, then the 1m protective checks."""
        self.last_close = float(bar.close_price)
        self.last_1m_dt = bar.datetime.isoformat()
        self._now_ts = ts_of(bar.datetime)
        self.bg_sig.update_bar(bar)
        self.bg_mgmt.update_bar(bar)
        self.on_1m(bar)

    def on_hour_bar(self, bar: BarData) -> None:
        am = self.am
        am.update_bar(bar)
        if not am.inited:
            return
        close = float(bar.close_price)
        ema_fast = float(am.ema(self.ema_fast_n))
        ema_slow_arr = am.ema(self.ema_slow_n, array=True)
        ema_slow, ema_slow_prev = float(ema_slow_arr[-1]), float(ema_slow_arr[-5])
        atr = float(am.atr(self.atr_n))
        adx = float(am.adx(self.adx_n))
        w = int(self.entry_window)
        dc_up = float(max(am.high[-(w + 1):-1]))
        dc_dn = float(min(am.low[-(w + 1):-1]))
        if not (atr > 0) or any(math.isnan(x) for x in (ema_fast, ema_slow, ema_slow_prev, adx)):
            return
        self.atr, self.ema_fast_v, self.ema_slow_v, self.adx_v = atr, ema_fast, ema_slow, adx
        ext = (close - ema_fast) / atr
        vol_ok = 0.002 <= atr / close <= 0.06
        common = adx >= self.adx_min and abs(ext) <= self.max_ext and vol_ok
        long_ok = common and ema_fast > ema_slow and ema_slow > ema_slow_prev and close > ema_slow
        short_ok = common and ema_fast < ema_slow and ema_slow < ema_slow_prev and close < ema_slow
        if self.live and self.trading:   # shadow-phase comparison against backtest values
            self.write_log(f"1h {bar.datetime.isoformat()} c={close} ef={ema_fast:.2f} es={ema_slow:.2f} "
                           f"atr={atr:.2f} adx={adx:.1f} dc={dc_dn:.2f}/{dc_up:.2f} L={long_ok} S={short_ok}")
        if not self.trading:
            return
        ts = self._now_ts or ts_of(bar.datetime)
        if not self._is_flat():
            self.signal_dir = 0
            self._manage_position_1h(close, ema_fast, ema_slow, atr, ts)
        else:
            self.signal_dir = 1 if long_ok else (-1 if short_ok else 0)
            if self.signal_dir > 0:
                self.trigger = round_to(dc_up + self.entry_buf * atr, self.pricetick or 0.01)
            elif self.signal_dir < 0:
                self.trigger = round_to(dc_dn - self.entry_buf * atr, self.pricetick or 0.01)
            # BarGenerator normally closes the hour window on the :59 bar,
            # before the 15m window of that same bar (so on_15m_bar manages
            # the fresh trigger).  When the :59 bar is missing (kline-stream
            # gap) the hour closes on the :00 bar instead and a stale trigger
            # / direction would stay armed until :14: manage the entry here
            # too, with a fresh risk decision (idempotent for the normal case).
            if not self._exit_reason:
                balance, equity = self._balance(close)
                decision = self.guard.pre_bar(bar, equity, balance)
                self._apply_decision(decision, close, ts)
                self._manage_entry(close, ts, decision, balance)
        self.sync_data()

    def _manage_position_1h(self, close: float, ema_fast: float, ema_slow: float, atr: float, ts: float) -> None:
        self.bars_in_trade = int(self.bars_in_trade) + 1
        tick = self.pricetick or 0.01
        if self.pos > 0:
            new_stop = round_to(max(self.stop_price, self.highest_since_entry - self.trail_mult * atr), tick)
            mfe = self.highest_since_entry - self.entry_price
            flip = ema_fast < ema_slow
        else:
            floor = self.stop_price if self.stop_price > 0 else float("inf")
            new_stop = round_to(min(floor, self.lowest_since_entry + self.trail_mult * atr), tick)
            mfe = self.entry_price - self.lowest_since_entry
            flip = ema_fast > ema_slow
        if self.stop_price <= 0 or abs(new_stop - self.stop_price) >= tick - 1e-12:
            self.stop_price = new_stop
            self._arm_stop(ts)
        reason = ""
        if flip:
            reason = "REGIME"
        elif self.bars_in_trade >= self.time_stop_bars and mfe < self.mfe_min_atr * atr:
            reason = "TIME"
        elif self.bars_in_trade >= self.max_hold_bars:
            reason = "MAXHOLD"
        if reason:
            self._request_exit(reason, close, ts)

    def on_15m_bar(self, bar: BarData) -> None:
        if not self.trading:
            return
        ts = self._now_ts or ts_of(bar.datetime)
        close = float(bar.close_price)
        balance, equity = self._balance(close)
        decision = self.guard.pre_bar(bar, equity, balance)
        self._apply_decision(decision, close, ts)
        if self.entry_orderid and not self.entry_orderid.startswith(STOPORDER_PREFIX):
            self._cancel_entry("chase limit unfilled at management bar")
        if self._is_flat() and not self._exit_reason:
            self._manage_entry(close, ts, decision, balance)
        self.sync_data()

    def on_1m(self, bar: BarData) -> None:
        if not self.trading:
            return
        ts = ts_of(bar.datetime)
        self._check_lots(float(bar.close_price))
        if not self._is_flat():
            self.highest_since_entry = max(self.highest_since_entry, float(bar.high_price))
            self.lowest_since_entry = min(self.lowest_since_entry or float(bar.low_price), float(bar.low_price))
            if self._exit_reason:
                self._chase_exit(ts, float(bar.close_price))
            else:
                self._ensure_stop(ts)
        else:
            self._exit_reason = ""
            if self.exit_orderid:      # flat: a live exit order could open a reverse position
                self.cancel_order(self.exit_orderid)

    # ------------------------------------------------------------------ #
    # risk decisions, reconcile, first tick
    # ------------------------------------------------------------------ #
    def _apply_decision(self, d: RiskDecision, price: float, ts: float) -> None:
        if d.cancel_entries and self.entry_orderid:
            self._cancel_entry(d.reason)
        if d.must_flatten and not self._is_flat():
            self._request_exit(f"RISK:{d.reason}", price, ts)

    def _reconcile(self, last_price: float, ts: float, force: bool) -> None:
        old_stop, old_entry, old_exit = self.stop_orderid, self.entry_orderid, self.exit_orderid
        saved_stop = self.stop_price
        res = self.guard.reconcile_live(last_price, ts, force=force)
        if res is None or res.outcome == "ok":
            return
        # The guard dropped the ids; the engine still holds the orders.  A
        # server exit left resting after a 'clear' would open a reverse
        # position when it fills; a stale stop would fire for the wrong size.
        for oid in (old_stop, old_entry, old_exit):
            if oid:
                self.cancel_order(oid)
        self.entry_orderid = self.stop_orderid = self.exit_orderid = ""
        self._cancel_pending = ""
        if res.outcome == "adopt" and not self._is_flat():
            direction = 1 if self.pos > 0 else -1
            if self.atr > 0:
                self.stop_price = self._initial_stop(self.entry_price, direction)
            elif saved_stop > 0 and (saved_stop < self.entry_price) == (direction > 0):
                self.stop_price = saved_stop     # no ATR yet (warmup failed): keep the persisted stop
                self.write_log(f"adopt without ATR: keeping persisted stop {saved_stop}")
            self._ensure_stop(ts)
        elif res.outcome == "clear":
            self._exit_reason = ""
            self._exit_from_stop = False
        self.sync_data()

    def _on_first_tick(self, tick: TickData, ts: float) -> None:
        self._reconcile(float(tick.last_price), ts, force=True)
        if not self._is_flat():
            self.highest_since_entry = max(self.highest_since_entry, float(tick.last_price))
            self.lowest_since_entry = min(self.lowest_since_entry or float(tick.last_price), float(tick.last_price))
            if self.stop_price <= 0 and self.entry_price > 0 and self.atr > 0:
                self.stop_price = self._initial_stop(self.entry_price, 1 if self.pos > 0 else -1)
            self._ensure_stop(ts)
        self.guard.check_flags()
        self.sync_data()

    # ------------------------------------------------------------------ #
    # entries
    # ------------------------------------------------------------------ #
    def _entry_volume(self, price: float, balance: float) -> float:
        return calc_volume(
            balance, self.risk_pct, price, self.stop_mult * self.atr, self.lot, self.max_leverage,
            self.guard.free_notional(balance), self.guard.streak_mult(), self.lot_tolerance,
            self.min_stop_pct, self.fee_gate_mult, fee_rt(self.fee_rate, self.slippage_pct),
        )

    def _manage_entry(self, close: float, ts: float, d: RiskDecision, balance: float) -> None:
        blocked = self.direction_blocked == self.signal_dir and ts < self.block_until_ts
        if not d.allow_entry or self.guard.halted or self.signal_dir == 0 or self.atr <= 0 or blocked:
            if self.entry_orderid:
                self._cancel_entry("entry no longer allowed")
            return
        vol = self._entry_volume(self.trigger, balance)
        if vol <= 0:
            if self._zero_size_trigger != self.trigger:   # log once per 1h trigger, not every 15m
                self._zero_size_trigger = self.trigger
                self.write_log(f"sizing returned 0 at trigger {self.trigger} (balance {balance:.2f}); skip")
            self._cancel_entry("no size")
            return
        self.entry_vol = vol
        long = self.signal_dir > 0
        direction = Direction.LONG if long else Direction.SHORT
        beyond = close > self.trigger if long else close < self.trigger
        if beyond:
            if self.entry_orderid and self.entry_orderid.startswith(STOPORDER_PREFIX):
                self._cancel_entry("close already beyond trigger")
            within = close <= self.trigger + self.chase_atr * self.atr if long else close >= self.trigger - self.chase_atr * self.atr
            if not within or self.entry_orderid:
                return
            price = round_to(close * (1 + ENTRY_LIMIT_PCT) if long else close * (1 - ENTRY_LIMIT_PCT), self.pricetick)
            self._send_entry(direction, price, vol, close, balance, ts, stop=False)
            return
        if self.entry_orderid:
            if self._armed_trigger == self.trigger and self._armed_vol == vol:
                return
            self._cancel_entry("trigger/volume changed")
            if self.entry_orderid:   # server limit cancel is async: retry next bar
                return
        self._send_entry(direction, self.trigger, vol, close, balance, ts, stop=True)

    def _send_entry(self, direction: Direction, price: float, vol: float, close: float, balance: float,
                    ts: float, stop: bool) -> None:
        ok, why = self.guard.check_order("entry", direction, price, vol, balance, last_price=close, is_stop=stop,
                                         ref_close=close, min_notional=self.lot.min_notional, ts=ts)
        if not ok:
            self.write_log(f"entry skipped: {why}")
            return
        ids = self.buy(price, vol, stop=stop) if direction == Direction.LONG else self.short(price, vol, stop=stop)
        if not ids:
            return
        self.entry_orderid = ids[0]
        self._armed_trigger, self._armed_vol = price if stop else self.trigger, vol
        self.guard.note_order_sent(ts)
        self.guard.acquire_lock(vol * self.size * price)
        self.write_log(f"entry {'stop' if stop else 'limit'} {direction.value} {vol} @ {price} ({self.entry_orderid})")

    def _cancel_entry(self, why: str) -> None:
        if self.entry_orderid:
            self.write_log(f"cancel entry {self.entry_orderid}: {why}")
            self.cancel_order(self.entry_orderid)

    # ------------------------------------------------------------------ #
    # protective stop and exits
    # ------------------------------------------------------------------ #
    # The 6 orders/min limiter (guard.check_order) applies to ENTRIES only.
    # Protective stops and exits are never rate limited: cancelling a stop
    # and then refusing to send its replacement would leave the position
    # naked exactly in the fast market that caused the burst (SPEC:
    # "protective stop always present", "never stop trying").

    def _ensure_stop(self, ts: float) -> None:
        """Protective stop always present: arm it whenever pos != 0 and nothing is tracking it."""
        if self._is_flat() or self.stop_orderid or self.exit_orderid or self._exit_reason:
            return
        if self.stop_price <= 0:
            if self.entry_price <= 0 or self.atr <= 0:
                return
            self.stop_price = self._initial_stop(self.entry_price, 1 if self.pos > 0 else -1)
        self._arm_stop(ts)

    def _arm_stop(self, ts: float) -> None:
        """(Re)send the local stop at ``stop_price`` for the whole position (cancel by id first)."""
        if self._is_flat() or self.stop_price <= 0 or not self.trading or self.exit_orderid:
            return
        vol = self._order_volume()
        if vol <= 0:
            self.write_log(f"stop not armed: position {self.pos} is below one step")
            return
        if self.stop_orderid:
            self.cancel_order(self.stop_orderid)   # local stop: synchronous
            self.stop_orderid = ""
        ids = self.sell(self.stop_price, vol, stop=True) if self.pos > 0 else self.cover(self.stop_price, vol, stop=True)
        if ids:
            self.stop_orderid = ids[0]
            self.guard.note_order_sent(ts)

    def _request_exit(self, reason: str, ref_price: float, ts: float, pct: float | None = None) -> None:
        if self._is_flat():
            return
        if not self._exit_reason:
            self._chase_count = 0
            self.write_log(f"exit requested: {reason}")
        self._exit_reason = reason
        self._ensure_exit(ref_price, ts, pct)

    def _ensure_exit(self, ref_price: float, ts: float, pct: float | None = None) -> None:
        """Send the limit exit for exactly ``abs(pos)`` when none is in flight; never gives up."""
        if self._is_flat() or not self._exit_reason or self.exit_orderid or not self.trading:
            return
        vol = self._order_volume()
        if vol <= 0:
            self.write_log(f"exit not sent: position {self.pos} is below one step")
            return
        if ts - self._exit_reject_ts < EXIT_REJECT_BACKOFF_SECS:
            # A rejected exit (position gone?) is retried after the forced
            # reconcile, not on every tick; keep the stop up meanwhile.
            if not self.stop_orderid:
                self._arm_stop(ts)
            return
        if self.stop_orderid:      # never let stop + limit both fill
            self.cancel_order(self.stop_orderid)
            self.stop_orderid = ""
        n = self._chase_count
        if pct is None:
            pct = CHASE_STEP * (n + 1) if n < CHASE_MAX else CHASE_SLOW_PCT
        covering = self.pos < 0
        if self.live and n >= CHASE_MAX and self._tick is not None:
            book = self._tick.ask_price_1 if covering else self._tick.bid_price_1
            ref_price = book or ref_price
        price = round_to(ref_price * (1 + pct) if covering else ref_price * (1 - pct), self.pricetick or 0.01)
        ids = self.cover(price, vol) if covering else self.sell(price, vol)
        if ids:
            self.exit_orderid = ids[0]
            self._exit_sent_ts = ts
            self._cancel_pending = ""
            self.guard.note_order_sent(ts)
            level = "CRITICAL " if n >= CHASE_MAX else ""
            self.write_log(f"{level}exit limit #{n} {vol} @ {price} ({self._exit_reason}, {self.exit_orderid})")

    def _chase_exit(self, ts: float, ref_price: float) -> None:
        """
        Bounded chase: re-price after CHASE_SECS (6x), then every CHASE_SLOW_SECS until flat.

        Live cancels are asynchronous: the chase step is counted once when the
        cancel is issued and ``_exit_sent_ts`` is reset, so the following
        ticks (up to ~5/s on Binance) neither re-cancel nor advance the
        counter until the cancel is confirmed (``on_order`` clears the id) or
        another full wait period has passed (lost cancel -> re-issue).
        """
        oid = self.exit_orderid
        if oid and not oid.startswith(STOPORDER_PREFIX):
            wait = CHASE_SECS if self._chase_count < CHASE_MAX else CHASE_SLOW_SECS
            if ts - self._exit_sent_ts >= wait:
                if self._cancel_pending != oid:
                    self._chase_count += 1
                    self._cancel_pending = oid
                self._exit_sent_ts = ts
                self.cancel_order(oid)    # async in live: resend once on_order clears the id
        self._ensure_exit(ref_price, ts)

    def _panic_check(self, tick: TickData, ts: float) -> None:
        """Price is through the stop but the local stop did not trigger: exit with a marketable limit."""
        if not self.stop_orderid or self.stop_price <= 0 or ts - self._last_panic_ts < PANIC_SECS:
            return
        last = float(tick.last_price)
        through = last < self.stop_price * (1 - PANIC_TRIGGER) if self.pos > 0 else last > self.stop_price * (1 + PANIC_TRIGGER)
        if not through:
            return
        self._last_panic_ts = ts
        ref = (tick.bid_price_1 if self.pos > 0 else tick.ask_price_1) or last
        self.write_log(f"PANIC: last {last} through stop {self.stop_price}")
        self._request_exit("PANIC", ref, ts, pct=PANIC_PCT)

    # ------------------------------------------------------------------ #
    # order / trade callbacks
    # ------------------------------------------------------------------ #
    @guarded
    def on_stop_order(self, stop_order: StopOrder) -> None:
        sid = stop_order.stop_orderid
        triggered = stop_order.status == StopOrderStatus.TRIGGERED
        child = stop_order.vt_orderids[0] if stop_order.vt_orderids else ""
        if sid == self.entry_orderid:
            if stop_order.status == StopOrderStatus.CANCELLED:
                self.entry_orderid = ""
            elif triggered:
                self.entry_orderid = child
        elif sid == self.stop_orderid:
            self.stop_orderid = ""
            if triggered:
                self.exit_orderid = child
                self._exit_from_stop = True
                self._exit_reason = self._exit_reason or "STOP"
                self._exit_sent_ts = self._now_ts
                self._chase_count = 0
                self.write_log(f"protective stop triggered @ {stop_order.price} -> {child}")

    @guarded
    def on_order(self, order: OrderData) -> None:
        self.guard.on_order(order)
        if order.is_active():
            return
        oid = order.vt_orderid
        if oid == self.entry_orderid:
            self.entry_orderid = ""
            if order.status == Status.REJECTED:
                self.write_log(f"entry order rejected ({oid})")
        elif oid == self.exit_orderid:
            self.exit_orderid = ""
            if order.status == Status.REJECTED:
                self.write_log(f"CRITICAL exit order rejected ({oid}); reconciling, then retrying")
                self._exit_reject_ts = self._now_ts
                if self.live:
                    self._reconcile_due = self._now_ts + RECONCILE_QUIET_SECS
        elif oid == self.stop_orderid:
            self.stop_orderid = ""
        if oid == self._cancel_pending:
            self._cancel_pending = ""
        if self.live:
            self.guard.note_fill(self._now_ts)
        self.sync_data()

    @guarded
    def on_trade(self, trade: TradeData) -> None:
        ts = ts_of(trade.datetime) if trade.datetime else self._now_ts
        px, vol = float(trade.price), float(trade.volume)
        signed = vol if trade.direction == Direction.LONG else -vol
        # The engine accumulates pos with binary floats (0.005 + 0.045 - 0.05
        # = -6.9e-18): normalise to the volume step so "flat" is exact.
        step = self._step()
        self.pos = round_to(self.pos, step)
        pos_before = round_to(self.pos - signed, step)
        fee = self.fee_rate * px * vol * self.size
        self.fees_paid += fee
        if self.live:
            self.guard.note_fill(ts)
            self._reconcile_due = ts + RECONCILE_QUIET_SECS    # SPEC 4.8: reconcile after each on_trade
        if self._is_flat(pos_before) or (pos_before > 0) == (signed > 0):
            self._on_open_fill(px, vol, pos_before, ts)
        else:
            self._on_close_fill(px, vol, pos_before, fee, ts)
        self.sync_data()

    def _on_open_fill(self, px: float, vol: float, pos_before: float, ts: float) -> None:
        if self._is_flat(pos_before):
            self.entry_price = px
            self.highest_since_entry = self.lowest_since_entry = px
            self.entry_bar_ts, self.bars_in_trade, self.stop_price = ts, 0, 0.0
            self._trade_pnl = 0.0
            self._trade_volume = 0.0
        else:   # partial fills: volume-weighted entry
            self.entry_price = (self.entry_price * abs(pos_before) + px * vol) / (abs(pos_before) + vol)
        direction = 1 if self.pos > 0 else -1
        self.stop_price = self._initial_stop(self.entry_price, direction)
        self._entry_risk_usd = abs(self.pos) * self.size * abs(self.entry_price - self.stop_price)
        self._arm_stop(ts)
        self.guard.update_open_notional(abs(self.pos) * self.size * px)
        self.write_log(f"filled entry {vol} @ {px}; pos={self.pos} stop={self.stop_price}")

    def _on_close_fill(self, px: float, vol: float, pos_before: float, fee: float, ts: float) -> None:
        direction = 1 if pos_before > 0 else -1
        closed = min(vol, abs(pos_before))
        # realized_pnl is GROSS (fees live in fees_paid; risk.get_equity
        # subtracts them once); the net figure is kept for the trade log / R.
        gross = (px - self.entry_price) * closed * self.size * direction
        entry_fee = self.fee_rate * self.entry_price * closed * self.size
        exit_fee = fee * (closed / vol) if vol > 0 else fee
        self.realized_pnl += gross
        self._trade_pnl += gross - entry_fee - exit_fee
        self._trade_volume += closed
        flipped = not self._is_flat() and (self.pos > 0) != (pos_before > 0)
        if not self._is_flat() and not flipped:
            # Partial close: keep the trade open.  Without an exit in flight
            # (external / manual reduction) the stop must cover the new size.
            if not self.exit_orderid and not self._exit_reason:
                self._arm_stop(ts)
            return
        self._close_round_trip(px, direction, ts)
        if flipped:
            # An over-fill (duplicate stop, manual close larger than pos)
            # reversed the position: treat it as an unplanned entry with its
            # own protective stop and leave it immediately.
            self.write_log(f"CRITICAL position flipped on exit fill: pos={self.pos}; closing it")
            self.guard.acquire_lock(abs(self.pos) * self.size * px)
            self._on_open_fill(px, abs(self.pos), 0.0, ts)
            self._request_exit("UNWANTED_FLIP", px, ts)

    def _close_round_trip(self, px: float, direction: int, ts: float) -> None:
        """Book the finished round trip and reset the trade state (position is flat or flipped)."""
        self.guard.on_trade_closed(self._trade_pnl, ts)
        if self._exit_from_stop:
            self.direction_blocked = direction
            self.block_until_ts = ts + self.reentry_cooldown_bars * 3600.0
        r = self._trade_pnl / self._entry_risk_usd if self._entry_risk_usd > 0 else 0.0
        gross = (px - self.entry_price) * self._trade_volume * self.size * direction
        self.trade_log.append(dict(entry_ts=self.entry_bar_ts, exit_ts=ts, direction=direction, entry=self.entry_price,
                                   exit=px, pnl=self._trade_pnl, gross=gross, fees=gross - self._trade_pnl,
                                   volume=self._trade_volume, risk_usd=self._entry_risk_usd, r=r,
                                   reason=self._exit_reason or "STOP"))
        self.write_log(f"closed {direction:+d} @ {px}: pnl {self._trade_pnl:.4f} ({r:.2f}R, {self._exit_reason or 'STOP'})")
        if self.stop_orderid:
            self.cancel_order(self.stop_orderid)
            self.stop_orderid = ""
        if self.exit_orderid:
            self.cancel_order(self.exit_orderid)
        self.entry_price = self.stop_price = self.highest_since_entry = self.lowest_since_entry = 0.0
        self.entry_bar_ts, self.bars_in_trade = 0.0, 0
        self._exit_reason, self._exit_from_stop, self._chase_count, self._trade_pnl = "", False, 0, 0.0
        self._trade_volume = 0.0
        self._cancel_pending = ""
        self.guard.release_lock()
