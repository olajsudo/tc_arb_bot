"""
No fixed trade-amount caps. Instead:

  1. Probe a cycle's INTRINSIC edge — its profit rate on a small trade,
     before slippage has a chance to distort it (a tiny probe barely
     moves the pool, so its return-rate reflects the real price gap).
  2. Map that edge to a fraction of the wallet's LIVE balance: a weak
     edge risks only a small slice, a strong edge risks up to
     BALANCE_FRACTION_MAX. This fraction becomes the ceiling for the
     actual profit-maximizing size search in amm_math — the real trade
     size still comes from that search (which naturally self-limits via
     slippage), this just bounds how far it's allowed to go.
  3. Always keep a LUNC gas reserve untouched, regardless of how
     attractive an opportunity looks.
"""
import logging
from decimal import Decimal

import config
import graph as graph_module

log = logging.getLogger("sizing")


def edge_to_fraction(edge_bps: Decimal) -> Decimal:
    """Linear interpolation between BALANCE_FRACTION_MIN and _MAX across
    [EDGE_LOW_BPS, EDGE_HIGH_BPS], clamped at both ends."""
    low = Decimal(config.EDGE_LOW_BPS)
    high = Decimal(config.EDGE_HIGH_BPS)
    min_frac = Decimal(str(config.BALANCE_FRACTION_MIN))
    max_frac = Decimal(str(config.BALANCE_FRACTION_MAX))

    if edge_bps <= 0:
        return Decimal(0)
    if edge_bps <= low:
        return min_frac
    if edge_bps >= high:
        return max_frac
    t = (edge_bps - low) / (high - low)
    return min_frac + t * (max_frac - min_frac)


def probe_amount_for(balance: int) -> int:
    amt = int(balance * config.PROBE_FRACTION)
    return max(amt, min(config.MIN_PROBE_AMOUNT, balance)) if balance > 0 else 0


def _probe_liquidity_scale(cycle, states, amount: int) -> Decimal:
    """
    Tightest ratio (<=1) `amount` would need to be scaled by so that no
    leg's simulated offer at that hop exceeds
    config.PROBE_MAX_POOL_RESERVE_FRACTION of that pool's relevant
    reserve. Same leg-by-leg walk as liquidity_cap_for_cycle, but against
    the much smaller probe-only fraction — a probe is supposed to barely
    move the pool, not consume a real-trade-sized slice of it.
    """
    if amount <= 0:
        return Decimal(1)

    leg_amounts = graph_module.simulate_cycle_legs(cycle, amount, states)
    tightest = Decimal(1)
    fraction = Decimal(str(config.PROBE_MAX_POOL_RESERVE_FRACTION))

    for edge, leg_offer in zip(cycle, leg_amounts):
        if leg_offer <= 0:
            continue
        state = states[id(edge.pool)]
        reserve_in = state.reserves[edge.asset_in.key()]
        cap = int(Decimal(reserve_in) * fraction)
        if cap <= 0:
            continue
        if leg_offer > cap:
            scale = Decimal(cap) / Decimal(leg_offer)
            if scale < tightest:
                tightest = scale

    return tightest


def probe_amount_for_cycle(cycle, states, balance: int, iterations: int = 4) -> int:
    """
    probe_amount_for(balance) sizes the probe purely off wallet balance,
    with no idea what route it's about to be run through. On a route
    that touches a thin pool, that probe can be large relative to that
    specific pool's own depth even though it's tiny relative to the
    wallet — and the probe's measured edge_bps then reflects the probe's
    OWN slippage rather than the route's real intrinsic edge (see
    config.PROBE_MAX_POOL_RESERVE_FRACTION's comment). This shrinks the
    balance-based probe down to what the shallowest pool on THIS cycle
    can actually absorb before it's ever simulated.

    Iterates (like liquidity_cap_for_cycle) rather than applying one
    correction, since scaling the starting offer down doesn't scale
    every downstream leg's offer by the same ratio (AMM curvature isn't
    linear). Returns 0 if even the balance floor doesn't fit — same
    "nothing to probe" signal max_offer_for_cycle already treats amount
    0 as.
    """
    amount = probe_amount_for(balance)
    for _ in range(iterations):
        if amount <= 0:
            return 0
        scale = _probe_liquidity_scale(cycle, states, amount)
        if scale >= 1:
            break
        amount = int(amount * scale)
    return amount


def _cycle_signature(cycle) -> tuple:
    """
    A stable, hashable key identifying this exact cycle (same pools, same
    direction through each) — used to memoize sizing results within a
    single loop iteration. Two DIFFERENT starting assets can rediscover
    the SAME physical route (arbitrage_bot.run_once calls find_cycles
    once per asset in assets_to_check, so any cycle touching more than
    one of those assets gets found — and independently re-evaluated —
    from each entry point it passes through). When that happens, every
    downstream sizing call for that route hits the identical reserves,
    identical ceiling, and identical answer; there's nothing new to
    compute the second, third, ... time.
    """
    return tuple((id(edge.pool), edge.asset_in.key()) for edge in cycle)


def max_offer_for_cycle(cycle, states, balance: int, asset, cache: dict = None):
    """
    Returns (sizing_ceiling, probe_edge_bps) — sizing_ceiling in base
    units (0 means "don't trade this": no measurable edge, or no balance
    to work with). probe_edge_bps is returned too (previously computed
    here and discarded) so callers don't have to re-run the same probe
    simulate_cycle() call again just to log or reason about it — added
    2026-08-07 after a real trace showed arbitrage_bot.run_once
    recomputing this exact probe a second time, per cycle, purely for a
    debug log line, on every single loop.
    """
    if balance <= 0:
        return 0, Decimal(0)

    probe = probe_amount_for_cycle(cycle, states, balance)
    if probe <= 0:
        return 0, Decimal(0)

    probe_out = graph_module.simulate_cycle(cycle, probe, states)
    if probe_out <= probe:
        return 0, Decimal(0)  # no edge at all on the probe — don't bother sizing up

    edge_bps = Decimal(probe_out - probe) / Decimal(probe) * Decimal(10000)
    fraction = edge_to_fraction(edge_bps)
    if fraction <= 0:
        return 0, edge_bps

    max_offer = int(balance * fraction)

    # ADDED 2026-08-26 at the user's explicit request — see config.
    # SINGLE_TRIP_SIZE_MULTIPLIER's comment. Applied here, directly to the
    # wallet-fraction ceiling, BEFORE the liquidity/spread caps below run
    # — those still get the final say on what's actually safe for the
    # pool, this just means a bigger single trip gets proposed to them in
    # the first place instead of the bot needing several smaller trips to
    # use up the same real capacity.
    max_offer = int(max_offer * Decimal(str(config.SINGLE_TRIP_SIZE_MULTIPLIER)))

    # Reserve gas headroom if this cycle starts in LUNC.
    if asset.kind == "native" and asset.id == config.DENOM_LUNC:
        headroom = balance - config.GAS_RESERVE_ULUNA
        max_offer = min(max_offer, max(0, headroom))

    # ADDED 2026-08-25: makes it possible to tell, from the log alone,
    # WHICH constraint actually capped a trade — the wallet-balance-
    # fraction curve above (edge_to_fraction, a risk-sizing choice based
    # on how strong the measured edge is) or liquidity_cap_for_cycle
    # below (a pool-depth safety limit, unrelated to how confident the
    # bot is in the edge). Repeatedly came up as impossible to tell apart
    # from a DEX's own trade history alone (real trades showing small,
    # repeated bot activity next to much larger trades from other
    # traders on the same opportunity) — this makes the next log
    # self-explanatory instead of needing another round of back-and-forth
    # to pin down which lever actually needs to change.
    #
    # DOWNGRADED info->debug 2026-09-01: this fires for EVERY cycle
    # examined in the discovery loop (hundreds to low-thousands per
    # loop, same as arb.run_once's per-cycle "Checked ..." line), but was
    # left at INFO — which config.py's own LOG_LEVEL docstring documents
    # as reserved for "Best opportunity / Skipping / Timing / errors"
    # only. That meant this line kept flooding Railway's 500 logs/sec
    # cap (dropped messages, confirmed via logs_1788221891674.csv: 1395
    # of 1412 lines in one loop were this logger, in bursts of 400+/sec)
    # even after the "Checked ..." line was fixed, since LOG_LEVEL=INFO
    # (the default) never gated this one. Now respects the same
    # DEBUG-only-when-you-ask-for-it convention as everything else here.
    wallet_fraction_ceiling = max_offer
    liquidity_capped = liquidity_cap_for_cycle(cycle, states, max_offer)
    if liquidity_capped < wallet_fraction_ceiling:
        binding = "LIQUIDITY_CAP (pool depth, not wallet size or edge confidence)"
    else:
        binding = "WALLET_FRACTION (edge_bps=%.1f -> fraction=%.4f of balance=%d)" % (
            edge_bps, fraction, balance)
    log.debug("Binding constraint for %s: %s — wallet_fraction_ceiling=%d liquidity_capped=%d "
              "final=%d", graph_module.cycle_label(cycle), binding, wallet_fraction_ceiling,
              liquidity_capped, liquidity_capped)

    return liquidity_capped, edge_bps


def spread_cap_for_cycle(cycle, states, ceiling: int, tolerance_bps: int,
                          spread_ceiling_bps: int, iterations: int = 14) -> int:
    """
    Binary-searches downward for the largest starting amount (<= ceiling)
    at which EVERY leg's required spread (graph_module.compute_leg_
    execution_params's own check) stays within spread_ceiling_bps.

    Why this exists: liquidity_cap_for_cycle above caps trade size to a
    FRACTION of each pool's reserve, which is the right thing for
    protecting a deep pool from being drained — but it does NOT guarantee
    the resulting per-leg spread stays under spread_ceiling_bps. For a
    genuinely thin pool (a few thousand dollars of reserve, common for a
    freshly-listed token like REV), 5% of that tiny reserve can still move
    price by far more than spread_ceiling_bps allows. Previously that
    meant the entire cycle got thrown away by compute_leg_execution_params
    at execution time — even when a smaller size on the exact same cycle
    would clear the ceiling AND still be profitable. This finds that
    smaller size instead of giving up outright.

    Monotonicity assumption: shrinking the offer never increases any leg's
    price impact (xyk spread grows with offer size), so a bisection search
    is safe here the same way it is in amm_math.find_optimal_trade_size.

    Returns 0 if even a tiny amount fails the ceiling (only possible if
    tolerance_bps itself already exceeds spread_ceiling_bps, or a pool's
    reserves are degenerate/zero) — callers should treat 0 as "this cycle
    isn't safely tradable at any size right now."

    iterations default lowered from 24 to 14 (2^-14 ≈ 0.006% of the
    original [floor_probe, ceiling] range — plenty for a safety-margin
    GATE, since the actual executed size and its exact spread are
    re-derived fresh in arbitrage_bot._prepare_execution right before
    broadcast anyway; this value only decides whether a candidate is
    worth trying, not what gets sent on-chain). Confirmed from a real log
    that pools sitting right at the spread_ceiling_bps boundary across
    their whole viable range (REV/LUNC, Astroport ASTRO/LUNC — both
    genuinely thin pools) drive this to run its FULL iteration count on
    nearly every cycle that touches them, since the search converges but
    never finds a candidate that passes early enough to break out sooner
    — 268 of these calls in one loop, ~1 full second of pure CPU, every
    single loop, not just on cold start. Halving iterations roughly
    halves that recurring cost without changing which cycles ultimately
    get accepted or rejected in practice.
    """
    def fits(candidate: int) -> bool:
        if candidate <= 0:
            return False
        leg_amounts = graph_module.simulate_cycle_legs(cycle, candidate, states)
        params = graph_module.compute_leg_execution_params(
            cycle, leg_amounts, states, tolerance_bps, spread_ceiling_bps)
        return params is not None

    if ceiling <= 0:
        return 0
    if fits(ceiling):
        return ceiling  # already within the ceiling — nothing to shrink

    lo, hi = 0, ceiling
    # Probe a floor a few orders of magnitude down before committing to a
    # full bisection — if even a tiny offer doesn't fit, there's nothing
    # to search for (see docstring).
    floor_probe = max(1, ceiling // 100000)
    if not fits(floor_probe):
        return 0

    lo = floor_probe
    for _ in range(iterations):
        mid = (lo + hi) // 2
        if mid == lo:
            break
        if fits(mid):
            lo = mid
        else:
            hi = mid
    return lo


def liquidity_cap_for_cycle(cycle, states, ceiling: int, iterations: int = 4) -> int:
    """
    Shrinks `ceiling` (if needed) so that no single leg of the cycle ever
    offers more than config.MAX_POOL_RESERVE_FRACTION of that specific
    pool's relevant reserve. Checked leg-by-leg using the actual expected
    offer at each hop (via graph_module.simulate_cycle_legs), not just the
    starting amount — a cycle can bottleneck at ANY pool along the way,
    not only the first one, and a pool with a small reserve gets a small
    real ceiling automatically even if the wallet's balance and edge
    strength alone would justify a much bigger trade.

    Scaling down the initial offer doesn't scale every leg's offer by
    exactly the same ratio (AMM curvature isn't linear), so this re-checks
    after each adjustment for a few iterations rather than assuming one
    correction is enough. It's a safety cap, not a precision instrument —
    converging to something a little more conservative than the exact
    boundary is fine; overshooting past a pool's real depth is not.
    """
    amount = ceiling
    if amount <= 0:
        return 0

    for _ in range(iterations):
        leg_amounts = graph_module.simulate_cycle_legs(cycle, amount, states)
        tightest_scale = Decimal(1)
        binding_pool = None

        for edge, leg_offer in zip(cycle, leg_amounts):
            if leg_offer <= 0:
                continue
            state = states[id(edge.pool)]
            reserve_in = state.reserves[edge.asset_in.key()]
            # RAISED 2026-08-24 at the user's explicit request: applies to
            # EVERY pool now (not scoped to a named thin-pool list — an
            # earlier version of this change was, but the user clarified
            # they want it universal). config.TRADE_SIZE_MULTIPLIER lets a
            # single trade consume up to 2x more of any pool's real depth
            # than MAX_POOL_RESERVE_FRACTION alone would allow, aimed at
            # capturing what used to take several loops' worth of separate
            # trades (each paying its own gas) in fewer, larger ones.
            #
            # WORTH KNOWING, stated plainly once: this is the same lever
            # tied to the 2026-07-12 loss, where a bigger fraction of a
            # pool's reserve in one trade meant the bot's own simulated
            # numbers diverged further from what actually happened
            # on-chain (real slippage ate ~76% of predicted profit that
            # time). Doubling it doubles that same exposure, on every
            # pool, not just thin ones. In practice this multiplier only
            # changes anything on pools where MAX_POOL_RESERVE_FRACTION
            # was already the binding constraint (i.e., thinner pools —
            # on a deep pool the cap rarely binds at all, so doubling it
            # changes nothing there). spread_cap_for_cycle still runs
            # afterward and can still shrink a cycle back down if the
            # resulting spread exceeds MAX_SPREAD_CEILING_BPS — that
            # check is unchanged and is a real second layer, not just
            # this one.
            fraction = Decimal(str(config.MAX_POOL_RESERVE_FRACTION)) * Decimal(str(config.TRADE_SIZE_MULTIPLIER))
            pool_cap = int(Decimal(reserve_in) * fraction)
            if pool_cap <= 0:
                continue
            if leg_offer > pool_cap:
                scale = Decimal(pool_cap) / Decimal(leg_offer)
                if scale < tightest_scale:
                    tightest_scale = scale
                    binding_pool = edge.pool.name

        if tightest_scale >= 1:
            break

        new_amount = int(amount * tightest_scale)
        # DOWNGRADED info->debug 2026-09-01, same reasoning as max_offer_for_cycle's
        # "Binding constraint" line above: this can fire up to `iterations` times per
        # cycle examined, for every cycle a thin pool binds on — which is common (see
        # the ampLUNC pools in this codebase's own logs). At INFO it bypassed the
        # DEBUG-gated per-cycle logging convention entirely and was the dominant
        # contributor (1395/1412 lines, 400+/sec bursts) to a repeat of the Railway
        # 500 logs/sec rate-limit hit, even after arb.py's "Checked ..." line was
        # already fixed for the same problem.
        log.debug("Liquidity cap: %s would exceed its reserve fraction at offer=%d — "
                  "shrinking cycle size %d -> %d",
                  binding_pool, amount, amount, new_amount)
        amount = new_amount
        if amount <= 0:
            return 0

    return amount