"""
Strategy 2 - ``SqueezeBreak15M`` (SPEC section 6).

Bollinger-inside-Keltner squeeze on 15-minute bars; a release followed by a
close beyond the squeeze range / 20-bar Donchian in the direction of a rising
(falling) EMA-200 enters with a limit order that is cancelled if unfilled at
the next 15m bar.  Sizing, fee gates, halts, locks and reconciliation are
delegated to ``sizing.py`` / ``risk.py``.  Ships disabled in ``DEPLOYMENT``
until the section 7 acceptance gate passes on the intended alt symbol.

Order mechanics (SPEC section 2): entry = limit; protective exit = engine-local
StopOrder (always present while ``pos != 0``); other exits = limit at 0.1 %
worse than close, chased live in ``on_tick``.  Never ``cancel_all``.
"""
from __future__ import annotations

from typing import Any

import numpy as np
from vnpy.trader.constant import Direction, Exchange, Interval
from vnpy.trader.object import BarData, OrderData, TickData, TradeData
from vnpy.trader.utility import ArrayManager, BarGenerator, round_to
from vnpy_ctastrategy.base import EngineType, StopOrder, StopOrderStatus
from vnpy_ctastrategy.template import CtaTemplate

from risk import TICK_THROTTLE_SECS, RiskDecision, RiskGuard, get_equity, guarded, ts_of
from settings import DIALS, base_from_vt_symbol, contract_name_for, exchange_from_vt_symbol, lot_info_for
from sizing import LotInfo, calc_volume
from sizing import fee_rt as fee_round_trip
from sizing import min_stop_pct as msp_of

EXIT_SLIP = 0.001          # limit exits 0.1 % worse than close, +0.1 % per chase step
CHASE_SECS = 5.0
CHASE_MAX = 6
CHASE_SLOW_SECS = 30.0
CHASE_SLOW_PCT = 0.005     # after CHASE_MAX steps: bid_1*(1-0.5 %) / ask_1*(1+0.5 %)
PANIC_PCT = 0.002
PANIC_SLIP = 0.003
PANIC_SECS = 30.0
MIN_LOTS = 4
BAR_SECS = 15 * 60
RECONCILE_DELAY = 5.0      # let the exchange position push arrive after a fill


class SqueezeBreak15M(CtaTemplate):
    """Squeeze-release breakout, 15m signals and management (SPEC section 6)."""

    author = "crypto_trader"

    # -- risk / fee keys shared with S1 (dial overwrites unless risk_dial == "custom")
    risk_dial: str = "normal"
    risk_pct: float = 0.015
    max_leverage: float = 3.0
    gross_leverage: float = 4.0
    daily_loss_pct: float = 0.06
    max_dd_halt: float = 0.25
    max_consec_losses: int = 4
    cooldown_hours: float = 8.0
    max_trades_day: int = 6
    fee_rate: float = 0.0005
    slippage_pct: float = 0.0008
    fee_gate_mult: float = 3.0
    min_stop_pct: float = 0.004
    lot_tolerance: float = 1.5
    min_notional: float = 0.0          # 0 -> exchange_filters.json / fallback table
    # -- signal / management
    bb_n: int = 20
    bb_dev: float = 2.0
    kc_n: int = 20
    kc_dev: float = 1.5
    min_squeeze_bars: int = 6
    release_valid_bars: int = 3
    dc_n: int = 20
    ema_reg_n: int = 200
    atr_n: int = 14
    stop_mult: float = 1.5
    trail_start_r: float = 1.0
    trail_mult: float = 2.0
    target_r: float = 0.0              # 0 = off (conservative dial sets 2.5)
    time_stop_bars: int = 32
    mfe_min_r: float = 0.75
    max_hold_bars: int = 96
    chase_pct: float = 0.0006
    capital: float = 50.0
    warmup_days: int = 5
    use_kline_stream: bool = True

    # -- persisted variables (JSON scalars only)
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
    direction_blocked: str = ""
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
    r_value: float = 0.0
    squeeze_count: int = 0
    squeeze_hi: float = 0.0
    squeeze_lo: float = 0.0
    release_age: int = 0

    parameters = [
        "risk_dial", "risk_pct", "max_leverage", "gross_leverage", "daily_loss_pct", "max_dd_halt",
        "max_consec_losses", "cooldown_hours", "max_trades_day", "fee_rate", "slippage_pct", "fee_gate_mult",
        "min_stop_pct", "lot_tolerance", "min_notional",
        "bb_n", "bb_dev", "kc_n", "kc_dev", "min_squeeze_bars", "release_valid_bars", "dc_n", "ema_reg_n",
        "atr_n", "stop_mult", "trail_start_r", "trail_mult", "target_r", "time_stop_bars", "mfe_min_r",
        "max_hold_bars", "chase_pct", "capital", "warmup_days", "use_kline_stream",
    ]
    variables = [
        "equity", "realized_pnl", "fees_paid", "entry_price", "stop_price", "highest_since_entry",
        "lowest_since_entry", "entry_bar_ts", "bars_in_trade", "last_1m_dt", "direction_blocked",
        "block_until_ts", "day_key", "day_start_equity", "peak_equity", "consec_losses", "cooldown_until",
        "trades_today", "reject_count", "halted", "halt_reason", "entry_orderid", "stop_orderid",
        "exit_orderid", "last_reconcile_ts", "r_value", "squeeze_count", "squeeze_hi", "squeeze_lo",
        "release_age",
    ]

    # -- lifecycle
    def on_init(self) -> None:
        """Apply the dial, build generators, warm up (trading is False here)."""
        self._apply_dial()
        self.trade_log: list[dict[str, Any]] = []
        self.live: bool = self.get_engine_type() != EngineType.BACKTESTING
        self.lot: LotInfo = LotInfo()
        self.size: float = 1.0
        self.pricetick: float = 0.01
        self.min_volume: float = 0.001
        self.last_price: float = 0.0
        self.last_close: float = 0.0
        self.atr: float = 0.0
        self.ema_reg: float = 0.0
        self.ema_reg_prev: float = 0.0
        self.dc_up: float = 0.0
        self.dc_dn: float = 0.0
        self._now_ts: float = 0.0
        self._exit_pending: bool = False
        self._exit_reason: str = ""
        self._exit_sent_ts: float = 0.0
        self._exit_chase_n: int = 0
        self._stop_vol: float = 0.0
        self._closing_via_stop: bool = False
        self._trade_fees: float = 0.0
        self._first_tick: bool = True
        self._lots_checked: bool = False
        self._last_eq_ts: float = -1e18
        self._last_panic_ts: float = -1e18
        self._reconcile_due: float = 0.0
        if self.equity <= 0:
            self.equity = float(self.capital)
        self._load_contract()
        self.guard: RiskGuard = RiskGuard(self, suffix="s2")
        self.bg1 = BarGenerator(self.on_bar)
        self.bg_sig = BarGenerator(self._noop, 15, self.on_15m_bar, Interval.MINUTE)
        self.am = ArrayManager(size=max(260, self.ema_reg_n + 60))
        self.load_bar(self.warmup_days, Interval.MINUTE)
        self.write_log(f"init done: dial={self.risk_dial} risk_pct={self.risk_pct} lot={self.lot}")

    @guarded
    def on_start(self) -> None:
        self._load_contract()
        if self.live:  # local stops / order ids die with the process
            self.entry_orderid = self.stop_orderid = self.exit_orderid = ""
            self._first_tick = True
        self.write_log(f"started pos={self.pos} stop={self.stop_price} halted={self.halted}/{self.halt_reason}")

    @guarded
    def on_stop(self) -> None:
        self.sync_data()
        self.write_log("stopped")

    def _noop(self, bar: BarData) -> None:
        return

    def _apply_dial(self) -> None:
        if self.risk_dial == "custom":
            return
        dial = DIALS.get(self.risk_dial)
        if not dial:
            self.write_log(f"unknown risk_dial {self.risk_dial!r}; keeping explicit parameters")
            return
        self.risk_pct = float(dial["risk_pct_s2"])
        self.max_leverage = float(dial["max_leverage"])
        self.gross_leverage = float(dial["gross_leverage"])
        self.daily_loss_pct = float(dial["daily_loss_pct"])
        self.max_dd_halt = float(dial["max_dd_halt"])
        self.max_consec_losses = int(dial["max_consec_losses"])
        self.cooldown_hours = float(dial["cooldown_hours"])
        self.max_trades_day = int(dial["max_trades_day_s2"])
        if self.risk_dial == "conservative" and self.target_r <= 0:
            self.target_r = 2.5

    def _load_contract(self) -> None:
        """Contract facts: gateway contract (live) or engine + filters file (backtest)."""
        base = base_from_vt_symbol(self.vt_symbol)
        try:
            name = contract_name_for(exchange_from_vt_symbol(self.vt_symbol), base)
        except ValueError:
            name = base
        size = float(self.get_size() or 0.0) or 1.0
        pricetick = float(self.get_pricetick() or 0.0)
        min_volume: float | None = None
        if self.live:
            contract = self.cta_engine.main_engine.get_contract(self.vt_symbol)
            if contract:
                name, size = contract.name or name, float(contract.size or 1.0)
                pricetick, min_volume = float(contract.pricetick), float(contract.min_volume)
        self.lot = lot_info_for(name, size=size, pricetick=pricetick or None, min_volume=min_volume)
        if self.min_notional > 0:
            self.lot = LotInfo(self.lot.size, self.lot.step, float(self.min_notional), self.lot.pricetick)
        else:
            self.min_notional = self.lot.min_notional
        self.size, self.pricetick, self.min_volume = self.lot.size, self.lot.pricetick, self.lot.step

    # -- market data
    @guarded
    def on_tick(self, tick: TickData) -> None:
        if not tick.last_price:
            return
        ts = ts_of(tick.datetime)
        self.last_price, self._now_ts = float(tick.last_price), ts
        if self._first_tick and self.trading:
            self._on_first_tick(tick, ts)
        b = tick.extra.get("bar") if (self.use_kline_stream and tick.extra) else None
        if b is not None:
            if b.datetime.isoformat() != self.last_1m_dt:
                b.symbol, b.exchange, b.vt_symbol = self.vt_symbol.split(".")[0], Exchange.GLOBAL, self.vt_symbol
                self.on_bar(b)
        else:
            self.bg1.update_tick(tick)
        if not self.trading:
            return
        if ts - self._last_eq_ts >= TICK_THROTTLE_SECS:
            self._last_eq_ts = ts
            balance, eq = get_equity(self, tick.last_price)
            d = self.guard.tick(tick, eq, balance)
            if d is not None:
                self._apply_decision(d, tick.last_price, ts)
        self._chase_exit(ts)
        self._ensure_exit(tick.last_price, ts, tick.bid_price_1, tick.ask_price_1)
        self._panic(tick, ts)
        force = 0 < self._reconcile_due <= ts
        if force:
            self._reconcile_due = 0.0
        res = self.guard.reconcile_live(tick.last_price, ts, force=force)
        if res is not None and res.outcome == "adopt":
            self._ensure_stop(tick.last_price, ts)
        self.guard.heartbeat(ts)

    def _on_first_tick(self, tick: TickData, ts: float) -> None:
        """Recover after (re)start: reconcile, refresh extremes, re-arm the stop."""
        self._first_tick = False
        self.guard.reconcile_live(tick.last_price, ts, force=True)
        if self.pos != 0:
            self.highest_since_entry = max(self.highest_since_entry, tick.last_price)
            self.lowest_since_entry = min(self.lowest_since_entry or tick.last_price, tick.last_price)
            self._ensure_stop(tick.last_price, ts)
        self._check_lots(tick.last_price)
        self.sync_data()

    @guarded
    def on_bar(self, bar: BarData) -> None:
        """1m bar (backtest engine, kline stream or BarGenerator)."""
        self.bg_sig.update_bar(bar)
        self.on_1m(bar)

    def on_1m(self, bar: BarData) -> None:
        self.last_close = float(bar.close_price)
        self.last_1m_dt = bar.datetime.isoformat()
        if not self.trading:
            return
        ts = ts_of(bar.datetime)
        self._now_ts = ts
        if self.pos != 0:
            self.highest_since_entry = max(self.highest_since_entry, bar.high_price)
            self.lowest_since_entry = min(self.lowest_since_entry or bar.low_price, bar.low_price)
        self._ensure_stop(bar.close_price, ts)
        self._ensure_exit(bar.close_price, ts)
        self.guard.heartbeat(ts)

    def on_15m_bar(self, bar: BarData) -> None:
        self.am.update_bar(bar)
        if not self.am.inited:
            return
        self._compute()
        if not self.trading:
            return
        ts, close = ts_of(bar.datetime), float(bar.close_price)
        self._check_lots(close)
        balance, eq = get_equity(self, close)
        unreal = self.pos * (close - self.entry_price) * self.size if (self.pos and self.entry_price > 0) else 0.0
        d = self.guard.pre_bar(bar, eq, balance, unreal)
        if self.entry_orderid and self._is_active(self.entry_orderid):
            self.cancel_order(self.entry_orderid)      # unfilled at the next 15m bar -> gone
        if self.pos != 0:
            self._manage(close, ts)
        elif d.allow_entry:
            self._try_entry(close, ts, balance)
        self._apply_decision(d, close, ts)
        if not self.live:
            self._chase_exit(ts)                       # backtest chase cadence = management bar
        self.sync_data()

    # -- indicators and squeeze state (derived from the arrays every bar)
    def _compute(self) -> None:
        am = self.am
        bb_up, bb_dn = am.boll(self.bb_n, self.bb_dev, array=True)
        kc_up, kc_dn = am.keltner(self.kc_n, self.kc_dev, array=True)
        with np.errstate(invalid="ignore"):
            sq = (bb_up < kc_up) & (bb_dn > kc_dn)
        self.atr = float(am.atr(self.atr_n))
        ema = am.ema(self.ema_reg_n, array=True)
        self.ema_reg, self.ema_reg_prev = float(ema[-1]), float(ema[-9])
        self.dc_up = float(np.max(am.high[-(self.dc_n + 1):-1]))
        self.dc_dn = float(np.min(am.low[-(self.dc_n + 1):-1]))
        i, age = len(sq) - 1, 0
        while i >= 0 and not sq[i] and age <= self.release_valid_bars:
            age, i = age + 1, i - 1
        count, hi, lo = 0, 0.0, 0.0
        if age <= self.release_valid_bars:
            while i >= 0 and sq[i]:
                count += 1
                hi = max(hi, float(am.high[i]))
                lo = float(am.low[i]) if lo == 0 else min(lo, float(am.low[i]))
                i -= 1
        self.squeeze_count, self.release_age = count, (age if count else 0)
        self.squeeze_hi, self.squeeze_lo = hi, lo

    def _fee_rt(self) -> float:
        return fee_round_trip(self.fee_rate, self.slippage_pct)

    def _msp(self) -> float:
        return msp_of(self.min_stop_pct, self.fee_gate_mult, self._fee_rt())

    def _setup_ok(self, close: float) -> bool:
        if self.atr <= 0 or close <= 0 or self.squeeze_count < self.min_squeeze_bars:
            return False
        if not 1 <= self.release_age <= self.release_valid_bars:
            return False
        ratio = self.atr / close
        return 0.0015 <= ratio <= 0.05 and self.stop_mult * ratio >= self.fee_gate_mult * self._fee_rt()

    # -- entries
    def _try_entry(self, close: float, ts: float, balance: float) -> None:
        if self.entry_orderid or self._exit_pending or not self._setup_ok(close):
            return
        blocked = self.direction_blocked if ts < self.block_until_ts else ""
        go_long = (close > max(self.squeeze_hi, self.dc_up) and close > self.ema_reg
                   and self.ema_reg >= self.ema_reg_prev and blocked != "long")
        go_short = (close < min(self.squeeze_lo, self.dc_dn) and close < self.ema_reg
                    and self.ema_reg <= self.ema_reg_prev and blocked != "short")
        if not go_long and not go_short:
            return
        side = "long" if go_long else "short"
        price = round_to(close * (1 + self.chase_pct) if go_long else close * (1 - self.chase_pct), self.pricetick)
        vol = calc_volume(balance, self.risk_pct, price, self.stop_mult * self.atr, self.lot, self.max_leverage,
                          self.guard.free_notional(balance), self.guard.streak_mult(), self.lot_tolerance,
                          self.min_stop_pct, self.fee_gate_mult, self._fee_rt())
        if vol <= 0:
            self.write_log(f"{side} setup at {close}: no acceptable size (balance {balance:.2f}, atr {self.atr:.2f})")
            return
        direction = Direction.LONG if go_long else Direction.SHORT
        ok, why = self.guard.check_order("entry", direction, price, vol, balance, self.last_price,
                                         min_notional=self.lot.min_notional, ts=ts)
        if not ok:
            self.write_log(f"entry skipped: {why}")
            return
        if not self.guard.acquire_lock(vol * self.size * price):
            self.write_log("entry skipped: symbol locked by another strategy")
            return
        ids = self.buy(price, vol) if go_long else self.short(price, vol)
        if not ids:
            self.guard.release_lock()
            return
        self.entry_orderid = ids[0]
        self.guard.note_order_sent(ts)
        self.write_log(f"ENTRY {side} limit {price} x {vol} (atr {self.atr:.2f}, squeeze {self.squeeze_count} bars, "
                       f"release age {self.release_age})")

    # -- management of an open position (each 15m bar)
    def _manage(self, close: float, ts: float) -> None:
        self.bars_in_trade += 1
        long = self.pos > 0
        r_px = self.r_value / (abs(self.pos) * self.size) if (self.r_value > 0 and self.pos) else self.stop_mult * self.atr
        mfe_px = (self.highest_since_entry - self.entry_price) if long else (self.entry_price - self.lowest_since_entry)
        if self.atr > 0 and r_px > 0 and mfe_px >= self.trail_start_r * r_px:
            cand = self.highest_since_entry - self.trail_mult * self.atr if long else self.lowest_since_entry + self.trail_mult * self.atr
            if self.stop_price > 0:
                cand = max(self.stop_price, cand) if long else min(self.stop_price, cand)
            self._set_stop(cand, ts)
        gain = (close - self.entry_price) if long else (self.entry_price - close)
        reason = ""
        if self.target_r > 0 and r_px > 0 and gain >= self.target_r * r_px:
            reason = "TARGET"
        elif self.bars_in_trade >= self.max_hold_bars:
            reason = "MAX_HOLD"
        elif self.bars_in_trade >= self.time_stop_bars and mfe_px < self.mfe_min_r * r_px:
            reason = "TIME_STOP"
        elif long and close < self.ema_reg and self.ema_reg < self.ema_reg_prev:
            reason = "REGIME"
        elif not long and close > self.ema_reg and self.ema_reg > self.ema_reg_prev:
            reason = "REGIME"
        if reason:
            self._request_exit(reason, close, ts)

    def _apply_decision(self, d: RiskDecision, ref: float, ts: float) -> None:
        if d.cancel_entries and self.entry_orderid and self._is_active(self.entry_orderid):
            self.cancel_order(self.entry_orderid)
        if d.must_flatten and self.pos != 0:
            self._request_exit(d.reason, ref, ts)

    def _check_lots(self, price: float) -> None:
        if self._lots_checked or price <= 0:
            return
        self._lots_checked = True
        balance, _ = get_equity(self, price)
        lots = self.lot.lots_max(balance, self.max_leverage, price)
        if lots < MIN_LOTS:
            self.write_log(f"lot notional {self.lot.lot_notional(price):.2f} USDT -> only {lots} lots at "
                           f"{self.max_leverage}x on {balance:.2f}; symbol refused")
            self.guard.halt("COARSE_LOTS")

    # -- protective stop (always present while pos != 0)
    def _is_active(self, orderid: str) -> bool:
        eng = self.cta_engine
        if not self.live:
            return orderid in eng.active_stop_orders or orderid in eng.active_limit_orders
        return orderid in eng.strategy_orderid_map.get(self.strategy_name, set())

    def _ensure_stop(self, close: float, ts: float) -> None:
        if self.pos == 0:
            return
        if self.stop_price <= 0:
            ref = self.entry_price if self.entry_price > 0 else close
            dist = max(self.stop_mult * self.atr, self._msp() * ref)
            self.stop_price = round_to(ref - dist if self.pos > 0 else ref + dist, self.pricetick)
            self.write_log(f"stop recomputed at {self.stop_price}")
        if not self.stop_orderid or not self._is_active(self.stop_orderid) or self._stop_vol != abs(self.pos):
            self._arm_stop(ts)

    def _set_stop(self, price: float, ts: float) -> None:
        price = round_to(price, self.pricetick)
        if self.stop_price > 0 and abs(price - self.stop_price) < self.pricetick:
            return
        self.stop_price = price
        self._arm_stop(ts)

    def _arm_stop(self, ts: float) -> None:
        if self.pos == 0 or self.stop_price <= 0:
            return
        if self.stop_orderid and self._is_active(self.stop_orderid):
            self.cancel_order(self.stop_orderid)   # local stop: cancelled synchronously
        vol = abs(self.pos)
        ids = self.sell(self.stop_price, vol, stop=True) if self.pos > 0 else self.cover(self.stop_price, vol, stop=True)
        if ids:
            self.stop_orderid, self._stop_vol = ids[0], vol
            self.guard.note_order_sent(ts)

    # -- limit exits with bounded chase
    def _request_exit(self, reason: str, ref: float, ts: float) -> None:
        if self.pos == 0:
            return
        if not self._exit_pending:
            self._exit_pending, self._exit_reason, self._exit_chase_n = True, reason, 0
            self.write_log(f"EXIT requested ({reason}) pos={self.pos} at {ref}")
        self._ensure_exit(ref, ts)

    def _ensure_exit(self, ref: float, ts: float, bid: float = 0.0, ask: float = 0.0) -> None:
        if not self._exit_pending or self.pos == 0 or self.exit_orderid or ref <= 0:
            return
        long, n = self.pos > 0, self._exit_chase_n
        if self._exit_reason == "PANIC" and n == 0:
            base = (bid if bid > 0 else ref) if long else (ask if ask > 0 else ref)
            price = base * (1 - PANIC_SLIP) if long else base * (1 + PANIC_SLIP)
        elif n < CHASE_MAX:
            price = ref * (1 - EXIT_SLIP * (n + 1)) if long else ref * (1 + EXIT_SLIP * (n + 1))
        else:
            base = (bid if bid > 0 else ref) if long else (ask if ask > 0 else ref)
            price = base * (1 - CHASE_SLOW_PCT) if long else base * (1 + CHASE_SLOW_PCT)
            self.write_log(f"CRITICAL: exit unfilled after {n} chases, sending at {price:.4f}")
        price = round_to(price, self.pricetick)
        vol = abs(self.pos)
        ids = self.sell(price, vol) if long else self.cover(price, vol)
        if ids:
            self.exit_orderid, self._exit_sent_ts = ids[0], ts
            self.guard.note_order_sent(ts)

    def _chase_exit(self, ts: float) -> None:
        if not (self._exit_pending and self.exit_orderid):
            return
        wait = CHASE_SECS if self._exit_chase_n < CHASE_MAX else CHASE_SLOW_SECS
        if self.live and ts - self._exit_sent_ts < wait:
            return
        if self._is_active(self.exit_orderid):
            self.cancel_order(self.exit_orderid)   # resent worse once the cancel is confirmed
            self._exit_chase_n += 1
            self._exit_sent_ts = ts

    def _panic(self, tick: TickData, ts: float) -> None:
        if self.pos == 0 or self.stop_price <= 0 or not self.stop_orderid or ts - self._last_panic_ts < PANIC_SECS:
            return
        last = tick.last_price
        if (self.pos > 0 and last < self.stop_price * (1 - PANIC_PCT)) or (self.pos < 0 and last > self.stop_price * (1 + PANIC_PCT)):
            self._last_panic_ts = ts
            self.write_log(f"CRITICAL: price {last} through stop {self.stop_price} without trigger; panic exit")
            if self._is_active(self.stop_orderid):
                self.cancel_order(self.stop_orderid)
            self._exit_pending, self._exit_reason, self._exit_chase_n = True, "PANIC", 0
            self._ensure_exit(last, ts, tick.bid_price_1, tick.ask_price_1)

    # -- order / trade callbacks
    @guarded
    def on_order(self, order: OrderData) -> None:
        self.guard.on_order(order)
        if order.is_active():
            return
        oid = order.vt_orderid
        if oid == self.entry_orderid:
            self.entry_orderid = ""
            if self.pos == 0:
                self.guard.release_lock()
        elif oid == self.exit_orderid:
            self.exit_orderid = ""     # rejected/cancelled exits are resent by _ensure_exit
        elif oid == self.stop_orderid:
            self.stop_orderid = ""
        self.sync_data()

    @guarded
    def on_stop_order(self, so: StopOrder) -> None:
        if so.stop_orderid != self.stop_orderid or so.status == StopOrderStatus.WAITING:
            return
        self.stop_orderid, self._stop_vol = "", 0.0
        if so.status == StopOrderStatus.TRIGGERED:
            if self.exit_orderid and self._is_active(self.exit_orderid):
                self.cancel_order(self.exit_orderid)
            self._closing_via_stop = True
            self._exit_pending, self._exit_reason, self._exit_chase_n = True, "STOP", 0
            if so.vt_orderids:
                self.exit_orderid, self._exit_sent_ts = so.vt_orderids[-1], self._now_ts
            self.write_log(f"stop triggered at {so.price}")

    @guarded
    def on_trade(self, trade: TradeData) -> None:
        ts = ts_of(trade.datetime) if trade.datetime else self._now_ts
        signed = trade.volume if trade.direction == Direction.LONG else -trade.volume
        pos_before = self.pos - signed
        price, vol = float(trade.price), float(trade.volume)
        fee = self.fee_rate * price * vol * self.size
        self.fees_paid += fee
        self._trade_fees += fee
        if pos_before == 0 or (pos_before * self.pos > 0 and abs(self.pos) > abs(pos_before)):
            if pos_before == 0:
                self._open_state(price, ts)
            else:  # unexpected add: average in
                self.entry_price = (self.entry_price * abs(pos_before) + price * vol) / abs(self.pos)
            self.guard.update_open_notional(abs(self.pos) * self.size * price)
            self._arm_stop(ts)
            self.write_log(f"FILL open {'long' if self.pos > 0 else 'short'} {vol} @ {price}; stop {self.stop_price}")
        else:
            closed = min(vol, abs(pos_before))
            pnl = (price - self.entry_price) * closed * self.size * (1 if pos_before > 0 else -1)
            self.realized_pnl += pnl
            if self.pos == 0 or pos_before * self.pos < 0:
                self._close_state(price, closed, pnl, ts, "long" if pos_before > 0 else "short")
                if self.pos != 0:
                    self.write_log(f"CRITICAL: position flipped to {self.pos}; closing it")
                    self._open_state(price, ts)
                    self._arm_stop(ts)
                    self._request_exit("UNWANTED_FLIP", price, ts)
            else:
                self.guard.update_open_notional(abs(self.pos) * self.size * price)
                self._arm_stop(ts)
        if self.live:
            self._reconcile_due = ts + RECONCILE_DELAY
        self.sync_data()

    def _open_state(self, price: float, ts: float) -> None:
        self.entry_price = price
        self.highest_since_entry = self.lowest_since_entry = price
        self.entry_bar_ts, self.bars_in_trade = ts, 0
        dist = max(self.stop_mult * self.atr, self._msp() * price)
        self.stop_price = round_to(price - dist if self.pos > 0 else price + dist, self.pricetick)
        self.r_value = dist * abs(self.pos) * self.size
        self._exit_pending, self._exit_reason, self._exit_chase_n = False, "", 0
        self._closing_via_stop = False

    def _close_state(self, price: float, closed: float, pnl: float, ts: float, side: str) -> None:
        reason = "STOP" if self._closing_via_stop else (self._exit_reason or "EXIT")
        net = pnl - self._trade_fees
        self.trade_log.append(dict(entry_ts=self.entry_bar_ts, exit_ts=ts, side=side, entry=self.entry_price,
                                   exit=price, volume=closed, pnl=pnl, fees=self._trade_fees, net=net,
                                   r=self.r_value, reason=reason, bars=self.bars_in_trade))
        self.write_log(f"FILL close {closed} @ {price} ({reason}) pnl {pnl:.4f} fees {self._trade_fees:.4f} "
                       f"R {self.r_value:.4f}")
        self.guard.on_trade_closed(net, ts)
        if self._closing_via_stop:
            self.direction_blocked = side
            self.block_until_ts = ts + self.release_valid_bars * BAR_SECS
        for oid in (self.stop_orderid, self.exit_orderid):
            if oid and self._is_active(oid):
                self.cancel_order(oid)
        self.entry_price = self.stop_price = self.highest_since_entry = self.lowest_since_entry = 0.0
        self.entry_bar_ts, self.bars_in_trade, self.r_value = 0.0, 0, 0.0
        self._stop_vol, self._trade_fees = 0.0, 0.0
        self._exit_pending, self._exit_reason, self._exit_chase_n = False, "", 0
        self._closing_via_stop = False
        self.guard.release_lock()
