"""
Position sizing for the crypto_trader strategies (spec section 3).

This module is deliberately pure: no vnpy imports, no I/O, no global state,
so every path can be unit-tested with plain numbers.  Both strategies call
``calc_volume`` with the numbers they already hold (balance, ATR-based stop
distance, contract lot info) and receive a volume that is an exact multiple
of the exchange step size, or ``0.0`` when no acceptable size exists.

Sizing is the minimum of four notionals:

* risk notional        - loss at the stop equals ``balance * risk_pct * streak_mult``
* leverage notional    - ``balance * max_leverage`` (per strategy)
* free notional        - gross account cap minus what other strategies hold
* min-notional bump    - only allowed when it stays inside ``lot_tolerance``

All monetary values are USDT.  ``size`` is the number of coins per volume
unit (Binance USDT-M: 1, OKX swap: ``ctVal``); ``step`` is the volume
quantum (Binance ``stepSize``, OKX 1 contract).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal


# ---------------------------------------------------------------------------
# Decimal rounding helpers
# ---------------------------------------------------------------------------

def _to_decimal(value: float) -> Decimal:
    """Convert via ``str`` so 0.1 stays 0.1 and not 0.1000000000000000055."""
    return Decimal(str(value))


def floor_to(value: float, target: float) -> float:
    """
    Round ``value`` down to the nearest multiple of ``target``.

    Uses ``Decimal`` so that ``floor_to(50 / 3000, 0.001) == 0.016`` exactly
    (a naive ``math.floor(value / target) * target`` yields 0.016000000000000004).
    """
    if target <= 0:
        raise ValueError(f"target must be positive, got {target}")
    step = _to_decimal(target)
    units = (_to_decimal(value) / step).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * step)


def ceil_to(value: float, target: float) -> float:
    """Round ``value`` up to the nearest multiple of ``target`` (Decimal-based)."""
    if target <= 0:
        raise ValueError(f"target must be positive, got {target}")
    step = _to_decimal(target)
    units = (_to_decimal(value) / step).to_integral_value(rounding=ROUND_CEILING)
    return float(units * step)


# ---------------------------------------------------------------------------
# Fee-derived thresholds
# ---------------------------------------------------------------------------

def fee_rt(fee_rate: float = 0.0005, slippage_pct: float = 0.0003) -> float:
    """
    Round-trip cost as a fraction of notional: two taker fees plus one
    slippage allowance.  ETHUSDT defaults give 0.0013; alts with
    ``slippage_pct=0.0008`` give 0.0018.
    """
    return 2.0 * fee_rate + slippage_pct


def min_stop_pct(
    min_stop_pct: float = 0.004,
    fee_gate_mult: float = 3.0,
    fee_rt: float = 0.0013,
) -> float:
    """
    Minimum stop distance as a fraction of price: the larger of the
    configured floor and ``fee_gate_mult`` round-trip costs, so a stop-out
    never costs less than the fees it took to get in and out.
    """
    return max(min_stop_pct, fee_gate_mult * fee_rt)


def streak_mult(consec_losses: int) -> float:
    """
    De-leveraging multiplier after consecutive losses:
    ``max(0.25, 0.5 ** max(consec_losses - 2, 0))`` -> 1, 1, 1, 0.5, 0.25, 0.25 ...
    """
    return max(0.25, 0.5 ** max(int(consec_losses) - 2, 0))


# ---------------------------------------------------------------------------
# Contract lot information
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LotInfo:
    """
    The four contract numbers sizing needs.

    size          coins per volume unit (Binance linear 1, OKX swap ctVal)
    step          volume quantum (Binance stepSize == minQty for majors, OKX 1)
    min_notional  exchange minimum order notional in USDT (Binance minNotional)
    pricetick     price quantum (tickSize / tickSz)
    """

    size: float = 1.0
    step: float = 0.001
    min_notional: float = 20.0
    pricetick: float = 0.01

    def lot_notional(self, price: float) -> float:
        """USDT value of the smallest tradeable increment (one step)."""
        return self.step * self.size * price

    def notional(self, volume: float, price: float) -> float:
        """USDT notional of ``volume`` units at ``price``."""
        return volume * self.size * price

    def lots_max(self, balance: float, max_leverage: float, price: float) -> int:
        """
        How many minimum lots the account can hold at ``max_leverage``:
        ``floor(balance * max_leverage / (step * size * price))``.
        Strategies refuse a symbol (``halt_reason="COARSE_LOTS"``) when this
        is below 4, because sizing could then never track risk.
        """
        lot_value = self.lot_notional(price)
        if lot_value <= 0 or balance <= 0 or max_leverage <= 0:
            return 0
        return int(math.floor(balance * max_leverage / lot_value))


# ---------------------------------------------------------------------------
# Main sizing function
# ---------------------------------------------------------------------------

def calc_volume(
    balance: float,
    risk_pct: float,
    price: float,
    stop_dist: float,
    lot: LotInfo,
    max_leverage: float,
    free_notional: float,
    streak_mult: float = 1.0,
    lot_tolerance: float = 1.5,
    min_stop_pct: float = 0.004,
    fee_gate_mult: float = 3.0,
    fee_rt: float = 0.0013,
) -> float:
    """
    Return the order volume (exact multiple of ``lot.step``) or ``0.0``.

    Parameters
    ----------
    balance        wallet balance in USDT (never ``available``)
    risk_pct       fraction of balance to lose if the stop is hit
    price          entry (trigger) price
    stop_dist      distance from entry to the protective stop, in price units
    lot            contract lot information
    max_leverage   per-strategy notional cap as a multiple of balance
    free_notional  ``gross_leverage * balance`` minus other strategies' open notional
    streak_mult    de-leveraging multiplier (see ``streak_mult``)
    lot_tolerance  how far above the risk budget a min-notional bump may go
    min_stop_pct   configured floor on stop distance as a fraction of price
    fee_gate_mult  stop must be at least this many round trips wide
    fee_rt         round-trip cost fraction (see ``fee_rt``)
    """
    if balance <= 0 or price <= 0 or lot.size <= 0 or lot.step <= 0:
        return 0.0
    if risk_pct <= 0 or max_leverage <= 0 or free_notional <= 0:
        return 0.0

    # Fee-derived floor on the stop distance.
    msp = max(min_stop_pct, fee_gate_mult * fee_rt)
    stop_dist = max(stop_dist, msp * price)

    risk_usd = balance * risk_pct * streak_mult
    if risk_usd <= 0:
        return 0.0

    n_risk = risk_usd * price / stop_dist
    n_lev = balance * max_leverage
    n = min(n_risk, n_lev, free_notional)

    unit_value = lot.size * price
    v = floor_to(n / unit_value, lot.step)

    if v * unit_value < lot.min_notional:
        v_up = ceil_to(lot.min_notional / unit_value, lot.step)
        notional_ok = v_up * unit_value <= min(n_lev, lot_tolerance * max(n, lot.min_notional))
        risk_ok = v_up * lot.size * stop_dist <= lot_tolerance * risk_usd
        v = v_up if (notional_ok and risk_ok) else 0.0

    # Absolute cap: never risk more than twice the dial, whatever the path.
    if v * lot.size * stop_dist > 2.0 * risk_usd:
        v = 0.0

    return v
