"""
Sizing tests: the four worked examples of spec section 3 are asserted
exactly, plus the rounding helpers and the ``lots_max`` tradeability rule.
No vnpy import is needed; ``sizing`` is pure.
"""
from __future__ import annotations

import pytest

from sizing import LotInfo, calc_volume, ceil_to, fee_rt, floor_to, min_stop_pct, streak_mult

# Common worked-example inputs: balance 50, normal dial risk 2 %, leverage
# override 5x, gross 4x with nothing open -> free_notional 200.
BALANCE = 50.0
RISK_PCT = 0.02
MAX_LEV = 5.0
FREE_NOTIONAL = 200.0
ETH = LotInfo(size=1.0, step=0.001, min_notional=20.0, pricetick=0.01)
BTC = LotInfo(size=1.0, step=0.001, min_notional=100.0, pricetick=0.1)


def _vol(price: float, stop_dist: float, lot: LotInfo = ETH, streak: float = 1.0) -> float:
    return calc_volume(
        balance=BALANCE, risk_pct=RISK_PCT, price=price, stop_dist=stop_dist, lot=lot,
        max_leverage=MAX_LEV, free_notional=FREE_NOTIONAL, streak_mult=streak,
        lot_tolerance=1.5, min_stop_pct=0.004, fee_gate_mult=3.0, fee_rt=0.0013,
    )


# ---------------------------------------------------------------------------
# Worked examples A-D
# ---------------------------------------------------------------------------

def test_case_a_risk_binds() -> None:
    """ATR 30, stop 60 (2 %): risk notional 50 binds -> 0.016 ETH."""
    v = _vol(price=3000.0, stop_dist=60.0)
    assert v == 0.016
    assert v * 3000.0 >= 20.0                  # above min notional
    assert v * 60.0 == pytest.approx(0.96)     # loss at stop 0.96 USDT (1.9 %)


def test_case_b_gross_cap_binds() -> None:
    """Dead market ATR 5: stop floored to 12, gross cap 200 binds -> 0.066 ETH."""
    v = _vol(price=3000.0, stop_dist=10.0)
    assert v == 0.066
    assert v * 3000.0 == pytest.approx(198.0)
    assert v * 12.0 <= 2.0 * BALANCE * RISK_PCT


def test_case_c_streak_below_min_notional() -> None:
    """After 4 losses (streak 0.25): bump to 0.007 fails the risk tolerance -> 0."""
    v = _vol(price=3000.0, stop_dist=60.0, streak=0.25)
    assert v == 0.0


def test_case_d_btc_too_coarse() -> None:
    """BTC 100k, stop 2000, min notional 100: bump risks 2.0 > 1.5 -> 0; lots_max 2 < 4."""
    v = _vol(price=100_000.0, stop_dist=2000.0, lot=BTC)
    assert v == 0.0
    assert BTC.lots_max(BALANCE, MAX_LEV, 100_000.0) == 2
    assert BTC.lots_max(BALANCE, MAX_LEV, 100_000.0) < 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "target", "expected"),
    [
        (50 / 3000, 0.001, 0.016),
        (0.06667, 0.001, 0.066),
        (0.0005, 0.001, 0.0),
        (0.016, 0.001, 0.016),
        (123.456, 0.1, 123.4),
        (7.9, 1.0, 7.0),
    ],
)
def test_floor_to(value: float, target: float, expected: float) -> None:
    assert floor_to(value, target) == expected


@pytest.mark.parametrize(
    ("value", "target", "expected"),
    [
        (20 / 3000, 0.001, 0.007),
        (100 / 100_000, 0.001, 0.001),
        (0.016, 0.001, 0.016),
        (123.401, 0.1, 123.5),
        (7.1, 1.0, 8.0),
    ],
)
def test_ceil_to(value: float, target: float, expected: float) -> None:
    assert ceil_to(value, target) == expected


def test_rounding_rejects_bad_step() -> None:
    with pytest.raises(ValueError):
        floor_to(1.0, 0.0)
    with pytest.raises(ValueError):
        ceil_to(1.0, -0.001)


def test_fee_rt_and_min_stop_pct() -> None:
    assert fee_rt() == pytest.approx(0.0013)                       # ETH: 2*0.0005 + 0.0003
    assert fee_rt(0.0005, 0.0008) == pytest.approx(0.0018)         # alts
    assert min_stop_pct() == pytest.approx(0.004)                  # 0.004 > 3*0.0013
    assert min_stop_pct(fee_rt=0.0018) == pytest.approx(0.0054)    # fee gate binds for alts


@pytest.mark.parametrize(
    ("losses", "expected"),
    [(0, 1.0), (1, 1.0), (2, 1.0), (3, 0.5), (4, 0.25), (5, 0.25), (10, 0.25)],
)
def test_streak_mult(losses: int, expected: float) -> None:
    assert streak_mult(losses) == expected


def test_lots_max_eth_normal_dial() -> None:
    # 50 * 3 / (0.001 * 3000) = 50 lots -> tradeable
    assert ETH.lots_max(50.0, 3.0, 3000.0) == 50
    assert ETH.lot_notional(3000.0) == pytest.approx(3.0)
    # OKX ETH-USDT-SWAP: ctVal 0.1, step 1 contract -> 300 USDT per lot -> 0 lots
    okx_eth = LotInfo(size=0.1, step=1.0, min_notional=0.0, pricetick=0.01)
    assert okx_eth.lots_max(50.0, 3.0, 3000.0) == 0
    assert okx_eth.lots_max(0.0, 3.0, 3000.0) == 0


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------

def test_volume_is_exact_step_multiple() -> None:
    v = _vol(price=3137.37, stop_dist=47.3)
    assert v > 0
    assert round(v / 0.001) * 0.001 == pytest.approx(v)
    assert floor_to(v, 0.001) == v


def test_min_notional_bump_allowed_when_within_tolerance() -> None:
    """Bump from below min notional is accepted when notional and risk stay within 1.5x."""
    # risk 1 USDT, stop 100 -> N_risk 30 -> v 0.010 -> notional 30 >= 20: no bump needed
    assert _vol(price=3000.0, stop_dist=100.0) == 0.01
    # stop 200 -> N_risk 15 -> v 0.005, notional 15 < 20 -> v_up 0.007, notional 21 <= 30,
    # loss 0.007*200 = 1.4 <= 1.5 -> accepted
    assert _vol(price=3000.0, stop_dist=200.0) == 0.007


def test_absolute_two_x_risk_cap() -> None:
    """A path that would risk more than 2x the dial returns 0 even if min notional passes."""
    lot = LotInfo(size=1.0, step=0.01, min_notional=20.0, pricetick=0.01)
    # risk 1, stop 60 -> N 50 -> v floor(0.01667, 0.01) = 0.01 -> notional 30 ok; loss 0.6 ok
    assert calc_volume(50.0, 0.02, 3000.0, 60.0, lot, 5.0, 200.0) == 0.01
    # coarse step 0.05: v = 0 -> bump to 0.05: notional 150 > min(250, 1.5*50=75) -> 0
    coarse = LotInfo(size=1.0, step=0.05, min_notional=20.0, pricetick=0.01)
    assert calc_volume(50.0, 0.02, 3000.0, 60.0, coarse, 5.0, 200.0) == 0.0


def test_free_notional_binds() -> None:
    """Another strategy holding notional shrinks this one's cap."""
    full = calc_volume(50.0, 0.02, 3000.0, 12.0, ETH, 5.0, 200.0)
    squeezed = calc_volume(50.0, 0.02, 3000.0, 12.0, ETH, 5.0, 90.0)
    assert full == 0.066
    assert squeezed == 0.03
    assert calc_volume(50.0, 0.02, 3000.0, 12.0, ETH, 5.0, 0.0) == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(balance=0.0),
        dict(price=0.0),
        dict(risk_pct=0.0),
        dict(max_leverage=0.0),
    ],
)
def test_degenerate_inputs_return_zero(kwargs: dict[str, float]) -> None:
    base: dict[str, float] = dict(balance=50.0, risk_pct=0.02, price=3000.0, stop_dist=60.0,
                                  max_leverage=5.0, free_notional=200.0)
    base.update(kwargs)
    assert calc_volume(lot=ETH, **base) == 0.0
