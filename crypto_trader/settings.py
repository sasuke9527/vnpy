"""
Static configuration for crypto_trader (spec section 1).

Contents:

* ``DIALS`` / ``FEES`` / ``MIN_NOTIONAL_FALLBACK`` / ``DEPLOYMENT`` - the
  numbers an engineer must not change silently.
* Path helpers: ``PROJ``, ``TRADER_DIR``, ``ensure_trader_dir()``,
  ``chdir_project()``.  vnpy resolves ``.vntrader`` from the cwd at import
  time, so every script must call ``chdir_project()`` *before* importing
  anything from vnpy.  For that reason this module imports no vnpy code at
  module level; ``gateway_class()`` imports lazily.
* ``load_env()`` - minimal ``KEY=VALUE`` parser for ``crypto_trader/.env``
  merged with ``os.environ`` (environment wins).  No python-dotenv needed.
* ``gateway_setting()`` / ``gateway_class()`` - the exact dict each
  gateway's ``connect()`` indexes, verified against vnpy_binance 2026.08.05
  (``linear_gateway.py`` ``default_setting``) and vnpy_okx 2026.06.10
  (``okx_gateway.py`` ``default_setting``).
* Symbol helpers - ``vt_symbol_for()``, ``symbol_for()``,
  ``contract_name_for()``, ``base_from_vt_symbol()``.
* ``load_exchange_filters()`` - ``.vntrader/exchange_filters.json`` with a
  hard-coded fallback table.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from sizing import LotInfo

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJ: Path = Path(__file__).resolve().parent
TRADER_DIR: Path = PROJ / ".vntrader"
ENV_FILE: Path = PROJ / ".env"
EXCHANGE_FILTERS_FILE: str = "exchange_filters.json"

#: Written into ``.vntrader/vt_setting.json`` when it does not exist yet.
VT_SETTING_DEFAULTS: dict[str, Any] = {"database.timezone": "UTC", "log.file": True}


def ensure_trader_dir(trader_dir: Path | None = None, write_settings: bool = True) -> Path:
    """
    Create ``<proj>/.vntrader`` (and ``vt_setting.json`` if missing) and
    return it.  Idempotent; never overwrites an existing settings file.
    """
    tdir = trader_dir or TRADER_DIR
    tdir.mkdir(parents=True, exist_ok=True)
    if write_settings:
        setting_file = tdir / "vt_setting.json"
        if not setting_file.exists():
            setting_file.write_text(json.dumps(VT_SETTING_DEFAULTS, indent=4), encoding="utf-8")
    return tdir


def chdir_project() -> Path:
    """
    Make ``PROJ`` the cwd and ensure ``.vntrader`` exists, so that vnpy's
    ``TRADER_DIR``/``TEMP_DIR`` resolve to the project folder.  Must run
    before the first ``import vnpy``.  Returns the trader dir.
    """
    tdir = ensure_trader_dir()
    os.chdir(PROJ)
    return tdir


def trader_file(name: str) -> Path:
    """Path of a file inside the project ``.vntrader`` folder."""
    return TRADER_DIR / name


# ---------------------------------------------------------------------------
# Risk dials, fees, fallbacks, deployment
# ---------------------------------------------------------------------------

#: Keys: risk_pct_s1, risk_pct_s2, max_leverage (per strategy), gross_leverage
#: (account), daily_loss_pct, max_dd_halt, max_consec_losses, cooldown_hours,
#: max_trades_day_s1, max_trades_day_s2.
DIALS: dict[str, dict[str, float | int]] = {
    "conservative": dict(risk_pct_s1=0.010, risk_pct_s2=0.0075, max_leverage=1.5, gross_leverage=2.0, daily_loss_pct=0.04,
                         max_dd_halt=0.15, max_consec_losses=3, cooldown_hours=12, max_trades_day_s1=2, max_trades_day_s2=4),
    "normal":       dict(risk_pct_s1=0.020, risk_pct_s2=0.015,  max_leverage=3.0, gross_leverage=4.0, daily_loss_pct=0.06,
                         max_dd_halt=0.25, max_consec_losses=4, cooldown_hours=8,  max_trades_day_s1=3, max_trades_day_s2=6),
    "aggressive":   dict(risk_pct_s1=0.040, risk_pct_s2=0.030,  max_leverage=5.0, gross_leverage=6.0, daily_loss_pct=0.10,
                         max_dd_halt=0.35, max_consec_losses=4, cooldown_hours=6,  max_trades_day_s1=3, max_trades_day_s2=6),
}

FEES: dict[str, Any] = dict(
    taker=0.0005,
    maker=0.0002,
    slippage_pct={"ETHUSDT": 0.0003, "BTCUSDT": 0.0002, "_alt": 0.0008},
)

#: Used only if ``exchange_filters.json`` is missing.
MIN_NOTIONAL_FALLBACK: dict[str, float] = {"ETHUSDT": 20.0, "BTCUSDT": 100.0, "_default": 20.0}

#: Full fallback filter rows in the same shape as ``exchange_filters.json``.
EXCHANGE_FILTERS_FALLBACK: dict[str, dict[str, float]] = {
    "ETHUSDT": {"tickSize": 0.01, "stepSize": 0.001, "minQty": 0.001, "minNotional": 20.0},
    "BTCUSDT": {"tickSize": 0.1, "stepSize": 0.001, "minQty": 0.001, "minNotional": 100.0},
    "_default": {"tickSize": 0.0001, "stepSize": 1.0, "minQty": 1.0, "minNotional": 20.0},
}

#: (strategy_name, class_name, vt_symbol, setting overrides).  S2 stays
#: commented out until the section 7 acceptance gate passes.
DEPLOYMENT: list[tuple[str, str, str, dict[str, Any]]] = [
    ("s1_eth", "DonchianTrendH1", "ETHUSDT_SWAP_BINANCE.GLOBAL", {"risk_dial": "normal"}),
    # ("s2_xrp", "SqueezeBreak15M", "XRPUSDT_SWAP_BINANCE.GLOBAL", {"risk_dial": "normal"}),  # enable only after section 7 gate passes
]

#: Exchange keys accepted in ``CT_EXCHANGE`` and their vnpy gateway names.
EXCHANGES: tuple[str, ...] = ("binance_linear", "okx")
GATEWAY_NAMES: dict[str, str] = {"binance_linear": "BINANCE_LINEAR", "okx": "OKX"}
DEFAULT_EXCHANGE: str = "binance_linear"
GATEWAY: str = GATEWAY_NAMES[DEFAULT_EXCHANGE]

#: Env var names (mirrors ``.env.example``).
ENV_KEYS: tuple[str, ...] = (
    "CT_EXCHANGE", "BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_SERVER",
    "OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE", "OKX_SERVER",
    "CT_PROXY_HOST", "CT_PROXY_PORT", "CT_RISK_DIAL", "CT_EQUITY_USDT",
)
ENV_DEFAULTS: dict[str, str] = {
    "CT_EXCHANGE": DEFAULT_EXCHANGE,
    "BINANCE_API_KEY": "", "BINANCE_API_SECRET": "", "BINANCE_SERVER": "REAL",
    "OKX_API_KEY": "", "OKX_SECRET_KEY": "", "OKX_PASSPHRASE": "", "OKX_SERVER": "REAL",
    "CT_PROXY_HOST": "", "CT_PROXY_PORT": "0",
    "CT_RISK_DIAL": "normal", "CT_EQUITY_USDT": "50",
}


# ---------------------------------------------------------------------------
# .env handling
# ---------------------------------------------------------------------------

def _strip_inline_comment(value: str) -> str:
    """Drop ``  # comment`` after a value (a ``#`` preceded by whitespace)."""
    for i, ch in enumerate(value):
        if ch == "#" and (i == 0 or value[i - 1].isspace()):
            return value[:i].rstrip()
    return value


def parse_env_text(text: str) -> dict[str, str]:
    """
    Parse ``KEY=VALUE`` lines.  Blank lines and ``#`` comments are skipped,
    an optional leading ``export`` is ignored, surrounding single or double
    quotes are removed, and unquoted values may carry a trailing comment.
    """
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            value = _strip_inline_comment(value)
        result[key] = value
    return result


def load_env(path: Path | None = None, environ: dict[str, str] | None = None) -> dict[str, str]:
    """
    Return the merged configuration: ``ENV_DEFAULTS`` <- ``.env`` file (if it
    exists) <- process environment.  Only keys present in the file or in
    ``ENV_KEYS`` are read from the environment, so unrelated variables never
    leak into the result.  Never writes anything.
    """
    env_path = path or ENV_FILE
    merged: dict[str, str] = dict(ENV_DEFAULTS)
    if env_path.exists():
        merged.update(parse_env_text(env_path.read_text(encoding="utf-8")))
    source = os.environ if environ is None else environ
    for key in set(ENV_KEYS) | set(merged):
        if key in source:
            merged[key] = source[key]
    return merged


def exchange_from_env(env: dict[str, str] | None = None) -> str:
    """``CT_EXCHANGE`` normalised to one of ``EXCHANGES`` (``ValueError`` otherwise)."""
    env = env if env is not None else load_env()
    exchange = env.get("CT_EXCHANGE", DEFAULT_EXCHANGE).strip().lower()
    if exchange not in EXCHANGES:
        raise ValueError(f"CT_EXCHANGE must be one of {EXCHANGES}, got {exchange!r}")
    return exchange


def risk_dial_from_env(env: dict[str, str] | None = None) -> str:
    """``CT_RISK_DIAL`` validated against ``DIALS``."""
    env = env if env is not None else load_env()
    dial = env.get("CT_RISK_DIAL", "normal").strip().lower()
    if dial not in DIALS:
        raise ValueError(f"CT_RISK_DIAL must be one of {tuple(DIALS)}, got {dial!r}")
    return dial


def equity_from_env(env: dict[str, str] | None = None) -> float:
    """``CT_EQUITY_USDT`` as a float (persisted equity fallback / backtest capital)."""
    env = env if env is not None else load_env()
    return float(env.get("CT_EQUITY_USDT", "50") or 50.0)


def _proxy(env: dict[str, str]) -> tuple[str, int]:
    host = env.get("CT_PROXY_HOST", "").strip()
    raw_port = env.get("CT_PROXY_PORT", "0").strip() or "0"
    try:
        port = int(float(raw_port))
    except ValueError as exc:
        raise ValueError(f"CT_PROXY_PORT must be an integer, got {raw_port!r}") from exc
    return host, port


# ---------------------------------------------------------------------------
# Gateway settings / classes
# ---------------------------------------------------------------------------

def gateway_name(exchange: str) -> str:
    """vnpy gateway name (``BINANCE_LINEAR`` / ``OKX``) for an exchange key."""
    return GATEWAY_NAMES[exchange]


def gateway_setting(exchange: str | None = None, env: dict[str, str] | None = None,
                    kline_stream: bool = True) -> dict[str, Any]:
    """
    The exact dict ``gateway.connect(setting)`` indexes.

    Binance linear (``linear_gateway.py`` connect): ``"API Key"``,
    ``"API Secret"``, ``"Server"`` (REAL|TESTNET), ``"Kline Stream"``
    ("True"|"False" string), ``"Proxy Host"``, ``"Proxy Port"`` (int).

    OKX (``okx_gateway.py`` connect): ``"API Key"``, ``"Secret Key"``,
    ``"Passphrase"``, ``"Server"`` (REAL|DEMO), ``"Proxy Host"``,
    ``"Proxy Port"``, ``"Spread Trading"`` ("False"), ``"Margin Currency"``.
    A legacy ``OKX_SERVER=TEST`` value is mapped to ``DEMO``.
    """
    env = env if env is not None else load_env()
    exchange = exchange or exchange_from_env(env)
    host, port = _proxy(env)

    if exchange == "binance_linear":
        server = env.get("BINANCE_SERVER", "REAL").strip().upper() or "REAL"
        if server not in ("REAL", "TESTNET"):
            raise ValueError(f"BINANCE_SERVER must be REAL or TESTNET, got {server!r}")
        return {
            "API Key": env.get("BINANCE_API_KEY", ""),
            "API Secret": env.get("BINANCE_API_SECRET", ""),
            "Server": server,
            "Kline Stream": "True" if kline_stream else "False",
            "Proxy Host": host,
            "Proxy Port": port,
        }

    if exchange == "okx":
        server = env.get("OKX_SERVER", "REAL").strip().upper() or "REAL"
        if server in ("TEST", "TESTNET"):
            server = "DEMO"
        if server not in ("REAL", "DEMO"):
            raise ValueError(f"OKX_SERVER must be REAL or DEMO, got {server!r}")
        return {
            "API Key": env.get("OKX_API_KEY", ""),
            "Secret Key": env.get("OKX_SECRET_KEY", ""),
            "Passphrase": env.get("OKX_PASSPHRASE", ""),
            "Server": server,
            "Proxy Host": host,
            "Proxy Port": port,
            "Spread Trading": "False",
            "Margin Currency": "",
        }

    raise ValueError(f"unknown exchange {exchange!r}; expected one of {EXCHANGES}")


def gateway_class(exchange: str | None = None) -> type:
    """
    ``BinanceLinearGateway`` or ``OkxGateway``.  Imported lazily so this
    module can be loaded before the cwd/``.vntrader`` dance.
    """
    exchange = exchange or exchange_from_env()
    cls: type
    if exchange == "binance_linear":
        from gateways import ReduceOnlyBinanceLinearGateway   # closes are sent reduceOnly (MIN_NOTIONAL exempt)
        cls = ReduceOnlyBinanceLinearGateway
    elif exchange == "okx":
        from vnpy_okx import OkxGateway
        cls = OkxGateway
    else:
        raise ValueError(f"unknown exchange {exchange!r}; expected one of {EXCHANGES}")
    return cls


# ---------------------------------------------------------------------------
# Symbol helpers
# ---------------------------------------------------------------------------

_QUOTES: tuple[str, ...] = ("USDT", "USDC", "USD")
_SYMBOL_SUFFIX: dict[str, str] = {"binance_linear": "_SWAP_BINANCE", "okx": "_SWAP_OKX"}


def split_base_quote(base: str) -> tuple[str, str]:
    """``"ETHUSDT"`` -> ``("ETH", "USDT")``; raises if no known quote suffix."""
    base = base.upper()
    for quote in _QUOTES:
        if base.endswith(quote) and len(base) > len(quote):
            return base[: -len(quote)], quote
    raise ValueError(f"cannot split {base!r} into base/quote (known quotes {_QUOTES})")


def symbol_for(exchange: str, base: str = "ETHUSDT") -> str:
    """vnpy symbol without exchange: ``ETHUSDT_SWAP_BINANCE`` / ``ETHUSDT_SWAP_OKX``."""
    if exchange not in _SYMBOL_SUFFIX:
        raise ValueError(f"unknown exchange {exchange!r}; expected one of {EXCHANGES}")
    return base.upper() + _SYMBOL_SUFFIX[exchange]


def vt_symbol_for(exchange: str, base: str = "ETHUSDT") -> str:
    """``ETHUSDT_SWAP_BINANCE.GLOBAL`` / ``ETHUSDT_SWAP_OKX.GLOBAL`` (both gateways use Exchange.GLOBAL)."""
    return symbol_for(exchange, base) + ".GLOBAL"


def contract_name_for(exchange: str, base: str = "ETHUSDT") -> str:
    """
    The exchange-native id stored in ``contract.name``: Binance ``ETHUSDT``,
    OKX ``ETH-USDT-SWAP``.  This is what REST endpoints and filter files key on.
    """
    if exchange == "binance_linear":
        return base.upper()
    if exchange == "okx":
        b, q = split_base_quote(base)
        return f"{b}-{q}-SWAP"
    raise ValueError(f"unknown exchange {exchange!r}; expected one of {EXCHANGES}")


def base_from_vt_symbol(vt_symbol: str) -> str:
    """``ETHUSDT_SWAP_BINANCE.GLOBAL`` -> ``ETHUSDT`` (also accepts a bare symbol)."""
    symbol = vt_symbol.split(".")[0]
    for suffix in _SYMBOL_SUFFIX.values():
        if symbol.endswith(suffix):
            return symbol[: -len(suffix)]
    return symbol.split("_")[0]


def exchange_from_vt_symbol(vt_symbol: str) -> str:
    """``..._SWAP_BINANCE.GLOBAL`` -> ``binance_linear``; ``..._SWAP_OKX.GLOBAL`` -> ``okx``."""
    symbol = vt_symbol.split(".")[0]
    for exchange, suffix in _SYMBOL_SUFFIX.items():
        if symbol.endswith(suffix):
            return exchange
    raise ValueError(f"cannot infer exchange from {vt_symbol!r}")


def deployment_for(exchange: str) -> list[tuple[str, str, str, dict[str, Any]]]:
    """``DEPLOYMENT`` with every vt_symbol rewritten for ``exchange``."""
    return [
        (name, cls, vt_symbol_for(exchange, base_from_vt_symbol(vt)), dict(setting))
        for name, cls, vt, setting in DEPLOYMENT
    ]


def slippage_for(base: str) -> float:
    """Backtest/live slippage fraction for a base symbol (``_alt`` for unknown)."""
    table: dict[str, float] = FEES["slippage_pct"]
    return table.get(base.upper(), table["_alt"])


# ---------------------------------------------------------------------------
# Exchange filters
# ---------------------------------------------------------------------------

def load_exchange_filters(path: Path | None = None) -> dict[str, dict[str, float]]:
    """
    Read ``.vntrader/exchange_filters.json`` (``{name: {tickSize, stepSize,
    minQty, minNotional}}`` as written by ``run_live.py`` /
    ``download_data.py --filters``).  When the file is missing or
    unreadable, return ``EXCHANGE_FILTERS_FALLBACK``.  A ``_default`` row is
    always present in the result.
    """
    file = path or trader_file(EXCHANGE_FILTERS_FILE)
    filters: dict[str, dict[str, float]] = {k: dict(v) for k, v in EXCHANGE_FILTERS_FALLBACK.items()}
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return filters
    if not isinstance(raw, dict):
        return filters
    loaded: dict[str, dict[str, float]] = {}
    for name, row in raw.items():
        if not isinstance(row, dict):
            continue
        loaded[str(name)] = {str(k): float(v) for k, v in row.items() if v is not None}
    if not loaded:
        return filters
    loaded.setdefault("_default", filters["_default"])
    return loaded


def filter_for(name: str, filters: dict[str, dict[str, float]] | None = None) -> dict[str, float]:
    """
    The filter row for a contract name (``ETHUSDT``), falling back to the
    ``_default`` row and to ``MIN_NOTIONAL_FALLBACK`` for ``minNotional``.
    """
    filters = filters if filters is not None else load_exchange_filters()
    row = dict(filters.get(name) or filters.get("_default") or EXCHANGE_FILTERS_FALLBACK["_default"])
    row.setdefault("minNotional", MIN_NOTIONAL_FALLBACK.get(name, MIN_NOTIONAL_FALLBACK["_default"]))
    return row


def min_notional_for(name: str, filters: dict[str, dict[str, float]] | None = None) -> float:
    """Minimum order notional in USDT for a contract name."""
    return float(filter_for(name, filters)["minNotional"])


def lot_info_for(name: str, size: float = 1.0, pricetick: float | None = None,
                 min_volume: float | None = None,
                 filters: dict[str, dict[str, float]] | None = None) -> LotInfo:
    """
    Build a ``LotInfo`` from the filters file, letting the live contract's
    ``size`` / ``pricetick`` / ``min_volume`` (when supplied) override the
    file, since the gateway numbers are what ``CtaEngine.round_to`` uses.
    """
    row = filter_for(name, filters)
    step = min_volume if min_volume else float(row.get("stepSize") or row.get("minQty") or 1.0)
    tick = pricetick if pricetick else float(row.get("tickSize") or 0.01)
    return LotInfo(size=size, step=step, min_notional=float(row["minNotional"]), pricetick=tick)
