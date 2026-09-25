"""
Risk guard for the crypto_trader strategies (SPEC section 4).

``RiskGuard(strategy)`` is a per-strategy helper object that reads and
writes only the strategy's persisted ``variables`` (so everything survives a
restart through ``cta_strategy_data.json``) plus one account-level
``SharedRiskState`` that every strategy in the process shares:

* LIVE      -> ``.vntrader/risk_state.json`` written atomically (tmp + rename)
* BACKTEST  -> an in-memory dict, fresh for every engine

The module imports only vnpy constants / data objects (never Qt) so it can be
imported without a running engine, and it never imports ``settings`` at module
level: dial values are read from the strategy attributes first, then from an
optional ``dials`` dict, then lazily from ``settings.DIALS`` and finally from
the built-in "normal" defaults.
"""
from __future__ import annotations

import json
import os
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from vnpy.trader.constant import Direction, Status
from vnpy.trader.object import BarData, TickData
from vnpy_ctastrategy.base import STOPORDER_PREFIX, EngineType

from sizing import LEVERAGE_SAFETY

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

STATE_FILENAME = "risk_state.json"
FLAG_KILL = "KILL"
FLAG_PAUSE = "PAUSE"
FLAG_RESUME = "RESUME"

#: halt reasons that a RESUME flag file may clear (COARSE_LOTS never is).
RESUMABLE_REASONS = frozenset({"REJECTS", "DRAWDOWN", "KILL", "EXCEPTION", "DESYNC"})
#: halt reasons that require the position to be flattened.
FLATTEN_REASONS = frozenset({"KILL", "DRAWDOWN"})

MAX_ORDERS_PER_MIN = 6
MAX_REJECTS = 3
TICK_THROTTLE_SECS = 5.0
RECONCILE_SECS = 60.0
#: No (unforced) reconcile for this long after a fill / order event: the
#: exchange position push (Binance ACCOUNT_UPDATE) arrives separately from
#: the trade push and a snapshot taken in between would be stale.
RECONCILE_QUIET_SECS = 10.0
HEARTBEAT_SECS = 10.0
MAX_PRICE_DISTANCE = 0.02

#: Built-in copy of the "normal" dial, used only when ``settings.py`` is
#: unavailable and the strategy does not carry the attribute.
_NORMAL_DIAL: dict[str, float] = dict(
    risk_pct_s1=0.020, risk_pct_s2=0.015, max_leverage=3.0, gross_leverage=4.0,
    daily_loss_pct=0.06, max_dd_halt=0.25, max_consec_losses=4, cooldown_hours=8,
    max_trades_day_s1=3, max_trades_day_s2=6,
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _utc(dt: datetime) -> datetime:
    """Return ``dt`` as an aware UTC datetime (naive input is taken as UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def day_key_of(dt: datetime) -> str:
    """UTC calendar date of ``dt`` as ``YYYY-MM-DD`` (the day-roll key)."""
    return _utc(dt).date().isoformat()


def ts_of(dt: datetime) -> float:
    """Epoch seconds of ``dt`` (UTC)."""
    return _utc(dt).timestamp()


def load_dials(name: str = "normal") -> dict[str, float]:
    """
    Return the dial dict ``name`` from ``settings.DIALS`` when that module is
    importable, otherwise the built-in normal dial.  Never raises.
    """
    try:
        from settings import DIALS  # lazy: settings.py may not exist yet
        dial = DIALS.get(name)
        if isinstance(dial, dict):
            return dict(dial)
    except Exception:  # noqa: BLE001 - any import/attr failure -> fallback
        pass
    return dict(_NORMAL_DIAL)


def notifications_configured() -> bool:
    """
    True when vnpy has a notification channel set up: e-mail credentials in
    ``vt_setting.json`` (``email.username`` + ``email.receiver``) or a WeChat
    bot in ``wechat_setting.json``.  Without one, ``main_engine
    .send_notification`` would start an ``EmailEngine`` thread that opens
    ``smtplib.SMTP_SSL`` with no socket timeout, and ``MainEngine.close()``
    then blocks on joining it - so callers skip the push and only log.
    """
    try:
        from vnpy.trader.setting import SETTINGS
        from vnpy.trader.utility import load_json
    except Exception:  # noqa: BLE001 - vnpy not importable: nothing to push through
        return False
    try:
        if SETTINGS.get("email.username") and SETTINGS.get("email.receiver"):
            return True
        wechat = load_json("wechat_setting.json")
        return bool(wechat.get("bot_id") and wechat.get("token"))
    except Exception:  # noqa: BLE001
        return False


def _log(strategy: Any, msg: str) -> None:
    """Log through the strategy when possible, else print."""
    try:
        strategy.write_log(msg)
    except Exception:  # noqa: BLE001
        print(msg)


def _sync(strategy: Any) -> None:
    """Persist strategy variables (no-op when the strategy has no sync_data)."""
    fn = getattr(strategy, "sync_data", None)
    if callable(fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Result objects
# --------------------------------------------------------------------------- #

@dataclass
class RiskDecision:
    """Outcome of ``RiskGuard.pre_bar`` / ``RiskGuard.tick``."""

    allow_entry: bool = True
    must_flatten: bool = False
    reason: str = ""
    #: pending entry orders should be cancelled (any "no entry" condition)
    cancel_entries: bool = False


@dataclass
class ReconcileResult:
    """Outcome of ``reconcile``: what the strategy state should become."""

    outcome: str                 # "ok" | "adopt" | "clear" | "desync"
    pos: float
    entry_price: float = 0.0
    highest_since_entry: float = 0.0
    lowest_since_entry: float = 0.0
    recompute_stop: bool = False
    reason: str = ""


@dataclass
class RiskLimits:
    """Numeric limits used by the guard (normally applied from DIALS)."""

    daily_loss_pct: float = 0.06
    max_dd_halt: float = 0.25
    max_consec_losses: int = 4
    cooldown_hours: float = 8.0
    max_trades_day: int = 3
    max_leverage: float = 3.0
    gross_leverage: float = 4.0
    max_orders_per_min: int = MAX_ORDERS_PER_MIN
    max_rejects: int = MAX_REJECTS

    @classmethod
    def from_strategy(cls, strategy: Any, dials: dict[str, float] | None = None,
                      suffix: str = "s1") -> RiskLimits:
        """
        Build limits from strategy attributes, falling back to ``dials`` and
        then to ``settings.DIALS[strategy.risk_dial]`` / the normal dial.
        """
        fallback: dict[str, float] | None = dials
        if fallback is None:
            fallback = load_dials(str(getattr(strategy, "risk_dial", "normal")))

        def pick(name: str, dial_name: str, default: float) -> float:
            val = getattr(strategy, name, None)
            if val is None:
                val = fallback.get(dial_name, default) if fallback else default
            return float(val)

        return cls(
            daily_loss_pct=pick("daily_loss_pct", "daily_loss_pct", 0.06),
            max_dd_halt=pick("max_dd_halt", "max_dd_halt", 0.25),
            max_consec_losses=int(pick("max_consec_losses", "max_consec_losses", 4)),
            cooldown_hours=pick("cooldown_hours", "cooldown_hours", 8.0),
            max_trades_day=int(pick("max_trades_day", f"max_trades_day_{suffix}", 3)),
            max_leverage=pick("max_leverage", "max_leverage", 3.0),
            gross_leverage=pick("gross_leverage", "gross_leverage", 4.0),
        )


# --------------------------------------------------------------------------- #
# Shared (account level) state
# --------------------------------------------------------------------------- #

def _default_shared() -> dict[str, Any]:
    return {
        "day_key": "",
        "day_start_equity": 0.0,
        "peak_equity": 0.0,
        "halted": False,
        "halt_reason": "",
        "locks": {},            # vt_symbol -> strategy_name
        "open_notional": {},    # strategy_name -> usd
        "consec_losses_global": 0,
    }


class SharedRiskState:
    """
    Account-level risk state shared by every strategy in the process.

    ``path=None`` -> in-memory only (backtest).  Otherwise the dict is loaded
    from and saved to ``path`` as JSON; writes are atomic (tmp + ``os.replace``)
    so a crash never leaves a half-written file.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path: Path | None = path
        self.data: dict[str, Any] = _default_shared()
        self.load()

    # -- persistence ------------------------------------------------------- #
    def load(self) -> None:
        """Re-read the file (other strategies may have written it)."""
        if self.path is None or not self.path.exists():
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                merged = _default_shared()
                merged.update(raw)
                self.data = merged
        except Exception:  # noqa: BLE001 - corrupt file: keep current values
            pass

    def save(self) -> None:
        """Atomically persist the dict (no-op in memory mode)."""
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001 - never let persistence kill a callback
            pass

    # -- typed accessors --------------------------------------------------- #
    @property
    def day_key(self) -> str:
        return str(self.data.get("day_key", ""))

    @day_key.setter
    def day_key(self, value: str) -> None:
        self.data["day_key"] = value

    @property
    def day_start_equity(self) -> float:
        return float(self.data.get("day_start_equity", 0.0))

    @day_start_equity.setter
    def day_start_equity(self, value: float) -> None:
        self.data["day_start_equity"] = float(value)

    @property
    def peak_equity(self) -> float:
        return float(self.data.get("peak_equity", 0.0))

    @peak_equity.setter
    def peak_equity(self, value: float) -> None:
        self.data["peak_equity"] = float(value)

    @property
    def halted(self) -> bool:
        return bool(self.data.get("halted", False))

    @halted.setter
    def halted(self, value: bool) -> None:
        self.data["halted"] = bool(value)

    @property
    def halt_reason(self) -> str:
        return str(self.data.get("halt_reason", ""))

    @halt_reason.setter
    def halt_reason(self, value: str) -> None:
        self.data["halt_reason"] = value

    @property
    def locks(self) -> dict[str, str]:
        locks = self.data.setdefault("locks", {})
        return locks  # type: ignore[no-any-return]

    @property
    def open_notional(self) -> dict[str, float]:
        notional = self.data.setdefault("open_notional", {})
        return notional  # type: ignore[no-any-return]

    @property
    def consec_losses_global(self) -> int:
        return int(self.data.get("consec_losses_global", 0))

    @consec_losses_global.setter
    def consec_losses_global(self, value: int) -> None:
        self.data["consec_losses_global"] = int(value)


# --------------------------------------------------------------------------- #
# Pure reconcile
# --------------------------------------------------------------------------- #

def reconcile(
    exchange_net_pos: float,
    local_pos: float,
    step: float,
    n_strategies_on_symbol: int = 1,
    exchange_entry_price: float = 0.0,
    last_price: float = 0.0,
    saved_highest: float = 0.0,
    saved_lowest: float = 0.0,
) -> ReconcileResult:
    """
    Compare the exchange net position with the strategy's persisted ``pos``.

    * within ``step/2``                       -> ``ok`` (nothing changes)
    * differs and >1 strategy on the symbol   -> ``desync`` (halt, notify)
    * differs and exchange is flat            -> ``clear`` (zero trade state)
    * differs and exchange holds a position   -> ``adopt`` (pos = exchange)

    Pure function: no side effects, fully testable.
    """
    tol = max(step, 0.0) / 2.0
    if abs(exchange_net_pos - local_pos) <= tol:
        return ReconcileResult("ok", local_pos, reason="in sync")

    if n_strategies_on_symbol > 1:
        return ReconcileResult(
            "desync", local_pos,
            reason=f"exchange {exchange_net_pos} != local {local_pos} with "
                   f"{n_strategies_on_symbol} strategies on symbol",
        )

    if abs(exchange_net_pos) <= tol:
        return ReconcileResult(
            "clear", 0.0,
            reason=f"exchange flat, local {local_pos} cleared",
        )

    entry = exchange_entry_price if exchange_entry_price > 0 else last_price
    highs = [x for x in (saved_highest, last_price, entry) if x > 0]
    lows = [x for x in (saved_lowest, last_price, entry) if x > 0]
    return ReconcileResult(
        "adopt", exchange_net_pos,
        entry_price=entry,
        highest_since_entry=max(highs) if highs else 0.0,
        lowest_since_entry=min(lows) if lows else 0.0,
        recompute_stop=True,
        reason=f"adopted exchange {exchange_net_pos} (local {local_pos})",
    )


# --------------------------------------------------------------------------- #
# Equity helper (SPEC section 2 "Equity")
# --------------------------------------------------------------------------- #

def get_equity(strategy: Any, close: float = 0.0) -> tuple[float, float]:
    """
    Return ``(balance, equity_mtm)`` for ``strategy``.

    LIVE: wallet balance of ``<gateway>.USDT`` (persisted ``strategy.equity``
    when the account is not there yet) plus unrealized pnl of this symbol's
    positions.  BACKTEST: ``capital + realized_pnl - fees_paid`` plus
    ``pos*(close-entry_price)*size``.  Any failure falls back to the persisted
    ``strategy.equity`` for both values and logs.
    """
    fallback = float(getattr(strategy, "equity", 0.0) or 0.0)
    try:
        pos = float(getattr(strategy, "pos", 0.0) or 0.0)
        if strategy.get_engine_type() == EngineType.BACKTESTING:
            capital = float(getattr(strategy, "capital", 0.0) or 0.0)
            realized = float(getattr(strategy, "realized_pnl", 0.0) or 0.0)
            fees = float(getattr(strategy, "fees_paid", 0.0) or 0.0)
            balance = capital + realized - fees
            px = close if close > 0 else float(getattr(strategy, "last_close", 0.0) or 0.0)
            entry = float(getattr(strategy, "entry_price", 0.0) or 0.0)
            size = float(getattr(strategy, "size", 0.0) or 0.0)
            if size <= 0:
                size = float(strategy.get_size() or 1.0)
            unreal = pos * (px - entry) * size if (pos and px > 0 and entry > 0) else 0.0
            equity_mtm = balance + unreal
        else:
            main_engine = strategy.cta_engine.main_engine
            contract = main_engine.get_contract(strategy.vt_symbol)
            gateway = contract.gateway_name if contract else str(
                getattr(strategy, "gateway_name", ""))
            acct = main_engine.get_account(f"{gateway}.USDT")
            balance = acct.balance if (acct and acct.balance > 0) else fallback
            unreal = 0.0
            unreal_all = 0.0
            for p in main_engine.get_all_positions():
                if p.gateway_name and gateway and p.gateway_name != gateway:
                    continue
                unreal_all += float(p.pnl or 0.0)
                if p.vt_symbol == strategy.vt_symbol:
                    unreal += float(p.pnl or 0.0)
            # OKX reports ``eq`` (already mark-to-market) as AccountData.balance
            # while Binance reports ``walletBalance``: derive the wallet for OKX
            # so unrealized pnl is never counted twice.
            if str(gateway).upper().startswith("OKX") and acct and acct.balance > 0:
                balance = acct.balance - unreal_all
            equity_mtm = balance + unreal
        if balance > 0:
            strategy.equity = balance
        return float(balance), float(equity_mtm)
    except Exception as exc:  # noqa: BLE001
        _log(strategy, f"get_equity failed ({exc!r}); using persisted equity {fallback}")
        return fallback, fallback


def exchange_net_position(strategy: Any) -> tuple[float, float] | None:
    """
    Signed net volume and entry price of this symbol on the exchange, read from
    ``main_engine.get_all_positions()`` (LONG +, SHORT -, NET signed).

    Returns ``None`` when the engine holds **no** PositionData row for the
    symbol: Binance ``/fapi/v3/positionRisk`` only lists symbols with an open
    position and the user stream only pushes positions that change, so an
    absent row means "unknown or flat", never simply "flat".
    """
    net = 0.0
    price = 0.0
    seen = False
    main_engine = strategy.cta_engine.main_engine
    for p in main_engine.get_all_positions():
        if p.vt_symbol != strategy.vt_symbol:
            continue
        seen = True
        vol = float(p.volume or 0.0)
        if p.direction == Direction.SHORT:
            vol = -abs(vol)
        elif p.direction == Direction.LONG:
            vol = abs(vol)
        net += vol
        if vol and p.price:
            price = float(p.price)
    if not seen:
        return None
    return net, price


def account_ready(strategy: Any) -> bool:
    """True once the gateway delivered the USDT account of this symbol's gateway."""
    main_engine = strategy.cta_engine.main_engine
    contract = main_engine.get_contract(strategy.vt_symbol)
    gateway = contract.gateway_name if contract else str(getattr(strategy, "gateway_name", ""))
    return main_engine.get_account(f"{gateway}.USDT") is not None


# --------------------------------------------------------------------------- #
# Exception wrapper (SPEC section 4.9)
# --------------------------------------------------------------------------- #

def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """
    Decorator for strategy callbacks: the first exception in a bar is logged,
    sets ``halted=True/"EXCEPTION"`` and returns ``None``; a second exception
    inside the same bar propagates so the engine stops the strategy and the
    watchdog re-inits it.  Requires ``self.guard`` (a RiskGuard); before it
    exists the function runs unwrapped.
    """

    @wraps(fn)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        guard = getattr(self, "guard", None)
        if guard is None:
            return fn(self, *args, **kwargs)
        return guard.safe_call(fn, self, *args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------- #
# RiskGuard
# --------------------------------------------------------------------------- #

class RiskGuard:
    """
    Per-strategy risk guard (SPEC section 4).  Reads and writes only the
    strategy's persisted variables and the account-level ``SharedRiskState``.
    """

    def __init__(
        self,
        strategy: Any,
        dials: dict[str, float] | None = None,
        state_dir: Path | None = None,
        shared: SharedRiskState | None = None,
        suffix: str = "s1",
    ) -> None:
        self.strategy = strategy
        self.limits = RiskLimits.from_strategy(strategy, dials, suffix)
        self.live: bool = self._detect_live()

        if state_dir is None:
            from vnpy.trader.utility import TEMP_DIR  # lazy: computed at import
            state_dir = Path(TEMP_DIR)
        self.state_dir: Path = Path(state_dir)

        if shared is not None:
            self.shared = shared
        elif self.live:
            self.shared = SharedRiskState(self.state_dir / STATE_FILENAME)
        else:
            self.shared = SharedRiskState(None)

        self._last_ts: float = 0.0
        self._last_close: float = 0.0
        self._last_tick_check: float = -1e18
        self._last_heartbeat: float = -1e18
        self._order_times: deque[float] = deque()
        self._exc_bar_key: str = ""
        self._last_decision: RiskDecision = RiskDecision()
        self._quiet_until: float = 0.0          # no unforced reconcile before this
        self._flat_pending_ts: float = 0.0      # first "no position row" reading awaiting confirmation
        self._not_ready_logged: bool = False
        self.pending_halt: str = ""             # halt raised while trading=False (sync is a no-op then)

    # ------------------------------------------------------------------ #
    # generic helpers
    # ------------------------------------------------------------------ #
    def _detect_live(self) -> bool:
        try:
            return bool(self.strategy.get_engine_type() != EngineType.BACKTESTING)
        except Exception:  # noqa: BLE001
            return False

    @property
    def name(self) -> str:
        return str(getattr(self.strategy, "strategy_name", "strategy"))

    @property
    def vt_symbol(self) -> str:
        return str(getattr(self.strategy, "vt_symbol", ""))

    def _get(self, attr: str, default: Any = 0.0) -> Any:
        val = getattr(self.strategy, attr, None)
        return default if val is None else val

    def _size(self) -> float:
        size = float(self._get("size", 0.0) or 0.0)
        if size > 0:
            return size
        try:
            return float(self.strategy.get_size() or 1.0)
        except Exception:  # noqa: BLE001
            return 1.0

    def _pricetick(self) -> float:
        tick = float(self._get("pricetick", 0.0) or 0.0)
        if tick > 0:
            return tick
        try:
            return float(self.strategy.get_pricetick() or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    def _step(self) -> float:
        step = float(self._get("min_volume", 0.0) or 0.0)
        if step <= 0:
            lot = getattr(self.strategy, "lot", None)
            step = float(getattr(lot, "step", 0.0) or 0.0)
        return step

    def is_flat(self, pos: float | None = None) -> bool:
        """
        True when ``pos`` (default: the strategy's) is zero within half a
        volume step.  The engine accumulates ``pos`` with binary floats, so
        partial fills can leave a residue such as ``-6.9e-18`` that must
        count as flat.
        """
        if pos is None:
            pos = float(self._get("pos", 0.0) or 0.0)
        return abs(pos) <= self._step() / 2.0

    def log(self, msg: str) -> None:
        _log(self.strategy, f"[risk] {msg}")

    def notify(self, msg: str) -> None:
        """Push through main_engine.send_notification when live; always log."""
        self.log(msg)
        if not self.live or not notifications_configured():
            return
        try:
            self.strategy.cta_engine.main_engine.send_notification(msg, f"{self.name} risk")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # halt management
    # ------------------------------------------------------------------ #
    @property
    def halted(self) -> bool:
        return bool(self._get("halted", False)) or self.shared.halted

    @property
    def halt_reason(self) -> str:
        own = str(self._get("halt_reason", ""))
        return own or self.shared.halt_reason

    def _reasons(self) -> set[str]:
        """Active halt reasons (own and shared), empty when not halted."""
        out: set[str] = set()
        if bool(self._get("halted", False)):
            out.add(str(self._get("halt_reason", "")))
        if self.shared.halted:
            out.add(self.shared.halt_reason)
        out.discard("")
        return out

    def halt(self, reason: str, account_wide: bool = False) -> bool:
        """
        Set ``halted=True/reason`` on the strategy (and the shared state when
        ``account_wide``).  A soft DAILY_LOSS halt may be upgraded to a hard
        one; hard reasons are never downgraded.  Returns True when changed.
        """
        changed = False
        s = self.strategy
        own_reason = str(self._get("halt_reason", ""))
        if not bool(self._get("halted", False)) or own_reason in ("", "DAILY_LOSS"):
            if not (bool(self._get("halted", False)) and own_reason == reason):
                s.halted = True
                s.halt_reason = reason
                changed = True
        if account_wide and (not self.shared.halted or
                             self.shared.halt_reason in ("", "DAILY_LOSS")):
            if not (self.shared.halted and self.shared.halt_reason == reason):
                self.shared.halted = True
                self.shared.halt_reason = reason
                changed = True
        if changed:
            self.notify(f"HALT {reason}")
            self.shared.save()
            _sync(s)
            if not bool(getattr(s, "trading", False)):
                # sync_data is a no-op while trading is False and CtaEngine
                # restores the persisted (unhalted) variables after on_init:
                # remember the halt so on_start can re-apply it.
                self.pending_halt = reason
        return changed

    def reapply_pending_halt(self) -> bool:
        """
        Re-apply a halt raised while ``trading`` was False (e.g. inside
        ``on_init``), which the engine's variable restore would otherwise
        discard.  Returns True when a halt was (re)applied.
        """
        reason = self.pending_halt
        self.pending_halt = ""
        if not reason:
            return False
        s = self.strategy
        if bool(self._get("halted", False)) and str(self._get("halt_reason", "")) == reason:
            return True
        s.halted = True
        s.halt_reason = reason
        self.log(f"re-applied halt {reason} raised before start")
        _sync(s)
        return True

    def clear_halt(self, reasons: frozenset[str] | set[str] = RESUMABLE_REASONS) -> bool:
        """Clear own and shared halts whose reason is in ``reasons``."""
        changed = False
        s = self.strategy
        if bool(self._get("halted", False)) and str(self._get("halt_reason", "")) in reasons:
            if str(s.halt_reason) == "DRAWDOWN":
                s.peak_equity = 0.0
            s.halted = False
            s.halt_reason = ""
            s.reject_count = 0
            changed = True
        if self.shared.halted and self.shared.halt_reason in reasons:
            if self.shared.halt_reason == "DRAWDOWN":
                self.shared.peak_equity = 0.0
            self.shared.halted = False
            self.shared.halt_reason = ""
            changed = True
        if changed:
            self.log("halt cleared")
            self.shared.save()
            _sync(s)
        return changed

    # ------------------------------------------------------------------ #
    # flag files
    # ------------------------------------------------------------------ #
    def _flag(self, name: str) -> Path:
        return self.state_dir / name

    def check_flags(self) -> tuple[bool, bool]:
        """
        Process KILL / PAUSE / RESUME files in the state dir.

        Returns ``(kill, pause)``.  RESUME clears resumable halts, then the
        RESUME file (and a KILL file, which RESUME overrides) are deleted.
        """
        if not self.live:      # backtests never read (or consume) the live operator flags
            return False, False
        try:
            if self._flag(FLAG_RESUME).exists():
                self.clear_halt(RESUMABLE_REASONS)
                for flag in (FLAG_RESUME, FLAG_KILL):
                    try:
                        self._flag(flag).unlink()
                    except FileNotFoundError:
                        pass
                self.log("RESUME processed")
            kill = self._flag(FLAG_KILL).exists()
            pause = self._flag(FLAG_PAUSE).exists()
        except Exception as exc:  # noqa: BLE001
            self.log(f"flag check failed: {exc!r}")
            return False, False
        if kill:
            self.halt("KILL", account_wide=True)
        return kill, pause

    # ------------------------------------------------------------------ #
    # core evaluation
    # ------------------------------------------------------------------ #
    def pre_bar(self, bar: BarData, equity_mtm: float, balance: float,
                unrealized_pnl: float | None = None) -> RiskDecision:
        """Run every check on a management bar (SPEC section 4, 1-7)."""
        ts = ts_of(bar.datetime)
        self._last_ts = ts
        self._last_close = float(bar.close_price)
        return self._evaluate(ts, day_key_of(bar.datetime), equity_mtm, balance,
                              unrealized_pnl, float(bar.close_price))

    def tick(self, tick: TickData, equity_mtm: float | None = None,
             balance: float | None = None) -> RiskDecision | None:
        """
        Live tick hook, throttled to one evaluation per 5 s.  Returns ``None``
        when throttled.  Without equity only flag files / halts are checked.
        """
        ts = ts_of(tick.datetime)
        self._last_ts = ts
        if tick.last_price:
            self._last_close = float(tick.last_price)
        if ts - self._last_tick_check < TICK_THROTTLE_SECS:
            return None
        self._last_tick_check = ts
        if equity_mtm is None:
            self.shared.load()
            kill, pause = self.check_flags()
            decision = self._decision(ts, kill, pause, None)
            self._last_decision = decision
            return decision
        return self._evaluate(ts, day_key_of(tick.datetime), equity_mtm,
                              balance if balance is not None else equity_mtm,
                              None, float(tick.last_price or self._last_close))

    def _evaluate(self, ts: float, day_key: str, equity_mtm: float, balance: float,
                  unrealized_pnl: float | None, close: float) -> RiskDecision:
        s = self.strategy
        sh = self.shared
        sh.load()
        changed = False

        # 1. flag files
        kill, pause = self.check_flags()

        # 2. day roll (UTC date of bar time)
        if day_key != str(self._get("day_key", "")) or day_key != sh.day_key:
            if day_key != sh.day_key:
                sh.day_key = day_key
                sh.day_start_equity = equity_mtm
                if sh.halted and sh.halt_reason == "DAILY_LOSS":
                    sh.halted = False
                    sh.halt_reason = ""
            s.day_key = day_key
            s.day_start_equity = sh.day_start_equity
            s.trades_today = 0
            if bool(self._get("halted", False)) and str(self._get("halt_reason", "")) == "DAILY_LOSS":
                s.halted = False
                s.halt_reason = ""
            changed = True
            self.log(f"day roll {day_key}: start equity {sh.day_start_equity:.4f}")

        if sh.day_start_equity <= 0:
            sh.day_start_equity = equity_mtm
            s.day_start_equity = equity_mtm
            changed = True

        # 4. drawdown halt (peak tracked first)
        if equity_mtm > sh.peak_equity:
            sh.peak_equity = equity_mtm
            changed = True
        s.peak_equity = sh.peak_equity
        dd_limit = sh.peak_equity * (1.0 - self.limits.max_dd_halt)
        if sh.peak_equity > 0 and equity_mtm <= dd_limit and not (
                self._reasons() & FLATTEN_REASONS):
            self.log(f"drawdown halt: equity {equity_mtm:.4f} <= {dd_limit:.4f} "
                     f"(peak {sh.peak_equity:.4f})")
            self.halt("DRAWDOWN", account_wide=True)
            changed = True

        # 3. daily loss (account level, realized + unrealized)
        dl_limit = sh.day_start_equity * (1.0 - self.limits.daily_loss_pct)
        daily_loss = sh.day_start_equity > 0 and equity_mtm <= dl_limit
        if daily_loss and not sh.halted:
            self.log(f"daily loss: equity {equity_mtm:.4f} <= {dl_limit:.4f}; "
                     f"no entries until next UTC day")
            self.halt("DAILY_LOSS", account_wide=True)
            changed = True

        # unrealized pnl of own position (for the daily-loss flatten rule)
        if unrealized_pnl is None:
            pos = float(self._get("pos", 0.0) or 0.0)
            entry = float(self._get("entry_price", 0.0) or 0.0)
            if pos and entry > 0 and close > 0:
                unrealized_pnl = pos * (close - entry) * self._size()
            else:
                unrealized_pnl = 0.0

        # lock release when flat and no entry order pending
        if self._release_lock_if_flat():
            changed = True

        if changed:
            sh.save()
            _sync(s)

        decision = self._decision(ts, kill, pause, unrealized_pnl)
        self._last_decision = decision
        return decision

    def _decision(self, ts: float, kill: bool, pause: bool,
                  unrealized_pnl: float | None) -> RiskDecision:
        """Translate the current state into a RiskDecision."""
        pos = float(self._get("pos", 0.0) or 0.0)
        has_pos = not self.is_flat(pos)
        d = RiskDecision()

        if kill:
            d.allow_entry = False
            d.cancel_entries = True
            d.must_flatten = has_pos
            d.reason = "KILL"
            return d

        reasons = self._reasons()
        if reasons:
            d.allow_entry = False
            d.cancel_entries = True
            d.reason = f"HALTED:{self.halt_reason}"
            if reasons & FLATTEN_REASONS:
                d.must_flatten = has_pos
            elif "DAILY_LOSS" in reasons:
                d.must_flatten = has_pos and (unrealized_pnl or 0.0) < 0
            return d

        if pause:
            d.allow_entry = False
            d.cancel_entries = True
            d.reason = "PAUSE"
            return d

        cooldown_until = float(self._get("cooldown_until", 0.0) or 0.0)
        if ts < cooldown_until:
            d.allow_entry = False
            d.cancel_entries = True
            d.reason = "COOLDOWN"
            return d

        if int(self._get("trades_today", 0) or 0) >= self.limits.max_trades_day:
            d.allow_entry = False
            d.cancel_entries = True
            d.reason = "TRADES_DAY"
            return d

        if not self.can_take_symbol():
            d.allow_entry = False
            d.cancel_entries = True
            d.reason = f"LOCKED:{self.shared.locks.get(self.vt_symbol, '')}"
            return d

        return d

    @property
    def last_decision(self) -> RiskDecision:
        return self._last_decision

    # ------------------------------------------------------------------ #
    # trades, streak, cooldown
    # ------------------------------------------------------------------ #
    def streak_mult(self) -> float:
        """Size multiplier ``0.5^(max(n-2,0))`` floored at 0.25."""
        n = int(self._get("consec_losses", 0) or 0)
        return max(0.25, 0.5 ** max(n - 2, 0))

    def on_trade_closed(self, pnl: float, ts: float | None = None) -> None:
        """
        Update ``consec_losses`` / ``trades_today`` after a round trip closes
        and start a cooldown when the streak reaches ``max_consec_losses``.
        The counter is kept (not reset) so ``streak_mult`` stays reduced until
        the next winning trade; every further loss at or above the threshold
        starts a fresh cooldown.
        """
        s = self.strategy
        if ts is None:
            ts = self._last_ts
        s.trades_today = int(self._get("trades_today", 0) or 0) + 1
        if pnl < 0:
            s.consec_losses = int(self._get("consec_losses", 0) or 0) + 1
            self.shared.consec_losses_global = self.shared.consec_losses_global + 1
        else:
            s.consec_losses = 0
            self.shared.consec_losses_global = 0
        n = int(s.consec_losses)
        if n >= self.limits.max_consec_losses:
            s.cooldown_until = ts + self.limits.cooldown_hours * 3600.0
            self.log(f"{n} consecutive losses -> cooldown "
                     f"{self.limits.cooldown_hours}h (mult {self.streak_mult():.2f})")
        self.shared.save()
        _sync(s)

    # ------------------------------------------------------------------ #
    # orders: pre-send sanity, rate, rejects
    # ------------------------------------------------------------------ #
    def check_order(
        self,
        intent: str,
        direction: Direction,
        price: float,
        volume: float,
        balance: float,
        last_price: float = 0.0,
        is_stop: bool = False,
        ref_close: float = 0.0,
        min_notional: float | None = None,
        ts: float | None = None,
    ) -> tuple[bool, str]:
        """
        Pre-send sanity (SPEC section 2).  ``intent`` is ``"entry"``,
        ``"stop"`` or ``"exit"`` (matched against ``<intent>_orderid`` for the
        duplicate check).  Returns ``(ok, reason)``; failure means skip.
        """
        if ts is None:
            ts = self._last_ts
        size = self._size()
        if volume <= 0:
            return False, "volume <= 0"
        notional = volume * size * price
        if min_notional is None:
            min_notional = float(self._get("min_notional", 0.0) or 0.0)
        if min_notional > 0 and notional < min_notional:
            return False, f"notional {notional:.2f} < min_notional {min_notional:.2f}"
        cap = LEVERAGE_SAFETY * balance * self.limits.max_leverage
        if notional > cap:
            return False, f"notional {notional:.2f} > {LEVERAGE_SAFETY}*balance*max_leverage {cap:.2f}"
        if self.live and last_price > 0 and not is_stop:
            dist = abs(price / last_price - 1.0)
            if dist > MAX_PRICE_DISTANCE:
                return False, f"price {price} is {dist:.2%} from last {last_price}"
        if is_stop and ref_close > 0:
            if direction == Direction.LONG and price <= ref_close:
                return False, f"buy stop {price} not above close {ref_close}"
            if direction == Direction.SHORT and price >= ref_close:
                return False, f"sell stop {price} not below close {ref_close}"
        tick = self._pricetick()
        if tick > 0:
            ratio = price / tick
            if abs(ratio - round(ratio)) > 1e-6:
                return False, f"price {price} not a multiple of pricetick {tick}"
        if self.orders_last_minute(ts) >= self.limits.max_orders_per_min:
            return False, f"order rate > {self.limits.max_orders_per_min}/min"
        existing = str(self._get(f"{intent}_orderid", "") or "")
        if existing:
            return False, f"duplicate {intent} order ({existing} active)"
        return True, ""

    def orders_last_minute(self, ts: float | None = None) -> int:
        """Number of orders recorded within the last 60 s."""
        if ts is None:
            ts = self._last_ts
        while self._order_times and ts - self._order_times[0] >= 60.0:
            self._order_times.popleft()
        return len(self._order_times)

    def note_order_sent(self, ts: float | None = None) -> None:
        """Record an order send time for the orders/min limit."""
        if ts is None:
            ts = self._last_ts
        self._order_times.append(ts)

    def on_order(self, order: Any) -> None:
        """
        Reject counter: ``Status.REJECTED`` increments, any accepted status
        resets; ``max_rejects`` consecutive rejects halt with ``"REJECTS"``.
        """
        s = self.strategy
        status = getattr(order, "status", None)
        if status == Status.REJECTED:
            s.reject_count = int(self._get("reject_count", 0) or 0) + 1
            self.log(f"order rejected ({s.reject_count}/{self.limits.max_rejects})")
            if s.reject_count >= self.limits.max_rejects:
                self.halt("REJECTS")
            _sync(s)
        elif status in (Status.NOTTRADED, Status.PARTTRADED, Status.ALLTRADED):
            if int(self._get("reject_count", 0) or 0):
                s.reject_count = 0
                _sync(s)

    # ------------------------------------------------------------------ #
    # gross cap and symbol locks
    # ------------------------------------------------------------------ #
    def free_notional(self, balance: float) -> float:
        """``gross_leverage*balance - sum(open_notional of other strategies)``."""
        self.shared.load()
        others = sum(float(v) for k, v in self.shared.open_notional.items() if k != self.name)
        return max(0.0, self.limits.gross_leverage * balance - others)

    def can_take_symbol(self) -> bool:
        owner = self.shared.locks.get(self.vt_symbol)
        return owner is None or owner == self.name

    def acquire_lock(self, notional: float) -> bool:
        """Lock the symbol for this strategy when an entry order is sent."""
        self.shared.load()
        if not self.can_take_symbol():
            return False
        self.shared.locks[self.vt_symbol] = self.name
        self.shared.open_notional[self.name] = float(notional)
        self.shared.save()
        return True

    def update_open_notional(self, notional: float) -> None:
        """Refresh this strategy's open notional (after fills)."""
        self.shared.load()
        self.shared.open_notional[self.name] = float(notional)
        self.shared.save()

    def release_lock(self) -> None:
        """Release the symbol lock and zero the open notional."""
        self.shared.load()
        changed = False
        if self.shared.locks.get(self.vt_symbol) == self.name:
            del self.shared.locks[self.vt_symbol]
            changed = True
        if self.name in self.shared.open_notional:
            del self.shared.open_notional[self.name]
            changed = True
        if changed:
            self.shared.save()

    def _release_lock_if_flat(self) -> bool:
        entry_id = str(self._get("entry_orderid", "") or "")
        if self.is_flat() and not entry_id and (
                self.shared.locks.get(self.vt_symbol) == self.name
                or self.name in self.shared.open_notional):
            self.release_lock()
            return True
        return False

    # ------------------------------------------------------------------ #
    # reconcile
    # ------------------------------------------------------------------ #
    def reconcile(
        self,
        exchange_net_pos: float,
        exchange_entry_price: float = 0.0,
        last_price: float = 0.0,
        n_strategies_on_symbol: int = 1,
        step: float | None = None,
        ts: float | None = None,
    ) -> ReconcileResult:
        """
        Apply the pure ``reconcile`` result to the strategy: adopt / clear the
        position state, or halt with ``"DESYNC"`` and notify.  Never sends an
        order.  ``sync_data()`` after any change.
        """
        s = self.strategy
        if step is None:
            step = float(self._get("min_volume", 0.0) or 0.0)
        if ts is None:
            ts = self._last_ts
        res = reconcile(
            exchange_net_pos, float(self._get("pos", 0.0) or 0.0), step,
            n_strategies_on_symbol, exchange_entry_price, last_price,
            float(self._get("highest_since_entry", 0.0) or 0.0),
            float(self._get("lowest_since_entry", 0.0) or 0.0),
        )
        s.last_reconcile_ts = ts
        if res.outcome == "ok":
            return res
        self.log(f"reconcile {res.outcome}: {res.reason}")
        if res.outcome == "desync":
            self.halt("DESYNC")
            return res
        s.pos = res.pos
        s.entry_price = res.entry_price
        s.highest_since_entry = res.highest_since_entry
        s.lowest_since_entry = res.lowest_since_entry
        if res.outcome == "clear":
            s.stop_price = 0.0
            s.bars_in_trade = 0
            s.entry_bar_ts = 0.0
            s.entry_orderid = ""
            s.stop_orderid = ""
            s.exit_orderid = ""
            self.release_lock()
        else:
            s.stop_price = 0.0     # recomputed by the strategy from current ATR
            s.stop_orderid = ""
        _sync(s)
        return res

    def note_fill(self, ts: float | None = None) -> None:
        """
        A fill / order event happened: suppress unforced reconciles for
        ``RECONCILE_QUIET_SECS`` so the exchange position push can land.
        """
        if ts is None:
            ts = self._last_ts
        self._quiet_until = max(self._quiet_until, ts + RECONCILE_QUIET_SECS)

    def _server_order_pending(self) -> bool:
        """An entry / exit order is resting on the exchange (not an engine-local stop)."""
        for name in ("entry_orderid", "exit_orderid"):
            oid = str(self._get(name, "") or "")
            if oid and not oid.startswith(STOPORDER_PREFIX):
                return True
        return False

    def reconcile_live(self, last_price: float, ts: float | None = None,
                       force: bool = False) -> ReconcileResult | None:
        """
        Live reconcile against ``main_engine`` positions, throttled to 60 s
        (``force=True`` for the first tick / after a trade).  Returns ``None``
        when skipped or when the engine could not be read.

        Safety rules (a wrong reconcile is worse than a late one):

        * nothing runs before the gateway delivered the USDT account (the
          position snapshot is requested right after it);
        * unforced runs are skipped for ``RECONCILE_QUIET_SECS`` after a fill
          and while a server entry / exit order is resting;
        * an absent position row (see ``exchange_net_position``) is only taken
          as "flat" when two consecutive readings >= ``RECONCILE_SECS`` apart
          agree; an explicit zero-volume row clears immediately.
        """
        if not self.live:
            return None
        if ts is None:
            ts = self._last_ts
        last = float(self._get("last_reconcile_ts", 0.0) or 0.0)
        if not force:
            if ts - last < RECONCILE_SECS or ts < self._quiet_until or self._server_order_pending():
                return None
        try:
            if not account_ready(self.strategy):
                if not self._not_ready_logged:
                    self._not_ready_logged = True
                    self.log("reconcile deferred: account / position data not received yet")
                self.strategy.last_reconcile_ts = ts
                return None
            found = exchange_net_position(self.strategy)
            n = len(self.strategy.cta_engine.symbol_strategy_map.get(self.vt_symbol, [])) or 1
        except Exception as exc:  # noqa: BLE001
            self.log(f"reconcile skipped: {exc!r}")
            return None
        if found is None:
            if self.is_flat():
                self._flat_pending_ts = 0.0
                self.strategy.last_reconcile_ts = ts
                return ReconcileResult("ok", float(self._get("pos", 0.0) or 0.0), reason="no row, local flat")
            if self._flat_pending_ts <= 0 or ts - self._flat_pending_ts < RECONCILE_SECS:
                if self._flat_pending_ts <= 0:
                    self._flat_pending_ts = ts
                    self.log(f"no position row for {self.vt_symbol} while local pos="
                             f"{self._get('pos', 0.0)}; clear pending confirmation in {RECONCILE_SECS:.0f}s")
                self.strategy.last_reconcile_ts = ts
                return None
            self.log("no position row confirmed twice: treating the exchange as flat")
            net, price = 0.0, 0.0
        else:
            net, price = found
        self._flat_pending_ts = 0.0
        return self.reconcile(net, price, last_price, n, ts=ts)

    # ------------------------------------------------------------------ #
    # heartbeat and exception wrapper
    # ------------------------------------------------------------------ #
    def heartbeat(self, ts: float | None = None) -> None:
        """Write ``.vntrader/heartbeat_<name>`` (epoch) at most every 10 s; live only."""
        if not self.live:
            return
        if ts is None:
            ts = self._last_ts
        if ts - self._last_heartbeat < HEARTBEAT_SECS:
            return
        self._last_heartbeat = ts
        try:
            path = self.state_dir / f"heartbeat_{self.name}"
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(f"{ts:.0f}", encoding="utf-8")
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001
            pass

    def safe_call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """
        Run ``fn``; on the first exception in the current bar log the traceback,
        halt with ``"EXCEPTION"`` and return ``None``; a second exception in
        the same bar propagates.
        """
        try:
            result = fn(*args, **kwargs)
        except Exception:
            key = str(self._get("last_1m_dt", "") or "") or f"{self._last_ts:.0f}"
            if self._exc_bar_key == key:
                raise
            self._exc_bar_key = key
            self.log(f"exception in {getattr(fn, '__name__', 'callback')}:\n"
                     f"{traceback.format_exc()}")
            self.halt("EXCEPTION")
            return None
        if self._exc_bar_key:
            # The halt is "for the current bar": a callback that completes
            # cleanly on a later bar clears an EXCEPTION halt automatically
            # (hard reasons are untouched; a second exception in the same bar
            # still propagates above).
            key = str(self._get("last_1m_dt", "") or "") or f"{self._last_ts:.0f}"
            if key != self._exc_bar_key:
                self._exc_bar_key = ""
                if self.clear_halt({"EXCEPTION"}):
                    self.log("EXCEPTION halt auto-cleared after a clean bar")
        return result


__all__ = [
    "FLATTEN_REASONS",
    "LEVERAGE_SAFETY",
    "RECONCILE_QUIET_SECS",
    "RESUMABLE_REASONS",
    "ReconcileResult",
    "RiskDecision",
    "RiskGuard",
    "RiskLimits",
    "SharedRiskState",
    "account_ready",
    "day_key_of",
    "exchange_net_position",
    "get_equity",
    "guarded",
    "load_dials",
    "notifications_configured",
    "reconcile",
    "ts_of",
]
