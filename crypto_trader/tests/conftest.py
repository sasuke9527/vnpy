"""
pytest bootstrap for crypto_trader.

vnpy computes ``TRADER_DIR``/``TEMP_DIR`` once, at first import of
``vnpy.trader.utility``: ``cwd/.vntrader`` if that folder exists, else
``~/.vntrader``.  To keep tests hermetic (own sqlite DB, own
``vt_setting.json`` with UTC timezone, no leakage into the owner's home
folder) this module, at import time and before any vnpy import:

1. creates a fresh temporary directory containing ``.vntrader/`` and a
   ``vt_setting.json`` (``database.timezone = UTC``, file logging off),
2. ``chdir``s into it,
3. inserts the project folder (``crypto_trader/``) at the front of
   ``sys.path`` so ``import sizing`` / ``import settings`` /
   ``import strategies...`` resolve the same way they do in production.

pytest imports ``conftest.py`` during collection, before any test module,
so this runs before the first ``import vnpy`` anywhere in the test run.
No network is used by any fixture here.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

PROJ: Path = Path(__file__).resolve().parents[1]
_ORIG_CWD: str = os.getcwd()
_TMP_ROOT: Path = Path(tempfile.mkdtemp(prefix="ct_pytest_"))
TEST_TRADER_DIR: Path = _TMP_ROOT / ".vntrader"
TEST_TRADER_DIR.mkdir(parents=True, exist_ok=True)
(TEST_TRADER_DIR / "vt_setting.json").write_text(
    json.dumps({"database.timezone": "UTC", "log.file": False, "log.console": False}, indent=4),
    encoding="utf-8",
)
# ``strategies/`` must be discoverable from cwd for CtaEngine.load_strategy_class
# (it scans ``cwd/strategies``); mirror the project folder with a symlink.
_STRATEGIES_SRC: Path = PROJ / "strategies"
if _STRATEGIES_SRC.is_dir() and not (_TMP_ROOT / "strategies").exists():
    try:
        os.symlink(_STRATEGIES_SRC, _TMP_ROOT / "strategies", target_is_directory=True)
    except OSError:
        shutil.copytree(_STRATEGIES_SRC, _TMP_ROOT / "strategies")

os.chdir(_TMP_ROOT)
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

assert "vnpy.trader.utility" not in sys.modules, (
    "vnpy was imported before tests/conftest.py; TRADER_DIR isolation is broken"
)


def _cleanup() -> None:
    try:
        os.chdir(_ORIG_CWD)
    except OSError:
        pass
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


atexit.register(_cleanup)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def proj_dir() -> Path:
    """The ``crypto_trader/`` project folder."""
    return PROJ


@pytest.fixture(scope="session")
def trader_dir() -> Path:
    """
    The temporary ``.vntrader`` folder vnpy is using for this test session.
    Verified against ``vnpy.trader.utility.TEMP_DIR`` on first use.
    """
    from vnpy.trader.utility import TEMP_DIR

    assert Path(TEMP_DIR).resolve() == TEST_TRADER_DIR.resolve(), (
        f"vnpy TEMP_DIR is {TEMP_DIR}, expected {TEST_TRADER_DIR}"
    )
    return TEST_TRADER_DIR


@pytest.fixture(scope="session")
def db(trader_dir: Path) -> Iterator[Any]:
    """vnpy database instance (sqlite inside the temp trader dir)."""
    from vnpy.trader.database import get_database

    database = get_database()
    yield database
    # vnpy_sqlite keeps a module-level peewee connection; nothing to close explicitly.


SYNTH_SYMBOL: str = "ETHUSDT_SWAP_BINANCE"
SYNTH_DAYS: int = 30
SYNTH_START: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def synth_bars() -> list[Any]:
    """
    30 days of deterministic 1-minute bars (seed=1) for
    ``ETHUSDT_SWAP_BINANCE`` / ``Exchange.GLOBAL``, in memory (not saved).
    Note ``save_bar_data`` mutates the passed objects; save a copy if a test
    also needs the originals.
    """
    from vnpy.trader.constant import Exchange, Interval

    from synth_data import generate_bars

    n = SYNTH_DAYS * 24 * 60
    return generate_bars(SYNTH_SYMBOL, Exchange.GLOBAL, Interval.MINUTE, SYNTH_START, n, seed=1)


@pytest.fixture(scope="session")
def synth_db(db: Any, synth_bars: list[Any]) -> Any:
    """
    ``db`` with the ``synth_bars`` saved (deep-copied first, so the
    ``synth_bars`` fixture stays pristine).  Returns the database.
    """
    import copy

    from vnpy.trader.database import BaseDatabase

    assert isinstance(db, BaseDatabase)
    db.save_bar_data(copy.deepcopy(synth_bars))
    try:
        from vnpy_ctastrategy.backtesting import load_bar_data

        load_bar_data.cache_clear()
    except Exception:  # optional dependency in some test subsets
        pass
    return db
