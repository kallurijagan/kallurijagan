"""Re-buy rule and fill mechanics of the S3 trade-tape engine."""

import random
from fractions import Fraction

from conftest import P, S, T0, run, tr, win
from s3tape.engine import ONE, Engine, Params, RunConfig, cost, maker_fee, payout

C = 100  # hundredths per contract


def _st(eng, side, w=0):
    return eng.state[w][side]


def _decs(eng, kind):
    return [d for d in eng.decisions if d["kind"] == kind]


def test_round_trip_then_rebuy_joins_back_of_line():
    trades = [
        tr(1, 20, 15_000, 200),     # through 1.6c: initial YES buy fills 200
        tr(2, 100, 870_000, 200),   # through 86c: the YES sell fills 200 -> re-buy at 101 s
        tr(3, 100.5, 15_000, 200),  # before the re-buy exists: fills nothing
        tr(4, 101, 15_000, 200),    # same instant as the re-buy is posted: fills nothing
        tr(5, 200, 16_000, 500),    # AT 1.6c: through-only does not fill (line ahead may remain)
        tr(6, 300, 15_000, 50),     # through: re-buy fills 50
    ]
    eng = run([win(trades, result="no")])
    yes = _st(eng, "yes")
    rebuys = _decs(eng, "rebuy")
    assert len(rebuys) == 1
    assert rebuys[0]["t_us"] == T0 + 101 * S and rebuys[0]["qty_posted"] == 200 * C
    assert rebuys[0]["outcome"] == "posted"
    assert yes.rebuys_posted == 1 and yes.rebuy_fills == 1 and yes.rebuy_qty_filled == 50 * C
    assert [f["trade_id"] for f in eng.fills] == ["t1", "t2", "t6"]
    # -3.20 + 172.00 - 0.80 (50 re-bought contracts lose: result NO)
    assert eng.window_pnl(0) == -3_200_000 + 172_000_000 - 800_000
    assert yes.held_at_settle == 50 * C


def test_without_rebuys_one_round_trip_only():
    trades = [tr(1, 20, 15_000, 200), tr(2, 100, 870_000, 200), tr(6, 300, 15_000, 200)]
    eng = run([win(trades, result="no")], rebuy=False)
    assert _decs(eng, "rebuy") == []
    assert eng.window_pnl(0) == -3_200_000 + 172_000_000


def test_first_in_line_fills_at_price_through_does_not():
    trades = [tr(1, 20, 16_000, 200)]
    assert run([win(trades, result="no")], fm="through").state[0]["yes"].bought == 0
    fil = run([win(trades, result="no")], fm="first_in_line")
    assert fil.state[0]["yes"].bought == 200 * C
    assert fil.fills[0]["how"] == "at"


def test_no_rebuy_at_or_after_close():
    # sell fills at close - 1 s: re-buy decision lands exactly at the close -> skipped
    trades = [tr(1, 20, 15_000, 200), tr(2, 899, 870_000, 200)]
    eng = run([win(trades)])
    rb = _decs(eng, "rebuy")
    assert len(rb) == 1 and rb[0]["outcome"].startswith("skipped")
    assert _st(eng, "yes").rebuys_posted == 0
    # sell fills at close - 0.5 s: decision after the close -> skipped
    trades = [tr(1, 20, 15_000, 200), tr(2, 899.5, 870_000, 200)]
    eng = run([win(trades)])
    assert _decs(eng, "rebuy")[0]["outcome"].startswith("skipped")
    # a buy fill 0.5 s before the close: no sell can be posted, contracts settle
    trades = [tr(1, 899.5, 15_000, 200)]
    eng = run([win(trades, result="yes")])
    assert _decs(eng, "sell")[0]["outcome"].startswith("skipped")
    assert eng.window_pnl(0) == payout(200 * C) - cost(16_000, 200 * C)


def test_crossing_rebuy_rejected_and_never_reposted():
    trades = [
        tr(1, 20, 15_000, 200),
        tr(2, 100, 870_000, 200),            # sell fills -> re-buy decision at 101 s
        tr(3, 100.5, 16_000, 5, taker="bid"),  # a taker lifted a YES ask AT 1.6c: our bid would cross
        tr(4, 200, 15_000, 500),             # later through trades: nothing is resting, nothing fills
    ]
    eng = run([win(trades, result="no")])
    rb = _decs(eng, "rebuy")
    assert len(rb) == 1 and rb[0]["outcome"].startswith("rejected")
    yes = _st(eng, "yes")
    assert yes.rejected == 1 and yes.rebuys_posted == 0 and yes.bought == 200 * C
    assert "crossing-rejected" in eng.flags[0]
    assert len(_decs(eng, "rebuy")) == 1   # never re-posted


def test_position_cap_never_exceeded():
    class Checking(Engine):
        def _after_change(self, t, w):
            for st in self.state[w].values():
                assert 0 <= st.held + st.working_buy <= self.p.max_side_qty
                assert 0 <= st.working_sell <= st.held
            super()._after_change(t, w)

    trades = [
        tr(1, 20, 15_000, 100),     # initial fills 100 (100 still working)
        tr(2, 50, 870_000, 100),    # sold -> re-buy 100 at 51 s (100 + 100 working, 0 held)
        tr(3, 60, 15_000, 1000),    # fills initial 100 first (earlier), then re-buy 100: held 200
        tr(4, 70, 870_000, 1000),   # both sells fill -> two re-buys of 100
        tr(5, 80, 15_000, 1000),    # both re-buys fill: held 200 again
    ]
    eng = run([win(trades, result="yes")], engine_cls=Checking)
    yes = _st(eng, "yes")
    # bought 100 + 100 + 100 + 200 = 500, sold 100 + 200 = 300, held 200 at the close
    assert yes.bought == 500 * C and yes.sold == 300 * C and yes.held_at_settle == 200 * C
    assert yes.rebuys_posted == 3 and yes.cap_bound == 0


def test_position_cap_binds_when_room_is_short():
    eng = Engine([win([])], P, RunConfig("S3", True, "through"))
    st = eng.state[0]["yes"]
    st.held = 150 * C
    eng._decide(T0 + 30 * S, 0, "yes", "buy", "rebuy", 100 * C, None)
    assert eng.decisions[-1]["qty_posted"] == 50 * C and st.cap_bound == 1


def test_cash_reserve_and_reduce_size_for_rebuys():
    eng = Engine([win([])], P, RunConfig("S3", True, "through"))
    eng.cash = 1_000_000            # $1.00 free
    eng._decide(T0 + 30 * S, 0, "yes", "buy", "rebuy", 200 * C, None)
    d = eng.decisions[-1]
    assert d["qty_posted"] == 6_250 and "cash-limited" in d["outcome"]     # 62.50 contracts x 1.6c = $1.00
    assert eng.reserved == 1_000_000 and eng.cash - eng.reserved == 0
    eng._decide(T0 + 31 * S, 0, "no", "buy", "rebuy", 200 * C, None)
    assert eng.decisions[-1]["outcome"].endswith("not posted")

    fee = Engine([win([])], P, RunConfig("S3", True, "through", Fraction("0.0175")))
    fee._decide(T0 + 30 * S, 0, "yes", "buy", "rebuy", 200 * C, None)
    assert fee.reserved == cost(16_000, 200 * C) + 60_000          # $3.20 + $0.06 fee


def test_same_instant_order_btc_before_eth_yes_before_no():
    p = Params(**{**P.__dict__, "starting_cash": 8_000_000})       # $8.00
    eng = run([win([], coin="ETH", ticker="E"), win([], coin="BTC", ticker="B")], params=p)
    posted = [(d["coin"], d["side"], d["qty_posted"]) for d in eng.decisions]
    assert posted == [("BTC", "yes", 20_000), ("BTC", "no", 20_000), ("ETH", "yes", 10_000), ("ETH", "no", 0)]
    assert eng.reserved == 0 and eng.cash == 8_000_000


def test_partial_fills_each_get_their_own_sell_and_rebuy():
    trades = [
        tr(1, 20, 15_000, 120),     # buy fill 120 -> sell 120 at 21 s
        tr(2, 30, 15_000, 80),      # buy fill 80 -> sell 80 at 31 s
        tr(3, 40, 870_000, 50),     # fills 50 of the earlier sell -> re-buy 50
        tr(4, 50, 870_000, 200),    # 70 + 80 (the trade's other 50 are unused) -> re-buys 70 and 80
    ]
    eng = run([win(trades, result="no")])
    sells = [d["qty_posted"] for d in _decs(eng, "sell")]
    rebuys = [d["qty_posted"] for d in _decs(eng, "rebuy")]
    assert sells == [120 * C, 80 * C]
    assert rebuys == [50 * C, 70 * C, 80 * C]
    assert sum(f["qty"] for f in eng.fills if f["trade_id"] == "t4") == 150 * C


def test_each_trade_fills_its_size_once_best_price_for_taker_first():
    trades = [
        tr(1, 20, 985_000, 200),    # NO side through (NO at 1.5c): NO buy fills 200
        tr(2, 30, 10_000, 250),     # YES at 1c: through our NO sell (YES bid 14c) and our YES buy (bid 1.6c)
    ]
    eng = run([win(trades, result="yes")])
    f2 = [f for f in eng.fills if f["trade_id"] == "t2"]
    assert [(f["side"], f["action"], f["qty"]) for f in f2] == [("no", "sell", 200 * C), ("yes", "buy", 50 * C)]
    assert sum(f["qty"] for f in f2) == 250 * C


def test_block_trades_never_fill():
    eng = run([win([tr(1, 20, 10_000, 1000, block=True)])])
    assert eng.fills == [] and eng.block_trades == 1


def test_maker_fee_formula():
    r = Fraction("0.0175")
    assert maker_fee(r, 16_000, 200 * C) == 60_000        # 0.0551 -> $0.06
    assert maker_fee(r, 860_000, 200 * C) == 430_000      # 0.4214 -> $0.43
    assert maker_fee(Fraction(0), 860_000, 200 * C) == 0


def _kalshi_net_pnl(fills, result):
    """Independent re-implementation: one signed position per market, YES-book cash flows."""
    cash = 0
    pos = 0
    for f in fills:
        q, p = f["qty"], f["price"]
        if f["side"] == "yes":
            sign = 1 if f["action"] == "buy" else -1
            pos += sign * q
            cash -= sign * p * q // 100
        else:
            sign = 1 if f["action"] == "buy" else -1   # buying NO = selling YES at 1 - p
            pos -= sign * q
            cash += sign * (ONE - p) * q // 100
        cash -= f["fee"]
    return cash + (pos * 10_000 if result == "yes" else 0)


def test_random_runs_net_pnl_identity_and_ledger_invariants():
    rng = random.Random(20261118)
    prices = [5_000, 10_000, 15_000, 16_000, 17_000, 140_000, 500_000, 850_000, 860_000, 870_000,
              983_000, 984_000, 985_000, 995_000]
    for case in range(60):
        windows = []
        for k, coin in enumerate(("BTC", "ETH")):
            ts = sorted(rng.uniform(0, 905) for _ in range(rng.randint(0, 60)))
            trades = [tr(f"{case}-{k}-{i}", t, rng.choice(prices), rng.choice([1, 5, 37.5, 120, 200, 900]),
                         taker=rng.choice(["bid", "ask", None]), block=rng.random() < 0.05)
                      for i, t in enumerate(ts)]
            windows.append(win(trades, result=rng.choice(["yes", "no"]), coin=coin, ticker=f"{coin}{case}"))
        for rebuy in (True, False):
            for fm in ("through", "first_in_line"):
                for rate in (0, Fraction("0.0175")):
                    p = Params(**{**P.__dict__, "starting_cash": rng.choice([3_000_000, 9_000_000, 1_000_000_000])})
                    eng = run(windows, rebuy=rebuy, fm=fm, rate=rate, params=p)   # asserts net identity itself
                    total = 0
                    for w, wn in enumerate(eng.windows):
                        fills = eng.window_fills[w]
                        assert _kalshi_net_pnl(fills, wn.result) == eng.window_pnl(w)
                        total += eng.window_pnl(w)
                        for st in eng.state[w].values():
                            assert st.held == 0 and st.working_buy == 0 and st.working_sell == 0
                    assert eng.reserved == 0
                    assert eng.cash == p.starting_cash + total
                    if rate == 0:
                        assert eng.min_free[0] >= 0      # never borrows
