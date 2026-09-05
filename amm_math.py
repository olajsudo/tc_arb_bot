"""
xyk (constant product) swap simulation matching Terraswap/Astroport's
on-chain formula, plus a search routine for the profit-maximizing trade
size across two pools.

On-chain formula (both protocols use the same shape for standard xyk pairs):

    cp             = offer_pool * ask_pool
    raw_return     = ask_pool - cp / (offer_pool + offer_amount)
    spread_amount  = offer_amount * ask_pool / offer_pool - raw_return
    commission     = raw_return * commission_rate
    return_amount  = raw_return - spread_amount - commission
"""
from decimal import Decimal, getcontext
from dataclasses import dataclass

getcontext().prec = 50


@dataclass
class SwapResult:
    return_amount: int
    spread_amount: int
    commission_amount: int


def simulate_swap(offer_amount: int, offer_pool: int, ask_pool: int,
                   commission_rate: Decimal) -> SwapResult:
    if offer_amount <= 0 or offer_pool <= 0 or ask_pool <= 0:
        return SwapResult(0, 0, 0)

    offer_amount_d = Decimal(offer_amount)
    offer_pool_d = Decimal(offer_pool)
    ask_pool_d = Decimal(ask_pool)

    cp = offer_pool_d * ask_pool_d
    raw_return = ask_pool_d - cp / (offer_pool_d + offer_amount_d)
    spread_amount = offer_amount_d * ask_pool_d / offer_pool_d - raw_return
    commission_amount = raw_return * commission_rate
    # The contract nets the raw AMM output against BOTH spread and
    # commission — a previous version of this function only subtracted
    # commission_amount, which overstated return_amount by exactly
    # spread_amount on every swap. Small for any offer that isn't huge
    # relative to pool depth, but real, deterministic, and reproducible
    # bit-for-bit for a given (reserves, offer_amount) pair — which is
    # exactly the "Cannot Sub" mismatch seen against simulate_fee().
    return_amount = raw_return - spread_amount - commission_amount

    return SwapResult(
        return_amount=int(return_amount),
        spread_amount=int(spread_amount),
        commission_amount=int(commission_amount),
    )


def find_optimal_trade_size(profit_fn, low: int, high: int, iterations: int = 60) -> int:
    """
    Ternary search for the offer_amount (int, base units) that maximizes
    profit_fn(offer_amount). Assumes profit_fn is concave over [low, high],
    which holds for arbitrage across two xyk pools net of proportional fees.
    """
    if high <= low:
        return low
    lo, hi = low, high
    for _ in range(iterations):
        if hi - lo < 2:
            break
        m1 = lo + (hi - lo) // 3
        m2 = hi - (hi - lo) // 3
        if profit_fn(m1) < profit_fn(m2):
            lo = m1
        else:
            hi = m2
    # pick the best of a small neighborhood around the converged point
    candidates = range(max(low, lo - 2), min(high, hi + 2) + 1)
    return max(candidates, key=profit_fn) if candidates else lo