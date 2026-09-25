"""
Headless live runner for crypto_trader (SPEC section 8).

Sequence (``python run_live.py``):

1. ``chdir`` into the project and create ``.vntrader`` *before* the first
   vnpy import, so every state file (``cta_strategy_setting.json``,
   ``cta_strategy_data.json``, ``risk_state.json``, heartbeats, flag files,
   ``log/``) lives in ``crypto_trader/.vntrader``.
2. Read ``.env`` (``settings.load_env``), refresh ``exchange_filters.json``
   from the public exchangeInfo / instruments endpoint (the old file is kept
   when the fetch fails).
3. ``EventEngine`` -> ``MainEngine`` -> ``add_gateway`` -> ``add_app`` ->
   ``connect`` -> wait for the deployed contracts (<= 120 s, +3 s for the
   OKX websockets) -> ``cta.init_engine()`` -> add / refresh the
   ``settings.DEPLOYMENT`` strategies.
4. Cancel orphan orders on the deployed symbols (fills of pre-restart
   orders would never reach ``pos``; local stops died with the process).
5. ``init_strategy(name).result(timeout=600)`` then ``start_strategy``.
6. Watchdog every 10 s: a strategy whose ``trading`` flag dropped (the
   engine clears ``inited`` *and* ``trading`` on any callback exception) is
   re-initialised and restarted, at most 3 times per rolling hour; the 4th
   time the runner writes ``KILL``, sends a notification and exits 2.  A
   heartbeat file older than 180 s makes the runner exit 3 so the service
   manager restarts the process.
7. SIGINT / SIGTERM: stop strategies, close gateways, exit 0.

Flags:

* ``--dry-run``  everything except network: validates ``.env``, prints the
  resolved gateway setting with secrets masked, checks the filters file,
  discovers the strategy classes through ``CtaEngine.load_strategy_class``
  and instantiates the deployed strategies once.
* ``--paper``    forces ``Server=TESTNET`` (Binance) / ``Server=DEMO`` (OKX).
* ``--env PATH`` read another env file (default ``crypto_trader/.env``).
* ``--no-filters`` skip the exchangeInfo refresh (use the existing file).

Exit codes: 0 ok / stopped by signal, 1 configuration error, 2 watchdog
gave up (KILL written), 3 stale heartbeat, 4 contract never arrived or a
strategy failed to initialise.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

PROJ: Path = Path(__file__).resolve().parent

# vnpy fixes TRADER_DIR at first import from cwd/.vntrader.  When run as a
# script we chdir into the project first; when imported (tests) the importer
# has already prepared its own trader dir and must not be disturbed.
if __name__ == "__main__":  # pragma: no cover - exercised by the CLI only
    (PROJ / ".vntrader").mkdir(parents=True, exist_ok=True)
    os.chdir(PROJ)
    if str(PROJ) not in sys.path:
        sys.path.insert(0, str(PROJ))

import settings as cfg  # noqa: E402  (imports no vnpy code)

from vnpy.trader.setting import SETTINGS  # noqa: E402

# Log settings must be in place before vnpy.trader.logger is imported (it
# configures the loguru sinks at import time from SETTINGS).
SETTINGS["log.active"] = True
SETTINGS["log.level"] = logging.INFO
SETTINGS["log.console"] = True
SETTINGS["log.file"] = True

from vnpy.event import EventEngine  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy.trader.logger import logger  # noqa: E402
from vnpy.trader.object import ContractData, OrderData  # noqa: E402
from vnpy_ctastrategy import CtaEngine, CtaStrategyApp  # noqa: E402
from vnpy_ctastrategy.template import CtaTemplate  # noqa: E402

import download_data as dl  # noqa: E402
import risk  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTRACT_WAIT_SECS: float = 120.0
ACCOUNT_WAIT_SECS: float = 60.0
OKX_WS_SETTLE_SECS: float = 3.0
ORDER_QUERY_SETTLE_SECS: float = 3.0
ORPHAN_SETTLE_SECS: float = 2.0
INIT_TIMEOUT_SECS: float = 600.0
WATCHDOG_SECS: float = 10.0
MAX_REINITS_PER_HOUR: int = 3
HEARTBEAT_STALE_SECS: float = 180.0

EXIT_OK: int = 0
EXIT_CONFIG: int = 1
EXIT_WATCHDOG: int = 2
EXIT_STALE: int = 3
EXIT_STARTUP: int = 4

SECRET_KEYS: frozenset[str] = frozenset({"API Key", "API Secret", "Secret Key", "Passphrase"})

_stop_requested: bool = False


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def log(msg: str, level: str = "INFO") -> None:
    """Runner log line through vnpy's loguru logger (console + ``.vntrader/log``)."""
    logger.bind(gateway_name="run_live").log(level, msg)


def mask(value: Any) -> str:
    """Never print a secret: only whether it is set and how long it is."""
    text = str(value or "")
    return f"<set, {len(text)} chars>" if text else "<empty>"


def masked_setting(setting: dict[str, Any]) -> dict[str, Any]:
    """Copy of a gateway setting with the secret fields masked."""
    return {k: (mask(v) if k in SECRET_KEYS else v) for k, v in setting.items()}


def request_stop(signum: int, frame: Any) -> None:
    """SIGINT / SIGTERM handler: ask the main loop to finish gracefully."""
    global _stop_requested
    _stop_requested = True
    log(f"signal {signum} received; stopping", "WARNING")


def sleep_interruptible(seconds: float) -> None:
    """Sleep in 1 s slices so a stop request is honoured quickly."""
    end = time.time() + seconds
    while not _stop_requested and time.time() < end:
        time.sleep(min(1.0, max(0.0, end - time.time())))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config(env_path: Path | None, paper: bool) -> dict[str, str]:
    """``settings.load_env`` plus the ``--paper`` override."""
    env = cfg.load_env(env_path)
    if paper:
        env["BINANCE_SERVER"] = "TESTNET"
        env["OKX_SERVER"] = "DEMO"
    return env


def validate_env(env: dict[str, str], exchange: str) -> list[str]:
    """Return a list of configuration problems (empty when everything is usable)."""
    problems: list[str] = []
    if exchange == "binance_linear":
        if not env.get("BINANCE_API_KEY"):
            problems.append("BINANCE_API_KEY is empty")
        if not env.get("BINANCE_API_SECRET"):
            problems.append("BINANCE_API_SECRET is empty")
    elif exchange == "okx":
        for key in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE"):
            if not env.get(key):
                problems.append(f"{key} is empty")
    try:
        cfg.gateway_setting(exchange, env)
    except ValueError as exc:
        problems.append(str(exc))
    try:
        cfg.risk_dial_from_env(env)
    except ValueError as exc:
        problems.append(str(exc))
    try:
        if cfg.equity_from_env(env) <= 0:
            problems.append("CT_EQUITY_USDT must be positive")
    except ValueError:
        problems.append(f"CT_EQUITY_USDT is not a number: {env.get('CT_EQUITY_USDT')!r}")
    if not cfg.DEPLOYMENT:
        problems.append("settings.DEPLOYMENT is empty; nothing to run")
    return problems


def resolve_deployment(exchange: str, env: dict[str, str]) -> list[tuple[str, str, str, dict[str, Any]]]:
    """
    ``settings.DEPLOYMENT`` rewritten for the configured exchange, with the
    ``CT_RISK_DIAL`` dial and ``CT_EQUITY_USDT`` (strategy ``capital``, the
    equity fallback for the startup race) applied to every entry.
    """
    dial = cfg.risk_dial_from_env(env)
    capital = cfg.equity_from_env(env)
    resolved: list[tuple[str, str, str, dict[str, Any]]] = []
    for name, class_name, vt_symbol, setting in cfg.deployment_for(exchange):
        merged = dict(setting)
        merged["risk_dial"] = dial
        merged["capital"] = capital
        resolved.append((name, class_name, vt_symbol, merged))
    return resolved


def refresh_filters(exchange: str, env: dict[str, str]) -> bool:
    """
    Fetch the exchange filters (Binance exchangeInfo / OKX instruments) into
    ``.vntrader/exchange_filters.json``.  On any failure the previous file is
    left untouched and ``False`` is returned.
    """
    try:
        session = dl.build_session(env)
        rows = dl.download_filters(exchange, session, save=True)
    except Exception as exc:  # noqa: BLE001 - network problems must not stop the runner
        log(f"exchange filters refresh failed ({exc!r}); keeping the existing file", "WARNING")
        return False
    log(f"exchange filters refreshed: {len(rows)} instruments -> {dl.filters_file()}")
    return True


def report_filters(exchange: str, deployment: Sequence[tuple[str, str, str, dict[str, Any]]]) -> list[str]:
    """Log the filter row of every deployed contract; return the names that fell back to the table."""
    filters = cfg.load_exchange_filters()
    filters_path = cfg.trader_file(cfg.EXCHANGE_FILTERS_FILE)
    in_file: set[str] = set()
    try:
        raw = json.loads(filters_path.read_text(encoding="utf-8"))
        in_file = {str(k) for k in raw} if isinstance(raw, dict) else set()
    except (OSError, ValueError):
        log(f"{filters_path.name} missing or unreadable; using the fallback table", "WARNING")
    fallback: list[str] = []
    for _name, _cls, vt_symbol, _setting in deployment:
        contract_name = cfg.contract_name_for(exchange, cfg.base_from_vt_symbol(vt_symbol))
        row = cfg.filter_for(contract_name, filters)
        source = filters_path.name if contract_name in in_file else "FALLBACK table"
        if contract_name not in in_file:
            fallback.append(contract_name)
        log(f"filters {contract_name} [{source}]: tickSize={row.get('tickSize')} stepSize={row.get('stepSize')} "
            f"minQty={row.get('minQty')} minNotional={row.get('minNotional')}"
            + (f" ctVal={row['ctVal']}" if "ctVal" in row else ""))
    return fallback


# ---------------------------------------------------------------------------
# Engine plumbing
# ---------------------------------------------------------------------------

def build_engines(exchange: str | None) -> tuple[MainEngine, CtaEngine, str]:
    """``EventEngine`` + ``MainEngine`` + optional gateway + CTA app; returns (main, cta, gateway_name)."""
    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    gateway_name = ""
    if exchange:
        gateway = main_engine.add_gateway(cfg.gateway_class(exchange))
        gateway_name = gateway.gateway_name
    cta_engine = cast(CtaEngine, main_engine.add_app(CtaStrategyApp))
    return main_engine, cta_engine, gateway_name


def wait_for_contracts(main_engine: MainEngine, vt_symbols: Sequence[str],
                       timeout: float = CONTRACT_WAIT_SECS) -> bool:
    """Poll ``get_contract`` for every deployed symbol; ``False`` on timeout or stop request."""
    deadline = time.time() + timeout
    missing = list(vt_symbols)
    while missing and time.time() < deadline and not _stop_requested:
        missing = [s for s in missing if main_engine.get_contract(s) is None]
        if missing:
            time.sleep(1.0)
    for vt_symbol in vt_symbols:
        contract: ContractData | None = main_engine.get_contract(vt_symbol)
        if contract:
            log(f"contract {vt_symbol}: name={contract.name} size={contract.size} pricetick={contract.pricetick} "
                f"min_volume={contract.min_volume} gateway={contract.gateway_name}")
    if missing:
        log(f"contracts not received within {timeout:.0f}s: {missing}", "ERROR")
        return False
    return True


def wait_for_account(main_engine: MainEngine, gateway_name: str, timeout: float = ACCOUNT_WAIT_SECS) -> bool:
    """
    Poll ``get_account(<gateway>.USDT)``: both gateways deliver the account
    right before the position snapshot, and ``RiskGuard.reconcile_live``
    refuses to run until it is there.  ``False`` on timeout (startup aborts:
    a strategy that cannot reconcile must not trade).
    """
    vt_accountid = f"{gateway_name}.USDT"
    deadline = time.time() + timeout
    while time.time() < deadline and not _stop_requested:
        acct = main_engine.get_account(vt_accountid)
        if acct is not None:
            log(f"account {vt_accountid}: balance={acct.balance} frozen={acct.frozen}; "
                f"positions known: {[(p.vt_symbol, p.volume) for p in main_engine.get_all_positions()]}")
            return True
        time.sleep(1.0)
    log(f"account {vt_accountid} not received within {timeout:.0f}s (no USDT balance, wrong keys or "
        f"permissions?); refusing to start", "ERROR")
    return False


def cancel_orphans(main_engine: MainEngine, vt_symbols: Sequence[str]) -> int:
    """Cancel every active order on the deployed symbols (orders of a previous process are orphans)."""
    symbols = set(vt_symbols)
    count = 0
    for order in main_engine.get_all_active_orders():
        o: OrderData = order
        if o.vt_symbol not in symbols:
            continue
        side = o.direction.value if o.direction else "?"
        log(f"cancelling orphan order {o.vt_orderid} {side} {o.volume} @ {o.price}", "WARNING")
        main_engine.cancel_order(o.create_cancel_request(), o.gateway_name)
        count += 1
    if count:
        time.sleep(ORPHAN_SETTLE_SECS)
    return count


def ensure_strategies(cta_engine: CtaEngine,
                      deployment: Sequence[tuple[str, str, str, dict[str, Any]]]) -> list[str]:
    """
    Make sure every ``DEPLOYMENT`` entry exists in the CTA engine with the
    resolved setting.  Saved strategies (re-added by ``init_engine``) get
    their setting refreshed through ``edit_strategy``; a saved entry whose
    ``vt_symbol`` differs from the deployment is a hard error (the engine
    cannot re-symbol a strategy; remove it from ``cta_strategy_setting.json``).
    Returns the problems found.
    """
    problems: list[str] = []
    for name, class_name, vt_symbol, setting in deployment:
        if class_name not in cta_engine.classes:
            problems.append(f"strategy class {class_name} not found in {sorted(cta_engine.classes)}")
            continue
        strategy: CtaTemplate | None = cta_engine.strategies.get(name)
        if strategy is None:
            cta_engine.add_strategy(class_name, name, vt_symbol, setting)
            if name not in cta_engine.strategies:
                problems.append(f"add_strategy({class_name}, {name}, {vt_symbol}) failed; see the engine log")
                continue
            log(f"added strategy {name} = {class_name}({vt_symbol}, {setting})")
            continue
        if strategy.vt_symbol != vt_symbol:
            problems.append(f"saved strategy {name} trades {strategy.vt_symbol}, deployment says {vt_symbol}; "
                            f"remove the entry from .vntrader/cta_strategy_setting.json first")
            continue
        if strategy.__class__.__name__ != class_name:
            problems.append(f"saved strategy {name} is a {strategy.__class__.__name__}, deployment says {class_name}")
            continue
        saved = cta_engine.strategy_setting.get(name, {}).get("setting", {})
        if saved != setting:
            cta_engine.edit_strategy(name, setting)
            log(f"refreshed setting of {name}: {setting}")
        else:
            log(f"strategy {name} already present ({class_name} on {vt_symbol})")
    return problems


def cancel_strategy_orders(cta_engine: CtaEngine, name: str) -> int:
    """
    Cancel every order the engine still tracks for ``name`` (engine level, so
    it works while ``trading`` is False).  After a callback exception the
    engine clears ``trading``/``inited`` but cancels nothing, and
    ``stop_strategy`` returns early: without this a re-initialised strategy
    would arm a second local stop next to the old one and both would fire.
    Returns the number of cancels issued; sleeps for server cancels.
    """
    strategy: CtaTemplate = cta_engine.strategies[name]
    tracked = list(cta_engine.strategy_orderid_map.get(name, set()))
    if not tracked:
        return 0
    server = 0
    for oid in tracked:
        log(f"{name}: cancelling tracked order {oid} before re-init", "WARNING")
        cta_engine.cancel_order(strategy, oid)
        if not oid.startswith("STOP"):
            server += 1
    if server:
        time.sleep(ORPHAN_SETTLE_SECS)
    return len(tracked)


def init_and_start(cta_engine: CtaEngine, name: str) -> bool:
    """``init_strategy`` (blocking on its Future) then ``start_strategy``; ``True`` when trading."""
    strategy: CtaTemplate = cta_engine.strategies[name]
    cancel_strategy_orders(cta_engine, name)
    try:
        cta_engine.init_strategy(name).result(timeout=INIT_TIMEOUT_SECS)
    except Exception as exc:  # noqa: BLE001 - timeout or a crash inside on_init
        log(f"{name}: init_strategy failed: {exc!r}", "ERROR")
        return False
    if not strategy.inited:
        log(f"{name}: not inited after init_strategy (exception in on_init?)", "ERROR")
        return False
    if not getattr(strategy, "warmup_ok", True):
        # on_init is @guarded: a failed load_bar halts inside on_init, but the
        # engine then restores the persisted variables and sets inited=True
        # regardless.  Never start on an empty ArrayManager (atr == 0).
        log(f"{name}: warmup failed (empty indicators); not starting", "ERROR")
        strategy.inited = False      # so the watchdog re-inits (load_bar again) instead of restarting
        return False
    cta_engine.start_strategy(name)
    if not strategy.trading:
        log(f"{name}: not trading after start_strategy (exception in on_start?)", "ERROR")
        return False
    log(f"{name}: started (pos={strategy.pos})")
    return True


def heartbeat_ts(name: str) -> float | None:
    """Epoch written by ``RiskGuard.heartbeat`` into ``.vntrader/heartbeat_<name>``; ``None`` if absent."""
    path = cfg.trader_file(f"heartbeat_{name}")
    try:
        return float(path.read_text(encoding="utf-8").strip() or 0.0) or path.stat().st_mtime
    except (OSError, ValueError):
        return None


def notify(main_engine: MainEngine, content: str, subject: str) -> None:
    """``main_engine.send_notification`` (email / wechat if configured); never raises."""
    log(f"NOTIFY {subject}: {content}", "CRITICAL")
    if not risk.notifications_configured():
        log("no email/wechat channel configured (vt_setting.json email.* / wechat_setting.json); "
            "notification logged only", "WARNING")
        return
    try:
        main_engine.send_notification(content, subject)
    except Exception as exc:  # noqa: BLE001
        log(f"send_notification failed: {exc!r}", "WARNING")


# ---------------------------------------------------------------------------
# Supervisor loop
# ---------------------------------------------------------------------------

class Supervisor:
    """Watchdog + heartbeat monitor for the started strategies (SPEC section 8.5)."""

    def __init__(self, main_engine: MainEngine, cta_engine: CtaEngine, names: Sequence[str]) -> None:
        self.main_engine = main_engine
        self.cta_engine = cta_engine
        self.names: list[str] = list(names)
        self.intentionally_stopped: set[str] = set()
        self.reinits: dict[str, deque[float]] = {n: deque() for n in self.names}
        self.started_at: dict[str, float] = {n: time.time() for n in self.names}

    def _reinit_budget_ok(self, name: str, now: float) -> bool:
        q = self.reinits[name]
        while q and now - q[0] > 3600.0:
            q.popleft()
        return len(q) < MAX_REINITS_PER_HOUR

    def check_once(self, now: float) -> int | None:
        """One watchdog pass; returns an exit code when the process must stop, else ``None``."""
        for name in self.names:
            strategy: CtaTemplate = self.cta_engine.strategies[name]
            if not strategy.trading and name not in self.intentionally_stopped:
                if not self._reinit_budget_ok(name, now):
                    self.write_kill(f"{name} stopped {MAX_REINITS_PER_HOUR} times within an hour")
                    return EXIT_WATCHDOG
                self.reinits[name].append(now)
                log(f"watchdog: {name} is not trading (inited={strategy.inited}); re-initialising "
                    f"({len(self.reinits[name])}/{MAX_REINITS_PER_HOUR} this hour)", "WARNING")
                if init_and_start(self.cta_engine, name):
                    self.started_at[name] = time.time()
                continue
            beat = heartbeat_ts(name)
            reference = max(self.started_at[name], beat or 0.0)
            age = now - reference
            if age > HEARTBEAT_STALE_SECS:
                log(f"heartbeat of {name} is {age:.0f}s old (> {HEARTBEAT_STALE_SECS:.0f}s); "
                    f"exiting so the service manager restarts the process", "CRITICAL")
                return EXIT_STALE
        return None

    def write_kill(self, reason: str) -> None:
        """Write the KILL flag, notify, and log (the strategies flatten and halt on the next tick)."""
        try:
            cfg.trader_file("KILL").write_text(f"{time.time():.0f} {reason}\n", encoding="utf-8")
        except OSError as exc:
            log(f"could not write KILL flag: {exc!r}", "ERROR")
        notify(self.main_engine, f"watchdog gave up: {reason}. KILL written; manual RESUME required.",
               "crypto_trader KILL")

    def run(self) -> int:
        """Loop until a stop request or a fatal condition; returns the exit code."""
        while not _stop_requested:
            code = self.check_once(time.time())
            if code is not None:
                return code
            sleep_interruptible(WATCHDOG_SECS)
        return EXIT_OK


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def dry_run(env: dict[str, str], exchange: str, deployment: Sequence[tuple[str, str, str, dict[str, Any]]]) -> int:
    """Validate everything that can be validated without a network connection."""
    problems = validate_env(env, exchange)
    setting = cfg.gateway_setting(exchange, env) if not any("must be" in p for p in problems) else {}
    log(f"trader dir: {cfg.TRADER_DIR}")
    log(f"exchange: {exchange} (gateway {cfg.gateway_name(exchange)})")
    log(f"gateway setting: {masked_setting(setting)}")
    log(f"risk dial: {env.get('CT_RISK_DIAL')}  equity fallback: {env.get('CT_EQUITY_USDT')} USDT")
    for name, class_name, vt_symbol, strat_setting in deployment:
        log(f"deployment: {name} = {class_name}({vt_symbol}) setting={strat_setting}")
    fallback = report_filters(exchange, deployment)
    if fallback:
        log(f"no exchange filter row for {fallback}; run `python download_data.py --exchange {exchange} "
            f"--filters --skip-bars` (run_live.py refreshes it automatically when online)", "WARNING")
    for flag in ("KILL", "PAUSE", "RESUME"):
        if cfg.trader_file(flag).exists():
            log(f"flag file present: .vntrader/{flag}", "WARNING")

    main_engine, cta_engine, _ = build_engines(None)
    try:
        cta_engine.load_strategy_class()
        log(f"strategy classes discovered: {sorted(cta_engine.classes)}")
        for name, class_name, vt_symbol, strat_setting in deployment:
            klass = cta_engine.classes.get(class_name)
            if klass is None:
                problems.append(f"strategy class {class_name} not discoverable from {Path.cwd() / 'strategies'}")
                continue
            try:
                instance: CtaTemplate = klass(cta_engine, name, vt_symbol, strat_setting)
                params = instance.get_parameters()
                log(f"{name}: {class_name} instantiated; risk_dial={params.get('risk_dial')} "
                    f"risk_pct={params.get('risk_pct')} max_leverage={params.get('max_leverage')} "
                    f"gross_leverage={params.get('gross_leverage')} capital={params.get('capital')}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{class_name}({name}) constructor failed: {exc!r}")
    finally:
        main_engine.close()

    for p in problems:
        log(f"PROBLEM: {p}", "ERROR")
    if problems:
        log(f"dry run FAILED with {len(problems)} problem(s)", "ERROR")
        return EXIT_CONFIG
    log("dry run OK: env, filters, strategy discovery and construction all passed (no connection made)")
    return EXIT_OK


def run_live(env: dict[str, str], exchange: str, deployment: Sequence[tuple[str, str, str, dict[str, Any]]],
             refresh: bool) -> int:
    """Connect, recover, trade, supervise.  Returns the process exit code."""
    problems = validate_env(env, exchange)
    for p in problems:
        log(f"PROBLEM: {p}", "ERROR")
    if problems:
        return EXIT_CONFIG

    setting = cfg.gateway_setting(exchange, env)
    log(f"trader dir: {cfg.TRADER_DIR}")
    log(f"exchange {exchange}: gateway setting {masked_setting(setting)}")
    if setting.get("Server") != "REAL":
        log(f"PAPER mode: Server={setting.get('Server')}", "WARNING")
    if refresh:
        refresh_filters(exchange, env)
    report_filters(exchange, deployment)
    if cfg.trader_file("KILL").exists():
        log("KILL flag present at startup: strategies will flatten and halt; create RESUME to clear", "WARNING")

    vt_symbols = [vt for _n, _c, vt, _s in deployment]
    main_engine, cta_engine, gateway_name = build_engines(exchange)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    code = EXIT_OK
    try:
        main_engine.connect(setting, gateway_name)
        if not wait_for_contracts(main_engine, vt_symbols):
            return EXIT_STARTUP
        # OKX connects its websockets only after every instrument list arrived;
        # both gateways query open orders right after the contracts.
        time.sleep(OKX_WS_SETTLE_SECS if exchange == "okx" else 0.0)
        if not wait_for_account(main_engine, gateway_name):
            return EXIT_STARTUP
        time.sleep(ORDER_QUERY_SETTLE_SECS)

        cta_engine.init_engine()        # loads strategy classes, re-adds saved strategies, restores variables
        problems = ensure_strategies(cta_engine, deployment)
        for p in problems:
            log(f"PROBLEM: {p}", "ERROR")
        if problems:
            return EXIT_CONFIG

        n = cancel_orphans(main_engine, vt_symbols)
        log(f"orphan orders cancelled: {n}")

        names = [name for name, _c, _v, _s in deployment]
        for name in names:
            if _stop_requested:
                return EXIT_OK
            if not init_and_start(cta_engine, name):
                return EXIT_STARTUP
        log(f"all strategies running: {names}; watchdog every {WATCHDOG_SECS:.0f}s")
        code = Supervisor(main_engine, cta_engine, names).run()
        return code
    finally:
        log("shutting down: stopping strategies and closing gateways")
        try:
            main_engine.close()     # CtaEngine.close -> stop_all_strategies (cancel_all + sync), then gateways
        except Exception as exc:  # noqa: BLE001
            log(f"main_engine.close failed: {exc!r}", "ERROR")
        log(f"exit code {code}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="crypto_trader headless live runner (vnpy CtaStrategy).")
    p.add_argument("--dry-run", action="store_true",
                   help="validate env / filters / strategy discovery and print the masked gateway setting; no connection")
    p.add_argument("--paper", action="store_true", help="Binance Server=TESTNET / OKX Server=DEMO")
    p.add_argument("--env", default=None, metavar="PATH", help="env file to read (default crypto_trader/.env)")
    p.add_argument("--no-filters", action="store_true", help="skip the exchange filters refresh (keep the existing file)")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env_path = Path(args.env).expanduser().resolve() if args.env else None
    if env_path is not None and not env_path.exists():
        log(f"env file not found: {env_path}", "ERROR")
        return EXIT_CONFIG
    env = load_config(env_path, bool(args.paper))
    try:
        exchange = cfg.exchange_from_env(env)
        deployment = resolve_deployment(exchange, env)
    except ValueError as exc:
        log(f"PROBLEM: {exc}", "ERROR")
        return EXIT_CONFIG
    if args.dry_run:
        return dry_run(env, exchange, deployment)
    return run_live(env, exchange, deployment, refresh=not args.no_filters)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
