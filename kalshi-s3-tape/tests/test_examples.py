"""Made-up example windows run through the real engine: how S3 earns and loses money.

Each example is one BTC window; prices are YES prices; every trade is 200 contracts.
These are illustrations of the rules, not market results.
"""

import pytest

from conftest import run, tr, win

# (name, trades as (seconds after open, YES price), result,
#  expected P&L in micro-dollars: {(variant, fill bound): pnl})
EXAMPLES = [
    ("price never reaches 1.6c: nothing fills",
     [(300, 500_000), (600, 400_000)], "yes",
     {"S3": (0, 0), "S3 without re-buys": (0, 0)}),
    ("a side drops to 1.5c and never comes back",
     [(300, 15_000)], "no",
     {"S3": (-3_200_000, -3_200_000), "S3 without re-buys": (-3_200_000, -3_200_000)}),
    ("drops to 1.5c, comes back to 87c",
     [(300, 15_000), (600, 870_000)], "yes",
     {"S3": (168_800_000, 168_800_000), "S3 without re-buys": (168_800_000, 168_800_000)}),
    ("comes back, drops again and stays dead",
     [(300, 15_000), (500, 870_000), (700, 15_000)], "no",
     {"S3": (165_600_000, 165_600_000), "S3 without re-buys": (168_800_000, 168_800_000)}),
    ("comes back twice",
     [(200, 15_000), (350, 870_000), (500, 15_000), (650, 870_000)], "yes",
     {"S3": (337_600_000, 337_600_000), "S3 without re-buys": (168_800_000, 168_800_000)}),
    ("touches exactly 1.6c and exactly 86c",
     [(300, 16_000), (600, 860_000)], "yes",
     {"S3": (0, 168_800_000), "S3 without re-buys": (0, 168_800_000)}),
]


@pytest.mark.parametrize("name,points,result,expected", EXAMPLES, ids=[e[0] for e in EXAMPLES])
def test_example_window(name, points, result, expected):
    trades = [tr(i, s, px, 200, taker="ask" if px < 500_000 else "bid") for i, (s, px) in enumerate(points)]
    for variant, (through, first) in expected.items():
        rebuy = variant == "S3"
        assert run([win(trades, result=result)], rebuy=rebuy, fm="through").window_pnl(0) == through
        assert run([win(trades, result=result)], rebuy=rebuy, fm="first_in_line").window_pnl(0) == first
