import sys
from fractions import Fraction
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from s3tape.engine import Engine, Params, RunConfig, Trade, Window  # noqa: E402

S = 1_000_000                       # one second in microseconds
T0 = 1_763_460_000 * S              # 2025-11-18 10:00:00 UTC, a :00 window open


P = Params(
    buy_price=16_000, buy_qty=20_000, sell_price=860_000, rebuy_price=16_000,
    entry_delay_us=12 * S, sell_delay_us=1 * S, rebuy_delay_us=1 * S,
    max_side_qty=20_000, starting_cash=1_000_000_000,
    coin_order=("BTC", "ETH"), side_order=("yes", "no"),
)


def tr(i, t_s, yes_px, contracts, taker=None, block=False, open_us=T0):
    """Trade i at open + t_s seconds, YES price in micro-dollars, size in whole contracts."""
    return Trade(f"t{i}", open_us + int(t_s * S), yes_px, int(contracts * 100), taker, block)


def win(trades, result="yes", coin="BTC", ticker=None, open_us=T0, close_s=900, settle_s=905):
    return Window(ticker or f"KX{coin}15M-TEST-{open_us}", coin, open_us, open_us + close_s * S,
                  result, open_us + settle_s * S, list(trades))


def run(windows, rebuy=True, fm="through", rate=0, params=P, engine_cls=Engine):
    cfg = RunConfig("S3" if rebuy else "S3 without re-buys", rebuy, fm, Fraction(rate))
    return engine_cls(windows, params, cfg).run()


@pytest.fixture
def helpers():
    return dict(S=S, T0=T0, P=P, tr=tr, win=win, run=run)
