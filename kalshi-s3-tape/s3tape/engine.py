"""S3 trade-tape engine: paper simulation of S3 on Kalshi's public trade tape.

This is an INDEPENDENT implementation written in a cloud session that could not see
backtests/sell85/engine.py or the s1_price_grid results, so it is NOT the engine those results
came from and has NOT been checked against them.

Model (all units exact integers: micro-dollars, hundredths of a contract, UTC microseconds):
  * One shared paper account for BTC and ETH. Every working buy reserves price x size (+ fee
    in the fee case); never borrow; a buy is reduced to what free cash allows.
  * S3 decisions: open + 12 s maker buys of 200 YES and 200 NO at 1.6c; 1 s after each buy
    fill a maker sell of that quantity at 86c; with re-buys, 1 s after each sell fill a maker
    buy of that quantity at 1.6c on the same side, only if the decision time is before the
    close; a side never holds + works more than 200; everything unfilled is cancelled at the
    close; unsold contracts settle at the official result.
  * Unified YES book: buy YES @p = YES bid @p; sell YES @s = YES ask @s; buy NO @p = YES ask
    @(1-p); sell NO @s = YES bid @(1-s).
  * Fill bounds (the queue at our price is not in the public tape, so two bounds are run):
      through        an order fills only from trades strictly THROUGH its price (the level
                     must have emptied, so everyone ahead of us is gone) - fewest fills;
      first_in_line  an order also fills from trades AT its price, as if first in line -
                     most fills.
    Each trade fills at most its own size, once, best price for the taker first across our
    orders, then earliest posted. Only trades strictly after an order is posted can fill it.
    Block trades (matched off the order book) never fill anything.
  * Maker check: no historical book is available, so a decision is rejected (and never
    re-posted) only when the most recent trade shows the other side of the book at or beyond
    our price (a taker lifted an ask at or below our bid, or hit a bid at or above our ask).
  * Settlement cash arrives at the market's settlement_ts (close time if missing).
  * Netting (Kalshi keeps one signed position per market): the simulation keeps YES and NO
    separate. For every window it asserts that P&L is identical under net-position accounting,
    flags windows where both sides were held at once or a resting sell had no net position
    behind it, and tracks an approximate netted cash path (offset pairs return $1 at once;
    an unbacked resting sell ties up (1 - sell price) per contract).
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from fractions import Fraction
from math import ceil

ONE = 1_000_000                 # $1 in micro-dollars
PAYOUT_PER_HUNDREDTH = 10_000   # $1 per contract = 10_000 micro-dollars per 0.01 contract
FILL_MODELS = ("through", "first_in_line")
CLOSE, SETTLE, TRADE, DECIDE = 0, 1, 2, 3   # same-instant processing order


def cost(price: int, qty: int) -> int:
    v = price * qty
    if v % 100:
        raise ValueError(f"price {price} x qty {qty} is not a whole micro-dollar")
    return v // 100


def payout(qty: int) -> int:
    return qty * PAYOUT_PER_HUNDREDTH


def maker_fee(rate: Fraction, price: int, qty: int) -> int:
    """rate * C * P * (1 - P), rounded up to the cent, for ONE execution."""
    if rate == 0 or qty == 0:
        return 0
    dollars = rate * Fraction(qty, 100) * Fraction(price, ONE) * Fraction(ONE - price, ONE)
    return ceil(dollars * 100) * 10_000


def book_view(side: str, action: str, price: int) -> tuple[str, int]:
    """Our order as seen on the unified YES book: (bid|ask, YES price)."""
    if side == "yes":
        return ("bid", price) if action == "buy" else ("ask", price)
    return ("ask", ONE - price) if action == "buy" else ("bid", ONE - price)


@dataclass(frozen=True)
class Trade:
    trade_id: str
    t_us: int
    yes_px: int
    qty: int
    taker_book_side: str | None = None    # 'bid' = taker bought YES, 'ask' = taker sold YES
    is_block: bool = False


@dataclass
class Window:
    ticker: str
    coin: str
    open_us: int
    close_us: int
    result: str                     # 'yes' | 'no'
    settle_us: int
    trades: list[Trade] = field(default_factory=list)
    settle_time_assumed: bool = False


@dataclass(frozen=True)
class Params:
    buy_price: int
    buy_qty: int
    sell_price: int
    rebuy_price: int
    entry_delay_us: int
    sell_delay_us: int
    rebuy_delay_us: int
    max_side_qty: int
    starting_cash: int
    coin_order: tuple[str, ...]
    side_order: tuple[str, ...]

    @classmethod
    def from_rules(cls, r: dict) -> "Params":
        return cls(
            buy_price=r["buy_price"], buy_qty=r["buy_qty"], sell_price=r["sell_price"],
            rebuy_price=r["rebuy_price"], entry_delay_us=r["entry_delay_us"],
            sell_delay_us=r["sell_delay_us"], rebuy_delay_us=r["rebuy_delay_us"],
            max_side_qty=r["max_side_qty"], starting_cash=r["starting_cash"],
            coin_order=tuple(r["same_instant_order"]["coin"]),
            side_order=tuple(r["same_instant_order"]["side"]),
        )


@dataclass(frozen=True)
class RunConfig:
    label: str            # "S3" | "S3 without re-buys"
    rebuy: bool
    fill_model: str       # "through" | "first_in_line"
    maker_rate: Fraction = Fraction(0)

    @property
    def key(self) -> str:
        fee = "fee0" if self.maker_rate == 0 else f"fee{float(self.maker_rate):g}"
        return f"{'rebuy' if self.rebuy else 'norebuy'}_{self.fill_model}_{fee}"


@dataclass
class Order:
    oid: int
    w: int
    side: str
    action: str
    kind: str             # initial | sell | rebuy
    price: int
    qty: int
    post_us: int
    book_side: str
    yes_px: int
    parent: int | None
    filled: int = 0
    reserved: int = 0
    active: bool = True

    @property
    def remaining(self) -> int:
        return self.qty - self.filled


@dataclass
class SideState:
    held: int = 0
    working_buy: int = 0
    working_sell: int = 0
    bought: int = 0
    buy_cost: int = 0
    buy_fees: int = 0
    buy_fills: int = 0
    initial_filled: int = 0
    sold: int = 0
    sell_proceeds: int = 0
    sell_fees: int = 0
    sell_fills: int = 0
    rebuys_posted: int = 0
    rebuy_qty_posted: int = 0
    rebuy_fills: int = 0
    rebuy_qty_filled: int = 0
    rebuy_round_trip_qty: int = 0
    held_at_settle: int = 0
    payout: int = 0
    rejected: int = 0
    cash_limited: int = 0
    cap_bound: int = 0
    skipped_after_close: int = 0
    cancelled_buy_qty: int = 0
    cancelled_sell_qty: int = 0

    def pnl(self) -> int:
        return self.sell_proceeds + self.payout - self.buy_cost - self.buy_fees - self.sell_fees


def crosses(book_side: str, yes_px: int, last: Trade | None) -> bool:
    """Tape proxy for the maker check: the last trade shows the other side at/through our price."""
    if last is None or last.taker_book_side is None:
        return False
    if book_side == "bid":
        return last.taker_book_side == "bid" and last.yes_px <= yes_px
    return last.taker_book_side == "ask" and last.yes_px >= yes_px


class Engine:
    def __init__(self, windows: list[Window], params: Params, cfg: RunConfig):
        if cfg.fill_model not in FILL_MODELS:
            raise ValueError(f"unknown fill model {cfg.fill_model!r}")
        self.p, self.cfg = params, cfg
        self.coin_rank = {c: i for i, c in enumerate(params.coin_order)}
        self.side_rank = {s: i for i, s in enumerate(params.side_order)}
        self.windows = sorted(windows, key=lambda x: (x.open_us, self.coin_rank[x.coin], x.ticker))
        n = len(self.windows)
        self.cash = params.starting_cash
        self.reserved = 0
        self.orders: list[Order] = []
        self.active: list[list[Order]] = [[] for _ in range(n)]
        self.state = [{s: SideState() for s in params.side_order} for _ in range(n)]
        self.flags: list[set[str]] = [set() for _ in range(n)]
        self.last_trade: list[Trade | None] = [None] * n
        self.decisions: list[dict] = []
        self.fills: list[dict] = []
        self.cancels: list[dict] = []
        self.window_fills: list[list[dict]] = [[] for _ in range(n)]
        self.pnl_points: list[tuple[int, int, int]] = []     # (time, window, window P&L)
        self.net_adj = [0] * n
        self.net_adj_total = 0
        self.min_free = (self.cash, None)
        self.min_cash = (self.cash, None)
        self.min_free_net = (self.cash, None)
        self.fee_overruns = 0
        self.block_trades = 0
        self.after_close_trades = 0
        self._heap: list = []
        self._seq = 0
        self.done = False

    # ---------------------------------------------------------------- event loop
    def _push(self, t: int, rank: int, w: int, side: str | None, payload) -> None:
        self._seq += 1
        sr = self.side_rank[side] if side else 0
        heapq.heappush(self._heap, (t, rank, self.coin_rank[self.windows[w].coin], w, sr, self._seq, side, payload))

    def run(self) -> "Engine":
        p = self.p
        for w, win in enumerate(self.windows):
            entry = win.open_us + p.entry_delay_us
            for side in p.side_order:
                self._push(entry, DECIDE, w, side, ("buy", "initial", p.buy_qty, None))
            self._push(win.close_us, CLOSE, w, None, None)
            self._push(max(win.settle_us, win.close_us), SETTLE, w, None, None)
            for tr in win.trades:
                self._push(tr.t_us, TRADE, w, None, tr)
        while self._heap:
            t, rank, _, w, _, _, side, payload = heapq.heappop(self._heap)
            if rank == DECIDE:
                self._decide(t, w, side, *payload)
            elif rank == TRADE:
                self._trade(t, w, payload)
            elif rank == CLOSE:
                self._close(t, w)
            else:
                self._settle(t, w)
        self.done = True
        return self

    # ---------------------------------------------------------------- decisions
    def _affordable(self, price: int, want: int) -> int:
        free = self.cash - self.reserved

        def need(q: int) -> int:
            return cost(price, q) + maker_fee(self.cfg.maker_rate, price, q)

        if need(want) <= free:
            return want
        if free <= 0:
            return 0
        lo, hi = 0, want
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if need(mid) <= free:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _decide(self, t: int, w: int, side: str, action: str, kind: str, qty: int, parent: int | None) -> None:
        p, win, st = self.p, self.windows[w], self.state[w][side]
        if action == "buy":
            price = p.buy_price if kind == "initial" else p.rebuy_price
        else:
            price = p.sell_price
        book_side, yes_px = book_view(side, action, price)
        last = self.last_trade[w]
        rec = {
            "t_us": t, "ticker": win.ticker, "coin": win.coin, "side": side, "action": action,
            "kind": kind, "price": price, "qty_wanted": qty, "qty_posted": 0, "parent": parent,
            "last_trade_yes_px": last.yes_px if last else None,
            "last_trade_taker": last.taker_book_side if last else None,
            "last_trade_age_us": (t - last.t_us) if last else None,
            "oid": None, "outcome": "",
        }
        self.decisions.append(rec)
        if t >= win.close_us:
            rec["outcome"] = "skipped: decision at or after the close"
            st.skipped_after_close += 1
            return
        if crosses(book_side, yes_px, last):
            rec["outcome"] = "rejected: would cross (last trade shows the other side at/through our price)"
            st.rejected += 1
            self.flags[w].add("crossing-rejected")
            return
        q = qty
        reserve = 0
        if action == "buy":
            room = p.max_side_qty - st.held - st.working_buy
            if q > room:
                st.cap_bound += 1
                rec["outcome"] = "cap-bound; "
                q = max(room, 0)
            if q > 0:
                q2 = self._affordable(price, q)
                if q2 < q:
                    st.cash_limited += 1
                    self.flags[w].add("cash-limited")
                    rec["outcome"] += f"cash-limited {q} -> {q2}; "
                    q = q2
            if q <= 0:
                rec["outcome"] += "not posted"
                return
            reserve = cost(price, q) + maker_fee(self.cfg.maker_rate, price, q)
            self.reserved += reserve
        elif st.held - st.working_sell < q:
            raise AssertionError(f"{win.ticker} {side}: sell of {q} without contracts behind it")
        o = Order(
            oid=len(self.orders) + 1, w=w, side=side, action=action, kind=kind, price=price,
            qty=q, post_us=t, book_side=book_side, yes_px=yes_px, parent=parent, reserved=reserve,
        )
        self.orders.append(o)
        self.active[w].append(o)
        if action == "buy":
            st.working_buy += q
        else:
            st.working_sell += q
        if kind == "rebuy":
            st.rebuys_posted += 1
            st.rebuy_qty_posted += q
        rec["outcome"] += "posted"
        rec["qty_posted"] = q
        rec["oid"] = o.oid
        self._after_change(t, w)

    # ---------------------------------------------------------------- trades and fills
    def _trade(self, t: int, w: int, tr: Trade) -> None:
        if tr.is_block:
            self.block_trades += 1
            return
        if t >= self.windows[w].close_us:
            self.after_close_trades += 1
            return
        fil = self.cfg.fill_model == "first_in_line"
        y = tr.yes_px
        bids: list[Order] = []
        asks: list[Order] = []
        for o in self.active[w]:
            if not o.active or o.post_us >= t:
                continue
            if o.book_side == "bid":
                if o.yes_px > y or (fil and o.yes_px == y):
                    bids.append(o)
            elif o.yes_px < y or (fil and o.yes_px == y):
                asks.append(o)
        if bids and asks:
            raise AssertionError("our own orders crossed each other")
        if bids:
            cands = sorted(bids, key=lambda o: (-o.yes_px, o.post_us, o.oid))
        else:
            cands = sorted(asks, key=lambda o: (o.yes_px, o.post_us, o.oid))
        left = tr.qty
        for o in cands:
            if left <= 0:
                break
            f = min(o.remaining, left)
            if f > 0:
                self._fill(t, w, o, f, tr)
                left -= f
        if cands:
            self.active[w] = [o for o in self.active[w] if o.active]
        self.last_trade[w] = tr

    def _fill(self, t: int, w: int, o: Order, f: int, tr: Trade) -> None:
        p, st, rate = self.p, self.state[w][o.side], self.cfg.maker_rate
        gross = cost(o.price, f)
        fee = maker_fee(rate, o.price, f)
        o.filled += f
        if o.remaining == 0:
            o.active = False
        if o.action == "buy":
            self.cash -= gross + fee
            new_res = cost(o.price, o.remaining) + maker_fee(rate, o.price, o.remaining)
            released = o.reserved - new_res
            if gross + fee > released:
                self.fee_overruns += 1     # per-execution fee rounding cost more than was reserved
            self.reserved -= released
            o.reserved = new_res
            st.held += f
            st.working_buy -= f
            st.bought += f
            st.buy_cost += gross
            st.buy_fees += fee
            st.buy_fills += 1
            if o.kind == "initial":
                st.initial_filled += f
            if o.kind == "rebuy":
                st.rebuy_fills += 1
                st.rebuy_qty_filled += f
            self._push(t + p.sell_delay_us, DECIDE, w, o.side, ("sell", "sell", f, o.oid))
        else:
            self.cash += gross - fee
            st.held -= f
            st.working_sell -= f
            st.sold += f
            st.sell_proceeds += gross
            st.sell_fees += fee
            st.sell_fills += 1
            if o.parent is not None and self.orders[o.parent - 1].kind == "rebuy":
                st.rebuy_round_trip_qty += f
            if self.cfg.rebuy:
                self._push(t + p.rebuy_delay_us, DECIDE, w, o.side, ("buy", "rebuy", f, o.oid))
        rec = {
            "t_us": t, "ticker": self.windows[w].ticker, "coin": self.windows[w].coin,
            "oid": o.oid, "kind": o.kind, "side": o.side, "action": o.action, "price": o.price,
            "qty": f, "fee": fee, "trade_id": tr.trade_id, "trade_yes_px": tr.yes_px,
            "trade_qty": tr.qty, "how": "at" if o.yes_px == tr.yes_px else "through",
            "order_posted_us": o.post_us,
        }
        self.fills.append(rec)
        self.window_fills[w].append(rec)
        self._after_change(t, w)

    # ---------------------------------------------------------------- close and settlement
    def _close(self, t: int, w: int) -> None:
        for o in self.active[w]:
            if not o.active:
                continue
            o.active = False
            st = self.state[w][o.side]
            if o.action == "buy":
                st.working_buy -= o.remaining
                st.cancelled_buy_qty += o.remaining
                self.reserved -= o.reserved
                o.reserved = 0
            else:
                st.working_sell -= o.remaining
                st.cancelled_sell_qty += o.remaining
            self.cancels.append({
                "t_us": t, "ticker": self.windows[w].ticker, "oid": o.oid, "kind": o.kind,
                "side": o.side, "action": o.action, "unfilled_qty": o.remaining,
            })
        self.active[w] = []
        self._after_change(t, w)

    def _settle(self, t: int, w: int) -> None:
        win = self.windows[w]
        total = 0
        for side, st in self.state[w].items():
            st.held_at_settle = st.held
            if win.result == side:
                st.payout = payout(st.held)
                self.cash += st.payout
            st.held = 0
            total += st.pnl()
        self._check_net_identity(w, total)
        self.pnl_points.append((t, w, total))
        self._after_change(t, w)

    def _check_net_identity(self, w: int, separate_pnl: int) -> None:
        """P&L under Kalshi-style net-position accounting must equal the separate-sides P&L."""
        cash_flow = 0
        net = 0          # signed YES position (negative = NO)
        for f in self.window_fills[w]:
            q, px = f["qty"], f["price"]
            if f["side"] == "yes" and f["action"] == "buy":
                net += q
                cash_flow -= cost(px, q)
            elif f["side"] == "yes":
                net -= q
                cash_flow += cost(px, q)
            elif f["action"] == "buy":           # buy NO @p == sell YES @(1-p)
                net -= q
                cash_flow += cost(ONE - px, q)
            else:                                 # sell NO @s == buy YES @(1-s)
                net += q
                cash_flow -= cost(ONE - px, q)
            cash_flow -= f["fee"]
        net_pnl = cash_flow + (payout(net) if self.windows[w].result == "yes" else 0)
        if net_pnl != separate_pnl:
            raise AssertionError(f"{self.windows[w].ticker}: net P&L {net_pnl} != separate {separate_pnl}")

    # ---------------------------------------------------------------- cash paths and netting flags
    def _after_change(self, t: int, w: int) -> None:
        ys, ns = self.state[w]["yes"], self.state[w]["no"]
        offset = min(ys.held, ns.held)
        if offset > 0:
            self.flags[w].add("both-sides-held")
        long_yes = max(ys.held - ns.held, 0)
        long_no = max(ns.held - ys.held, 0)
        unbacked_yes = max(0, ys.working_sell - long_yes)
        unbacked_no = max(0, ns.working_sell - long_no)
        if unbacked_yes or unbacked_no:
            self.flags[w].add("unbacked-sell-if-netted")
        collateral = cost(ONE - self.p.sell_price, unbacked_yes) + cost(ONE - self.p.sell_price, unbacked_no)
        adj = payout(offset) - collateral
        self.net_adj_total += adj - self.net_adj[w]
        self.net_adj[w] = adj
        free = self.cash - self.reserved
        if free < self.min_free[0]:
            self.min_free = (free, t)
        if self.cash < self.min_cash[0]:
            self.min_cash = (self.cash, t)
        if free + self.net_adj_total < self.min_free_net[0]:
            self.min_free_net = (free + self.net_adj_total, t)

    # ---------------------------------------------------------------- results
    def window_pnl(self, w: int) -> int:
        return sum(st.pnl() for st in self.state[w].values())

    def max_drawdown(self) -> tuple[int, int]:
        """(max drawdown, final cumulative P&L) over window settlements in time order."""
        cum = peak = dd = 0
        for t, w, pnl in sorted(self.pnl_points, key=lambda x: (x[0], self.coin_rank[self.windows[x[1]].coin], x[1])):
            cum += pnl
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
        return dd, cum


def comeback_cycles(win: Window, p: Params) -> dict[str, list[tuple[int, int | None]]]:
    """Per side: list of (time it traded <= buy price, time it later traded >= sell price or None),
    using non-block trades after open + entry delay and before the close."""
    entry = win.open_us + p.entry_delay_us
    out: dict[str, list[tuple[int, int | None]]] = {}
    for side in p.side_order:
        cycles: list[tuple[int, int | None]] = []
        low: int | None = None
        for tr in win.trades:
            if tr.is_block or tr.t_us <= entry or tr.t_us >= win.close_us:
                continue
            sp = tr.yes_px if side == "yes" else ONE - tr.yes_px
            if low is None:
                if sp <= p.buy_price:
                    low = tr.t_us
            elif sp >= p.sell_price:
                cycles.append((low, tr.t_us))
                low = None
        if low is not None:
            cycles.append((low, None))
        out[side] = cycles
    return out
