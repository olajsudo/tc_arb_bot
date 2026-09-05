"""
Treats every pool as two directed edges (swap asset_in -> asset_out) and
searches for cycles that start and end at the same asset — e.g.
USTC -[TW1]-> LUNC -[TP1]-> TERRA -[TP2]-> USTC. This generalizes the
"two pools, one pair" case to any number of pools over any number of
shared assets, so adding a 5th or 6th pool later is just adding it to the
pool list.
"""
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Dict

import tax as tax_module
from amm_math import simulate_swap
from assets import Asset
from pool_client import DexPool, PoolState
from executor import LegParams
from decimal import Decimal, ROUND_UP, ROUND_HALF_UP

log = logging.getLogger("graph")


@dataclass
class Edge:
    pool: DexPool
    asset_in: Asset
    asset_out: Asset


def build_edges(pools: List[DexPool]) -> List[Edge]:
    edges = []
    for p in pools:
        edges.append(Edge(p, p.asset_x, p.asset_y))
        edges.append(Edge(p, p.asset_y, p.asset_x))
    return edges


def find_cycles(edges: List[Edge], start_asset: Asset, max_hops: int = 4) -> List[List[Edge]]:
    """
    DFS over the edge list for simple cycles (no pool reused within one
    cycle) that return to start_asset in 2..max_hops hops.
    """
    cycles = []
    start_key = start_asset.key()

    def dfs(path: List[Edge], used_pool_ids: set, current_key: str):
        if len(path) >= 2 and current_key == start_key:
            cycles.append(list(path))
        if len(path) >= max_hops:
            return
        for e in edges:
            if e.asset_in.key() != current_key:
                continue
            if id(e.pool) in used_pool_ids:
                continue
            path.append(e)
            used_pool_ids.add(id(e.pool))
            dfs(path, used_pool_ids, e.asset_out.key())
            used_pool_ids.remove(id(e.pool))
            path.pop()

    dfs([], set(), start_key)
    return cycles


def simulate_cycle(cycle: List[Edge], start_amount: int,
                    states: Dict[str, PoolState]) -> int:
    """
    Runs start_amount through each hop of the cycle using pre-fetched pool
    states (so a sizing search doesn't re-query the chain on every
    candidate amount). Applies each pool's commission, plus tax on BOTH
    sides of every hop:
      - outgoing: Terra Classic's native stability tax when sending a
        native coin into a contract as `funds`.
      - incoming: a CW20 token's own transfer tax, if any — confirmed
        empirically for LCW (see config.cw20_transfer_tax_rate): a real
        swap's reported return_amount didn't match the actual wallet
        balance received, meaning the token deducts tax on transfers
        INTO the wallet too, not just when we send it out.
    """
    amount = start_amount
    for edge in cycle:
        if amount <= 0:
            return 0
        taxed_offer = amount - tax_module.calculate_tax(amount, edge.asset_in, direction="out")
        state = states[id(edge.pool)]
        reserve_in = state.reserves[edge.asset_in.key()]
        reserve_out = state.reserves[edge.asset_out.key()]
        result = simulate_swap(taxed_offer, reserve_in, reserve_out, state.commission_rate)
        received = result.return_amount
        received -= tax_module.calculate_tax(received, edge.asset_out, direction="in")
        amount = received
    return amount


def simulate_cycle_costs_uusd(cycle: List[Edge], start_amount: int,
                               states: Dict[str, PoolState], pools: List[DexPool],
                               ustc: Asset, lunc: Asset) -> Decimal:
    """
    Returns the TOTAL real cost paid across every leg of this cycle at
    this exact size — AMM commission AND tax on BOTH sides of every hop
    (outgoing native stability tax, incoming CW20 transfer tax) — summed
    together in uusd terms via price_in_uusd, so legs in different assets
    can be added.

    Deliberately mirrors simulate_cycle's loop step-for-step (same
    taxed_offer -> simulate_swap -> incoming-tax sequence) instead of
    being computed independently, so this can never drift from what
    simulate_cycle actually nets out of `amount` on the way through — it
    just also keeps the pieces simulate_cycle discards after folding them
    into the running total. ADDED 2026-08-04: an earlier attempt at this
    (in arbitrage_bot.py, before this lived here) only summed the
    outgoing-side native tax via leg_amounts and missed commission
    entirely plus the incoming-side CW20 tax — silently understating the
    real cost a margin check is supposed to be measured against. Gas is
    NOT included here (arbitrage_bot adds real simulated gas separately;
    that's a per-transaction cost, not a per-leg AMM/tax cost, and is
    already measured independently via simulate_fee).
    """
    amount = start_amount
    total_uusd = Decimal(0)
    for edge in cycle:
        if amount <= 0:
            break
        outgoing_tax = tax_module.calculate_tax(amount, edge.asset_in, direction="out")
        if outgoing_tax > 0:
            total_uusd += Decimal(outgoing_tax) * price_in_uusd(
                edge.asset_in, pools, states, ustc, lunc)
        taxed_offer = amount - outgoing_tax
        state = states[id(edge.pool)]
        reserve_in = state.reserves[edge.asset_in.key()]
        reserve_out = state.reserves[edge.asset_out.key()]
        result = simulate_swap(taxed_offer, reserve_in, reserve_out, state.commission_rate)
        if result.commission_amount > 0:
            total_uusd += Decimal(result.commission_amount) * price_in_uusd(
                edge.asset_out, pools, states, ustc, lunc)
        received = result.return_amount
        incoming_tax = tax_module.calculate_tax(received, edge.asset_out, direction="in")
        if incoming_tax > 0:
            total_uusd += Decimal(incoming_tax) * price_in_uusd(
                edge.asset_out, pools, states, ustc, lunc)
        amount = received - incoming_tax
    return total_uusd


def simulate_cycle_legs(cycle: List[Edge], start_amount: int,
                         states: Dict[str, PoolState]) -> List[int]:
    """
    Returns the OFFER amount for every leg (length len(cycle)) instead of
    just the final result — e.g. offers[0] == start_amount, offers[1] ==
    whatever leg 0 is expected to deliver, and so on. Needed anywhere a
    caller has to construct realistic per-leg messages ahead of time.

    IMPORTANT: offers[-1] is the amount going INTO the final leg, NOT the
    cycle's final output — do not use this function to get the cycle's
    end result, use simulate_cycle() for that. (An earlier version of
    this file made exactly that mistake by having simulate_cycle derive
    its answer from this function's return value.)
    """
    offers = []
    amount = start_amount
    for edge in cycle:
        offers.append(amount)
        if amount <= 0:
            amount = 0
            continue
        taxed_offer = amount - tax_module.calculate_tax(amount, edge.asset_in, direction="out")
        state = states[id(edge.pool)]
        reserve_in = state.reserves[edge.asset_in.key()]
        reserve_out = state.reserves[edge.asset_out.key()]
        result = simulate_swap(taxed_offer, reserve_in, reserve_out, state.commission_rate)
        received = result.return_amount
        received -= tax_module.calculate_tax(received, edge.asset_out, direction="in")
        amount = received
    return offers


_DECIMAL_PLACES = 6  # matches the asset-decimals convention used elsewhere in
                      # this codebase; keeps belief_price/max_spread strings
                      # well within what Terraport/Terraswap-family contracts
                      # will actually parse (raw Decimal division can produce
                      # 40+ fractional digits, which some of these contracts
                      # reject outright with a parse error)


def _fmt_decimal(d: Decimal, places: int = _DECIMAL_PLACES, rounding=ROUND_HALF_UP) -> str:
    quant = Decimal(1).scaleb(-places)
    return str(d.quantize(quant, rounding=rounding))


def compute_leg_execution_params(cycle: List[Edge], leg_amounts: List[int],
                                  states: Dict[str, PoolState],
                                  tolerance_bps: int, ceiling_bps: int):
    """
    Per leg, returns a LegParams (see executor.py) anchored to the SAME
    snapshot reserves already used to size/price this cycle.

    Terraswap-family legs (edge.pool.pool_kind == "terraswap", the
    default): LegParams(kind="terraswap", belief_price=..., max_spread=...,
    min_receive=None). belief_price follows the pair contract's own
    convention: expected_return = offer_amount / belief_price, i.e.
    belief_price is reserve_in / reserve_out (pre-trade spot, no
    commission). max_spread = this leg's OWN simulated price impact
    (curvature + commission — already reflected in the profit number this
    cycle was approved on) + tolerance_bps of real extra room for the pool
    having moved between this snapshot and execution.

    Garuda legs (edge.pool.pool_kind == "garuda"): LegParams(kind="garuda",
    belief_price=None, max_spread=None, min_receive=...). Garuda's
    pair_base ExecuteMsg::Swap has no belief_price/max_spread field at all
    (confirmed 2026-08-04 via probe_garuda_schema.py) — min_receive is the
    ONLY on-chain slippage protection it accepts. Floored below this leg's
    simulated return_amount by tolerance_bps, the same real-world margin
    the Terraswap-family max_spread grants for pool movement between this
    snapshot and execution, just expressed as an absolute floor instead of
    a spread percentage. The same price-impact-vs-ceiling_bps check below
    still applies to Garuda legs even though the pool itself can't be told
    a spread ceiling directly — it's this function's own gate on whether
    the leg is safe to trade at all, independent of what the message can
    express.

    If a leg's required spread already exceeds ceiling_bps, returns None
    — caller should abort the whole cycle rather than send an unbounded/
    sanity-violating spread or min_receive floor.

    Terraswap belief_price/max_spread are quantized to _DECIMAL_PLACES
    before being returned as strings — raw Decimal division can carry 40+
    fractional digits, which at least one contract on this chain
    (Terraport) rejects outright with a decimal-parsing error rather than
    truncating it silently. max_spread rounds UP (never send a tighter
    tolerance than what was actually computed); belief_price rounds to
    nearest (it's a reference price, not a safety bound, so rounding
    direction doesn't matter for safety). min_receive is a plain integer
    (base units) and always rounds DOWN (never demand more than a
    slightly-worse-but-still-acceptable fill could deliver).
    """
    params = []
    for edge, offer_amount in zip(cycle, leg_amounts):
        pool_kind = getattr(edge.pool, "pool_kind", "terraswap")
        if offer_amount <= 0:
            if pool_kind == "garuda":
                params.append(LegParams(kind="garuda", belief_price=None, max_spread=None, min_receive=0))
            else:
                params.append(LegParams(kind="terraswap", belief_price="0", max_spread="0", min_receive=None))
            continue
        state = states[id(edge.pool)]
        reserve_in = state.reserves[edge.asset_in.key()]
        reserve_out = state.reserves[edge.asset_out.key()]
        if reserve_in <= 0 or reserve_out <= 0:
            return None

        belief_price = Decimal(reserve_in) / Decimal(reserve_out)

        taxed_offer = offer_amount - tax_module.calculate_tax(offer_amount, edge.asset_in, direction="out")
        result = simulate_swap(taxed_offer, reserve_in, reserve_out, state.commission_rate)
        expected_return_spot = Decimal(taxed_offer) / belief_price if belief_price > 0 else Decimal(0)
        if expected_return_spot <= 0:
            return None

        spread_amount = max(Decimal(0), expected_return_spot - Decimal(result.return_amount))
        spread_bps = (spread_amount / expected_return_spot) * Decimal(10000)
        required_spread_bps = spread_bps + Decimal(tolerance_bps)

        if required_spread_bps > Decimal(ceiling_bps):
            # DEMOTED 2026-08-06 from WARNING to DEBUG: this fires on every
            # candidate size the sizing search probes on a thin pool, not
            # just on a final "this cycle can never be traded" verdict — the
            # caller (sizing.spread_cap_for_cycle) is EXPECTED to hit this
            # repeatedly while shrinking toward a safe size, and returning
            # None here is itself the real signal the caller acts on.
            # Confirmed via arb_20260806.log: two known-thin pools
            # (Astroport ampLUNC/USTC, Terraswap ampLUNC/USTC 2) alone
            # produced 2010 WARNING lines in a single log capture — more log
            # volume than every INFO line combined — with no new information
            # in the repeats (same pool, same ~1000-1300 bps range, over and
            # over as the search narrows in). That's not an operator-actionable
            # signal at WARNING severity, and formatting/emitting that many
            # log lines was a real contributor to loop time. Still visible
            # with LOG_LEVEL=DEBUG for troubleshooting a specific pool.
            # ADDED 2026-09-01: the bps number alone can't tell you WHY a
            # leg is stuck above ceiling_bps even at a near-zero offer_amount
            # (a real case: Astroport ampLUNC/USTC required >1000bps at
            # every candidate spread_cap_for_cycle tried, all the way down
            # to its floor_probe, well above what the pool's own ~30bps
            # commission would predict for a near-zero trade — meaning
            # something about reserve_in/reserve_out/offer_amount itself,
            # not just curvature, was driving it, and the bps number alone
            # gave no way to tell which). Logging the actual inputs here
            # turns that into something readable straight from the log
            # instead of requiring a guess.
            log.debug("%s: required max_spread %.1f bps exceeds ceiling %.1f bps — "
                      "refusing to compute safe execution params. "
                      "[reserve_in=%d reserve_out=%d offer_amount=%d taxed_offer=%d "
                      "belief_price=%s expected_return_spot=%s actual_return=%d "
                      "commission_rate=%s]",
                      edge.pool.name, required_spread_bps, Decimal(ceiling_bps),
                      reserve_in, reserve_out, offer_amount, taxed_offer,
                      _fmt_decimal(belief_price), _fmt_decimal(expected_return_spot),
                      result.return_amount, _fmt_decimal(state.commission_rate))
            return None

        if pool_kind == "garuda":
            min_receive = int(Decimal(result.return_amount)
                               * (Decimal(10000) - Decimal(tolerance_bps)) / Decimal(10000))
            params.append(LegParams(kind="garuda", belief_price=None, max_spread=None,
                                     min_receive=max(0, min_receive)))
        else:
            belief_price_str = _fmt_decimal(belief_price)
            max_spread_str = _fmt_decimal(required_spread_bps / Decimal(10000), rounding=ROUND_UP)
            params.append(LegParams(kind="terraswap", belief_price=belief_price_str,
                                     max_spread=max_spread_str, min_receive=None))
    return params


def _simulate_leg_net(offer_amount: int, reserve_in: int, reserve_out: int,
                       commission_rate: Decimal, asset_in: Asset, asset_out: Asset) -> int:
    """One same-direction swap through this pool, net of BOTH-side tax —
    mirrors simulate_cycle's per-edge step exactly, factored out so
    simulate_leg_split (below) can call it repeatedly with updated
    reserves without duplicating the tax-and-swap sequence."""
    if offer_amount <= 0:
        return 0
    outgoing_tax = tax_module.calculate_tax(offer_amount, asset_in, direction="out")
    taxed_offer = offer_amount - outgoing_tax
    result = simulate_swap(taxed_offer, reserve_in, reserve_out, commission_rate)
    incoming_tax = tax_module.calculate_tax(result.return_amount, asset_out, direction="in")
    return max(0, result.return_amount - incoming_tax)


def simulate_leg_split(offer_total: int, reserve_in: int, reserve_out: int,
                        commission_rate: Decimal, num_trips: int,
                        asset_in: Asset, asset_out: Asset) -> int:
    """
    Total net output (tax + commission + spread all accounted, both
    sides) of routing offer_total through this ONE pool as num_trips
    sequential same-direction trades instead of one, each updating the
    working reserves from the trade before it.

    WHY THIS HELPS (real, not a modeling artifact): amm_math.simulate_swap
    mirrors the actual Terraswap/Astroport-family contract formula, which
    nets return_amount = raw_return - spread_amount - commission — the
    contract deducts the price-impact (spread) term from what the trader
    receives IN ADDITION TO commission, not just commission alone (see
    amm_math.py's docstring/comment on this, confirmed against a real
    simulate_fee "Cannot Sub" mismatch). spread_amount grows faster than
    linearly with offer size relative to reserves, so it's cheaper,
    trip-for-trip, to pay that penalty on several smaller offers than
    once on the combined size — confirmed by direct simulation: for a
    2,000 uluna-equivalent offer against 10,000/10,000 reserves, one trade
    nets ~1.297B (after tax+commission+spread) versus ~1.588B net across
    10 smaller trips against the same pool — an ~22% improvement in net
    output for the SAME total amount moved. This is NOT free extra
    liquidity access — the total amount routed through the pool, and
    therefore the total price impact and MAX_POOL_RESERVE_FRACTION
    exposure, is IDENTICAL either way. Splitting only changes how much
    of the AMM curve's output the split-deduction formula lets you keep.
    """
    if num_trips <= 0 or offer_total <= 0:
        return 0
    per_trip = offer_total // num_trips
    remainder = offer_total - per_trip * num_trips
    r_in, r_out = reserve_in, reserve_out
    total_net = 0
    for i in range(num_trips):
        amt = per_trip + (remainder if i == num_trips - 1 else 0)
        if amt <= 0 or r_in <= 0 or r_out <= 0:
            continue
        outgoing_tax = tax_module.calculate_tax(amt, asset_in, direction="out")
        taxed_offer = amt - outgoing_tax
        result = simulate_swap(taxed_offer, r_in, r_out, commission_rate)
        incoming_tax = tax_module.calculate_tax(result.return_amount, asset_out, direction="in")
        net = max(0, result.return_amount - incoming_tax)
        total_net += net
        r_in += amt
        r_out -= result.return_amount
    return total_net


def find_optimal_leg_split(offer_total: int, reserve_in: int, reserve_out: int,
                            commission_rate: Decimal, asset_in: Asset, asset_out: Asset,
                            gas_cost_per_trip: int, max_trips: int = 1):
    """
    Sweeps num_trips in [1, max_trips] and returns the count that
    maximizes NET output (simulate_leg_split's total minus
    gas_cost_per_trip * num_trips — the real cost of each extra bundled
    message in the same atomic transaction). Both benefit (spread
    reduction) and cost (gas) are monotone-ish in num_trips but in
    opposite directions, so this is a small, cheap sweep rather than a
    ternary search.

    OVERRIDDEN 2026-08-26 from max_trips=20 to 1 at the user's explicit
    instruction. This is the mechanism that was actually behind the
    "23 messages in one bundle" complaint — plan_repeated_cycle_execution
    repeating the WHOLE cycle (already capped to max_trips=1 as of the
    same change) was only part of it; THIS function, splitting a SINGLE
    leg's already-decided offer into up to 20 sequential sub-trades to
    squeeze ~22% more net output out of the spread-deduction curve, could
    multiply message count on its own — independently, per leg, so a
    4-hop cycle could turn into dozens of messages for a marginal edge.
    The user's framing was explicit: capture more profit by making the
    trade BIGGER (see config.SINGLE_TRIP_SIZE_MULTIPLIER), not by
    fragmenting it into more messages. With max_trips=1 the loop below
    only ever evaluates num_trips=1, so this always returns a single
    undivided trip — the function is left in place (not deleted) so a
    future change can raise this again without rebuilding it.

    gas_cost_per_trip must already be converted into asset_in's own units
    by the caller (gas is always paid in uluna on-chain; see
    arbitrage_bot.evaluate_cycle's _gas_cost_in_start_asset for the same
    conversion pattern) — this function does no unit conversion itself.

    Returns (best_num_trips, best_total_net). best_num_trips is always
    >= 1 (falls back to a single undivided trade if splitting never
    helps net-of-gas, e.g. a deep pool where spread is already
    negligible — the search naturally converges to 1 there, it is never
    forced).
    """
    best_n, best_net = 1, None
    for n in range(1, max_trips + 1):
        total_net = simulate_leg_split(offer_total, reserve_in, reserve_out,
                                        commission_rate, n, asset_in, asset_out)
        net_after_gas = total_net - gas_cost_per_trip * n
        if best_net is None or net_after_gas > best_net:
            best_n, best_net = n, net_after_gas
    return best_n, best_net


def plan_leg_execution(edge: Edge, offer_amount: int, state: PoolState,
                        tolerance_bps: int, ceiling_bps: int,
                        gas_cost_per_trip_in_leg_asset: int, max_trips: int = 1):
    """
    Decides how many sequential same-direction trips to split THIS leg's
    already-decided offer_amount into (see find_optimal_leg_split), then
    computes a real LegParams (belief_price/max_spread or min_receive) for
    EACH trip against progressively updated reserves — the same
    step-for-step sequence compute_leg_execution_params uses for a single
    trade, just repeated per trip so each message's on-chain slippage
    check is anchored to what the pool will actually look like when that
    specific message executes (which has already moved from the trip(s)
    before it, within the same atomic tx).

    offer_amount here is the FULL total for this leg, already shrunk to
    whatever liquidity_cap_for_cycle/spread_cap_for_cycle decided is safe
    — this function does not change that total, only how it's split into
    messages. If even a single trip can't clear ceiling_bps, returns None
    (same "abort this whole cycle" signal compute_leg_execution_params
    gives) rather than silently trading a subset.

    Returns a list of (trip_amount, LegParams) tuples, in execution order.
    """
    reserve_in = state.reserves[edge.asset_in.key()]
    reserve_out = state.reserves[edge.asset_out.key()]
    if offer_amount <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return None

    num_trips, _ = find_optimal_leg_split(
        offer_amount, reserve_in, reserve_out, state.commission_rate,
        edge.asset_in, edge.asset_out, gas_cost_per_trip_in_leg_asset, max_trips)

    per_trip = offer_amount // num_trips
    remainder = offer_amount - per_trip * num_trips
    pool_kind = getattr(edge.pool, "pool_kind", "terraswap")

    plan = []
    r_in, r_out = reserve_in, reserve_out
    for i in range(num_trips):
        amt = per_trip + (remainder if i == num_trips - 1 else 0)
        if amt <= 0 or r_in <= 0 or r_out <= 0:
            return None

        belief_price = Decimal(r_in) / Decimal(r_out)
        taxed_offer = amt - tax_module.calculate_tax(amt, edge.asset_in, direction="out")
        result = simulate_swap(taxed_offer, r_in, r_out, state.commission_rate)
        expected_return_spot = Decimal(taxed_offer) / belief_price if belief_price > 0 else Decimal(0)
        if expected_return_spot <= 0:
            return None

        spread_amount = max(Decimal(0), expected_return_spot - Decimal(result.return_amount))
        spread_bps = (spread_amount / expected_return_spot) * Decimal(10000)
        required_spread_bps = spread_bps + Decimal(tolerance_bps)
        if required_spread_bps > Decimal(ceiling_bps):
            log.debug("%s: trip %d/%d required max_spread %.1f bps exceeds ceiling %.1f bps — "
                      "aborting this leg's split plan.",
                      edge.pool.name, i + 1, num_trips, required_spread_bps, Decimal(ceiling_bps))
            return None

        if pool_kind == "garuda":
            min_receive = int(Decimal(result.return_amount)
                               * (Decimal(10000) - Decimal(tolerance_bps)) / Decimal(10000))
            params = LegParams(kind="garuda", belief_price=None, max_spread=None,
                                min_receive=max(0, min_receive))
        else:
            params = LegParams(kind="terraswap",
                                belief_price=_fmt_decimal(belief_price),
                                max_spread=_fmt_decimal(required_spread_bps / Decimal(10000), rounding=ROUND_UP),
                                min_receive=None)
        plan.append((amt, params))

        r_in += amt
        r_out -= result.return_amount

    return plan


def plan_cycle_execution(cycle: List[Edge], leg_amounts: List[int],
                          states: Dict[str, PoolState], tolerance_bps: int, ceiling_bps: int,
                          gas_cost_per_trip_uluna: int, pools, ustc: Asset, lunc: Asset,
                          max_trips_per_leg: int = 1):
    """
    Builds a flat execution plan across the WHOLE cycle: for each edge,
    plan_leg_execution decides its own optimal trip count independently
    (a deep leg naturally converges to 1 trip; a thin leg may split into
    several) — returns a single flat list of (edge, trip_amount,
    LegParams) covering every trip of every leg, in execution order,
    ready for executor.execute_plan_atomic. Returns None if any leg's
    plan is unsafe (mirrors compute_leg_execution_params's all-or-nothing
    behavior — a cycle either executes as a whole or not at all).

    gas_cost_per_trip_uluna is converted into each leg's own asset_in
    units via price_in_uusd, the same pattern evaluate_cycle uses for the
    cycle-level gas estimate — gas is always paid in uluna regardless of
    which asset a given leg happens to be offering.
    """
    flat_plan = []
    for edge, offer_amount in zip(cycle, leg_amounts):
        if offer_amount <= 0:
            continue
        if edge.asset_in.id == lunc.id and edge.asset_in.kind == "native":
            gas_in_leg_asset = gas_cost_per_trip_uluna
        else:
            price = price_in_uusd(edge.asset_in, pools, states, ustc, lunc)
            lunc_price = price_in_uusd(lunc, pools, states, ustc, lunc)
            if price <= 0 or lunc_price <= 0:
                return None
            gas_in_leg_asset = int(Decimal(gas_cost_per_trip_uluna) * lunc_price / price)

        state = states[id(edge.pool)]
        leg_plan = plan_leg_execution(edge, offer_amount, state, tolerance_bps, ceiling_bps,
                                       gas_in_leg_asset, max_trips_per_leg)
        if leg_plan is None:
            return None
        for trip_amount, params in leg_plan:
            flat_plan.append((edge, trip_amount, params))

    return flat_plan if flat_plan else None


def apply_cycle_trade_to_states(cycle: List[Edge], start_amount: int,
                                 states: Dict[str, PoolState]) -> Dict[str, PoolState]:
    """
    Returns a NEW states dict reflecting what every pool's reserves would
    look like immediately after start_amount is traded through this
    cycle — mirrors simulate_cycle's exact per-edge math (same tax +
    commission + spread sequence) but keeps the resulting reserve deltas
    instead of discarding them. Only pools this cycle actually touches
    get a new PoolState; every other pool's entry is the SAME object as
    in the input states dict (shared by reference, not copied) — cheap,
    and correct, since nothing here ever mutates the original states
    dict or its PoolState objects in place.

    Built for arbitrage_bot.plan_repeated_cycle_execution: lets the
    greedy multi-trip planner re-run evaluate_cycle's full sizing
    pipeline against "what the pool would look like after the previous
    trip" without ever touching the network — the whole planning loop
    stays pure local math, same as simulate_cycle/spread_cap_for_cycle
    already are.

    IMPORTANT: reserve_in is updated by the FULL offer amount (matching
    how the real contract accounts for it — the attached `funds` is the
    pre-outgoing-tax amount, same as taxed_offer's relationship to amount
    in simulate_cycle), and reserve_out is updated by result.return_amount
    (the pool's own gross output, BEFORE any incoming-side CW20 transfer
    tax — that tax happens at the token contract on receipt, not inside
    this pool's own balance, so it must not be subtracted from reserve_out
    here even though simulate_cycle correctly subtracts it from what the
    WALLET ultimately receives).
    """
    new_states = dict(states)  # shallow copy — untouched pools stay shared
    amount = start_amount
    for edge in cycle:
        if amount <= 0:
            break
        pool_key = id(edge.pool)
        state = new_states[pool_key]
        taxed_offer = amount - tax_module.calculate_tax(amount, edge.asset_in, direction="out")
        reserve_in = state.reserves[edge.asset_in.key()]
        reserve_out = state.reserves[edge.asset_out.key()]
        result = simulate_swap(taxed_offer, reserve_in, reserve_out, state.commission_rate)

        new_reserves = dict(state.reserves)
        new_reserves[edge.asset_in.key()] = reserve_in + amount
        new_reserves[edge.asset_out.key()] = max(0, reserve_out - result.return_amount)
        new_states[pool_key] = PoolState(name=state.name, pair_address=state.pair_address,
                                          reserves=new_reserves, commission_rate=state.commission_rate)

        received = result.return_amount
        received -= tax_module.calculate_tax(received, edge.asset_out, direction="in")
        amount = received
    return new_states


def cycle_label(cycle: List[Edge]) -> str:
    parts = [str(cycle[0].asset_in)]
    for e in cycle:
        parts.append(f"-[{e.pool.name}]->{e.asset_out}")
    return "".join(parts)


def price_in_uusd(asset: Asset, pools: List[DexPool], states: Dict[str, PoolState],
                   ustc: Asset, lunc: Asset) -> Decimal:
    """
    Best-effort spot price of `asset` in uusd terms, used only for
    converting gas cost / profit thresholds across different offer
    assets — not used in the actual swap math. Prefers a direct pool with
    USTC; falls back to routing through LUNC if needed.
    """
    if asset.key() == ustc.key():
        return Decimal(1)

    def direct_price(a: Asset, quote: Asset):
        for p in pools:
            keys = {p.asset_x.key(), p.asset_y.key()}
            if a.key() in keys and quote.key() in keys:
                state = states[id(p)]
                r_a = state.reserves[a.key()]
                r_q = state.reserves[quote.key()]
                if r_a > 0:
                    return Decimal(r_q) / Decimal(r_a)
        return None

    direct = direct_price(asset, ustc)
    if direct is not None:
        return direct

    via_lunc = direct_price(asset, lunc)
    lunc_in_uusd = direct_price(lunc, ustc)
    if via_lunc is not None and lunc_in_uusd is not None:
        return via_lunc * lunc_in_uusd

    log.warning("Could not price %s in uusd terms (no direct or LUNC-routed pool found)", asset)
    return Decimal(0)