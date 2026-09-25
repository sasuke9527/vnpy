"""
Synthetic OHLCV generator for offline validation of strategies and runners.

Exchanges are unreachable from some environments (CI, sandboxed cloud
sessions), so every backtest in tests/ runs on bars produced here.  The
generator is deterministic (seeded) and regime-switching: it alternates
trending and choppy segments with stochastic volatility so that both
breakout and mean-reversion logic get exercised.

Usage (CLI):
    python synth_data.py --symbol ETHUSDT_SWAP_BINANCE --exchange GLOBAL --days 60 --seed 1

Usage (library):
    bars = generate_bars("ETHUSDT_SWAP_BINANCE", Exchange.GLOBAL, Interval.MINUTE, start, n)
    get_database().save_bar_data(bars)
"""
from __future__ import annotations

import argparse
import math
from datetime import datetime, timedelta

import numpy as np

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import DB_TZ, get_database
from vnpy.trader.object import BarData

INTERVAL_MINUTES: dict[Interval, int] = {
    Interval.MINUTE: 1,
    Interval.HOUR: 60,
    Interval.DAILY: 1440,
}


def generate_bars(
    symbol: str,
    exchange: Exchange,
    interval: Interval,
    start: datetime,
    n: int,
    seed: int = 0,
    start_price: float = 3000.0,
    base_vol_per_year: float = 0.60,
    gateway_name: str = "SYNTH",
) -> list[BarData]:
    """
    Return ``n`` consecutive bars beginning at ``start`` (tz-aware, DB_TZ).

    Price path: log-price random walk with a regime-switching drift and a
    slowly mean-reverting volatility multiplier.  Regimes last between 6
    hours and 3 days; half of them trend (drift up or down), half chop.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=DB_TZ)
    else:
        start = start.astimezone(DB_TZ)

    minutes = INTERVAL_MINUTES[interval]
    rng = np.random.default_rng(seed)

    bars_per_year = 365 * 24 * 60 / minutes
    sigma_bar = base_vol_per_year / math.sqrt(bars_per_year)

    log_p = math.log(start_price)
    vol_mult = 1.0
    drift = 0.0
    regime_left = 0
    bars: list[BarData] = []
    dt = start

    for _ in range(n):
        if regime_left <= 0:
            regime_left = int(rng.integers(6 * 60 // minutes, 3 * 24 * 60 // minutes))
            if rng.random() < 0.5:
                # trending regime: drift of +-1..3 daily sigmas spread over the regime
                sign = 1.0 if rng.random() < 0.5 else -1.0
                daily_sigma = base_vol_per_year / math.sqrt(365)
                drift = sign * rng.uniform(1.0, 3.0) * daily_sigma / (24 * 60 / minutes)
            else:
                drift = 0.0
        regime_left -= 1

        # stochastic vol: log-OU around 1.0
        vol_mult = math.exp(
            0.98 * math.log(vol_mult) + 0.10 * rng.standard_normal()
        )
        vol_mult = min(max(vol_mult, 0.3), 4.0)
        s = sigma_bar * vol_mult

        o = math.exp(log_p)
        # intra-bar path of 4 sub-steps to form a realistic high/low
        path = [o]
        lp = log_p
        for _ in range(4):
            lp += drift / 4 + s / 2 * rng.standard_normal()
            path.append(math.exp(lp))
        c = path[-1]
        h = max(path) * (1 + abs(rng.standard_normal()) * s * 0.25)
        low = min(path) * (1 - abs(rng.standard_normal()) * s * 0.25)
        log_p = math.log(c)

        rel_range = (h - low) / o
        volume = float(rng.lognormal(mean=math.log(50 + 2e4 * rel_range), sigma=0.5))

        bars.append(
            BarData(
                symbol=symbol,
                exchange=exchange,
                datetime=dt,
                interval=interval,
                volume=volume,
                turnover=volume * c,
                open_interest=0.0,
                open_price=round(o, 2),
                high_price=round(h, 2),
                low_price=round(low, 2),
                close_price=round(c, 2),
                gateway_name=gateway_name,
            )
        )
        dt += timedelta(minutes=minutes)

    return bars


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbol", default="ETHUSDT_SWAP_BINANCE", help="vnpy symbol, e.g. ETHUSDT_SWAP_BINANCE or ETHUSDT_SWAP_OKX")
    parser.add_argument("--exchange", default="GLOBAL", help="vnpy Exchange enum name; both crypto gateways use GLOBAL")
    parser.add_argument("--interval", default="1m", choices=["1m", "1h", "d"])
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--start", default="2026-01-01", help="YYYY-MM-DD")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--price", type=float, default=3000.0)
    args = parser.parse_args()

    interval = Interval(args.interval)
    minutes = INTERVAL_MINUTES[interval]
    n = args.days * 24 * 60 // minutes
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=DB_TZ)

    bars = generate_bars(
        args.symbol, Exchange[args.exchange], interval, start, n,
        seed=args.seed, start_price=args.price,
    )
    db = get_database()
    db.save_bar_data(bars)
    print(f"saved {len(bars)} {interval.value} bars for {args.symbol}.{args.exchange} "
          f"from {bars[0].datetime} to {bars[-1].datetime} "
          f"(close {bars[0].close_price:.2f} -> {bars[-1].close_price:.2f})")


if __name__ == "__main__":
    main()
