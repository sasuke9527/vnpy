"""
``gateways.ReduceOnlyBinanceLinearGateway``: close orders (``Offset.CLOSE``,
what ``CtaTemplate.sell`` / ``cover`` send) must reach Binance with
``reduceOnly=true`` inside the signed ``order.place`` params; open orders
must not.  The websocket is never connected: ``send_packet`` is captured.
"""
from __future__ import annotations

from typing import Any

import pytest
from vnpy.event import EventEngine
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Product
from vnpy.trader.object import ContractData, OrderRequest

import settings
from gateways import CLOSE_OFFSETS, ReduceOnlyBinanceLinearGateway, ReduceOnlyTradeApi, reduce_only_for

SYMBOL = "ETHUSDT_SWAP_BINANCE"


@pytest.fixture()
def gateway() -> Any:
    gw = ReduceOnlyBinanceLinearGateway(EventEngine(), "BINANCE_LINEAR")
    contract = ContractData(symbol=SYMBOL, exchange=Exchange.GLOBAL, name="ETHUSDT", product=Product.SWAP, size=1,
                            pricetick=0.01, min_volume=0.001, net_position=True, gateway_name="BINANCE_LINEAR")
    gw.symbol_contract_map[SYMBOL] = contract
    gw.name_contract_map["ETHUSDT"] = contract
    api = gw.trade_api
    assert isinstance(api, ReduceOnlyTradeApi)
    api.key, api.secret, api.order_prefix = "k", b"s", "t_"
    api.packets: list[dict[str, Any]] = []  # type: ignore[attr-defined]
    api.send_packet = api.packets.append  # type: ignore[method-assign]
    return gw


def _req(offset: Offset, direction: Direction, order_type: OrderType = OrderType.LIMIT) -> OrderRequest:
    return OrderRequest(symbol=SYMBOL, exchange=Exchange.GLOBAL, direction=direction, type=order_type,
                        volume=0.004, price=2850.0, offset=offset, reference="CtaStrategy_s1_eth")


def test_close_orders_are_reduce_only_and_signed(gateway: Any) -> None:
    vt_orderid = gateway.send_order(_req(Offset.CLOSE, Direction.SHORT))
    assert vt_orderid.startswith("BINANCE_LINEAR.")
    packet = gateway.trade_api.packets[-1]
    params = packet["params"]
    assert packet["method"] == "order.place"
    assert params["reduceOnly"] == "true"
    assert params["side"] == "SELL" and params["quantity"] == "0.004" and params["type"] == "LIMIT"
    assert "signature" in params and "timestamp" in params
    # the signature covers reduceOnly: recompute it the way TradeApi.sign does
    import hashlib
    import hmac
    payload = "&".join(f"{k}={v}" for k, v in sorted((k, v) for k, v in params.items() if k != "signature"))
    assert params["signature"] == hmac.new(b"s", payload.encode(), hashlib.sha256).hexdigest()


def test_cover_and_market_closes_are_reduce_only(gateway: Any) -> None:
    gateway.send_order(_req(Offset.CLOSE, Direction.LONG))
    assert gateway.trade_api.packets[-1]["params"]["reduceOnly"] == "true"
    gateway.send_order(_req(Offset.CLOSE, Direction.SHORT, OrderType.MARKET))
    assert gateway.trade_api.packets[-1]["params"]["reduceOnly"] == "true"


def test_open_orders_carry_no_reduce_only(gateway: Any) -> None:
    gateway.send_order(_req(Offset.OPEN, Direction.LONG))
    params = gateway.trade_api.packets[-1]["params"]
    assert "reduceOnly" not in params
    assert gateway.trade_api._reduce_only_pending is False


def test_helpers_and_settings_wiring() -> None:
    assert reduce_only_for(_req(Offset.CLOSE, Direction.SHORT))
    assert not reduce_only_for(_req(Offset.OPEN, Direction.SHORT))
    assert Offset.CLOSE in CLOSE_OFFSETS and Offset.OPEN not in CLOSE_OFFSETS
    cls = settings.gateway_class("binance_linear")
    assert cls is ReduceOnlyBinanceLinearGateway
    assert cls.default_name == "BINANCE_LINEAR" and "API Key" in cls.default_setting
