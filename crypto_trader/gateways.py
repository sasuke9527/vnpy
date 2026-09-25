"""
Gateway adaptations for crypto_trader.

``ReduceOnlyBinanceLinearGateway`` is ``vnpy_binance.BinanceLinearGateway``
with one change: every order whose ``OrderRequest.offset`` is a close
(``sell`` / ``cover`` in ``CtaTemplate``) is sent with ``reduceOnly=true``.

Why this matters (SPEC section 2 "no reduceOnly -> exits always send exactly
abs(pos)" is not enough on Binance USDT-M):

* Binance enforces ``MIN_NOTIONAL`` (ETHUSDT: 20 USDT) on every order that is
  **not** reduce-only (error -4164 "... unless you choose reduce only").  A
  position opened at the min-notional bump (21 USDT) whose value dropped 5 %,
  or the remainder of a partially filled entry, can therefore not be closed
  with a plain limit: every stop child / chase exit is rejected forever and
  the account keeps a naked position.
* A reduce-only order can never open or flip a position (-2022 "ReduceOnly
  Order is rejected"), which also neutralises a stale stop that fires for a
  position that no longer exists.

The gateway keeps ``net_position=True`` on its contracts, so the CTA engine's
offset converter passes ``Offset.CLOSE`` through unchanged and the flag is
derived from the strategy's intent alone.  One-way position mode (a manual
account prerequisite, see README) accepts ``reduceOnly``; hedge mode does
not, which is one more reason the runner requires one-way mode.

The module imports vnpy lazily via ``settings.gateway_class`` so the project
can still be imported before the ``.vntrader`` working-directory dance.
"""
from __future__ import annotations

from typing import Any

from vnpy.event import EventEngine
from vnpy.trader.constant import Offset
from vnpy.trader.object import OrderRequest
from vnpy_binance.linear_gateway import BinanceLinearGateway, TradeApi

CLOSE_OFFSETS: frozenset[Offset] = frozenset({Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY})


class ReduceOnlyTradeApi(TradeApi):
    """``TradeApi`` whose ``order.place`` requests carry ``reduceOnly`` for close orders."""

    def __init__(self, gateway: BinanceLinearGateway) -> None:
        super().__init__(gateway)
        self._reduce_only_pending: bool = False

    def send_order(self, req: OrderRequest) -> str:
        """Flag close requests, then let the stock implementation build and send the packet."""
        self._reduce_only_pending = req.offset in CLOSE_OFFSETS
        try:
            return str(super().send_order(req))
        finally:
            self._reduce_only_pending = False

    def sign(self, params: dict) -> None:
        """
        Inject ``reduceOnly`` *before* the signature is computed (the stock
        ``send_order`` calls ``sign`` exactly once, with the order params).
        Binance's futures API documents the field as the string "true"/"false".
        """
        if self._reduce_only_pending and "reduceOnly" not in params:
            params["reduceOnly"] = "true"
        super().sign(params)


class ReduceOnlyBinanceLinearGateway(BinanceLinearGateway):
    """``BinanceLinearGateway`` with reduce-only closes (same ``default_name`` / setting keys)."""

    def __init__(self, event_engine: EventEngine, gateway_name: str) -> None:
        super().__init__(event_engine, gateway_name)
        self.trade_api = ReduceOnlyTradeApi(self)


def reduce_only_for(req: Any) -> bool:
    """True when ``req`` (an ``OrderRequest``) is a close and must carry ``reduceOnly``."""
    return getattr(req, "offset", None) in CLOSE_OFFSETS


__all__ = ["CLOSE_OFFSETS", "ReduceOnlyBinanceLinearGateway", "ReduceOnlyTradeApi", "reduce_only_for"]
