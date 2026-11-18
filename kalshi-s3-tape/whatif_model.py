"""WHAT-IF MODEL, NOT MARKET DATA: luck in S3 (no re-buys) if the user's backtest averages hold.

Assumptions (from the user's s1_price_grid summary for 1.6c / 86c, conservative fills):
96 coin-windows per day, each independent of the others; per window 2.4% a caught comeback
(+$168.80), 97.0% a dead side (-$3.20), 0.6% nothing ($0). No re-buys. Real markets have quiet
and busy stretches, so this model understates risk. Money in cents (exact integers).

Run: python whatif_model.py   (standard library only, Python 3.12+; about 15 s)
"""
import random
from math import comb

W, P_WIN, P_ZERO = 96, 0.024, 0.006
WIN, LOSS = 16880, -320          # cents
rng = random.Random(20261118)

# exact daily distribution of k (comebacks caught); z does not change the sign
pk = [comb(W, k) * P_WIN**k * (1 - P_WIN)**(W - k) for k in range(W + 1)]
mean_day = W * (P_WIN * WIN + (1 - P_WIN - P_ZERO) * LOSS)
print(f"mean per day: ${mean_day/100:,.2f}")
print("P(k comebacks in a day):", {k: round(pk[k], 4) for k in range(7)})
print(f"P(day is a loss) = P(k<=1) = {pk[0] + pk[1]:.4f}")

def day():
    k = rng.binomialvariate(W, P_WIN)
    z = rng.binomialvariate(W - k, P_ZERO / (1 - P_WIN))
    return k * WIN + (W - k - z) * LOSS

def path(days):
    return [day() for _ in range(days)]

def stats(n_paths, days):
    finals, dds, worst30 = [], [], []
    for _ in range(n_paths):
        d = path(days)
        cum = peak = dd = 0
        for x in d:
            cum += x; peak = max(peak, cum); dd = max(dd, peak - cum)
        finals.append(cum); dds.append(dd)
        if days >= 30:
            s = sum(d[:30]); w = s
            for i in range(30, days):
                s += d[i] - d[i - 30]; w = min(w, s)
            worst30.append(w)
    return sorted(finals), dds, worst30

def pct(xs, q):
    return xs[min(len(xs) - 1, int(q * len(xs)))]

f30, dd30, _ = stats(100_000, 30)
print(f"30 days: mean ${sum(f30)/len(f30)/100:,.0f}; 5%..95%: ${pct(f30,.05)/100:,.0f} .. ${pct(f30,.95)/100:,.0f}; "
      f"P(loss) {sum(x<0 for x in f30)/len(f30):.4f}; P(max drawdown >= $987.20) {sum(d>=98720 for d in dd30)/len(dd30):.4f}; "
      f"median max drawdown ${sorted(dd30)[len(dd30)//2]/100:,.0f}")
f195, dd195, w30 = stats(20_000, 195)
print(f"195 days (like your backtest): mean ${sum(f195)/len(f195)/100:,.0f}; "
      f"P(worst 30 days <= -$999) {sum(x<=-99900 for x in w30)/len(w30):.4f}; "
      f"P(max drawdown >= $1,636) {sum(d>=163600 for d in dd195)/len(dd195):.4f}; "
      f"median worst 30 days ${sorted(w30)[len(w30)//2]/100:,.0f}; median max drawdown ${sorted(dd195)[len(dd195)//2]/100:,.0f}")

# Cash runs short when cumulative P&L from the START falls to about -$987.20 ($1,000 minus the
# $12.80 reserved for one BTC+ETH window pair). Daily resolution (one day can lose at most $307.20).
rng.seed(7)
n = 100_000
short = 0
mins = []
for _ in range(n):
    cum = low = 0
    for x in path(30):
        cum += x
        low = min(low, cum)
    mins.append(low)
    short += low <= -98720
mins.sort()
print(f"30 days: P(cash runs short) {short/n:.4f}; P(account ever below $1,000) {sum(m<0 for m in mins)/n:.4f}; "
      f"1% worst low point ${pct(mins,.01)/100:,.0f}; 5% worst low point ${pct(mins,.05)/100:,.0f}")
