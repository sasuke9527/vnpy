#!/usr/bin/env python3
"""
Backtest runner for crypto_trader (SPEC section 7).

Runs one strategy on one symbol over one period through vnpy's
``BacktestingEngine`` (1-minute bars from ``.vntrader/database.db``), then
adds what the engine does not model:

* **funding** - walks ``engine.get_all_trades()`` in time order with a
  signed position and charges ``pos * size * close_at_stamp * rate`` at every
  8-hour funding stamp (00/08/16 UTC) inside a holding interval.  Rates come
  from ``.vntrader/funding_<BASE>.json`` (written by ``download_data.py
  --funding``); without a file a constant 0.0001 per 8 h is assumed.  Daily
  funding totals are subtracted from ``net_pnl`` and the statistics are
  recomputed, once at the measured rate and once as a stress pass (rates x2,
  or 0.0003 when falling back to the constant).
* **fee sweep** (``--fees-sweep``) - full re-runs at taker rates 0.0004 /
  0.0005 / 0.0007 (the strategy's own ``fee_rate`` follows the engine rate).
* **grid** (``--grid``) - the 3x3x3 grid of the strategy's tunables (SPEC
  sections 5/6) via ``vnpy.trader.optimize.run_bf_optimization``; prints the
  median and best Sharpe and whether the median is >= 70 % of the best cell.
* **acceptance gate** - the SPEC section 7 table with PASS / FAIL per rule
  (expectancy in R from ``strategy.trade_log``, Sharpe, drawdown, trade
  count, pnl after dropping the top 5 % of trades, fee / funding stress).

Usage::

    python backtest.py --strategy DonchianTrendH1 --symbol ETHUSDT_SWAP_BINANCE.GLOBAL \\
        --start 2024-01-01 --end 2024-12-31 [--capital 50] [--dial normal] [--rate 0.0005] \\
        [--slippage-pct 0.0003] [--set key=value ...] [--funding-file f.json] [--fees-sweep] \\
        [--grid] [--report out.json] [--chart out.html] [--trader-dir DIR]

Working directory: vnpy resolves ``.vntrader`` from the cwd at import time.
This script therefore, *before* importing vnpy, uses ``--trader-dir DIR``
when given, else keeps the cwd when it already holds a ``.vntrader``
folder (probe / test layouts), else ``chdir``s into the project folder.
The chart is written only with ``--chart PATH``; nothing is ever shown.
"""
from __future__ import annotations

import argparse
import functools
import importlib
import inspect
import json
import math
import os
import statistics as pystats
import sys
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJ: Path = Path(__file__).resolve().parent
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

import settings  # noqa: E402  (imports no vnpy code)


def _bootstrap(argv: Sequence[str]) -> Path:
    """
    Fix the working directory *before* the first vnpy import and return the
    ``.vntrader`` folder vnpy will use.  ``--trader-dir`` wins; otherwise a
    cwd that already contains ``.vntrader`` is kept; otherwise the project
    folder is used.  A ``vt_setting.json`` (UTC database timezone) is written
    only when missing.
    """
    if "vnpy.trader.utility" in sys.modules:   # already resolved by the host process (pytest)
        from vnpy.trader.utility import TEMP_DIR
        return Path(TEMP_DIR)
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--trader-dir", default=None)
    ns, _unknown = pre.parse_known_args(list(argv))
    if ns.trader_dir:
        work = Path(ns.trader_dir).expanduser().resolve()
        if work.name == ".vntrader":
            work = work.parent
        tdir = settings.ensure_trader_dir(work / ".vntrader")
        os.chdir(work)
        return tdir
    local = Path.cwd() / ".vntrader"
    if local.is_dir():
        return local.resolve()
    return settings.chdir_project()


WORK_TRADER_DIR: Path = _bootstrap(sys.argv[1:])

import numpy as np  # noqa: E402
from pandas import DataFrame  # noqa: E402
from vnpy.trader.constant import Direction, Interval  # noqa: E402
from vnpy.trader.object import BarData, TradeData  # noqa: E402
from vnpy.trader.optimize import OptimizationSetting, run_bf_optimization  # noqa: E402
from vnpy_ctastrategy.backtesting import BacktestingEngine, load_bar_data  # noqa: E402
from vnpy_ctastrategy.template import CtaTemplate  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STRATEGY_MODULES: dict[str, str] = {
    "DonchianTrendH1": "strategies.donchian_trend_h1",
    "SqueezeBreak15M": "strategies.squeeze_break_15m",
}

#: The only tunables the SPEC allows in optimisation (3x3x3 per strategy).
GRID_TUNABLES: dict[str, dict[str, list[float]]] = {
    "DonchianTrendH1": {"entry_window": [18, 24, 36], "stop_mult": [1.5, 2.0, 2.5], "trail_mult": [2.0, 2.5, 3.0]},
    "SqueezeBreak15M": {"min_squeeze_bars": [4, 6, 8], "stop_mult": [1.25, 1.5, 2.0], "trail_mult": [1.5, 2.0, 2.5]},
}
GRID_TARGET: str = "sharpe_ratio"

FEE_SWEEP_RATES: tuple[float, ...] = (0.0004, 0.0005, 0.0007)
FUNDING_HOURS_UTC: tuple[int, ...] = (0, 8, 16)
FUNDING_FALLBACK_RATE: float = 0.0001      # per 8 h when no funding file exists
FUNDING_FALLBACK_STRESS: float = 0.0003    # SPEC section 7 stress value for the fallback
FUNDING_STRESS_MULT: float = 2.0           # stress multiplier for measured rates
ANNUAL_DAYS: int = 365

#: Risk keys the dial overwrites unless ``risk_dial == "custom"``.
DIAL_KEYS: tuple[str, ...] = ("risk_pct", "max_leverage", "gross_leverage", "daily_loss_pct", "max_dd_halt",
                              "max_consec_losses", "cooldown_hours", "max_trades_day")

GATE_EXPECTANCY_R: float = 0.15
GATE_SHARPE: float = 1.0
GATE_MAX_DD_PCT: float = -25.0
GATE_MIN_TRADES: int = 100
GATE_GRID_RATIO: float = 0.70
GATE_DROP_TOP_FRAC: float = 0.05
GATE_ALT_SLIPPAGE: float = 0.0008
EXIT_GATE_FAIL_ABORTED: int = 3    # replay aborted / strategy exceptions: never a usable result

STAT_KEYS: tuple[str, ...] = (
    "total_net_pnl", "end_balance", "total_return", "annual_return", "max_drawdown", "max_ddpercent",
    "max_drawdown_duration", "sharpe_ratio", "ewm_sharpe", "return_drawdown_ratio", "total_commission",
    "total_slippage", "total_trade_count", "profit_days", "loss_days",
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _py(value: Any) -> Any:
    """Convert numpy scalars / dates / nested containers into JSON-able Python values."""
    if isinstance(value, dict):
        return {str(k): _py(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_py(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def parse_date(text: str) -> datetime:
    """``YYYY-MM-DD`` (or ISO datetime) -> naive datetime (a wall time in the database timezone)."""
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        dt = datetime.fromisoformat(text)
        return dt.replace(tzinfo=None) if dt.tzinfo is None else dt.astimezone(timezone.utc).replace(tzinfo=None)


def load_strategy_class(name: str) -> type[CtaTemplate]:
    """Import ``strategies.<module>`` and return the ``CtaTemplate`` subclass registered under ``name``."""
    module_name = STRATEGY_MODULES.get(name)
    if module_name is None:
        raise SystemExit(f"unknown strategy {name!r}; choose one of {sorted(STRATEGY_MODULES)}")
    module = importlib.import_module(module_name)
    cls = getattr(module, name)
    if not (inspect.isclass(cls) and issubclass(cls, CtaTemplate)):
        raise SystemExit(f"{module_name}.{name} is not a CtaTemplate subclass")
    return cls


def coerce_setting_value(cls: type[CtaTemplate], key: str, raw: str) -> Any:
    """Cast a ``--set key=value`` string to the type of the class-level default."""
    if key not in cls.parameters:
        raise SystemExit(f"{cls.__name__} has no parameter {key!r}; parameters: {', '.join(cls.parameters)}")
    default = getattr(cls, key, None)
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(float(raw))
    if isinstance(default, float):
        return float(raw)
    return raw


def parse_overrides(cls: type[CtaTemplate], items: Iterable[str]) -> dict[str, Any]:
    """``["key=value", ...]`` -> typed dict (errors are fatal with a helpful message)."""
    overrides: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--set expects key=value, got {item!r}")
        key, _, raw = item.partition("=")
        overrides[key.strip()] = coerce_setting_value(cls, key.strip(), raw)
    return overrides


def contract_specs(vt_symbol: str, trader_dir: Path) -> dict[str, Any]:
    """
    size / pricetick / min_notional / step for ``vt_symbol`` from
    ``exchange_filters.json`` (working trader dir first, then the project's,
    then the fallback table).  Binance: size 1; OKX: ``ctVal`` when the
    filters carry it, else 1 (volumes are then coins, not contracts).
    """
    exchange = settings.exchange_from_vt_symbol(vt_symbol)
    base = settings.base_from_vt_symbol(vt_symbol)
    name = settings.contract_name_for(exchange, base)
    candidates = [trader_dir / settings.EXCHANGE_FILTERS_FILE, settings.trader_file(settings.EXCHANGE_FILTERS_FILE)]
    source = "fallback table"
    filters = settings.load_exchange_filters(candidates[-1])
    for path in candidates:
        if path.exists():
            filters = settings.load_exchange_filters(path)
            source = str(path)
            break
    row = settings.filter_for(name, filters)
    size = 1.0
    warning = ""
    if exchange == "okx":
        if row.get("ctVal"):
            size = float(row["ctVal"])
        else:
            warning = (f"no ctVal for {name} in {source}: size=1 (volume in coins); run "
                       f"download_data.py --exchange okx --filters for contract-based sizing")
    pricetick = float(row.get("tickSize") or 0.0) or 0.01
    lot = settings.lot_info_for(name, size=size, pricetick=pricetick, filters=filters)
    return dict(exchange=exchange, base=base, name=name, size=size, pricetick=pricetick, step=lot.step,
                min_notional=lot.min_notional, filters_source=source, warning=warning)


# ---------------------------------------------------------------------------
# Engine parameters and a single run
# ---------------------------------------------------------------------------

@dataclass
class EngineParams:
    """Everything ``BacktestingEngine.set_parameters`` needs (picklable for grid workers)."""

    vt_symbol: str
    start: datetime
    end: datetime
    rate: float
    slippage: float          # absolute price units per unit volume (slippage_pct * reference price)
    size: float
    pricetick: float
    capital: float
    annual_days: int = ANNUAL_DAYS

    def apply(self, engine: BacktestingEngine) -> bool:
        """Call ``set_parameters``; returns whether ``annual_days`` was accepted by this vnpy version."""
        kwargs: dict[str, Any] = dict(vt_symbol=self.vt_symbol, interval=Interval.MINUTE, start=self.start,
                                      end=self.end, rate=self.rate, slippage=self.slippage, size=self.size,
                                      pricetick=self.pricetick, capital=self.capital)
        supported = "annual_days" in inspect.signature(engine.set_parameters).parameters
        if supported:
            kwargs["annual_days"] = self.annual_days
        engine.set_parameters(**kwargs)
        if not supported and hasattr(engine, "annual_days"):
            engine.annual_days = self.annual_days   # older/newer engines: attribute still drives the stats
            supported = True
        return supported


@dataclass
class BacktestRun:
    """Result of one engine run (kept in memory for post-processing)."""

    strategy_name: str
    setting: dict[str, Any]
    params: EngineParams
    engine: BacktestingEngine
    strategy: CtaTemplate
    df: DataFrame | None
    stats: dict[str, Any]
    trades: list[TradeData]
    trade_log: list[dict[str, Any]]
    ref_price: float
    bars: int
    annual_days_ok: bool
    exception_logs: int
    aborted: bool
    output_lines: list[str] = field(default_factory=list)


def reference_price(history: Sequence[BarData]) -> float:
    """Mean close over the loaded period; the absolute slippage is ``slippage_pct * this``."""
    if not history:
        return 0.0
    return float(sum(b.close_price for b in history) / len(history))


def run_backtest(strategy_name: str, params: EngineParams, setting: dict[str, Any],
                 slippage_pct: float | None = None, quiet: bool = True) -> BacktestRun:
    """
    Build a fresh engine, run the strategy and compute the raw statistics.
    When ``slippage_pct`` is given, ``params.slippage`` is (re)derived from
    the mean close of the loaded bars and stored back into ``params`` so that
    later passes reuse the identical absolute slippage.
    """
    cls = load_strategy_class(strategy_name)
    engine = BacktestingEngine()
    captured: list[str] = []

    def _output(msg: str) -> None:
        captured.append(str(msg))
        if not quiet:
            print(f"{datetime.now()}\t{msg}")

    engine.output = _output
    annual_ok = params.apply(engine)
    engine.add_strategy(cls, dict(setting))
    engine.load_data()
    history: list[BarData] = list(engine.history_data)
    if not history:
        raise SystemExit(f"no 1m bars for {params.vt_symbol} between {params.start:%Y-%m-%d} and "
                         f"{params.end:%Y-%m-%d} in {WORK_TRADER_DIR / 'database.db'}; run download_data.py "
                         f"or synth_data.py first")
    ref = reference_price(history)
    if slippage_pct is not None:
        params.slippage = float(slippage_pct) * ref
        engine.slippage = params.slippage
    engine.run_backtesting()
    aborted = any("触发异常" in line or "Traceback" in line for line in captured)
    df = engine.calculate_result()
    stats: dict[str, Any] = engine.calculate_statistics(output=False) if df is not None and not df.empty else {}
    strategy = engine.strategy
    trade_log = list(getattr(strategy, "trade_log", []) or [])
    exception_logs = sum(1 for line in engine.logs if "Traceback" in line or "EXCEPTION" in line)
    trades = sorted(engine.get_all_trades(), key=lambda t: (t.datetime or datetime.min, _tradeid_num(t)))
    return BacktestRun(strategy_name, dict(setting), params, engine, strategy, df, stats, trades, trade_log,
                       ref, len(history), annual_ok, exception_logs, aborted, captured)


def _tradeid_num(trade: TradeData) -> int:
    try:
        return int(str(trade.tradeid).split(".")[-1])
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Funding post-processing
# ---------------------------------------------------------------------------

@dataclass
class FundingResult:
    """Funding charges for one run at one rate scaling."""

    total: float
    daily: dict[date, float]
    stamps_charged: int
    stamps_from_file: int
    stamps_fallback: int
    rate_source: str
    multiplier: float
    fallback_rate: float
    charges: list[tuple[float, float, float, float]] = field(default_factory=list)   # (ts, pos, close, cost)


def load_funding_file(path: Path | None) -> dict[int, float]:
    """``[[fundingTime_ms, rate], ...]`` -> ``{hour_index: rate}`` (stamps floored to the hour)."""
    if path is None or not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"WARNING: cannot read funding file {path}: {exc!r}; using the fallback rate")
        return {}
    rates: dict[int, float] = {}
    for item in raw if isinstance(raw, list) else []:
        try:
            ms, rate = item
            rates[int(int(ms) // 3_600_000)] = float(rate)
        except (TypeError, ValueError):
            continue
    return rates


def funding_stamps(first_ts: float, last_ts: float) -> list[float]:
    """Epoch seconds of every 00/08/16 UTC stamp with ``first_ts < stamp <= last_ts``."""
    day = datetime.fromtimestamp(first_ts, tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    stamps: list[float] = []
    while True:
        for hour in FUNDING_HOURS_UTC:
            ts = (day + timedelta(hours=hour)).timestamp()
            if first_ts < ts <= last_ts:
                stamps.append(ts)
        if day.timestamp() > last_ts:
            break
        day += timedelta(days=1)
    return stamps


def compute_funding(trades: Sequence[TradeData], history: Sequence[BarData], size: float,
                    rates: dict[int, float], multiplier: float = 1.0,
                    fallback_rate: float = FUNDING_FALLBACK_RATE) -> FundingResult:
    """
    Walk the fills in time order with a signed position and charge
    ``pos * size * close_at_stamp * rate * multiplier`` at every 8 h stamp in
    ``(t_fill_i, t_fill_i+1]`` (positive = long pays).  ``close_at_stamp`` is
    the last 1m bar at or before the stamp.  Daily totals are keyed by the
    bar timezone's calendar date (the daily result index of the engine).
    """
    empty = FundingResult(0.0, {}, 0, 0, 0, "file" if rates else "fallback", multiplier, fallback_rate)
    if not history or not trades:
        return empty
    tz = history[0].datetime.tzinfo or timezone.utc
    bar_ts = [b.datetime.timestamp() for b in history]
    closes = [float(b.close_price) for b in history]
    stamps = funding_stamps(bar_ts[0], bar_ts[-1])
    if not stamps:
        return empty

    def close_at(ts: float) -> float:
        i = bisect_right(bar_ts, ts) - 1
        return closes[max(i, 0)]

    events: list[tuple[float, float]] = []
    for t in trades:
        if t.datetime is None:
            continue
        signed = float(t.volume) if t.direction == Direction.LONG else -float(t.volume)
        events.append((t.datetime.timestamp(), signed))
    if not events:
        return empty
    events.sort(key=lambda e: e[0])

    daily: dict[date, float] = {}
    charges: list[tuple[float, float, float, float]] = []
    total = 0.0
    n_file = n_fallback = 0
    pos = 0.0
    prev_ts = events[0][0]
    idx = 0
    n_events = len(events)
    while idx <= n_events:
        # collapse simultaneous fills before charging the interval that ends here
        cur_ts = events[idx][0] if idx < n_events else bar_ts[-1]
        if abs(pos) > 1e-12 and cur_ts > prev_ts:
            lo = bisect_right(stamps, prev_ts)
            hi = bisect_right(stamps, cur_ts)
            for s in stamps[lo:hi]:
                hour_key = int(s // 3600)
                if hour_key in rates:
                    rate = rates[hour_key]
                    n_file += 1
                else:
                    rate = fallback_rate
                    n_fallback += 1
                close = close_at(s)
                cost = pos * size * close * rate * multiplier
                total += cost
                d = datetime.fromtimestamp(s, tz=tz).date()
                daily[d] = daily.get(d, 0.0) + cost
                charges.append((s, pos, close, cost))
        if idx == n_events:
            break
        while idx < n_events and events[idx][0] == cur_ts:
            pos += events[idx][1]
            idx += 1
        prev_ts = cur_ts
    return FundingResult(total, daily, n_file + n_fallback, n_file, n_fallback,
                         "file" if rates else "fallback", multiplier, fallback_rate, charges)


def apply_funding(engine: BacktestingEngine, df: DataFrame, funding: FundingResult) -> tuple[DataFrame, dict[str, Any]]:
    """Subtract daily funding from ``net_pnl`` on a copy and recompute the engine statistics."""
    df2 = df.copy()
    adj = [float(funding.daily.get(d, 0.0)) for d in df2.index]
    missing = set(funding.daily) - set(df2.index)
    if missing:
        print(f"WARNING: {len(missing)} funding day(s) have no daily row and were dropped: "
              f"{sorted(missing)[:3]}...")
    df2["funding"] = adj
    df2["net_pnl"] = df2["net_pnl"] - df2["funding"]
    stats: dict[str, Any] = engine.calculate_statistics(df2, output=False)
    return df2, stats


# ---------------------------------------------------------------------------
# Trade-log analytics (expectancy in R, drop-top-5 %)
# ---------------------------------------------------------------------------

def trade_pnl_risk(row: dict[str, Any], slippage: float = 0.0, size: float = 1.0) -> tuple[float, float]:
    """
    ``(net_pnl, risk_usd)`` for one ``trade_log`` row.  Supports both
    strategy conventions: S1 rows carry ``pnl`` (net) + ``risk_usd`` + ``r``
    (ratio); S2 rows carry ``pnl`` (gross), ``fees``, ``net`` and ``r`` (the
    risk in USDT).

    ``slippage`` is the engine's absolute slippage per unit volume: the
    backtester books it only in the daily result (never in fill prices), so
    it is charged here on both legs (``2 * volume * size * slippage``) using
    the row's ``volume`` to keep the R statistics consistent with the
    engine's net pnl.
    """
    if "risk_usd" in row:
        pnl = float(row.get("net", row.get("pnl", 0.0)) or 0.0)
        risk = float(row.get("risk_usd") or 0.0)
    elif "net" in row:
        pnl, risk = float(row.get("net") or 0.0), float(row.get("r") or 0.0)
    else:
        pnl, risk = float(row.get("pnl") or 0.0), float(row.get("risk_usd") or 0.0)
    if slippage:
        pnl -= 2.0 * float(row.get("volume") or 0.0) * size * slippage
    return pnl, risk


def expectancy_r(trade_log: Sequence[dict[str, Any]], funding_total: float = 0.0,
                 slippage: float = 0.0, size: float = 1.0) -> dict[str, Any]:
    """
    Mean R per round trip (``pnl / risk_usd``).  ``funding_total`` (USDT over
    the whole run) is spread evenly over the trades and expressed in R via the
    mean risk, giving the after-funding expectancy.  ``slippage`` / ``size``
    charge the engine's per-unit slippage on every round trip (see
    ``trade_pnl_risk``).
    """
    rs: list[float] = []
    risks: list[float] = []
    pnls: list[float] = []
    wins = 0
    for row in trade_log:
        pnl, risk = trade_pnl_risk(row, slippage, size)
        pnls.append(pnl)
        if risk > 0:
            rs.append(pnl / risk)
            risks.append(risk)
            wins += pnl > 0
    n = len(rs)
    mean_r = float(pystats.fmean(rs)) if rs else 0.0
    mean_risk = float(pystats.fmean(risks)) if risks else 0.0
    funding_r = (funding_total / n) / mean_risk if (n and mean_risk > 0) else 0.0
    return dict(round_trips=len(trade_log), sized_trades=n, expectancy_r=mean_r,
                expectancy_r_after_funding=mean_r - funding_r, win_rate=(wins / n if n else 0.0),
                mean_risk_usd=mean_risk, sum_pnl=float(sum(pnls)),
                median_r=(float(pystats.median(rs)) if rs else 0.0),
                best_r=(max(rs) if rs else 0.0), worst_r=(min(rs) if rs else 0.0))


def drop_top_trades_pnl(trade_log: Sequence[dict[str, Any]], frac: float = GATE_DROP_TOP_FRAC,
                        slippage: float = 0.0, size: float = 1.0) -> dict[str, Any]:
    """Sum of trade pnl (net of fees and engine slippage) after removing the best ``ceil(frac * n)`` trades."""
    pnls = sorted((trade_pnl_risk(row, slippage, size)[0] for row in trade_log), reverse=True)
    n = len(pnls)
    k = int(math.ceil(frac * n)) if n else 0
    return dict(trades=n, dropped=k, dropped_pnl=float(sum(pnls[:k])), remaining_pnl=float(sum(pnls[k:])),
                total_pnl=float(sum(pnls)))


# ---------------------------------------------------------------------------
# Grid (brute-force optimisation of the SPEC tunables)
# ---------------------------------------------------------------------------

def _grid_evaluate(strategy_name: str, params: EngineParams, target: str,
                   setting: dict[str, Any]) -> tuple[dict[str, Any], float, dict[str, Any]]:
    """Worker function (module level so ``spawn`` workers can unpickle it): one cell -> (setting, target, stats)."""
    run = run_backtest(strategy_name, params, setting, quiet=True)
    stats = {k: _py(v) for k, v in run.stats.items()}
    stats["round_trips"] = len(run.trade_log)
    stats["exception_logs"] = run.exception_logs
    stats["aborted"] = run.aborted
    if run.aborted or run.exception_logs:
        return setting, float("-inf"), stats      # a truncated replay must never rank
    return setting, float(stats.get(target, 0.0) or 0.0), stats


def _target_value(result: tuple[dict[str, Any], float, dict[str, Any]]) -> float:
    return result[1]


def run_grid(strategy_name: str, params: EngineParams, base_setting: dict[str, Any],
             workers: int | None = None, target: str = GRID_TARGET) -> dict[str, Any]:
    """
    3x3x3 grid over ``GRID_TUNABLES[strategy_name]`` with every other setting
    fixed.  Uses ``vnpy.trader.optimize.run_bf_optimization`` (spawn process
    pool) with a worker that builds engines exactly like the main run
    (``annual_days=365``); ``workers=1`` or a pool failure runs serially.
    """
    tunables = GRID_TUNABLES.get(strategy_name)
    if not tunables:
        raise SystemExit(f"no grid tunables defined for {strategy_name}")
    opt = OptimizationSetting()
    opt.set_target(target)
    for key, value in base_setting.items():
        if key not in tunables:
            opt.add_parameter(key, value)
    for key, values in tunables.items():
        opt.params[key] = list(values)
    cells = opt.generate_settings()
    evaluate = functools.partial(_grid_evaluate, strategy_name, params, target)

    results: list[tuple[dict[str, Any], float, dict[str, Any]]]
    mode = "serial"
    if workers is None or workers > 1:
        try:
            results = run_bf_optimization(evaluate, opt, _target_value, max_workers=workers,  # type: ignore[arg-type]
                                          output=lambda msg: None)
            mode = f"process pool ({workers or os.cpu_count() or 1} workers)"
        except Exception as exc:  # noqa: BLE001 - sandbox without spawn support etc.
            print(f"WARNING: process-pool grid failed ({exc!r}); running the {len(cells)} cells serially")
            results = [evaluate(cell) for cell in cells]
    else:
        results = [evaluate(cell) for cell in cells]
    results.sort(key=_target_value, reverse=True)

    values = [r[1] for r in results if math.isfinite(r[1])]
    best = max(values) if values else 0.0
    median = float(pystats.median(values)) if values else 0.0
    ratio = (median / best) if best > 0 else None
    rows = []
    for setting, value, stats in results:
        rows.append(dict(cell={k: setting[k] for k in tunables}, **{target: value},
                         total_net_pnl=stats.get("total_net_pnl"), max_ddpercent=stats.get("max_ddpercent"),
                         total_trade_count=stats.get("total_trade_count"), round_trips=stats.get("round_trips")))
    return dict(target=target, tunables=tunables, cells=len(cells), mode=mode, best=best, median=median,
                median_over_best=ratio, passed=(ratio is not None and ratio >= GATE_GRID_RATIO),
                best_cell=(rows[0]["cell"] if rows else {}), best_cell_stats=(results[0][2] if results else {}),
                rows=rows)


# ---------------------------------------------------------------------------
# Acceptance gate
# ---------------------------------------------------------------------------

@dataclass
class GateRow:
    rule: str
    value: float | None
    threshold: str
    passed: bool | None      # None = not evaluated in this run
    note: str = ""

    def status(self) -> str:
        return "n/a" if self.passed is None else ("PASS" if self.passed else "FAIL")


def acceptance_gate(strategy_name: str, stats_funded: dict[str, Any], stats_stress: dict[str, Any],
                    expectancy: dict[str, Any], drop_top: dict[str, Any], funding_total: float,
                    months: float, slippage_pct: float, grid: dict[str, Any] | None,
                    fee_sweep: list[dict[str, Any]] | None, aborted: bool = False,
                    exception_logs: int = 0) -> list[GateRow]:
    """SPEC section 7 rules, one row each (rules needing --grid / --fees-sweep are n/a without them)."""
    rows: list[GateRow] = []
    clean = not aborted and exception_logs == 0
    rows.append(GateRow("replay completed without exceptions", float(exception_logs), "== 0", clean,
                        "engine aborted the replay" if aborted else ("strategy exception logs" if not clean else "")))
    exp = float(expectancy["expectancy_r_after_funding"])
    rows.append(GateRow("expectancy per trade (R, after funding)", exp, f">= {GATE_EXPECTANCY_R}",
                        exp >= GATE_EXPECTANCY_R, f"pre-funding {expectancy['expectancy_r']:.3f} R over "
                        f"{expectancy['sized_trades']} sized trades"))
    sharpe = float(stats_funded.get("sharpe_ratio", 0.0) or 0.0)
    rows.append(GateRow("sharpe_ratio (365 d, after funding)", sharpe, f">= {GATE_SHARPE}", sharpe >= GATE_SHARPE,
                        "must hold on the validate AND the test period (run this CLI per period)"))
    dd = float(stats_funded.get("max_ddpercent", 0.0) or 0.0)
    rows.append(GateRow("max_ddpercent (after funding)", dd, f">= {GATE_MAX_DD_PCT}", dd >= GATE_MAX_DD_PCT))
    n_trades = int(expectancy["round_trips"])
    need_months = 24 if strategy_name == "DonchianTrendH1" else 12
    months_ok = months >= need_months
    rows.append(GateRow("round trips", float(n_trades), f">= {GATE_MIN_TRADES}",
                        n_trades >= GATE_MIN_TRADES and months_ok,
                        f"over >= {need_months} months of data; this run covers {months:.1f} months"
                        + ("" if months_ok else f" (short by {need_months - months:.1f})")))
    remaining = float(drop_top["remaining_pnl"]) - funding_total
    rows.append(GateRow("pnl after dropping top 5 % trades (minus funding)", remaining, "> 0", remaining > 0,
                        f"dropped {drop_top['dropped']} trade(s) worth {drop_top['dropped_pnl']:.2f}"))
    if grid is not None:
        ratio = grid.get("median_over_best")
        rows.append(GateRow("grid median sharpe / best cell", ratio, f">= {GATE_GRID_RATIO}",
                            bool(grid.get("passed")) if ratio is not None else False,
                            f"best {grid['best']:.2f} median {grid['median']:.2f} over {grid['cells']} cells"))
    else:
        rows.append(GateRow("grid median sharpe / best cell", None, f">= {GATE_GRID_RATIO}", None, "run with --grid"))
    if fee_sweep:
        worst = max(fee_sweep, key=lambda r: r["rate"])
        pnl_hi = float(worst["stats_funded"].get("total_net_pnl", 0.0) or 0.0)
        rows.append(GateRow(f"net pnl at rate {worst['rate']} (after funding)", pnl_hi, "> 0", pnl_hi > 0))
    else:
        rows.append(GateRow("net pnl at rate 0.0007 (after funding)", None, "> 0", None, "run with --fees-sweep"))
    pnl_stress = float(stats_stress.get("total_net_pnl", 0.0) or 0.0)
    rows.append(GateRow("net pnl after funding stress pass", pnl_stress, "> 0", pnl_stress > 0))
    if strategy_name == "SqueezeBreak15M":
        rows.append(GateRow("S2: slippage_pct used (alt assumption)", slippage_pct, f">= {GATE_ALT_SLIPPAGE}",
                            slippage_pct >= GATE_ALT_SLIPPAGE, "expectancy rule above must pass with this slippage"))
    return rows


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.{digits}f}"
    return str(value)


def print_stats_table(columns: list[tuple[str, dict[str, Any]]], keys: Sequence[str] = STAT_KEYS) -> None:
    """Side-by-side statistics columns (raw / +funding / stress)."""
    width = 28
    print(" " * width + "".join(f"{name:>16}" for name, _ in columns))
    for key in keys:
        print(f"{key:<{width}}" + "".join(f"{_fmt(stats.get(key)):>16}" for _, stats in columns))


def print_gate(rows: Sequence[GateRow]) -> None:
    print("\n== acceptance gate (SPEC section 7) ==")
    print(f"{'rule':<52}{'value':>12}{'threshold':>12}  {'result':<6} note")
    for row in rows:
        digits = 4 if isinstance(row.value, float) and 0 < abs(row.value) < 0.01 else 3
        print(f"{row.rule:<52}{_fmt(row.value, digits):>12}{row.threshold:>12}  {row.status():<6} {row.note}")
    evaluated = [r for r in rows if r.passed is not None]
    skipped = len(rows) - len(evaluated)
    verdict = "PASS" if evaluated and all(r.passed for r in evaluated) else "FAIL"
    print(f"GATE: {verdict} ({len(evaluated)} rule(s) evaluated, {skipped} not evaluated)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="crypto_trader backtest (SPEC section 7)",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--strategy", required=True, choices=sorted(STRATEGY_MODULES))
    p.add_argument("--symbol", default=settings.vt_symbol_for(settings.DEFAULT_EXCHANGE, "ETHUSDT"),
                   help="vt_symbol, e.g. ETHUSDT_SWAP_BINANCE.GLOBAL or ETHUSDT_SWAP_OKX.GLOBAL")
    p.add_argument("--start", required=True, help="YYYY-MM-DD (database-timezone wall time, UTC by setting)")
    p.add_argument("--end", default=None, help="YYYY-MM-DD inclusive (default: today)")
    p.add_argument("--capital", type=float, default=50.0)
    p.add_argument("--dial", default="normal", choices=[*sorted(settings.DIALS), "custom"],
                   help="risk dial; 'custom' keeps the risk keys you pass with --set")
    p.add_argument("--rate", type=float, default=settings.FEES["taker"], help="taker fee rate (also the strategy's fee_rate)")
    p.add_argument("--slippage-pct", type=float, default=None,
                   help="slippage as a fraction of price (default: settings.FEES table for the base symbol)")
    p.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                   help="strategy parameter override (repeatable)")
    p.add_argument("--funding-file", default=None, help="[[fundingTime_ms, rate], ...] JSON (default: .vntrader/funding_<BASE>.json)")
    p.add_argument("--fees-sweep", action="store_true", help=f"re-run at rates {FEE_SWEEP_RATES}")
    p.add_argument("--grid", action="store_true", help="3x3x3 grid of the SPEC tunables (sharpe_ratio)")
    p.add_argument("--workers", type=int, default=None, help="grid worker processes (1 = serial in-process)")
    p.add_argument("--report", default=None, help="write the full JSON report here")
    p.add_argument("--chart", default=None, metavar="HTML", help="write the plotly chart to this file (never shown)")
    p.add_argument("--no-chart", action="store_true", help="accepted for compatibility (charts are off by default)")
    p.add_argument("--trader-dir", default=None, help="folder whose .vntrader holds database.db (default: cwd if it has one, else the project)")
    p.add_argument("--verbose", action="store_true", help="show engine output lines")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    cls = load_strategy_class(args.strategy)
    overrides = parse_overrides(cls, args.overrides)
    if args.dial != "custom":
        clashing = sorted(k for k in overrides if k in DIAL_KEYS)
        if clashing:
            print(f"WARNING: --set {clashing} will be overwritten by --dial {args.dial}; use --dial custom to keep them")

    start = parse_date(args.start)
    end = parse_date(args.end) if args.end else datetime.combine(date.today(), datetime.min.time())
    if start >= end:
        raise SystemExit(f"--start {start:%Y-%m-%d} must be before --end {end:%Y-%m-%d}")
    months = (end - start).days / 30.4375

    specs = contract_specs(args.symbol, WORK_TRADER_DIR)
    if specs["warning"]:
        print(f"WARNING: {specs['warning']}")
    slippage_pct = float(args.slippage_pct) if args.slippage_pct is not None else settings.slippage_for(specs["base"])

    setting: dict[str, Any] = {"capital": float(args.capital), "risk_dial": args.dial, "fee_rate": float(args.rate),
                               "slippage_pct": slippage_pct}
    setting.update(overrides)
    params = EngineParams(vt_symbol=args.symbol, start=start, end=end, rate=float(args.rate), slippage=0.0,
                          size=float(specs["size"]), pricetick=float(specs["pricetick"]), capital=float(args.capital))

    load_bar_data.cache_clear()
    print(f"== crypto_trader backtest: {args.strategy} on {args.symbol} {start:%Y-%m-%d}..{end:%Y-%m-%d} "
          f"({months:.1f} months) ==")
    print(f"trader dir: {WORK_TRADER_DIR}  filters: {specs['filters_source']}")
    run = run_backtest(args.strategy, params, setting, slippage_pct=slippage_pct, quiet=not args.verbose)
    print(f"engine: interval=1m rate={params.rate} slippage={params.slippage:.4f} ({slippage_pct}*{run.ref_price:.2f}) "
          f"size={params.size} pricetick={params.pricetick} capital={params.capital} "
          f"annual_days={params.annual_days if run.annual_days_ok else 'unsupported'} "
          f"min_notional={specs['min_notional']} step={specs['step']}")
    print(f"setting: {json.dumps(_py(setting), sort_keys=True)}")
    print(f"bars: {run.bars}  fills: {len(run.trades)}  round trips: {len(run.trade_log)}  "
          f"strategy exception logs: {run.exception_logs}  halted={getattr(run.strategy, 'halted', None)}"
          f"/{getattr(run.strategy, 'halt_reason', '')!r}")
    if run.aborted:
        print("ERROR: the engine aborted the replay on an exception (see engine output / --verbose)")
    if run.df is None or run.df.empty or not run.stats:
        print("no daily results; nothing to evaluate")
        return 2

    # -- funding ------------------------------------------------------------
    funding_path = Path(args.funding_file) if args.funding_file else WORK_TRADER_DIR / f"funding_{specs['base']}.json"
    rates = load_funding_file(funding_path)
    history = list(run.engine.history_data)
    if rates:
        funding = compute_funding(run.trades, history, params.size, rates, 1.0)
        stress = compute_funding(run.trades, history, params.size, rates, FUNDING_STRESS_MULT)
        stress_label = f"+funding x{FUNDING_STRESS_MULT:g}"
    else:
        funding = compute_funding(run.trades, history, params.size, {}, 1.0, FUNDING_FALLBACK_RATE)
        stress = compute_funding(run.trades, history, params.size, {}, 1.0, FUNDING_FALLBACK_STRESS)
        stress_label = f"+funding {FUNDING_FALLBACK_STRESS}"
    df_funded, stats_funded = apply_funding(run.engine, run.df, funding)
    _df_stress, stats_stress = apply_funding(run.engine, run.df, stress)
    src = (f"file {funding_path} ({funding.stamps_from_file} stamps from file, {funding.stamps_fallback} fallback)"
           if rates else f"no file at {funding_path}: constant {FUNDING_FALLBACK_RATE}/8h "
           f"(stress {FUNDING_FALLBACK_STRESS}/8h)")
    print(f"funding: {src}; charged {funding.stamps_charged} stamp(s): {funding.total:+.4f} USDT "
          f"(stress {stress.total:+.4f})")
    print("\n== statistics ==")
    columns = [("raw", run.stats), ("+funding", stats_funded), (stress_label, stats_stress)]
    print_stats_table(columns)
    print(f"{'funding_total':<28}{0.0:>16.4f}{funding.total:>16.4f}{stress.total:>16.4f}")

    expectancy = expectancy_r(run.trade_log, funding.total, params.slippage, params.size)
    drop_top = drop_top_trades_pnl(run.trade_log, slippage=params.slippage, size=params.size)
    print(f"\nexpectancy: {expectancy['expectancy_r']:.3f} R pre-funding, "
          f"{expectancy['expectancy_r_after_funding']:.3f} R after funding; win rate "
          f"{expectancy['win_rate']:.2%}; mean risk {expectancy['mean_risk_usd']:.3f} USDT; "
          f"median {expectancy['median_r']:.2f} R best {expectancy['best_r']:.2f} R worst {expectancy['worst_r']:.2f} R")
    print(f"drop top 5 %: {drop_top['dropped']} trade(s) worth {drop_top['dropped_pnl']:.4f} of "
          f"{drop_top['total_pnl']:.4f} -> remaining {drop_top['remaining_pnl']:.4f} USDT")

    # -- fee sweep ------------------------------------------------------------
    fee_sweep: list[dict[str, Any]] | None = None
    if args.fees_sweep:
        fee_sweep = []
        print(f"\n== fee sweep {FEE_SWEEP_RATES} ==")
        print(f"{'rate':<8}{'net_pnl':>12}{'+funding':>12}{'sharpe':>10}{'max_dd%':>10}{'fills':>7}{'trips':>7}")
        for rate in FEE_SWEEP_RATES:
            if rate == params.rate and setting.get("fee_rate") == rate:
                pass_run = run
            else:
                pass_params = EngineParams(**{**asdict(params), "rate": rate})
                pass_run = run_backtest(args.strategy, pass_params, {**setting, "fee_rate": rate}, quiet=True)
            if pass_run.df is None or pass_run.df.empty:
                continue
            pass_hist = list(pass_run.engine.history_data)
            pass_funding = (compute_funding(pass_run.trades, pass_hist, params.size, rates, 1.0) if rates
                            else compute_funding(pass_run.trades, pass_hist, params.size, {}, 1.0, FUNDING_FALLBACK_RATE))
            _df, pass_stats = apply_funding(pass_run.engine, pass_run.df, pass_funding)
            row = dict(rate=rate, stats_raw={k: _py(v) for k, v in pass_run.stats.items()},
                       stats_funded={k: _py(v) for k, v in pass_stats.items()}, funding_total=pass_funding.total,
                       round_trips=len(pass_run.trade_log), exception_logs=pass_run.exception_logs)
            fee_sweep.append(row)
            print(f"{rate:<8}{_fmt(pass_run.stats.get('total_net_pnl')):>12}{_fmt(pass_stats.get('total_net_pnl')):>12}"
                  f"{_fmt(pass_stats.get('sharpe_ratio'), 2):>10}{_fmt(pass_stats.get('max_ddpercent'), 2):>10}"
                  f"{_fmt(pass_stats.get('total_trade_count')):>7}{len(pass_run.trade_log):>7}")

    # -- grid -----------------------------------------------------------------
    grid: dict[str, Any] | None = None
    if args.grid:
        print(f"\n== grid {GRID_TUNABLES[args.strategy]} target={GRID_TARGET} ==")
        grid = run_grid(args.strategy, params, setting, workers=args.workers)
        ratio = grid["median_over_best"]
        print(f"{grid['cells']} cells via {grid['mode']}: best sharpe {grid['best']:.3f} "
              f"median {grid['median']:.3f} median/best {_fmt(ratio, 2)} "
              f"-> {'PASS' if grid['passed'] else 'FAIL'} (>= {GATE_GRID_RATIO})")
        print(f"{'cell':<48}{'sharpe':>9}{'net_pnl':>10}{'max_dd%':>9}{'fills':>7}")
        for row in grid["rows"]:
            print(f"{json.dumps(row['cell']):<48}{_fmt(row[GRID_TARGET], 3):>9}{_fmt(row['total_net_pnl'], 3):>10}"
                  f"{_fmt(row['max_ddpercent'], 2):>9}{_fmt(row['total_trade_count']):>7}")

    # -- gate -----------------------------------------------------------------
    gate = acceptance_gate(args.strategy, stats_funded, stats_stress, expectancy, drop_top, funding.total,
                           months, slippage_pct, grid, fee_sweep, run.aborted, run.exception_logs)
    print_gate(gate)
    if run.aborted or run.exception_logs:
        print("ERROR: statistics above come from a truncated / exception-ridden replay; verdict forced to FAIL")

    # -- chart / report -------------------------------------------------------
    if args.chart:
        fig = run.engine.show_chart(df_funded)
        if fig is not None:
            fig.write_html(args.chart)
            print(f"chart written to {args.chart}")
    if args.report:
        report = dict(
            generated=datetime.now(timezone.utc).isoformat(),
            args=vars(args), strategy=args.strategy, vt_symbol=args.symbol, setting=setting,
            engine=asdict(params) | {"annual_days_supported": run.annual_days_ok, "ref_price": run.ref_price,
                                      "slippage_pct": slippage_pct},
            contract=specs, bars=run.bars, fills=len(run.trades), months=months,
            aborted=run.aborted, exception_logs=run.exception_logs,
            strategy_state={k: getattr(run.strategy, k, None) for k in getattr(run.strategy, "variables", [])},
            stats_raw=run.stats, stats_funded=stats_funded, stats_funding_stress=stats_stress,
            funding=dict(source=funding.rate_source, file=str(funding_path), total=funding.total,
                         stress_total=stress.total, stress_label=stress_label, stamps_charged=funding.stamps_charged,
                         stamps_from_file=funding.stamps_from_file, stamps_fallback=funding.stamps_fallback,
                         charges=[dict(ts=datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(), pos=pos,
                                       close=close, cost=cost) for ts, pos, close, cost in funding.charges]),
            expectancy=expectancy, drop_top=drop_top, fee_sweep=fee_sweep, grid=grid,
            gate=[asdict(r) | {"status": r.status()} for r in gate],
            trade_log=run.trade_log,
            daily=[dict(date=d, net_pnl=row["net_pnl"], funding=row["funding"], balance=row["balance"],
                        trade_count=row["trade_count"]) for d, row in df_funded.iterrows()],
        )
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(_py(report), indent=2, sort_keys=False), encoding="utf-8")
        print(f"report written to {out}")
    if run.aborted or run.exception_logs:
        return EXIT_GATE_FAIL_ABORTED
    return 0


if __name__ == "__main__":
    sys.exit(main())
