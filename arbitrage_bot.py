"""
Multi-pool, multi-asset arbitrage across:
  - two Terraswap LUNC/USTC pools
  - Terraport TERRA/LUNC and TERRA/USTC pools (TERRA is a CW20 token)
  - Terraport LCW/LUNC and LCW/USTC pools (LCW is a CW20 token with its
    own ~5% transfer tax — see config.cw20_transfer_tax_rate)
  - Terraswap/Astroport MIR/USTC, Astroport ASTRO/LUNC + ASTRO/USTC,
    Terraswap/Terraport TRIT/LUNC + TRIT/USTC
  - Terraport/Garuda DeFi JURIS/LUNC (same pair, two venues — Garuda is a
    new venue for this bot, unverified beyond commission auto-resolution)
    and Terraport JURIS/TERRA (a CW20/CW20 pair, no native asset on
    either side)
  - USDC.eth.axl (native IBC asset, not CW20) against LUNC, USTC, and
    TERRA on Terraport, plus USTC on Terraswap — USDC.eth.axl/USTC exists
    on both venues
  - Terraport REV/LUNC and REV/USTC (REV is a CW20 token)

No fixed trade-amount caps. Each loop:
  1. Fetch reserves for every configured pool once.
  2. For each asset as a starting point, find every cycle (2..N hops)
     that returns to that same asset using distinct pools.
  3. For each cycle: probe its intrinsic edge, size it as a fraction of
     your LIVE wallet balance of the starting asset (sizing.py — weak
     edge -> small fraction, strong edge -> up to BALANCE_FRACTION_MAX),
     then find the actual profit-maximizing size within that ceiling.
     Profit accounts for each pool's commission, native stability tax,
     CW20 transfer tax (both directions), a slippage buffer, and
     estimated gas.
  4. Before executing, compute a real belief_price/max_spread for every
     leg from the SAME snapshot reserves the cycle was sized against
     (graph.compute_leg_execution_params) — anchoring the on-chain fill
     check to what was actually simulated, instead of the old flat
     max_spread=0.02 with no belief_price at all.
  5. Execute the best cycle above MIN_PROFIT_UUSD, leg by leg — using the
     wallet's ACTUAL balance change after each leg (not the swap event)
     to decide the next leg's offer amount, so a token with an
     undocumented transfer tax can never cause an over-send. Also
     corrects for gas contamination when a leg's output is LUNC itself
     (gas is always paid in LUNC, so raw balance delta there is swap
     proceeds minus gas, not proceeds alone).

Run: python arbitrage_bot.py
"""
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

import config
from assets import Asset
from amm_math import find_optimal_trade_size
from pool_client import DexPool, GarudaPool, get_asset_balance_raw, get_all_native_balances_raw
from terra_client import TerraClient
from executor import execute_leg, execute_cycle_atomic, build_leg_msg

import graph as graph_module
import sizing

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("arb")

GAS_UNITS_PER_HOP = 500_000  # rough estimate per MsgExecuteContract

# ADDED 2026-08-27 after arb_20260827.log showed ONE structurally-broken
# cycle (LUNC-[Terraport JURIS/LUNC]->JURIS-[Garuda BENANCE/JURIS]->
# BENANCE-[Garuda BENANCE/LUNC]->LUNC) failing its REAL check ("Insufficient
# return amount" on the same leg, message index 2) on 37 out of 37 attempts
# across a 10-minute session — and because it consistently ranked #1 by
# cheap flat-guess profit, it was the ONLY candidate ever tried each loop
# (candidates_sorted's "break on first non-passing" rule means nothing
# ranked below the one candidate that clears the flat floor ever gets a
# chance), starving out the ~69 other real candidates found that same
# loop. This is a general problem, independent of whatever the JURIS/
# BENANCE-specific root cause turns out to be (leading suspicion: Garuda's
# commission rate was taken from docs before the venue was ever live-
# tested and is never re-verified — see config.GARUDA_COMMISSION_RATE's
# comment — the same class of mistake already confirmed once for
# Terraport's assumed-vs-real commission rate): a cycle that fails its
# REAL check (not just the cheap flat-guess floor) gets marked as
# temporarily not-passing for config.CYCLE_FAIL_COOLDOWN_SECONDS, so the
# NEXT loop's candidate ranking doesn't hand it the same monopoly again.
# Keyed by sizing._cycle_signature (same pools, same direction through
# each) so this survives being rediscovered from a different start asset.
# In-memory only (resets on restart) — deliberately not persisted, since
# a fresh process should get a clean chance to re-evaluate everything.
_recent_real_check_failures = {}


def _cycle_in_cooldown(cycle) -> bool:
    sig = sizing._cycle_signature(cycle)
    failed_at = _recent_real_check_failures.get(sig)
    if failed_at is None:
        return False
    return (time.time() - failed_at) < config.CYCLE_FAIL_COOLDOWN_SECONDS


def _mark_cycle_real_check_failed(cycle):
    sig = sizing._cycle_signature(cycle)
    _recent_real_check_failures[sig] = time.time()


def apply_slippage_buffer(amount: int) -> int:
    return amount * (10000 - config.SLIPPAGE_BUFFER_BPS) // 10000

def clears_min_profit(start_asset, net_profit_start_asset: int, profit_uusd: int) -> bool:
    """
    A cycle counts as profitable if it clears the floor appropriate to
    its OWN starting asset — LUNC-rooted cycles are judged directly in
    uluna (MIN_PROFIT_ULUNA), everything else via MIN_PROFIT_UUSD. See
    config.MIN_PROFIT_ULUNA's comment for why this replaced a single
    uusd-converted check for every asset.
    """
    if start_asset.kind == "native" and start_asset.id == config.DENOM_LUNC:
        return net_profit_start_asset >= config.MIN_PROFIT_ULUNA
    return profit_uusd >= config.MIN_PROFIT_UUSD

def force_trade_listener(event: threading.Event):
    """
    Runs for the life of the process in a daemon thread, reading stdin
    line by line. Typing config.FORCE_TRADE_KEYWORD (case-insensitive)
    and pressing Enter arms `event` — a one-shot flag that the NEXT loop
    iteration consumes to force through its best-found cycle at a small,
    probe-sized offer, bypassing only the MIN_PROFIT_UUSD gates. Every
    other safety check (spread ceiling, real gas simulation, gas reserve
    floor, DRY_RUN) still applies exactly as it does for a real
    opportunity — see run_once's forced-trade block.

    Only useful when stdin is an interactive terminal. run_bot.sh only
    pipes stdout/stderr through `tee` (not stdin), so typing still reaches
    this process when launched that way. If stdin isn't readable at all
    (piped from /dev/null, some process managers/services), this thread
    just exits quietly on the first read error — the keyword trigger
    becomes unavailable for the rest of the run, but the bot's normal
    loop is completely unaffected either way.
    """
    log.info("Type '%s' + Enter at any time to arm a forced, non-profit-gated test "
              "trade on the best cycle found on the NEXT loop (useful for confirming "
              "live/atomic execution actually works without waiting for a real "
              "profitable opportunity).", config.FORCE_TRADE_KEYWORD)
    try:
        for line in sys.stdin:
            if line.strip().lower() == config.FORCE_TRADE_KEYWORD.strip().lower():
                event.set()
                log.warning("Forced test trade ARMED via '%s' — will fire on the best cycle "
                            "found on the next loop iteration, at a small probe-sized offer "
                            "(still subject to spread/gas/liquidity safety checks, just not "
                            "the profit threshold).", config.FORCE_TRADE_KEYWORD)
    except Exception as e:
        log.warning("Force-trade stdin listener stopped (%s) — the keyword trigger is no "
                    "longer available for the rest of this run; the bot's normal loop is "
                    "unaffected.", e)


def commission_refresh_loop(pools, interval_seconds: int = 30):
    """
    Runs for the life of the process in a daemon thread. Gives every pool
    a chance to refresh its live commission_rate on a short, steady
    cadence — DexPool.refresh_commission_if_due() only actually issues
    the LCD simulation query once that pool's own 600s TTL has elapsed,
    so most ticks here are no-ops; this loop just makes sure a due
    refresh happens promptly instead of piggybacking on the next
    get_state() call.

    Parallelized across pools (small worker cap, same pattern as
    run_once's pool-state fetch) — NOT sequential. A real log caught the
    sequential version costing ~6s on cold start: every pool's TTL is
    expired at once right after the process starts (never resolved
    before), so a for-loop calling refresh_commission_if_due() one pool
    at a time turned into ~20 back-to-back LCD round trips competing with
    run_once's own concurrent fetch for the same connections — exactly
    the kind of serialized latency this thread was supposed to eliminate,
    not reintroduce.

    Deliberately kept OFF run_once's hot path either way (see
    pool_client.DexPool.get_state's docstring) — even parallelized, a
    commission refresh's LCD round trip should never block the reserve
    fetch every loop's opportunity search is timed against. Failures here
    are logged and skipped; a pool that fails to refresh just keeps its
    last-known commission_rate (or the configured default), same
    fail-safe behavior as before.
    """
    def _safe_refresh(p):
        try:
            p.refresh_commission_if_due()
        except Exception as e:
            log.warning("%s: background commission refresh failed (%s) — "
                        "keeping current rate %s.", p.name, e, p.commission_rate)

    while True:
        with ThreadPoolExecutor(max_workers=min(len(pools), 6)) as executor:
            list(executor.map(_safe_refresh, pools))
        time.sleep(interval_seconds)


def evaluate_cycle(cycle, states, lunc_price_uusd: Decimal, balance: int,
                    start_asset_price_uusd: Decimal):
    start_asset = cycle[0].asset_in
    max_offer, probe_edge_bps = sizing.max_offer_for_cycle(cycle, states, balance, start_asset)

    if max_offer <= 0:
        return 0, 0, 0, -(10 ** 18), probe_edge_bps

    def _gas_cost_in_start_asset():
        gas_units = GAS_UNITS_PER_HOP * len(cycle)
        gas_cost_uluna = int(Decimal(gas_units) * config.GAS_PRICE)
        if start_asset.id == config.DENOM_LUNC and start_asset.kind == "native":
            return gas_cost_uluna
        if start_asset_price_uusd > 0:
            # gas_cost_uluna * lunc_price_uusd = gas cost in UUSD terms (always
            # correct, regardless of start_asset). Dividing by start_asset's OWN
            # uusd price converts that into start_asset's native units.
            gas_cost_in_uusd = Decimal(gas_cost_uluna) * lunc_price_uusd
            return int(gas_cost_in_uusd / start_asset_price_uusd)
        # No price found for start_asset (e.g. an unrouted/illiquid pool) —
        # fail safe by treating gas as unaffordable rather than silently
        # using a wrong-unit number, same spirit as this codebase's other
        # "reject rather than guess" fallbacks.
        return None

    gas_cost_in_start_asset = _gas_cost_in_start_asset()
    if gas_cost_in_start_asset is None:
        return 0, 0, 0, -(10 ** 18), probe_edge_bps

    # Quick-reject — ADDED 2026-08-07 after pool count grew to ~39 (this
    # loop's per-cycle cost was already flagged as a real ~1s/loop CPU
    # cost in sizing.spread_cap_for_cycle's own docstring back when there
    # were far fewer pools/cycles to check). probe_edge_bps reflects the
    # AMM's near-zero-size marginal rate — real slippage only makes the
    # EFFECTIVE rate worse as size grows, never better, so extrapolating
    # that rate linearly across the full max_offer is a safe UPPER BOUND
    # on real achievable gross profit at this size. This can never wrongly
    # skip a genuinely profitable cycle (the real number is always <= this
    # estimate) — it only skips cycles that can't clear the profit floor
    # even under the most optimistic possible reading, before paying for
    # spread_cap_for_cycle's binary search or find_optimal_trade_size's
    # ternary search on them.
    optimistic_gross = int(Decimal(max_offer) * probe_edge_bps / Decimal(10000))
    optimistic_net = optimistic_gross - gas_cost_in_start_asset
    if start_asset.id == config.DENOM_LUNC and start_asset.kind == "native":
        optimistic_passes = optimistic_net >= config.MIN_PROFIT_ULUNA
    else:
        optimistic_passes = int(optimistic_net * start_asset_price_uusd) >= config.MIN_PROFIT_UUSD
    if not optimistic_passes:
        return 0, 0, gas_cost_in_start_asset, -(10 ** 18), probe_edge_bps

    # liquidity_cap_for_cycle (inside max_offer_for_cycle) only limits size
    # to a FRACTION of each pool's reserve — it doesn't guarantee the
    # resulting per-leg spread clears MAX_SPREAD_CEILING_BPS. For a thin
    # pool that's not enough: shrink further to the largest size that's
    # actually safe to execute, so a real (if smaller) opportunity on a
    # thin pool gets traded instead of thrown away entirely once
    # compute_leg_execution_params would have rejected the bigger size
    # later anyway.
    real_max_offer = sizing.spread_cap_for_cycle(
        cycle, states, max_offer, config.SPREAD_TOLERANCE_BPS, config.MAX_SPREAD_CEILING_BPS)

    if real_max_offer <= 0:
        # SHADOW-ONLY CHECK — never used for real sizing or execution.
        # spread_cap_for_cycle takes ceiling_bps as a plain argument (it's
        # never read from config inside graph.py), so calling it again
        # with SHADOW_SPREAD_CEILING_BPS here cannot influence the real
        # max_offer above or anything this function returns. Only runs on
        # cycles the REAL ceiling already killed, so it adds no cost to
        # any cycle that was going to size/trade anyway.
        shadow_max_offer = sizing.spread_cap_for_cycle(
            cycle, states, max_offer, config.SPREAD_TOLERANCE_BPS, config.SHADOW_SPREAD_CEILING_BPS)
        if shadow_max_offer > 0:
            shadow_final = graph_module.simulate_cycle(cycle, shadow_max_offer, states)
            shadow_final = apply_slippage_buffer(shadow_final)
            shadow_gross = shadow_final - shadow_max_offer
            shadow_net = shadow_gross - gas_cost_in_start_asset
            if start_asset.id == config.DENOM_LUNC and start_asset.kind == "native":
                shadow_passes = shadow_net >= config.MIN_PROFIT_ULUNA
            else:
                shadow_passes = int(shadow_net * start_asset_price_uusd) >= config.MIN_PROFIT_UUSD
            log.info("SHADOW (not traded) %s: at %d bps ceiling this cycle would size to "
                      "offer=%d net_in_start_asset=%d — %s MIN_PROFIT.",
                      graph_module.cycle_label(cycle), config.SHADOW_SPREAD_CEILING_BPS,
                      shadow_max_offer, shadow_net, "CLEARS" if shadow_passes else "still misses")
        return 0, 0, 0, -(10 ** 18), probe_edge_bps

    max_offer = real_max_offer

    def profit_fn(amount: int) -> int:
        if amount <= 0:
            return -(10 ** 18)
        final_amount = graph_module.simulate_cycle(cycle, amount, states)
        final_amount = apply_slippage_buffer(final_amount)
        return final_amount - amount

    low = max(1, max_offer // 10000)
    best_amount = find_optimal_trade_size(profit_fn, low, max_offer)
    gross_profit = profit_fn(best_amount)

    # ADDED 2026-08-26 at the user's explicit instruction, built directly
    # from real trade history (a Terraport REV pool, wallet uae0): the bot
    # correctly found a buy-cheap-on-USTC/sell-on-LUNC route, but
    # find_optimal_trade_size above found its TRUE profit-MAXIMIZING
    # point — on a thin pool that peak can be small (that trace showed
    # three separate ~5 USTC trades across three consecutive loops instead
    # of one ~15 USTC trade). The user's framing: if a bigger size (target
    # floor below) STILL shows real profit — not necessarily the
    # mathematical peak, just genuinely profitable — take the bigger size
    # in ONE trade instead of leaving the rest for a later loop to
    # rediscover (and pay gas for) separately. max_offer here is already
    # the real safety ceiling (post liquidity_cap_for_cycle AND
    # spread_cap_for_cycle), so this can never push past what's actually
    # safe for the pool — it only pushes UP from the peak toward that
    # ceiling, never beyond it.
    if start_asset.id == config.DENOM_LUNC and start_asset.kind == "native":
        target_floor = config.TARGET_TRIP_AMOUNT_ULUNA
    elif start_asset_price_uusd > 0:
        target_floor = int(Decimal(config.TARGET_TRIP_UUSD_EQUIV) / start_asset_price_uusd)
    else:
        target_floor = 0
    target_amount = min(max_offer, max(best_amount, target_floor))
    if target_amount > best_amount:
        target_profit = profit_fn(target_amount)
        target_net = target_profit - gas_cost_in_start_asset
        # "profit still exists" — a real, positive net after gas, same bar
        # everything else in this function judges profitability by. Not
        # required to match the peak's profit, only to still be a genuine
        # gain rather than a wash.
        if target_net > 0:
            best_amount, gross_profit = target_amount, target_profit

    # SIZE floor — distinct from the PROFIT floor below. A trip can be
    # marginally profitable (clears MIN_PROFIT_ULUNA/MIN_PROFIT_UUSD) and
    # still be too small to be worth a fixed per-hop gas payment — this is
    # exactly what let plan_repeated_cycle_execution chain several tiny
    # trips into one gas-heavy bundle on a thin pool (see config.
    # MIN_TRIP_GAS_MULTIPLE's comment). SCALES with the trip's own real
    # gas cost (gas_cost_in_start_asset is already in the same units as
    # best_amount, so this is a single unit-consistent comparison
    # regardless of start asset or hop count — no separate LUNC/uusd
    # constants needed). Checked here, centrally, so it applies uniformly
    # to the first trip AND every additional trip the greedy
    # repeated-cycle loop considers (that loop calls this same function
    # per trip and already stops as soon as amount<=0 comes back).
    below_size_floor = best_amount < gas_cost_in_start_asset * config.MIN_TRIP_GAS_MULTIPLE
    if below_size_floor:
        return 0, 0, gas_cost_in_start_asset, -(10 ** 18), probe_edge_bps

    net_profit_start_asset = gross_profit - gas_cost_in_start_asset
    return best_amount, gross_profit, gas_cost_in_start_asset, net_profit_start_asset, probe_edge_bps


def build_cycle_msgs(terra, cycle, leg_amounts, leg_params):
    """
    Builds the actual MsgExecuteContract list for every leg of a cycle,
    using leg_amounts[i] as the offer for edge i and leg_params[i] (a
    LegParams — see executor.py) as that leg's execution parameters —
    both must come from the same snapshot (graph_module.simulate_cycle_legs
    / compute_leg_execution_params) so the gas simulation and the real
    execution are evaluating identical messages, not slightly different
    ones. leg_amounts[1:] reflects what the PREVIOUS leg is actually
    expected to deliver — not a copy of the initial offer. A leg that
    attaches real funds (any native-asset offer) will be rejected by
    simulate_fee() with an insufficient-funds error if its amount doesn't
    match what the prior leg can plausibly produce, since the chain
    validates spendable balance even during simulation.

    executor.build_leg_msg picks the right message shape (Terraswap-family
    belief_price/max_spread, or Garuda min_receive) per leg_params[i].kind
    — this function itself doesn't need to know which pools are which.
    """
    return [
        build_leg_msg(terra.address, edge, leg_amounts[i], leg_params[i])
        for i, edge in enumerate(cycle)
    ]


def real_gas_cost_uluna(terra, cycle, leg_amounts, leg_params, fallback_gas_units_per_hop=GAS_UNITS_PER_HOP,
                         account_number=None, sequence=None):
    """
    Best-effort real gas estimate via terra.simulate_fee(), using the
    SAME leg_amounts/leg_params the cycle will actually be executed with.
    Falls back to the flat per-hop guess (with a warning) if simulation
    fails for any reason — a failed simulate_fee is not itself proof the
    trade is safe, so callers should treat the fallback path
    conservatively.

    account_number/sequence should come from run_once's single per-loop
    terra.get_account_number_and_sequence() call — passing them through
    here avoids simulate_fee's own implicit per-call account lookup (see
    TerraClient.simulate_fee's docstring), which used to cost an EXTRA
    LCD round trip on every candidate tried in the fallback loop below.
    """
    try:
        msgs = build_cycle_msgs(terra, cycle, leg_amounts, leg_params)
        fee = terra.simulate_fee(msgs, account_number=account_number, sequence=sequence)
        total = sum(int(c.amount) for c in fee.amount if c.denom == config.GAS_DENOM)
        if total > 0:
            return total, True
        log.warning("simulate_fee returned no %s fee amount — falling back to flat gas estimate.",
                    config.GAS_DENOM)
    except Exception as e:
        log.warning("simulate_fee failed (%s) — falling back to flat gas estimate.", e)

    gas_units = fallback_gas_units_per_hop * len(cycle)
    return int(Decimal(gas_units) * config.GAS_PRICE), False



def _refresh_states_for_cycle(cycle, states):
    """
    Re-fetches ONLY the pool(s) touched by this one candidate cycle,
    fresh, right before it's about to be traded on — instead of trusting
    the loop-start snapshot every candidate has been sized against, which
    is typically 20-30+ real seconds old by the time a candidate reaches
    this point. Confirmed via arb_20260830.log (09:35:56-09:36:00): a
    26.77s discovery+sizing pass, then simulate_fee immediately failed
    with "Operation exceeds max spread limit" — the pool had already
    moved past the belief_price/max_spread computed off that stale
    snapshot before the bot even asked the chain about it.

    Cheap by construction: a cycle touches a handful of pools (2-4
    typically), not the ~50-pool universe run_once fetches at loop
    start, so this costs 1-4 real LCD round-trips (~0.1-0.4s each,
    per-pool timings observed elsewhere in the same log) instead of the
    ~20-30s the full discovery pass took to even get here. Falls back to
    this loop's existing state for any pool whose fresh fetch fails,
    same spirit as run_once's own last_good_states fallback — never
    raises, never blocks the candidate loop.
    """
    fresh_states = dict(states)
    seen_pool_ids = set()
    for edge in cycle:
        p = edge.pool
        if id(p) in seen_pool_ids:
            continue
        seen_pool_ids.add(id(p))
        try:
            fresh_states[id(p)] = p.get_state()
        except Exception as e:
            log.warning("%s: fresh pre-trade state refetch failed (%s) — using this loop's "
                        "existing (staler) state for this pool instead.", p.name, e)
    return fresh_states


def _prepare_execution(terra, pools, states, ustc, lunc, cycle, amount, gross_profit,
                        start_asset, label, bypass_profit_gate=False,
                        account_number=None, sequence=None):
    """
    Runs the SAME per-candidate safety checks that used to only ever run
    once (on whichever single cycle happened to rank #1 by flat-guess
    profit_uusd): spread-ceiling execution params, then the real
    simulate_fee() gas check re-pricing profit against actual chain fees.

    Returns (leg_amounts, leg_params, real_profit_uusd, real_gas_uluna) if
    this candidate clears every check (the specific failure is already
    logged before returning None otherwise) — callers loop over candidates
    trying each until one of these succeeds, instead of giving up on the
    whole loop iteration when just the single best-by-flat-guess pick
    fails. This is what actually lets a smaller, real edge (e.g. through
    JURIS/TERRA) get traded on a loop where a bigger-looking but ultimately
    ungaseable cycle (e.g. through REV) keeps winning the flat-guess
    ranking every time.

    bypass_profit_gate=True skips both the MIN_PROFIT_UUSD/MIN_PROFIT_ULUNA
    flat floor AND the MIN_PROFIT_MARGIN_MULTIPLE margin-over-real-cost
    check (used only by the forced-test-trade path) — every other check
    (spread ceiling, real gas simulation succeeding at all) still applies.
    """
    leg_amounts = graph_module.simulate_cycle_legs(cycle, amount, states)
    leg_params = graph_module.compute_leg_execution_params(
        cycle, leg_amounts, states, config.SPREAD_TOLERANCE_BPS, config.MAX_SPREAD_CEILING_BPS)
    if leg_params is None:
        log.info("Skipping %s: could not compute a safe execution spread within the %.1f%% "
                  "sanity ceiling for at least one leg.", label, config.MAX_SPREAD_CEILING_BPS / 100)
        return None

    log.info("Execution params for %s: %s", label,
              [(f"kind={p.kind}", f"belief={p.belief_price}", f"max_spread={p.max_spread}",
                f"min_receive={p.min_receive}") for p in leg_params])

    # profit_uusd the caller ranked candidates by was computed using
    # GAS_UNITS_PER_HOP, a flat guess, so every candidate could be scored
    # cheaply without hitting the LCD for each one. Before committing real
    # funds to THIS candidate, get the chain's own fee estimate for the
    # actual messages it would send, and re-price profit against that.
    real_gas_uluna, used_real_simulate = real_gas_cost_uluna(
        terra, cycle, leg_amounts, leg_params, account_number=account_number, sequence=sequence)

    if not used_real_simulate:
        log.info("Skipping %s: real gas simulation failed, so there's no verified number to "
                  "trade on — refusing to fall back to the flat guess that already caused a "
                  "real loss once (2026-07-12). A simulate_fee failure on an overflow/"
                  "insufficient-funds error usually means leg_amounts (used to build messages) "
                  "and the slippage-buffered amount the profit decision was based on have "
                  "drifted apart — check apply_slippage_buffer usage between evaluate_cycle "
                  "and simulate_cycle_legs if this keeps happening.", label)
        return None

    if start_asset.id == config.DENOM_LUNC and start_asset.kind == "native":
        real_gas_in_start_asset = real_gas_uluna
    else:
        real_gas_in_start_asset = int(real_gas_uluna * graph_module.price_in_uusd(
            lunc, pools, states, ustc, lunc))
    real_net_in_start_asset = gross_profit - real_gas_in_start_asset
    real_profit_uusd = int(real_net_in_start_asset * graph_module.price_in_uusd(
        start_asset, pools, states, ustc, lunc))

    log.info("Real gas check (%s): gas_uluna=%d real_profit_uusd=%d",
              "simulate_fee" if used_real_simulate else "FALLBACK flat guess",
              real_gas_uluna, real_profit_uusd)
    is_lunc_gated = start_asset.kind == "native" and start_asset.id == config.DENOM_LUNC
    required_min = config.MIN_PROFIT_ULUNA if is_lunc_gated else config.MIN_PROFIT_UUSD
    required_min_units = "uluna" if is_lunc_gated else "uusd"
    margin_over_min = (real_net_in_start_asset if is_lunc_gated else real_profit_uusd) - required_min
    log.info("Cost breakdown for %s: gross_profit_after_commission_and_tax=%s(%s units) "
              "real_gas=%d uluna(%s) -> net_after_fees_and_tax=%s(%s units), "
              "required_min=%d %s, margin_over_min=%d %s",
              label, gross_profit, start_asset, real_gas_uluna, "uluna",
              real_net_in_start_asset, start_asset, required_min, required_min_units,
              margin_over_min, required_min_units)

    flat_floor_passes = clears_min_profit(start_asset, real_net_in_start_asset, real_profit_uusd)

    # Margin-multiple check: net profit must ALSO be at least
    # MIN_PROFIT_MARGIN_MULTIPLE times the real total per-trade cost this
    # specific cycle actually paid — AMM commission + tax on both sides of
    # every leg (graph_module.simulate_cycle_costs_uusd) plus real
    # simulated gas — not just clear the flat floor. A cycle can clear
    # MIN_PROFIT_UUSD/MIN_PROFIT_ULUNA by a hair while fees+tax ate nearly
    # all of gross profit; this catches that case even when the flat
    # floor alone would have let it through. See config.py's
    # MIN_PROFIT_MARGIN_MULTIPLE comment for the reasoning.
    #
    # Uses graph_module.simulate_cycle_costs_uusd rather than re-deriving
    # commission/tax here — an EARLIER version of this check computed only
    # the outgoing-side native tax itself (via leg_amounts), which missed
    # AMM commission entirely and the incoming-side CW20 transfer tax
    # (fees are charged on every leg, both directions, not just the
    # native-tax side) — silently understating real cost and making the
    # margin requirement weaker than intended. simulate_cycle_costs_uusd
    # mirrors simulate_cycle's own math step-for-step so this can't drift
    # from what simulate_cycle actually nets out.
    real_fees_and_tax_uusd = graph_module.simulate_cycle_costs_uusd(
        cycle, amount, states, pools, ustc, lunc)
    real_gas_uusd = Decimal(real_gas_uluna) * graph_module.price_in_uusd(lunc, pools, states, ustc, lunc)
    real_total_cost_uusd = real_fees_and_tax_uusd + real_gas_uusd
    required_margin_uusd = config.MIN_PROFIT_MARGIN_MULTIPLE * real_total_cost_uusd
    actual_margin_uusd = Decimal(real_profit_uusd)
    margin_passes = actual_margin_uusd >= required_margin_uusd
    log.info("Margin check for %s: real_fees_and_tax_uusd=%s real_gas_uusd=%s total_cost_uusd=%s "
              "required(%sx)=%s actual_profit_uusd=%d -> %s",
              label, real_fees_and_tax_uusd, real_gas_uusd, real_total_cost_uusd,
              config.MIN_PROFIT_MARGIN_MULTIPLE, required_margin_uusd, real_profit_uusd,
              "PASS" if margin_passes else "FAIL")

    real_passes = flat_floor_passes and margin_passes
    if not bypass_profit_gate and not real_passes:
        reason = ("real gas cost erases the edge" if not flat_floor_passes
                   else "profit doesn't clear the required margin over real commission+tax+gas cost")
        log.info("Skipping %s: %s (net_in_start_asset=%d %s, real_profit_uusd=%d, "
                  "real_total_cost_uusd=%s, required_margin_uusd=%s) — this would have been "
                  "a loss or too thin a margin to survive estimate error.",
                  label, reason, real_net_in_start_asset, start_asset, real_profit_uusd,
                  real_total_cost_uusd, required_margin_uusd)
        return None
    if bypass_profit_gate and not real_passes:
        log.warning("Forced test trade %s: net_in_start_asset=%d %s (real_profit_uusd=%d) is "
                     "below its floor or required margin — proceeding anyway since this is a "
                     "deliberate non-profit-seeking test, not a real opportunity.",
                     label, real_net_in_start_asset, start_asset, real_profit_uusd)

    return leg_amounts, leg_params, real_profit_uusd, real_gas_uluna


def cycle_touches_a_token(cycle) -> bool:
    """
    True if this cycle trades through at least one CW20 token (a real
    tradeable pair, e.g. XXX/USTC vs XXX/LUNC) rather than staying purely
    within native LUNC<->USTC (a currency-rebalancing loop that never
    touches an actual token pair). Used to rank real pair-arbitrage ahead
    of pure LUNC<->USTC round trips — see the candidate sort below.
    """
    return any(edge.asset_in.kind != "native" or edge.asset_out.kind != "native"
               for edge in cycle)



def plan_repeated_cycle_execution(cycle, states, balance, start_asset, lunc_price_uusd,
                                   start_asset_price_uusd, gas_cost_per_trip_start_asset,
                                   max_trips=1):
    """
    Greedily finds MORE trips through the SAME already-winning cycle,
    re-running evaluate_cycle's full sizing pipeline against a LOCAL,
    progressively-updated copy of reserves (graph_module.
    apply_cycle_trade_to_states) after each trip — never touches the
    network, so this is cheap: evaluate_cycle's own cost was measured at
    ~1-6ms/cycle via this codebase's own timing instrumentation
    (arb_20260824.log), and this calls it at most max_trips times.

    OVERRIDDEN 2026-08-26 to max_trips=1 at the user's explicit
    instruction, superseding the 2026-08-25 max_trips=8 rationale below
    (kept for history) and the 2026-08-26-morning max_trips=3 revision.
    The user's own words: "I want it to spend 2x more — not through many
    msg or trips or hops, just increase the size to get more profit."
    With max_trips=1 this function only ever returns the single best
    trip — the loop below still runs but exits after one iteration — and
    capturing MORE of a persisting opportunity is now the job of
    config.SINGLE_TRIP_SIZE_MULTIPLIER (applied upstream in sizing.
    max_offer_for_cycle) instead of repeated trips. This function is left
    in place rather than deleted so a future change can raise max_trips
    again without rebuilding this logic from scratch, but nothing in the
    current codebase calls it with any other value.

    (Historical context, no longer the operative reasoning — kept because
    it explains what this function is FOR, just not why max_trips is 1):
    built directly from real evidence (arb_20260825.log): the SAME cycle
    (LUNC-[Terraport REV/LUNC]->REV-[Terraport REV/USTC]->USTC-[Astroport
    LUNC/USTC]->LUNC) executed 4 separate times across ~85 real seconds,
    each paying its own ~89.5M uluna gas fee — 19.2% of the total profit
    captured across those 4 trades went to redundant gas alone. This
    replaces "wait for the next loop to rediscover the same opportunity"
    with "keep trading it now, in one bundle, while it's still here." That
    benefit is real, but the user has now explicitly chosen "one bigger
    trade" over "several trades in one bundle" as the way to capture it —
    see SINGLE_TRIP_SIZE_MULTIPLIER.

    Stops adding trips when: no more measurable edge, the next trip's
    OWN expected profit can't cover gas_cost_per_trip_start_asset (a flat
    guess — real gas for the whole final bundle still gets one real
    simulate_fee check by the caller before broadcast, same pattern as
    build_split_plan_for_execution), or max_trips is reached (a safety
    cap on how large a single bundled transaction gets, independent of
    any one pool's MAX_POOL_RESERVE_FRACTION — that cap is still enforced
    fresh on every trip via evaluate_cycle -> sizing.max_offer_for_cycle,
    since each trip's local reserves have already moved).

    Returns a list of (trip_amount, gross_profit, leg_amounts, leg_params)
    tuples, in execution order — gross_profit is returned per trip
    (already correctly computed against THAT trip's own local reserve
    snapshot inside this loop) so callers doing a final real-gas re-check
    can sum it directly instead of re-deriving it against the wrong
    (pre-trade) reserves. Empty list if not even the first trip is
    profitable (callers should treat that the same as "nothing to trade").
    """
    trips = []
    local_states = states
    remaining_balance = balance

    for trip_num in range(max_trips):
        amount, gross_profit, _, net_profit, edge_bps = evaluate_cycle(
            cycle, local_states, lunc_price_uusd, remaining_balance, start_asset_price_uusd)
        if amount <= 0:
            break
        if gross_profit - gas_cost_per_trip_start_asset <= 0:
            log.debug("%s: trip %d's own profit (%d) wouldn't cover another trip's gas "
                      "(%d) — stopping the greedy extension here.",
                      graph_module.cycle_label(cycle), trip_num + 1, gross_profit,
                      gas_cost_per_trip_start_asset)
            break

        leg_amounts = graph_module.simulate_cycle_legs(cycle, amount, local_states)
        leg_params = graph_module.compute_leg_execution_params(
            cycle, leg_amounts, local_states, config.SPREAD_TOLERANCE_BPS,
            config.MAX_SPREAD_CEILING_BPS)
        if leg_params is None:
            break

        trips.append((amount, gross_profit, leg_amounts, leg_params))
        local_states = graph_module.apply_cycle_trade_to_states(cycle, amount, local_states)
        # Conservative (never overestimates what's left): amount is what
        # LEFT the wallet this trip, before this trip's own proceeds come
        # back — using the smaller pre-proceeds figure for the next trip's
        # balance-fraction sizing errs toward under-, not over-, sizing.
        remaining_balance = max(0, remaining_balance - amount)

    if len(trips) > 1:
        log.info("%s: greedy extension found %d profitable trip(s) instead of 1 "
                  "(amounts: %s).", graph_module.cycle_label(cycle), len(trips),
                  [t[0] for t in trips])
    return trips


def build_split_plan_for_execution(terra, pools, states, ustc, lunc, cycle, leg_amounts,
                                    gross_profit, start_asset, label,
                                    account_number=None, sequence=None):
    """
    Given a cycle ALREADY APPROVED by _prepare_execution (naive, one-
    message-per-leg checks all passed), builds the split-optimized real
    execution plan (graph.plan_cycle_execution) and re-runs the FULL
    profit decision against it — not just "did simulate_fee succeed" —
    before returning it.

    FIXED 2026-08-23: the original version of this function only checked
    that simulate_fee succeeded (real_gas_uluna > 0), not that profit
    still cleared MIN_PROFIT/MIN_PROFIT_MARGIN_MULTIPLE against the split
    plan's real gas. Confirmed via arb_20260823.log that this was a real,
    live bug: a 4-leg cycle split into a 10-message plan cost 297,480,112
    real uluna in gas versus the 76,218,270 uluna priced into the
    original decision — a 3.9x underestimate — meaning the trade that
    executed had a real net profit of roughly 566,000 uusd, not the
    ~2,886,391 uusd the log reported and _prepare_execution approved it
    on. It happened to still be profitable that time; nothing in the code
    guaranteed that. A leg that gets split into N trips sends N real
    on-chain messages, and gas scales with message count — this MUST be
    checked again with the same rigor as the original decision, not
    assumed to still clear because the naive plan already passed.

    IMPORTANT — what this does NOT do: it never changes leg_amounts
    (the total amount routed through any pool). MAX_POOL_RESERVE_FRACTION,
    already applied upstream via sizing.liquidity_cap_for_cycle, still
    caps the TOTAL exposure per pool exactly as before; this only decides
    how that already-capped total gets divided into on-chain messages to
    extract more of the AMM's real output (see graph.simulate_leg_split's
    docstring for why splitting nets more, real, on-chain value for the
    identical total size) — but that extra value has to be weighed
    against the extra real gas each additional message costs, which is
    exactly what was missing before this fix.

    Returns (flat_plan, real_gas_uluna, real_profit_uusd) if the split
    plan clears the SAME real checks _prepare_execution already required
    of the naive plan (re-priced with the split plan's own real gas),
    else (None, None, None) — caller should fall back to the already-
    approved single-message plan rather than aborting the trade
    entirely, since the naive plan is still valid and already passed
    every safety check on its own (lower, but real and verified) numbers.
    """
    gas_guess_per_trip = int(Decimal(GAS_UNITS_PER_HOP) * config.GAS_PRICE)
    flat_plan = graph_module.plan_cycle_execution(
        cycle, leg_amounts, states, config.SPREAD_TOLERANCE_BPS, config.MAX_SPREAD_CEILING_BPS,
        gas_guess_per_trip, pools, ustc, lunc)
    if flat_plan is None:
        return None, None, None

    from executor import build_leg_msg
    msgs = [build_leg_msg(terra.address, edge, trip_amount, params)
            for edge, trip_amount, params in flat_plan]
    try:
        fee = terra.simulate_fee(msgs, account_number=account_number, sequence=sequence)
        real_gas_uluna = sum(int(c.amount) for c in fee.amount if c.denom == config.GAS_DENOM)
        if real_gas_uluna <= 0:
            log.warning("Split plan simulate_fee returned no real gas amount — falling back "
                        "to the already-approved single-message plan.")
            return None, None, None
    except Exception as e:
        log.warning("Split plan (%d messages) failed real gas simulation (%s) — falling back "
                    "to the already-approved single-message plan.", len(msgs), e)
        return None, None, None

    # Re-run the FULL profit decision against the split plan's own real
    # gas — mirrors _prepare_execution's math exactly (same formula, same
    # two gates), just with real_gas_uluna coming from THIS plan's
    # simulate_fee instead of the naive plan's. gross_profit itself does
    # NOT change with message count (splitting doesn't change leg_amounts,
    # commission, or tax totals — only gas) so it's safe to reuse the
    # value _prepare_execution already computed for this cycle.
    if start_asset.id == config.DENOM_LUNC and start_asset.kind == "native":
        real_gas_in_start_asset = real_gas_uluna
    else:
        real_gas_in_start_asset = int(real_gas_uluna * graph_module.price_in_uusd(
            lunc, pools, states, ustc, lunc))
    real_net_in_start_asset = gross_profit - real_gas_in_start_asset
    real_profit_uusd = int(real_net_in_start_asset * graph_module.price_in_uusd(
        start_asset, pools, states, ustc, lunc))

    flat_floor_passes = clears_min_profit(start_asset, real_net_in_start_asset, real_profit_uusd)

    real_fees_and_tax_uusd = graph_module.simulate_cycle_costs_uusd(
        cycle, leg_amounts[0], states, pools, ustc, lunc)
    real_gas_uusd = Decimal(real_gas_uluna) * graph_module.price_in_uusd(lunc, pools, states, ustc, lunc)
    real_total_cost_uusd = real_fees_and_tax_uusd + real_gas_uusd
    required_margin_uusd = config.MIN_PROFIT_MARGIN_MULTIPLE * real_total_cost_uusd
    margin_passes = Decimal(real_profit_uusd) >= required_margin_uusd

    log.info("Split plan margin re-check for %s: messages=%d real_gas_uluna=%d "
              "(naive plan's gas was priced lower before this re-check) real_profit_uusd=%d "
              "required(%sx)=%s -> %s",
              label, len(flat_plan), real_gas_uluna, real_profit_uusd,
              config.MIN_PROFIT_MARGIN_MULTIPLE, required_margin_uusd,
              "PASS" if (flat_floor_passes and margin_passes) else "FAIL")

    if not flat_floor_passes or not margin_passes:
        log.warning("Split plan for %s no longer clears the profit gate once its OWN real gas "
                    "(%d uluna across %d messages) is priced in — falling back to the "
                    "already-approved single-message plan instead of sending a trade that "
                    "wouldn't itself have been approved.", label, real_gas_uluna, len(flat_plan))
        return None, None, None

    log.info("Split execution plan ready: %d total messages across %d cycle leg(s), "
              "real_gas_uluna=%d, real_profit_uusd=%d (re-verified).",
              len(flat_plan), len(cycle), real_gas_uluna, real_profit_uusd)
    return flat_plan, real_gas_uluna, real_profit_uusd


def build_pools_and_assets(terra):
    """
    Builds every Asset and DexPool/GarudaPool this bot knows about, plus
    the list of assets to root cycle-search from. Factored out of main()
    (2026-08-07) so other scripts — e.g. a tax-per-hop smoke test — can
    get the EXACT same live pool universe main() trades against instead
    of hand-maintaining a second, drifting copy of this list.

    Returns (pools, assets_to_check, lunc, ustc).
    """
    lunc = Asset(kind="native", id=config.DENOM_LUNC, decimals=6, display="LUNC")
    ustc = Asset(kind="native", id=config.DENOM_USTC, decimals=6, display="USTC")
    usdc_axl = Asset(kind="native", id=config.DENOM_USDC_AXL, decimals=6, display="USDC.eth.axl")
    terra_token = Asset(kind="cw20", id=config.TERRA_CW20_ADDRESS,
                         decimals=config.TERRA_DECIMALS, display="TERRA")
    lcw_token = Asset(kind="cw20", id=config.LCW_CW20_ADDRESS,
                       decimals=config.LCW_DECIMALS, display="LCW")
    mir_token = Asset(kind="cw20", id=config.MIR_CW20_ADDRESS,
                       decimals=config.MIR_DECIMALS, display="MIR")
    astro_token = Asset(kind="cw20", id=config.ASTRO_CW20_ADDRESS,
                         decimals=config.ASTRO_DECIMALS, display="ASTRO")
    trit_token = Asset(kind="cw20", id=config.TRIT_CW20_ADDRESS,
                        decimals=config.TRIT_DECIMALS, display="TRIT")
    juris_token = Asset(kind="cw20", id=config.JURIS_CW20_ADDRESS,
                         decimals=config.JURIS_DECIMALS, display="JURIS")
    rev_token = Asset(kind="cw20", id=config.REV_CW20_ADDRESS,
                       decimals=config.REV_DECIMALS, display="REV")
    future_token = Asset(kind="cw20", id=config.FUTURE_CW20_ADDRESS,
                          decimals=config.FUTURE_DECIMALS, display="FUTURE")
    amplunc_token = Asset(kind="cw20", id=config.AMPLUNC_CW20_ADDRESS,
                           decimals=config.AMPLUNC_DECIMALS, display="ampLUNC")
    cwlunc_token = Asset(kind="cw20", id=config.CWLUNC_CW20_ADDRESS,
                          decimals=config.CWLUNC_DECIMALS, display="cwLUNC")
    cwustc_token = Asset(kind="cw20", id=config.CWUSTC_CW20_ADDRESS,
                          decimals=config.CWUSTC_DECIMALS, display="cwUSTC")
    usdc = Asset(kind="native", id=config.DENOM_USDC, decimals=6, display="USDC")
    benance_token = Asset(kind="cw20", id=config.BENANCE_CW20_ADDRESS,
                           decimals=config.BENANCE_DECIMALS, display="BENANCE")
    gdex_token = Asset(kind="cw20", id=config.GDEX_CW20_ADDRESS,
                        decimals=config.GDEX_DECIMALS, display="GDEX")
    grdx_token = Asset(kind="cw20", id=config.GRDX_CW20_ADDRESS,
                        decimals=config.GRDX_DECIMALS, display="GRDX")
    fun_token = Asset(kind="cw20", id=config.FUN_CW20_ADDRESS,
                       decimals=config.FUN_DECIMALS, display="FUN")
    bon_token = Asset(kind="cw20", id=config.BON_CW20_ADDRESS,
                       decimals=config.BON_DECIMALS, display="BON")
    moon_token = Asset(kind="cw20", id=config.MOON_CW20_ADDRESS,
                        decimals=config.MOON_DECIMALS, display="MOON")
    jeff_token = Asset(kind="cw20", id=config.JEFF_CW20_ADDRESS,
                        decimals=config.JEFF_DECIMALS, display="JEFF")
    dfc_token = Asset(kind="cw20", id=config.DFC_CW20_ADDRESS,
                       decimals=config.DFC_DECIMALS, display="DFC")
    lix_token = Asset(kind="cw20", id=config.LIX_CW20_ADDRESS,
                       decimals=config.LIX_DECIMALS, display="LIX")
    ltk_token = Asset(kind="cw20", id=config.LTK_CW20_ADDRESS,
                       decimals=config.LTK_DECIMALS, display="LTK")
    elpaco_token = Asset(kind="cw20", id=config.ELPACO_CW20_ADDRESS,
                          decimals=config.ELPACO_DECIMALS, display="ELPACO")
    rotti_token = Asset(kind="cw20", id=config.ROTTI_CW20_ADDRESS,
                         decimals=config.ROTTI_DECIMALS, display="ROTTI")
    tnews_token = Asset(kind="cw20", id=config.TNEWS_CW20_ADDRESS,
                         decimals=config.TNEWS_DECIMALS, display="TNEWS")
    degenap_token = Asset(kind="cw20", id=config.DEGENAP_CW20_ADDRESS,
                           decimals=config.DEGENAP_DECIMALS, display="DEGENAP")
    idev_token = Asset(kind="cw20", id=config.IDEV_CW20_ADDRESS,
                        decimals=config.IDEV_DECIMALS, display="IDEV")

    pools = [
        DexPool(config.TERRASWAP_POOL_1_NAME, terra, config.TERRASWAP_POOL_1,
                 lunc, ustc, config.TERRASWAP_COMMISSION_RATE),
        DexPool(config.TERRASWAP_POOL_2_NAME, terra, config.TERRASWAP_POOL_2,
                 lunc, ustc, config.TERRASWAP_COMMISSION_RATE),
        DexPool("Terraport TERRA/LUNC", terra, config.TERRAPORT_POOL_TERRA_LUNC,
                 terra_token, lunc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport TERRA/USTC", terra, config.TERRAPORT_POOL_TERRA_USTC,
                 terra_token, ustc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport LCW/LUNC", terra, config.TERRAPORT_POOL_LCW_LUNC,
                 lcw_token, lunc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport LCW/USTC", terra, config.TERRAPORT_POOL_LCW_USTC,
                 lcw_token, ustc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraswap MIR/USTC", terra, config.TERRASWAP_POOL_MIR_USTC,
                 mir_token, ustc, config.TERRASWAP_COMMISSION_RATE),
        DexPool("Astroport MIR/USTC", terra, config.ASTROPORT_POOL_MIR_USTC,
                 mir_token, ustc, config.ASTROPORT_COMMISSION_RATE),
        DexPool("Astroport ASTRO/LUNC", terra, config.ASTROPORT_POOL_ASTRO_LUNC,
                 astro_token, lunc, config.ASTROPORT_COMMISSION_RATE),
        DexPool("Astroport ASTRO/USTC", terra, config.ASTROPORT_POOL_ASTRO_USTC,
                 astro_token, ustc, config.ASTROPORT_COMMISSION_RATE),
        DexPool("Terraswap TRIT/LUNC", terra, config.TERRASWAP_POOL_TRIT_LUNC,
                 trit_token, lunc, config.TERRASWAP_COMMISSION_RATE),
        DexPool("Terraport TRIT/LUNC", terra, config.TERRAPORT_POOL_TRIT_LUNC,
                 trit_token, lunc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraswap TRIT/USTC", terra, config.TERRASWAP_POOL_TRIT_USTC,
                 trit_token, ustc, config.TERRASWAP_COMMISSION_RATE),
        DexPool("Terraport TRIT/USTC", terra, config.TERRAPORT_POOL_TRIT_USTC,
                 trit_token, ustc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport JURIS/LUNC", terra, config.TERRAPORT_POOL_JURIS_LUNC,
                 juris_token, lunc, config.TERRAPORT_COMMISSION_RATE),
        # Garuda JURIS/LUNC — ENABLED 2026-08-06. check_new_venues_
        # interface.py showed PASS, and smoke_test_new_tokens.py's
        # test_juris_lunc_garuda_round_trip confirmed clean the same day:
        # 0.00 bps on LUNC->JURIS, and the ~150bps JURIS->LUNC gap
        # correctly matching the already-modeled native stability tax on
        # the returned LUNC (not a new CW20-side tax) — consistent with
        # the earlier Terraport JURIS/LUNC reading, so JURIS's own tax
        # behavior (0% either direction) holds across both venues.
        #
        # Original 2026-07-14 finding (STALE — schema fixed venue-wide
        # 2026-08-04, see probe_garuda_schema.py): smoke_test_juris.py
        # found Garuda's `pair_base` contract used a non-Terraswap-
        # standard ExecuteMsg schema for the offer asset — a tagged enum
        # with `native`/`cw20` variants directly, not the {info, amount}
        # struct executor.py sent everywhere else at the time. The real
        # swap failed at client-side gas estimation (no funds/gas lost)
        # with: "Error parsing into type pair_base::msg::ExecuteMsg:
        # unknown variant `info`, expected `native` or `cw20`".
        GarudaPool("Garuda JURIS/LUNC", terra, config.GARUDA_POOL_JURIS_LUNC,
                    juris_token, lunc, config.GARUDA_COMMISSION_RATE),
        DexPool("Terraport JURIS/TERRA", terra, config.TERRAPORT_POOL_JURIS_TERRA,
                 juris_token, terra_token, config.TERRAPORT_COMMISSION_RATE),
        # REV/LUNC and REV/USTC — scanned every loop like every other pool
        # now (no more special-cased periodic scan_interval). Previously
        # these were only fed into cycle generation every
        # PERIODIC_POOL_SCAN_INTERVAL loops to save CPU on two thin pools;
        # that special-casing has been removed on purpose so REV gets no
        # more or less attention than any other pool in this list.
        DexPool("Terraport REV/LUNC", terra, config.TERRAPORT_POOL_REV_LUNC,
                 rev_token, lunc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport REV/USTC", terra, config.TERRAPORT_POOL_REV_USTC,
                 rev_token, ustc, config.TERRAPORT_COMMISSION_RATE),
        # BON/LUNC, BON/USTC — ADDED for testing at the user's explicit
        # request (2026-08-28). Not yet independently smoke-tested; treat
        # the first few loops touching these two pools as trust-but-verify,
        # same as REV/TRIT/JURIS were when first added.
        DexPool("Terraport BON/LUNC", terra, config.TERRAPORT_POOL_BON_LUNC,
                 bon_token, lunc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport BON/USTC", terra, config.TERRAPORT_POOL_BON_USTC,
                 bon_token, ustc, config.TERRAPORT_COMMISSION_RATE),
        # PARKED 2026-07-15 (user request) — all four USDC.eth.axl pools.
        # Each smoke-tested clean (real round trips completed 2026-07-15;
        # only the well-understood ~50bps native-output stability tax showed
        # up, no shortfalls beyond that, USDC.eth.axl/TERRA is LOW LIQUIDITY
        # per config.TERRAPORT_POOL_USDCAXL_TERRA), but left out of live
        # sizing for now. Re-enable by uncommenting; no other changes needed.
        # DexPool("Terraport USDC.eth.axl/LUNC", terra, config.TERRAPORT_POOL_USDCAXL_LUNC,
        #          usdc_axl, lunc, config.TERRAPORT_COMMISSION_RATE),
        # DexPool("Terraport USDC.eth.axl/USTC", terra, config.TERRAPORT_POOL_USDCAXL_USTC,
        #          usdc_axl, ustc, config.TERRAPORT_COMMISSION_RATE),
        # DexPool("Terraport USDC.eth.axl/TERRA", terra, config.TERRAPORT_POOL_USDCAXL_TERRA,
        #          usdc_axl, terra_token, config.TERRAPORT_COMMISSION_RATE),
        # DexPool("Terraswap USTC/USDC.eth.axl", terra, config.TERRASWAP_POOL_USTC_USDCAXL,
        #          ustc, usdc_axl, config.TERRASWAP_COMMISSION_RATE),

        # --- Terraport FUTURE/LUNC, FUTURE/TERRA, FUTURE/TRIT — enabled.
        # FUTURE is just a token (project name "Futureflare"); these pools
        # are on Terraport, an already-trusted venue — not a new DEX, so
        # none of the interface-mismatch risk Garuda hit applies here. Not
        # yet independently smoke-tested against this specific token though
        # — treat the first few loops touching these as trust-but-verify,
        # same as REV was when first added. FUTURE/TERRA and FUTURE/TRIT are
        # both CW20/CW20 pairs (no native side), so asset_x/asset_y order
        # doesn't matter for commission resolution the way it does for
        # FUTURE/LUNC (see pool_client.DexPool.__init__'s docstring).
        DexPool("Terraport FUTURE/LUNC", terra, config.FUTUREFLARE_POOL_FUTURE_LUNC,
                 future_token, lunc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport FUTURE/TERRA", terra, config.FUTUREFLARE_POOL_FUTURE_TERRA,
                 future_token, terra_token, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport FUTURE/TRIT", terra, config.FUTUREFLARE_POOL_FUTURE_TRIT,
                 future_token, trit_token, config.TERRAPORT_COMMISSION_RATE),

        # --- White Whale LUNC/USTC — PARKED 2026-08-03 after a REAL LOSS.
        # A live atomic trade through this pool predicted +57,325,426 uluna
        # net profit (after modeled commission, tax, and real chain-simulated
        # gas — comfortably clearing MIN_PROFIT_ULUNA) but the actual on-chain
        # result was -17,128,825 uluna: a ~74.5M uluna (~0.56% of the 13.29B
        # uluna principal) gap between predicted and real. This lines up with
        # an on-chain tax_map query for this pool/token showing a contract-
        # level tax hook (on_send/on_transfer keyed on src_cond/dst_cond
        # being ContractCode vs Always, rate 0.02) that is COMPLETELY SEPARATE
        # from the flat x/treasury stability tax tax.py models — tax.py only
        # ever queries /terra/treasury/v1beta1/tax_rate and has no awareness
        # this per-pool mechanism exists. Curve type (constant_product) and
        # asset pairing were independently confirmed fine beforehand; this is
        # a different, newly-discovered risk class, same shape as the
        # USDC.eth.axl fee-on-input bug — an unmodeled real cost, not a
        # threshold problem. Do NOT re-enable by raising MIN_PROFIT; that
        # would not have prevented this loss since the missing cost isn't in
        # the model at all. Re-enable only once the tax_map mechanism is
        # either correctly modeled in tax.py or confirmed not to apply to
        # legs this bot actually trades.
        # DexPool("White Whale LUNC/USTC", terra, config.WHITEWHALE_POOL_LUNC_USTC,
        #          lunc, ustc, config.WHITEWHALE_COMMISSION_RATE),

        # --- Astroport LUNC/USTC — enabled. NOT part of the original ask;
        # discovered by accident when check_pool_curve_type.py revealed that
        # terra1m6ywlgn6wrjuagcmmezzz2a029gtldhey5k552 (pasted as
        # "ampLUNC/LUNC") is actually a plain native/native LUNC/USTC pool —
        # no CW20 asset involved at all. Confirmed pair_type={"xyk":{}}.
        # Astroport is already a trusted venue (ASTRO/MIR pairs below), and
        # this is the same native/native shape as Terraswap Pool 1/2 — but
        # per pool_client.DexPool.__init__'s docstring, a native/native pair
        # has no CW20 side to self-correct commission via live simulation,
        # so ASTROPORT_COMMISSION_RATE below is a starting default, not a
        # confirmed rate for THIS pool specifically — treat the first few
        # loops as trust-but-verify like any new pool.
        DexPool("Astroport LUNC/USTC", terra, config.ASTROPORT_POOL_LUNC_USTC,
                 lunc, ustc, config.ASTROPORT_COMMISSION_RATE),

        # --- ampLUNC pools on Astroport — enabled. CORRECTED 2026-08-02: the
        # pasted pool list had these two addresses' pairs swapped (see
        # config.py's comment above ASTROPORT_POOL_LUNC_USTC for the full
        # correction) — this is the fixed mapping, verified via each pool's
        # own {"pair":{}} query, both confirmed pair_type={"xyk":{}}.
        DexPool("Astroport ampLUNC/LUNC", terra, config.AMPLUNC_ASTROPORT_POOL_LUNC,
                 amplunc_token, lunc, config.ASTROPORT_COMMISSION_RATE),
        DexPool("Astroport ampLUNC/USTC", terra, config.AMPLUNC_ASTROPORT_POOL_USTC,
                 amplunc_token, ustc, config.ASTROPORT_COMMISSION_RATE),

        # --- ampLUNC pools on Terraswap — enabled. CORRECTED 2026-08-02: 2 of
        # these 3 addresses had their pair labels wrong (see config.py's
        # comment above AMPLUNC_TERRASWAP_POOL_LUNC for the full mapping) —
        # this is the fixed pairing, verified via each pool's actual
        # asset_infos. No pair_type field on any of them, which is expected
        # (not a red flag) — Terraswap predates that Astroport convention and
        # has no stable-pool code path to report regardless.
        DexPool("Terraswap ampLUNC/LUNC", terra, config.AMPLUNC_TERRASWAP_POOL_LUNC,
                 amplunc_token, lunc, config.TERRASWAP_COMMISSION_RATE),
        DexPool("Terraswap ampLUNC/USTC 1", terra, config.AMPLUNC_TERRASWAP_POOL_USTC_1,
                 amplunc_token, ustc, config.TERRASWAP_COMMISSION_RATE),
        DexPool("Terraswap ampLUNC/USTC 2", terra, config.AMPLUNC_TERRASWAP_POOL_USTC_2,
                 amplunc_token, ustc, config.TERRASWAP_COMMISSION_RATE),

        # --- ADDED 2026-08-04: reportedly the route people are using to
        # dodge the stability tax hike — trading in cwLUNC/cwUSTC (CW20
        # wrappers around native LUNC/USTC) instead of the natives
        # directly, since a cw20/cw20 leg carries no chain stability tax
        # at all (see config.py's CWLUNC_CW20_ADDRESS comment and
        # graph.simulate_cycle_costs_uusd's docstring). This one pool is
        # the wrap/unwrap step itself, so it DOES still have a native
        # LUNC leg and IS still taxed normally — Terraswap (already-
        # trusted venue), LOW LIQUIDITY per the supplied address, scanned
        # on PERIODIC_POOL_SCAN_INTERVAL like REV.
        DexPool("Terraswap cwLUNC/LUNC", terra, config.TERRASWAP_POOL_CWLUNC_LUNC,
                 cwlunc_token, lunc, config.TERRASWAP_COMMISSION_RATE,
                 scan_interval=config.PERIODIC_POOL_SCAN_INTERVAL),

        # --- USDC/LUNC (Terraswap) and LUNC/USDC (Terraport) — a NEW
        # native IBC USDC denom (config.DENOM_USDC), NOT the same asset as
        # the existing USDC.eth.axl (config.DENOM_USDC_AXL — different
        # ibc/ hash, do not assume the same tax/liquidity profile). Both
        # venues already trusted for interface; treat first few loops as
        # trust-but-verify like any freshly-added pool.
        DexPool("Terraswap USDC/LUNC", terra, config.TERRASWAP_POOL_USDC_LUNC,
                 usdc, lunc, config.TERRASWAP_COMMISSION_RATE),
        DexPool("Terraport LUNC/USDC", terra, config.TERRAPORT_POOL_LUNC_USDC,
                 usdc, lunc, config.TERRAPORT_COMMISSION_RATE),

        # --- WESO DeFi — UPDATED 2026-08-04 with real check_new_venues_
        # interface.py findings (see config.py's WESO section for the full
        # writeup). Still DISABLED, but for different reasons per pool now:
        #   - The original "cwLUNC/LUNC" address was the WESO swap ROUTER,
        #     not a pair contract — dropped entirely, nothing to enable
        #     here until a real pair address is found.
        #   - The original "cwUSTC/USTC" address is actually cwLUNC/cwUSTC
        #     (both CW20, no native side) — corrected below. Schema
        #     confirmed Terraswap-standard, but it's a pegged pair
        #     (wrapped asset vs. its own underlying) — same StableSwap-
        #     curve risk that hit ampLUNC/LUNC on Astroport. Needs a
        #     curve-type check before enabling, not just a schema pass.
        #   - JURIS/cwLUNC: schema CONFIRMED Terraswap-standard, rejected
        #     only for a too-small probe amount (business logic, not
        #     schema) — the strongest signal any new-venue pool has given
        #     so far, and NOT a pegged pair, so no curve-type concern.
        #     Re-run 2026-08-04 with PROBE_AMOUNT=2,000,000 gave a clean
        #     PASS — enabled below.
        # DexPool("WESO cwLUNC/cwUSTC", terra, config.WESO_POOL_CWLUNC_CWUSTC,
        #          cwlunc_token, cwustc_token, config.WESO_COMMISSION_RATE),
        # --- WESO JURIS/cwLUNC — PARKED 2026-08-29 at the user's request.
        # WESO's factory {"pairs":{}} listing (2026-08-29) shows this pool's
        # real pair_type is "reflective" — not xyk. amm_math.simulate_swap
        # only implements xyk (see its module docstring); every profit/
        # sizing number this bot ever computed for this pool was xyk math
        # applied to a pool that WESO itself classifies as a different,
        # undocumented curve. The pair-level {"pair":{}} query never surfaces
        # pair_type — only the factory listing does — which is why this
        # went unnoticed through the original 2026-08-04 schema check (that
        # confirmed response SHAPE, never curve TYPE). Do not re-enable
        # until "reflective" is understood well enough to confirm this
        # bot's pricing/sizing math actually matches it, the same bar
        # applied to the ampLUNC StableSwap-curve concern elsewhere in this
        # file.
        # DexPool("WESO JURIS/cwLUNC", terra, config.WESO_POOL_JURIS_CWLUNC,
        #          juris_token, cwlunc_token, config.WESO_COMMISSION_RATE,
        #          commission_probe_amount=config.WESO_COMMISSION_PROBE_AMOUNT),

        # WESO cwLUNC/LUNC — investigated 2026-08-29, not addable as a
        # DexPool. See WESO_POOL_CWLUNC_LUNC's history comment in config.py:
        # the supplied address is the cwLUNC CW20 token contract, not a pair;
        # real trades route through WESO_ROUTER_ADDRESS, which needs genuine
        # router integration this codebase doesn't implement.

        # --- LuncSwap.fun JURIS/USDC — ENABLED 2026-08-28. probe_luncswap_
        # schema.py confirmed both {"pair":{}} and {"pool":{}} match
        # Terraswap-standard response shapes (asset_infos / assets list),
        # same shape DexPool.get_state expects. pair_type is absent from
        # the {"pair":{}} response, not "xyk" — per the Terraswap ampLUNC/
        # LUNC comment above, that's expected for a plain Terraswap fork
        # (Astroport invented pair_type; plain Terraswap never had it) and
        # not itself a red flag, especially for a non-pegged pair like this
        # one where a StableSwap curve wouldn't make economic sense anyway.
        # CONFIRMED via arb_20260828.log's real reserves: LuncSwap's
        # implied JURIS/USDC price is within ~1.7% of the price implied by
        # routing JURIS->LUNC->USDC through already-trusted pools — well
        # inside the existing commission+tax stack, i.e. this pool is
        # pricing consistently with the rest of the market, not a stale or
        # broken feed. NOT YET CONFIRMED: the swap ExecuteMsg schema — the
        # probe only covers read-only queries; Garuda's actual failure was
        # at the swap/write path, not the read path, which looked fine
        # there too. executor.build_swap_msg assumes Terraswap-standard
        # ExecuteMsg; that's still an assumption here. Treat the first real
        # trade(s) as trust-but-verify — watch "Realized vs predicted"
        # closely, and consider a DRY_RUN pass or a small forced manual
        # trade first. commission_probe_amount is overridden (see
        # config.LUNCSWAP_COMMISSION_PROBE_AMOUNT's comment) because the
        # module-default probe size is worth a fraction of a cent on this
        # pool and could never produce a resolvable read — with the
        # override, DexPool's live commission-simulation query can now
        # self-correct LUNCSWAP_COMMISSION_RATE's 0.3% guess normally
        # (JURIS is asset_x / the CW20 side here, so the ordering DexPool
        # needs for that to work is already correct).
        DexPool("LuncSwap JURIS/USDC", terra, config.LUNCSWAP_POOL_JURIS_USDC,
                 juris_token, usdc, config.LUNCSWAP_COMMISSION_RATE,
                 commission_probe_amount=config.LUNCSWAP_COMMISSION_PROBE_AMOUNT),

        # --- Three more USDC pools — ENABLED 2026-08-30 after probe_new_
        # usdc_pools_20260830.py confirmed each one's real on-chain shape
        # (raw output kept in that run's log; summary below). Per the
        # trust-but-verify bar every other freshly-added pool here has
        # been held to (REV/TRIT/JEFF/DFC etc.) — schema confirmed, first
        # real trades through these three still want a close look at
        # "Realized vs predicted" before treating them as fully proven.
        #
        #   - Garuda USDC/LUNC: {"pair":{}} returned a 500 — EXPECTED for
        #     a genuine Garuda pair_base contract (see GarudaPool's
        #     docstring: no {"pair":{}} query exists on this contract
        #     family at all). {"pool":{}} returned the real Garuda shape
        #     (asset1/asset2/reserve1/reserve2), asset1=config.DENOM_USDC,
        #     asset2=uluna — matches its "Garuda Defi" label. Native/
        #     native pair, no CW20 side, so — same as every other Garuda
        #     pool — commission_rate is pinned to GARUDA_COMMISSION_RATE
        #     forever (no live self-correction available on this venue).
        GarudaPool("Garuda USDC/LUNC", terra, config.GARUDA_POOL_USDC_LUNC,
                    usdc, lunc, config.GARUDA_COMMISSION_RATE),

        #   - LuncSwap TERRA/USDC: Terraswap-family shape confirmed
        #     ({"pair":{}} asset_infos + {"pool":{}} assets[]), no
        #     pair_type field — same as the already-enabled ampLUNC/
        #     Terraswap pools, expected for this pool family, not a red
        #     flag. CW20 side CONFIRMED 2026-08-30 by the user to be
        #     TERRA_CW20_ADDRESS, native side is config.DENOM_USDC.
        #     terra_token set as asset_x (the CW20 side) so
        #     DexPool's live commission-simulation query can self-correct
        #     LUNCSWAP_COMMISSION_RATE's 0.3% guess, same reasoning as
        #     LuncSwap JURIS/USDC just above.
        DexPool("LuncSwap TERRA/USDC", terra, config.LUNCSWAP_POOL_TERRA_USDC,
                 terra_token, usdc, config.LUNCSWAP_COMMISSION_RATE),

        #   - TERRA/USDC (venue still unnamed): supplied with "dex is
        #     unknown" — this probe does NOT identify which DEX it is,
        #     only that its schema is Terraswap/Astroport-family
        #     (asset_infos + assets[] shape) and that it self-reports
        #     pair_type={"xyk":{}} explicitly, unlike the LuncSwap pool
        #     above — a different fingerprint, so treat this as a
        #     genuinely separate, still-unidentified venue rather than
        #     assuming it's LuncSwap too. Same CW20 (confirmed TERRA) /
        #     native USDC pair. default_commission below
        #     (ASTROPORT_COMMISSION_RATE, 0.3%) is a placeholder same as
        #     every other freshly-added Terraswap-family pool's starting
        #     guess — terra_token as asset_x lets it self-correct via
        #     live simulation the same way, so the exact starting value
        #     matters less here than on a native/native or Garuda pool.
        #     If you learn the actual venue name later, just update this
        #     pool's display name — no functional change needed.
        DexPool("TERRA/USDC (unidentified venue)", terra, config.TERRA_USDC_POOL_UNKNOWN,
                 terra_token, usdc, config.ASTROPORT_COMMISSION_RATE),

        # --- Garuda DeFi: BENANCE/LUNC, BENANCE/JURIS, GDEX/LUNC,
        # GDEX/GRDX, FUN/GDEX — ENABLED 2026-08-04 after probe_garuda_
        # schema.py confirmed the real pair_base message schema (native
        # offer_asset is a bare denom string, not a struct; CW20 Send-hook
        # needs a required min_receive field — see executor.
        # build_swap_msg_garuda's docstring) and check_new_venues_
        # interface.py's Garuda-aware check showed PASS on all 5 real pools
        # using that confirmed builder. Uses GarudaPool (not DexPool) —
        # different {"pool":{}} response shape, no live commission
        # resolution (fixed GARUDA_COMMISSION_RATE), no on-chain pair_type
        # confirmation (curve type trusted from Garuda's own docs, not
        # independently verified — see GarudaPool's class docstring).
        # Treat first several loops as trust-but-verify like any freshly
        # enabled venue, and watch the "Realized vs predicted" log line
        # closely on these specifically, since min_receive (not
        # max_spread) is the only on-chain protection they get.
        GarudaPool("Garuda BENANCE/LUNC", terra, config.GARUDA_POOL_BENANCE_LUNC,
                    benance_token, lunc, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda BENANCE/JURIS", terra, config.GARUDA_POOL_BENANCE_JURIS,
                    benance_token, juris_token, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda GDEX/LUNC", terra, config.GARUDA_POOL_GDEX_LUNC,
                    gdex_token, lunc, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda GDEX/GRDX", terra, config.GARUDA_POOL_GDEX_GRDX,
                    gdex_token, grdx_token, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda FUN/GDEX", terra, config.GARUDA_POOL_FUN_GDEX,
                    fun_token, gdex_token, config.GARUDA_COMMISSION_RATE),

        # --- FUN/LUNC, FUN/JURIS, GRDX/FUN on Garuda — ADDED 2026-08-29,
        # same already-trusted pair_base venue as the 5 Garuda pools
        # above. No per-pool schema probe needed (venue-level, not
        # pool-level, per check_new_venues_interface.py/
        # probe_garuda_schema.py). Not yet independently smoke-tested
        # against these specific pairs; treat first several loops as
        # trust-but-verify, same bar as GDEX/GRDX and GRDX/LUNC were.
        GarudaPool("Garuda FUN/LUNC", terra, config.GARUDA_POOL_FUN_LUNC,
                    fun_token, lunc, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda FUN/JURIS", terra, config.GARUDA_POOL_FUN_JURIS,
                    fun_token, juris_token, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda GRDX/FUN", terra, config.GARUDA_POOL_GRDX_FUN,
                    grdx_token, fun_token, config.GARUDA_COMMISSION_RATE),

        # --- Garuda GRDX/LUNC — ENABLED 2026-08-06. check_new_venues_
        # interface.py showed PASS (schema + reserves, same pair_base
        # shape as the 5 pools above). smoke_test_new_tokens.py confirmed
        # clean across 2 real round trips (2026-08-05, 2026-08-06): 0.00
        # bps on LUNC->GRDX both times, and the ~150bps GRDX->LUNC gap
        # both times correctly identified as Terra Classic's pre-existing
        # native stability tax on the returned LUNC (already priced
        # generically elsewhere), not a GRDX-specific transfer tax — see
        # config.py's CW20_DIRECTIONAL_TAX_BPS comment for why that
        # distinction matters and isn't double-counted here.
        GarudaPool("Garuda GRDX/LUNC", terra, config.GARUDA_POOL_GRDX_LUNC,
                    grdx_token, lunc, config.GARUDA_COMMISSION_RATE),

        # --- Garuda MOON/TERRA — ADDED 2026-08-29, already-trusted venue
        # (same pair_base schema as the 6 Garuda pools above). Not yet
        # independently smoke-tested against these specific tokens; treat
        # first several loops as trust-but-verify, same as GDEX/GRDX was.
        GarudaPool("Garuda MOON/TERRA", terra, config.GARUDA_POOL_MOON_TERRA,
                    moon_token, terra_token, config.GARUDA_COMMISSION_RATE),

        # --- ADDED 2026-08-29, probed via probe_new_pools_20260829.py.
        # {"pair":{}} 500'd on all four of these (expected — Garuda pools
        # never implement it, matches every other Garuda pool here);
        # {"pool":{}} matched the asset1/asset2/reserve1/reserve2 shape
        # cleanly on all four. Cross-checked implied prices against
        # independent routes before attaching, same bar as MOON:
        #   JURIS/GRDX: 322.4 direct vs 349.2 via JURIS/LUNC+GRDX/LUNC
        #     bridge (7.7% apart — plausible thin-market spread, not a
        #     decimals mismatch, which would show up as ~100x, not 8%).
        #   JEFF/LUNC "Unknown": 0.01814 direct vs 0.01797 via the
        #     independently-confirmed Terraport JEFF/LUNC pool below
        #     (<1% apart — strong agreement).
        #   JEFF/USDC "Unknown": matches within <1% of the JEFF/LUNC-then-
        #     LUNC/USDC bridge too.
        # JEFF/LUNC and JEFF/USDC were supplied as "Unknown pool" (venue
        # unconfirmed) but their {"pool":{}} response is byte-for-byte the
        # same shape Garuda's pair_base contract returns — either the same
        # venue under a UI the user didn't recognize, or a direct fork of
        # that contract. Either way GarudaPool reads them correctly. What's
        # NOT confirmed: the swap ExecuteMsg schema, since these aren't
        # confirmed-Garuda and no real transaction has been seen through
        # either of them (unlike the WESO JURIS/cwLUNC real-tx evidence).
        # If the write schema differs, the existing simulate-before-
        # broadcast safety net should catch a bad message before any funds
        # move — but treat any first real trade here as maximally
        # trust-but-verify, more so than the already-trusted-venue pools.
        # JEFF/USDC's reserves are also tiny (~$8.6 one side per the probe)
        # — correctly priced, but too thin to matter much at any real size;
        # kept in mainly because it's harmless and gives assets_to_check
        # another consistency point for JEFF.
        GarudaPool("Garuda JURIS/GRDX", terra, config.GARUDA_POOL_JURIS_GRDX,
                    juris_token, grdx_token, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda JEFF/LUNC", terra, config.JEFF_POOL_LUNC_UNKNOWN,
                    jeff_token, lunc, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda JEFF/USDC", terra, config.JEFF_POOL_USDC_UNKNOWN,
                    jeff_token, usdc, config.GARUDA_COMMISSION_RATE),

        # LUNC/DFC — three addresses supplied, only two attached here.
        # Garuda and Terraswap both confirmed clean (schema + 3-way price
        # cross-check below); the third ("Unknown", reportedly ~$12k LP)
        # is held back — see the comment further down in the Terraswap/
        # Terraport pool section for why.
        GarudaPool("Garuda LUNC/DFC", terra, config.GARUDA_POOL_LUNC_DFC,
                    dfc_token, lunc, config.GARUDA_COMMISSION_RATE),

        # --- GDEX/GRDX on Terraport — enabled (already-trusted venue). A
        # CW20/CW20 pair like JURIS/TERRA, so no asset_x-ordering concern.
        # Not yet independently smoke-tested against these specific
        # tokens; treat first few loops as trust-but-verify.
        DexPool("Terraport GDEX/GRDX", terra, config.TERRAPORT_POOL_GDEX_GRDX,
                 gdex_token, grdx_token, config.TERRAPORT_COMMISSION_RATE),

        # --- MOON/LUNC on Terraport — ADDED 2026-08-29, already-trusted
        # venue. CW20/native pair — asset_x is moon_token (the CW20 side),
        # matching DexPool.__init__'s ordering rule so live commission
        # resolution can self-correct normally instead of being pinned to
        # the TERRAPORT_COMMISSION_RATE default forever. Not yet
        # independently smoke-tested; treat first few loops as
        # trust-but-verify.
        DexPool("Terraport MOON/LUNC", terra, config.TERRAPORT_POOL_MOON_LUNC,
                 moon_token, lunc, config.TERRAPORT_COMMISSION_RATE),

        # --- FUN/LUNC on Terraport — ADDED 2026-08-29, already-trusted
        # venue. fun_token as asset_x (CW20 side) so commission
        # self-corrects instead of being pinned to the default forever.
        # Not yet independently smoke-tested; treat first several loops
        # as trust-but-verify.
        DexPool("Terraport FUN/LUNC", terra, config.TERRAPORT_POOL_FUN_LUNC,
                 fun_token, lunc, config.TERRAPORT_COMMISSION_RATE),

        # --- ADDED 2026-08-29, probed via probe_new_pools_20260829.py.
        # Both matched Terraswap-standard shape cleanly; both confirmed
        # by cross-checking price against the Garuda-labeled "Unknown"
        # JEFF/LUNC pool above (<1% apart) and against USDC via bridge.
        # jeff_token as asset_x (CW20 side) so commission self-corrects.
        DexPool("Terraport JEFF/LUNC", terra, config.TERRAPORT_POOL_JEFF_LUNC,
                 jeff_token, lunc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport JEFF/USTC", terra, config.TERRAPORT_POOL_JEFF_USTC,
                 jeff_token, ustc, config.TERRAPORT_COMMISSION_RATE),

        # --- LUNC/DFC on Terraswap — ADDED 2026-08-29. Matched Terraswap-
        # standard shape cleanly; price agrees with the Garuda LUNC/DFC
        # pool above within 0.1% (66.55 vs 66.62 LUNC/DFC) — best cross-
        # check of anything added this session.
        DexPool("Terraswap LUNC/DFC", terra, config.TERRASWAP_POOL_LUNC_DFC,
                 dfc_token, lunc, config.TERRASWAP_COMMISSION_RATE),

        # --- LUNC/DFC "Unknown, ~$12k LP" — INVESTIGATED 2026-08-29, NOT
        # ADDED. This is the deepest of the three DFC pools (reserves
        # confirm ~$12.8k total, matching the user's own estimate closely)
        # and its {"pool":{}} response DOES match the Terraswap-standard
        # {"assets":[...]} shape DexPool.get_state expects — but the same
        # {"pair":{}} response also included two fields no other pool in
        # this file has ever returned: "fee_rate": 3300 and a
        # "lp_stake_contract" address. That strongly suggests this is NOT
        # plain Terraswap/Terraport code, but some other fork with its own
        # (unknown) fee model and LP-staking mechanic layered on top —
        # same category of concern as WESO JURIS/cwLUNC's undocumented
        # "reflective" pair_type, just discovered from a different signal
        # (extra response fields instead of a factory pair_type listing).
        # Forcing this through a plain DexPool would apply
        # TERRASWAP_COMMISSION_RATE's flat xyk fee model, which may not
        # match what this contract actually charges, and its swap
        # ExecuteMsg format is unconfirmed. Not urgent to resolve — the
        # Garuda and Terraswap LUNC/DFC pools above already give this bot
        # two independently-confirmed, cross-consistent ways to trade this
        # pair. Revisit only if those two turn out insufficient and this
        # pool's real contract type gets identified first.
        # DexPool("LUNC/DFC (unknown DEX)", terra, config.LUNC_DFC_POOL_UNKNOWN,
        #          dfc_token, lunc, config.TERRASWAP_COMMISSION_RATE),

        # --- LIX, LTK, ELPACO, ROTTI — ADDED 2026-09-02, 14 pools across
        # LuncSwap.fun, Garuda DeFi, Terraswap, and Terraport supplied by
        # the user in one batch. Venues are all already-trusted (same
        # LuncSwap/Garuda/Terraswap/Terraport code paths every other pool
        # in this list uses).
        #
        # SMOKE-TESTED 2026-09-02/03 via smoke_test_lix_ltk_elpaco_rotti.py
        # and smoke_test_luncswap_lunc_lix.py — see config.py's comments
        # above LIX_TRANSFER_TAX_IN_BPS / LTK_TRANSFER_TAX_IN_BPS /
        # ELPACO_TRANSFER_TAX_BPS / ROTTI_TRANSFER_TAX_BPS for the
        # confirmed tax numbers. 13 of these 14 pools completed a full
        # real round trip cleanly. The other 1:
        #   - Garuda USDC/LTK: PARKED below — real on-chain error ("Invalid
        #     fee amount" at gas estimation, status 500), not a tax
        #     finding. Something about this specific pool/pair breaks fee
        #     estimation in terra_classic_sdk; needs its own investigation
        #     before re-enabling. USTC/LTK (Garuda) confirmed clean and
        #     covers the same "price LTK against a stablecoin" role.
        # (LuncSwap LUNC/LIX initially missed a confirmed run 2026-09-02 —
        # transient DNS error, no transaction broadcast — but got a clean
        # real round trip 2026-09-03: LUNC->LIX showed exactly 200.00bps,
        # a THIRD independent confirmation of LIX's buy tax across all 3
        # venues it trades on, and LIX->LUNC showed only the usual native
        # stability tax. Fully confirmed now, same bar as the other 13.)
        # Decimals assumed 6 (unconfirmed, but this is a Terra Classic-wide
        # constant so far — no evidence to doubt it).
        #
        # DexPool asset_x ordering: for the three native-paired pools below
        # (LuncSwap LUNC/LIX, Terraswap LUNC/LTK, Terraport ROTTI/LUNC) the
        # CW20 side is asset_x, matching DexPool.__init__'s ordering rule
        # so live commission resolution can self-correct instead of being
        # pinned to the default guess forever (see that docstring).
        DexPool("LuncSwap LUNC/LIX", terra, config.LUNCSWAP_POOL_LUNC_LIX,
                 lix_token, lunc, config.LUNCSWAP_COMMISSION_RATE),
        GarudaPool("Garuda LUNC/LIX", terra, config.GARUDA_POOL_LUNC_LIX,
                    lunc, lix_token, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda LTK/LIX", terra, config.GARUDA_POOL_LTK_LIX,
                    ltk_token, lix_token, config.GARUDA_COMMISSION_RATE),
        DexPool("Terraswap LUNC/LTK", terra, config.TERRASWAP_POOL_LUNC_LTK,
                 ltk_token, lunc, config.TERRASWAP_COMMISSION_RATE),
        GarudaPool("Garuda LUNC/LTK", terra, config.GARUDA_POOL_LUNC_LTK,
                    lunc, ltk_token, config.GARUDA_COMMISSION_RATE),
        # PARKED 2026-09-02 — real on-chain error during smoke testing
        # ("Invalid fee amount"; execute wasm contract failed at gas
        # estimation, HTTP 500), not a tax/venue-mechanics finding. Needs
        # its own investigation (possibly a fee-denom or precision issue
        # specific to this pair) before re-enabling. See the block comment
        # above for the confirmed alternative (USTC/LTK).
        # GarudaPool("Garuda USDC/LTK", terra, config.GARUDA_POOL_USDC_LTK,
        #             usdc, ltk_token, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda USTC/LTK", terra, config.GARUDA_POOL_USTC_LTK,
                    ustc, ltk_token, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda LTK/ELPACO", terra, config.GARUDA_POOL_LTK_ELPACO,
                    ltk_token, elpaco_token, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda LUNC/ELPACO", terra, config.GARUDA_POOL_LUNC_ELPACO,
                    lunc, elpaco_token, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda ROTTI/LUNC", terra, config.GARUDA_POOL_ROTTI_LUNC,
                    rotti_token, lunc, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda FUN/ROTTI", terra, config.GARUDA_POOL_FUN_ROTTI,
                    fun_token, rotti_token, config.GARUDA_COMMISSION_RATE),
        DexPool("Terraport ROTTI/LUNC", terra, config.TERRAPORT_POOL_ROTTI_LUNC,
                 rotti_token, lunc, config.TERRAPORT_COMMISSION_RATE),
        GarudaPool("Garuda ROTTI/JURIS", terra, config.GARUDA_POOL_ROTTI_JURIS,
                    rotti_token, juris_token, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda ROTTI/GRDX", terra, config.GARUDA_POOL_ROTTI_GRDX,
                    rotti_token, grdx_token, config.GARUDA_COMMISSION_RATE),

        # --- TNEWS, DEGENAP, IDEV — ADDED 2026-09-03, another user-supplied
        # batch. DFC/LUNC "no dex visible" from this same batch was NOT a
        # new pool — same address already on file as LUNC_DFC_POOL_UNKNOWN
        # (see that parked entry above); RE-CONFIRMED PARKED 2026-09-03 via
        # smoke_test_tnews_degenap_idev_dfc_terrapump.py's probe — same
        # fee_rate=3300/lp_stake_contract anomaly as the original 2026-08-29
        # finding, unchanged. Venues here are Terraport and Garuda DeFi
        # (both already-trusted) except one: Terra.pump — see the PARKED
        # entry below, still not wired in.
        #
        # SMOKE-TESTED 2026-09-03 — see config.py's comments above
        # DEGENAP_TRANSFER_TAX_BPS / TNEWS_TRANSFER_TAX_BPS /
        # IDEV_TRANSFER_TAX_BPS for the confirmed numbers (all three: 0bps,
        # confirmed via 2-4 independent legs each). 11 of these 12 pools
        # completed a full real round trip cleanly. The other 1:
        #   - Garuda USTC/DEGENAP: NOT confirmed — its min_receive
        #     computation failed client-side before broadcasting anything
        #     (aborted safely, no funds at risk, no negative evidence about
        #     the pool itself). DEGENAP's own tax is still solidly
        #     confirmed via its other 3 legs (all through Terraport). Re-run
        #     the smoke test to get a real confirmation on this specific
        #     pool before sizing trades through it.
        DexPool("Terraport DEGENAP/USTC", terra, config.TERRAPORT_POOL_DEGENAP_USTC,
                 degenap_token, ustc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport DEGENAP/LUNC", terra, config.TERRAPORT_POOL_DEGENAP_LUNC,
                 degenap_token, lunc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport DEGENAP/GRDX", terra, config.TERRAPORT_POOL_DEGENAP_GRDX,
                 degenap_token, grdx_token, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport DEGENAP/TNEWS", terra, config.TERRAPORT_POOL_DEGENAP_TNEWS,
                 degenap_token, tnews_token, config.TERRAPORT_COMMISSION_RATE),
        GarudaPool("Garuda USTC/DEGENAP", terra, config.GARUDA_POOL_USTC_DEGENAP,
                    ustc, degenap_token, config.GARUDA_COMMISSION_RATE),
        DexPool("Terraport TNEWS/LUNC", terra, config.TERRAPORT_POOL_TNEWS_LUNC,
                 tnews_token, lunc, config.TERRAPORT_COMMISSION_RATE),
        GarudaPool("Garuda ELPACO/IDEV", terra, config.GARUDA_POOL_ELPACO_IDEV,
                    elpaco_token, idev_token, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda ELPACO/USTC", terra, config.GARUDA_POOL_ELPACO_USTC,
                    elpaco_token, ustc, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda ELPACO/JURIS", terra, config.GARUDA_POOL_ELPACO_JURIS,
                    elpaco_token, juris_token, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda ELPACO/GRDX", terra, config.GARUDA_POOL_ELPACO_GRDX,
                    elpaco_token, grdx_token, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda IDEV/LUNC", terra, config.GARUDA_POOL_IDEV_LUNC,
                    idev_token, lunc, config.GARUDA_COMMISSION_RATE),
        GarudaPool("Garuda IDEV/LTK", terra, config.GARUDA_POOL_IDEV_LTK,
                    idev_token, ltk_token, config.GARUDA_COMMISSION_RATE),

        # PARKED 2026-09-03 — Terra.pump TNEWS/LUNC. This venue has never
        # been used anywhere in this bot before (zero prior references).
        # PROBED 2026-09-03 via smoke_test_tnews_degenap_idev_dfc_terrapump.py
        # — a DIFFERENT failure signal than LUNC_DFC_POOL_UNKNOWN's "extra
        # fields" case: both {"pair":{}} and {"pool":{}} queries returned a
        # flat HTTP 500 Internal Server Error from the LCD, not a malformed-
        # but-parseable response. That could mean the contract genuinely
        # doesn't implement either standard query (a pump.fun-style bonding-
        # curve contract plausibly uses entirely different query names —
        # e.g. something like {"get_pool_info":{}} — rather than the
        # Terraswap-standard {"pair":{}}/{"pool":{}}), or it could be a
        # transient/unrelated LCD indexing issue on this specific contract.
        # Re-probing (ideally against a second LCD endpoint, or checking a
        # block explorer for this address's actual accepted query messages)
        # would distinguish the two — a plain schema-probe retry won't, if
        # it's genuinely unsupported rather than transient. Not urgent —
        # TNEWS/LUNC already has a confirmed-trusted route via Terraport
        # (TERRAPORT_POOL_TNEWS_LUNC above).
        # DexPool("Terra.pump TNEWS/LUNC", terra, config.TERRAPUMP_POOL_TNEWS_LUNC,
        #          tnews_token, lunc, config.TERRASWAP_COMMISSION_RATE),

    ]

    assets_to_check = [lunc, ustc, terra_token, lcw_token, mir_token, astro_token, trit_token,
                        juris_token, usdc_axl, usdc, rev_token, future_token, amplunc_token,
                        bon_token, moon_token, jeff_token, dfc_token, lix_token, ltk_token,
                        elpaco_token, rotti_token, tnews_token, degenap_token, idev_token]
    return pools, assets_to_check, lunc, ustc


def main():
    config.validate()
    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)
    log.info("DRY_RUN=%s", config.DRY_RUN)
    log.info("Dynamic sizing: %.0f%%-%.0f%% of live balance, scaled by intrinsic edge "
              "(%d-%d bps)", config.BALANCE_FRACTION_MIN * 100, config.BALANCE_FRACTION_MAX * 100,
              config.EDGE_LOW_BPS, config.EDGE_HIGH_BPS)

    pools, assets_to_check, lunc, ustc = build_pools_and_assets(terra)
    edges = graph_module.build_edges(pools)
    force_trade_event = threading.Event()
    threading.Thread(target=force_trade_listener, args=(force_trade_event,), daemon=True).start()
    threading.Thread(target=commission_refresh_loop, args=(pools,), daemon=True).start()

    last_good_states = {}
    last_good_balances = {}
    loop_counter = 0
    while True:
        try:
            run_once(terra, pools, edges, assets_to_check, lunc, ustc, force_trade_event,
                     last_good_states, last_good_balances, loop_counter)
        except Exception as e:
            log.exception("Error in loop iteration: %s", e)
        loop_counter += 1
        time.sleep(config.POLL_INTERVAL_SECONDS)


def run_once(terra, pools, edges, assets_to_check, lunc: Asset, ustc: Asset,
             force_trade_event: threading.Event = None, last_good_states: dict = None,
             last_good_balances: dict = None, loop_counter: int = 0):
    # Fetch pool states CONCURRENTLY rather than one at a time — serial
    # fetching across ~20 pools was the dominant source of loop latency
    # (observed 70-90+ seconds some nights). pool_client.py's queries now
    # go through a raw, pooled requests.Session (see _query_contract_raw)
    # instead of the SDK's shared async client, after concurrent SDK calls
    # produced real "cannot enter context" asyncio failures.
    #
    # Concurrency is capped at 8, not one-worker-per-pool: firing ~20
    # simultaneous fresh-ish connections at a public LCD node produced
    # real SSL handshake timeouts under that much load (2026-07-31) even
    # with a connection pool in place. 8 is a starting point, not a
    # measured optimum — raise it if this proves stable, lower it if
    # timeouts recur.
    #
    # Failure isolation: a single pool's query failing (timeout, 5xx,
    # anything) no longer aborts the whole loop iteration the way it did
    # before — that turned one slow pool into a total loss of every
    # candidate for that entire loop. Each pool falls back to its last
    # successfully-fetched state if we have one (stale by one loop, which
    # is far better than a crashed iteration with nothing), or is simply
    # left out of this loop's candidates if we've never fetched it yet.
    if last_good_states is None:
        last_good_states = {}
    if last_good_balances is None:
        last_good_balances = {}

    states = {}

    def _safe_get_state(p):
        return p.get_state()

    # Block height is diagnostic-only (see terra_client.get_latest_block_height's
    # docstring) but used to sit fully AFTER the pool-fetch batch, paying its
    # own round trip on the critical path for no decision-relevant reason.
    # It's an SDK call (terra.lcd.tendermint), but pool queries below no
    # longer touch the SDK's shared async client at all (they're raw
    # requests via pool_client._query_contract_raw) — so running this one
    # SDK call on its own thread alongside the raw-REST pool fetch is safe
    # and just hides its latency behind the (usually slower) pool batch
    # instead of adding to it.
    block_height_result = {}

    def _fetch_block_height():
        try:
            block_height_result["height"] = terra.get_latest_block_height()
        except Exception as e:
            log.warning("Could not fetch block height this loop (%s) — logging -1.", e)
            block_height_result["height"] = -1

    block_height_thread = threading.Thread(target=_fetch_block_height)
    block_height_thread.start()

    # max_workers was capped at 8 back when this bot had ~20 pools (real SSL
    # handshake timeouts were observed at fully-unbounded concurrency against
    # a public LCD node — see this ThreadPoolExecutor's original comment
    # above). Raised to 12 on 2026-08-07 after pool count grew to ~39.
    # Raised again to 16 on 2026-08-29 after pool count reached 50 — at 12,
    # a full fetch needed ceil(50/12)=5 sequential batches, measured at
    # ~16.3s average loop time (arb_20260829.log, 26 loops) vs ~12-14s at
    # 43 pools/4 batches. ceil(50/16)=4 batches, matching the earlier,
    # faster cadence. 16 is, like 12 before it, a reasoned guess and NOT
    # re-verified against this specific LCD node's real concurrency limit —
    # if SSL/timeout errors start showing up in the logs (they did once
    # before, at unbounded concurrency), lower this back down before
    # raising anything else. Check the next several logs' error/warning
    # counts specifically for this before trusting it long-term.
    with ThreadPoolExecutor(max_workers=min(len(pools), 16)) as pool_executor:
        future_to_pool = {pool_executor.submit(_safe_get_state, p): p for p in pools}
        for future in as_completed(future_to_pool):
            p = future_to_pool[future]
            try:
                states[id(p)] = future.result()
                last_good_states[id(p)] = states[id(p)]
            except Exception as e:
                if id(p) in last_good_states:
                    log.warning("%s: query failed this loop (%s) — using last known state "
                                "instead of aborting the whole loop.", p.name, e)
                    states[id(p)] = last_good_states[id(p)]
                else:
                    log.warning("%s: query failed this loop (%s) and no previous state is "
                                "cached yet — excluding it from this loop's candidates.",
                                p.name, e)

    if len(states) < len(pools):
        missing = [p.name for p in pools if id(p) not in states]
        log.warning("Proceeding with %d/%d pools this loop — missing/excluded: %s",
                    len(states), len(pools), missing)
        pools = [p for p in pools if id(p) in states]
        edges = graph_module.build_edges(pools)

    block_height_thread.join()
    block_height = block_height_result.get("height", -1)
    log.info("Pool reserves this loop (block=%d): %s", block_height,
              {p.name: states[id(p)].reserves for p in pools})
    lunc_price_uusd = graph_module.price_in_uusd(lunc, pools, states, ustc, lunc)

    # Pools with scan_interval > 1 (currently just Terraswap cwLUNC/LUNC —
    # REV/LUNC and REV/USTC used to be here too but are no longer
    # special-cased; see their DexPool(...) construction in main()) are
    # still fetched and kept fully tradeable above, but only feed into
    # cycle GENERATION on loops where loop_counter is a multiple of their
    # interval. This is
    # what actually removes their CPU cost (sizing.spread_cap_for_cycle's
    # repeated binary search — confirmed from a real log at ~1s/loop) on
    # the loops they're skipped: no cycle can include an edge that was
    # never built, so evaluate_cycle never sees them at all on those
    # loops. `pools`/`edges` used for THIS loop's scan are a filtered
    # copy — the caller's original lists (used for next loop's fetch)
    # are untouched.
    scan_pools = [p for p in pools if loop_counter % p.scan_interval == 0]
    if len(scan_pools) < len(pools):
        skipped = [p.name for p in pools if p not in scan_pools]
        log.debug("Loop %d: skipping periodic-scan pools this loop (scanned every "
                  "%s loops instead of every loop): %s", loop_counter,
                  {p.scan_interval for p in pools if p.name in skipped}, skipped)
    scan_edges = graph_module.build_edges(scan_pools)

    # Live balances, once per loop, for every asset we might start a cycle
    # from. Now parallelized the same way pool states are: TerraClient's
    # SDK-wrapped get_balance/get_cw20_balance share the SDK's single
    # async event loop and are NOT safe to call from multiple threads at
    # once (same "cannot enter context" failure mode noted throughout
    # this file) — but pool_client.get_asset_balance_raw is a plain
    # `requests` call through the same connection-pooled Session pool
    # queries already use, with no shared async state, so it's safe to
    # fan out across threads. Same failure-isolation pattern as pool
    # states: a single asset's balance query failing falls back to its
    # last-known balance (or 0, with a warning, if never fetched) instead
    # of aborting the loop.
    # Natives (LUNC, USTC, USDC.eth.axl) come back in ONE round trip via
    # the bank module's list-all-balances endpoint instead of one by_denom
    # call each. CW20 balances have no such batch query on this chain, so
    # those still go out in parallel the way they did before this split —
    # this just removes the 2 extra redundant native round trips.
    native_assets = [a for a in assets_to_check if a.kind == "native"]
    cw20_assets = [a for a in assets_to_check if a.kind != "native"]

    balances = {}
    try:
        all_native = get_all_native_balances_raw(terra.address)
        for a in native_assets:
            bal = all_native.get(a.id, 0)
            balances[a.key()] = bal
            last_good_balances[a.key()] = bal
    except Exception as e:
        log.warning("Batched native balance query failed this loop (%s) — falling back to "
                    "last known balances for %s.", e, [str(a) for a in native_assets])
        for a in native_assets:
            balances[a.key()] = last_good_balances.get(a.key(), 0)

    def _safe_get_balance(asset):
        return get_asset_balance_raw(terra.address, asset)

    with ThreadPoolExecutor(max_workers=min(len(cw20_assets), 12) or 1) as bal_executor:
        future_to_asset = {bal_executor.submit(_safe_get_balance, a): a for a in cw20_assets}
        for future in as_completed(future_to_asset):
            a = future_to_asset[future]
            try:
                balances[a.key()] = future.result()
                last_good_balances[a.key()] = balances[a.key()]
            except Exception as e:
                if a.key() in last_good_balances:
                    log.warning("%s: balance query failed this loop (%s) — using last known "
                                "balance instead of excluding it from candidate sizing.",
                                a, e)
                    balances[a.key()] = last_good_balances[a.key()]
                else:
                    log.warning("%s: balance query failed this loop (%s) and no previous "
                                "balance is cached yet — treating as 0 for this loop.", a, e)
                    balances[a.key()] = 0

    # Fetched ONCE per loop and reused for every simulate_fee() call below
    # (candidate fallback loop + forced-test-trade path) instead of paying
    # for the SDK's own implicit per-call account lookup each time — see
    # TerraClient.get_account_number_and_sequence's docstring. Safe within
    # one run_once because nothing here actually broadcasts (sequence
    # can't change) until AFTER this function picks a winner and returns.
    # Defensive: if the installed SDK's Wallet API doesn't expose this
    # method the way assumed, fall back to None/None — real_gas_cost_uluna
    # then just lets simulate_fee() do its own per-call lookup, exactly as
    # it did before this optimization (slower, but correct).
    try:
        account_number, sequence = terra.get_account_number_and_sequence()
    except Exception as e:
        log.warning("Could not fetch account_number/sequence once for this loop (%s) — "
                    "simulate_fee calls below will each do their own lookup instead "
                    "(slower, not incorrect).", e)
        account_number, sequence = None, None

    candidates = []
    # TIMING — ADDED 2026-08-24: confirmed via arb_20260824.log that the
    # gap between the "Pool reserves this loop" snapshot and the first
    # real "Cost breakdown" log was 22 SECONDS for a single loop with
    # exactly 2 real-checked candidates — meaning almost none of that time
    # was the real simulate_fee/margin-check work; it was spent somewhere
    # in cycle-finding or the per-cycle sizing search below. This is the
    # actual, RECURRING cost that matters for reacting before another
    # trader does (the pool-fetch phase above is mostly a one-time,
    # already-parallelized cold-start cost — see run_once's docstring
    # comments on ThreadPoolExecutor). Rather than guess which sub-phase
    # dominates, this times each one explicitly so the next log answers
    # it directly instead of needing another round of speculation.
    _t_candidates_start = time.time()
    _t_find_cycles_total = 0.0
    _t_evaluate_cycle_total = 0.0
    _cycles_examined = 0
    _cycles_zero_edge = 0
    for asset in assets_to_check:
        balance = balances[asset.key()]
        price = graph_module.price_in_uusd(asset, scan_pools, states, ustc, lunc)
        _t0 = time.time()
        # CHANGED 2026-08-25 (reverted, same day): was briefly capped at
        # max_hops=2 to stop the bot bundling 3-4 legs into one high-gas
        # tx on trades that were too small to justify it. Turns out
        # hop-count wasn't the actual problem — trade SIZE was (fixed
        # confirmed via arb_20260825.log: BALANCE_FRACTION_MIN=0.15 above
        # already fixes that independently, by sizing every trade, 2-hop
        # or not, to at least 15% of live balance instead of 2%). Capping
        # hops just removed the multi-hop routes that were finding real
        # edge, and left only two thin, near-duplicate LUNC/USTC 2-hop
        # cycles: one (USTC-Pool1->LUNC-Astroport->USTC) that "passed" the
        # cheap flat-guess check every single loop but then failed the
        # REAL margin check every single loop too (profit ~1.0-1.2M uusd
        # vs required ~2.2M once real gas+tax were counted) — so nothing
        # ever executed — and another (LUNC-Pool2->USTC-Pool1->LUNC) that
        # sized itself to ~6.7 BILLION uluna (half the wallet) for barely
        # 14-25M uluna of net profit, since that "edge" was measured on a
        # tiny probe and mostly evaporates to slippage at real size against
        # two pools that are really the same shallow liquidity twice over.
        # Restoring max_hops=4 lets the bot see the 3-4 hop routes again
        # (worth using WHEN they clear real profit — the flat-guess ranking
        # + real margin check below already reject any cycle, at any hop
        # count, that doesn't) while the larger BALANCE_FRACTION_MIN keeps
        # per-trade gas from dominating a small trade the way it used to.
        cycles = graph_module.find_cycles(scan_edges, asset, max_hops=4)
        _t_find_cycles_total += time.time() - _t0
        for cycle in cycles:
            _cycles_examined += 1
            _t1 = time.time()
            amount, gross, gas_cost, net_in_start_asset, probe_edge_bps = evaluate_cycle(
                cycle, states, lunc_price_uusd, balance, price)
            _t_evaluate_cycle_total += time.time() - _t1

            # RATE-LIMITED 2026-08-31: this used to log.debug() unconditionally for
            # EVERY (asset, cycle) pair examined — confirmed via Railway hitting its
            # 500 logs/sec cap and dropping 7401 messages in one window
            # (logs_1788214501401_log.txt). The overwhelming majority of cycles have
            # probe_edge_bps=0.00 (no measurable edge at all on the probe — dead pool
            # pairs or routes with nothing to say) and were being logged one-for-one
            # anyway, at a volume of hundreds to low thousands of near-identical lines
            # per single discovery loop. Only the cycles with a MEASURABLE probe edge
            # are actually informative for debugging (why didn't this one convert to a
            # sized offer / clear the profit floor?) — the zero-edge majority is only
            # useful in aggregate, which the existing "Timing: cycle discovery+sizing"
            # summary line below now reports via _cycles_zero_edge instead of one DEBUG
            # line per cycle.
            if probe_edge_bps > 0:
                log.debug("Checked %s: probe_edge_bps=%.2f sized_offer=%d net_in_start_asset=%d "
                          "(needs > %d bps buffer + gas + %d min, before this counts as profitable)",
                          graph_module.cycle_label(cycle), probe_edge_bps, amount, net_in_start_asset,
                          config.SLIPPAGE_BUFFER_BPS, config.MIN_PROFIT_UUSD)
            else:
                _cycles_zero_edge += 1

            if amount <= 0:
                continue
            profit_uusd = int(net_in_start_asset * price)
            passes = clears_min_profit(asset, net_in_start_asset, profit_uusd)
            # A cycle whose REAL check just failed (not just this cheap
            # flat-guess floor) doesn't get to monopolize the ranking again
            # immediately — see _cycle_in_cooldown's comment above.
            if passes and _cycle_in_cooldown(cycle):
                passes = False
            candidates.append((cycle, amount, profit_uusd, net_in_start_asset, gross, passes))
    log.info("Timing: cycle discovery+sizing took %.2fs total (find_cycles=%.2fs, "
              "evaluate_cycle=%.2fs across %d cycles examined [%d zero-edge], %.1fms/cycle avg) "
              "before the real-check loop even starts.",
              time.time() - _t_candidates_start, _t_find_cycles_total, _t_evaluate_cycle_total,
              _cycles_examined, _cycles_zero_edge,
              (_t_evaluate_cycle_total / _cycles_examined * 1000) if _cycles_examined else 0)
    if not candidates:
        log.info("No sizeable cycles found this loop (either no edge, or no balance to work with).")
        return

    # Sorted, not just argmax — the flat-guess #1 pick routinely fails the
    # real gas/spread checks below while sitting on top of the list every
    # single loop (a REV-anchored cycle's huge apparent edge repeatedly
    # came in just under MIN_PROFIT_UUSD after real gas, in practice) — and
    # until now that meant the ENTIRE loop gave up as soon as #1 failed,
    # even when a smaller-but-real edge further down the list (e.g. through
    # JURIS/TERRA) would have cleared every check. We now fall through to
    # the next-best candidate instead of quitting after the first miss.

    # Passing candidates always sort ahead of non-passing ones, regardless
    # of currency — a LUNC-rooted cycle that clears MIN_PROFIT_ULUNA but
    # converts to a small profit_uusd number no longer gets buried behind
    # a USTC-rooted cycle that looks bigger in uusd terms but doesn't
    # actually clear ITS OWN bar (or vice versa).
    #
    # CHANGED 2026-08-26 at the user's explicit request: two more sort
    # keys added, both about PREFERRING THE SIMPLEST ROUTE rather than
    # picking whatever has the biggest flat-guess profit_uusd:
    #
    #   1. cycle_touches_a_token(cycle) — a real pair trade (e.g. buy XXX
    #      cheap on the XXX/USTC pool, sell it on the XXX/LUNC pool) now
    #      outranks a pure LUNC<->USTC round trip at equal pass/fail
    #      status, even if the LUNC<->USTC loop's flat-guess profit_uusd
    #      looks bigger. Real trade history (arb_20260826.log) showed
    #      these all-native loops sizing themselves to BILLIONS of uluna
    #      for a sliver of net profit, since the apparent edge was mostly
    #      an artifact of two pools that are really the same shallow
    #      liquidity counted twice — not a genuine token mispricing. This
    #      doesn't forbid an all-native trip outright (if it's truly the
    #      only thing that clears the real checks below, it still runs —
    #      e.g. to rebalance LUNC/USTC when a specific token trade
    #      actually needs the other side), it just no longer gets picked
    #      OVER a real pair opportunity just because its raw number is
    #      bigger.
    #   2. len(cycle) — among candidates that are otherwise tied on the
    #      first two keys, the SHORTER route wins. A direct 2-hop
    #      "identical pair" gap (XXX/USTC vs XXX/LUNC) is preferred over a
    #      3-4 hop detour through unrelated pools that nets similar
    #      profit — fewer legs means fewer messages and less real gas for
    #      the same captured edge. Longer routes are still available and
    #      still get picked when they're the ones that actually clear
    #      profit and a shorter route doesn't (e.g. JURIS, which has no
    #      direct LUNC/USTC-adjacent pool and genuinely needs
    #      TERRA->JURIS routing to exit).
    candidates_sorted = sorted(
        candidates,
        key=lambda c: (0 if c[5] else 1, 0 if cycle_touches_a_token(c[0]) else 1,
                        len(c[0]), -c[2]))

    # ADDED 2026-08-26 at the user's explicit instruction, overriding the
    # softer sort-based preference above with a HARD gate: "the bot must
    # not swap the native it gets from a trade instantly from now on,
    # only swap when everything (trades) is calm." Read as — a pure
    # LUNC<->USTC rebalancing trade is only allowed to fire when this
    # loop found NO real token-pair opportunity that clears its own
    # profit bar (i.e. things are "calm": nothing token-based is worth
    # doing right now). If ANY token-touching candidate passes, every
    # all-native candidate is dropped from consideration entirely this
    # loop — not just ranked lower — so the bot can't reach for a
    # LUNC<->USTC swap just because it happens to be sitting near the top
    # by raw profit_uusd while a real pair trade was also available.
    _token_candidate_passes = any(cycle_touches_a_token(c[0]) and c[5] for c in candidates)
    if _token_candidate_passes:
        _token_only = [c for c in candidates_sorted if cycle_touches_a_token(c[0])]
        if _token_only:
            candidates_sorted = _token_only

    top_cycle, top_amount, top_profit_uusd, top_net_in_start_asset, _, top_passes = candidates_sorted[0]


    top_label = graph_module.cycle_label(top_cycle)
    top_start_asset = top_cycle[0].asset_in
    top_balance = balances[top_start_asset.key()]
    probe = sizing.probe_amount_for(top_balance)
    probe_out = graph_module.simulate_cycle(top_cycle, probe, states) if probe > 0 else 0
    edge_bps = (Decimal(probe_out - probe) / Decimal(probe) * Decimal(10000)) if probe > 0 else Decimal(0)
    fraction = sizing.edge_to_fraction(edge_bps)
    log.info("Sizing detail: start_asset=%s live_balance=%d probe=%d probe_edge_bps=%.2f "
              "fraction=%.4f -> ceiling=%d actual_offer=%d",
              top_start_asset, top_balance, probe, edge_bps, fraction,
              int(top_balance * fraction), top_amount)
    log.info("Best opportunity: %s offer=%d profit_uusd=%d (checked %d sizeable cycles, "
              "ranked using flat gas guess)", top_label, top_amount, top_profit_uusd, len(candidates))

    # --- Forced test trade (keyword-armed, one-shot) ---
    # Overrides amount/gross_profit down to a small probe-sized offer on
    # the SAME top-ranked cycle, then runs it through _prepare_execution
    # with the profit gate bypassed — spread ceiling, real chain gas
    # simulation, the LUNC gas reserve floor, and DRY_RUN all still apply.
    # This is a deliberate single-cycle test, so it does NOT participate
    # in the candidate fallback loop below.
    if force_trade_event is not None and force_trade_event.is_set():
        force_trade_event.clear()
        forced_price = graph_module.price_in_uusd(top_start_asset, pools, states, ustc, lunc)
        forced_amount = sizing.probe_amount_for(top_balance)
        forced_amount = sizing.liquidity_cap_for_cycle(top_cycle, states, forced_amount)
        if forced_price > 0 and config.FORCE_TRADE_MAX_UUSD_EQUIV > 0:
            forced_amount = min(forced_amount, int(config.FORCE_TRADE_MAX_UUSD_EQUIV / forced_price))
        if forced_amount <= 0:
            log.warning("Forced test trade requested via '%s' but %s has no tradeable "
                        "balance/liquidity ceiling for %s this loop — ignoring this trigger, "
                        "try again next loop.", config.FORCE_TRADE_KEYWORD, top_start_asset, top_label)
        else:
            forced_final = apply_slippage_buffer(graph_module.simulate_cycle(top_cycle, forced_amount, states))
            forced_gross_profit = forced_final - forced_amount
            log.warning("=== FORCED TEST TRADE (via '%s' keyword) === cycle=%s forced_offer=%d %s "
                        "(deliberately small probe size, NOT real profit-based sizing) — "
                        "bypassing the MIN_PROFIT_UUSD gate only, to verify live/atomic "
                        "execution actually works; spread ceiling, real gas simulation, and "
                        "the gas reserve floor still apply below.",
                        config.FORCE_TRADE_KEYWORD, top_label, forced_amount, top_start_asset)
            prepared = _prepare_execution(terra, pools, states, ustc, lunc, top_cycle, forced_amount,
                                           forced_gross_profit, top_start_asset, top_label,
                                           bypass_profit_gate=True,
                                           account_number=account_number, sequence=sequence)
            if prepared is None:
                return
            leg_amounts, leg_params, profit_uusd, real_gas_uluna = prepared
            cycle, amount, label, start_asset, is_forced = (
                top_cycle, forced_amount, top_label, top_start_asset, True)
            return _execute_winning_cycle(terra, cycle, amount, leg_amounts, leg_params,
                                           label, profit_uusd, is_forced, forced_gross_profit,
                                           real_gas_uluna,
                                           pools=pools, states=states, ustc=ustc, lunc=lunc,
                                           account_number=account_number, sequence=sequence)

    if not top_passes:
        log.info("No profitable opportunity this cycle (best: %s profit_uusd=%d, "
                  "net_in_start_asset=%d %s — doesn't clear %s)",
                  top_label, top_profit_uusd, top_net_in_start_asset, top_start_asset,
                  "MIN_PROFIT_ULUNA" if top_start_asset.id == config.DENOM_LUNC
                  and top_start_asset.kind == "native" else "MIN_PROFIT_UUSD")
        return

    # --- Candidate fallback loop ---
    # Try candidates in descending flat-guess-profit order, capped at
    # MAX_CANDIDATES_PER_LOOP (each attempt costs one real simulate_fee
    # network round trip), stopping at the first one that clears every
    # real check. Candidates whose flat-guess profit_uusd is already below
    # MIN_PROFIT_UUSD are skipped without spending a network call on them —
    # real gas only ever makes the number worse, never better, so they
    # cannot possibly pass.
    tried = 0
    _t_realcheck_start = time.time()
    for cand_cycle, cand_amount, cand_profit_uusd, _, cand_gross_profit, cand_passes in candidates_sorted:
        if not cand_passes:
            break  # sort puts all passing candidates first — nothing further down can pass either
        if tried >= config.MAX_CANDIDATES_PER_LOOP:
            log.info("Reached MAX_CANDIDATES_PER_LOOP=%d for this loop without a survivor — "
                      "stopping here rather than issuing unbounded simulate_fee calls. "
                      "(real-check loop took %.2fs for %d attempt(s) before giving up)",
                      config.MAX_CANDIDATES_PER_LOOP, time.time() - _t_realcheck_start, tried)
            break
        tried += 1
        cand_label = graph_module.cycle_label(cand_cycle)
        cand_start_asset = cand_cycle[0].asset_in

        # Pre-trade freshness check — re-fetch just THIS candidate's pool(s)
        # and re-run the same sizing math against that fresh state before
        # paying for a real simulate_fee round trip on numbers discovery may
        # have computed 20-30s ago. See _refresh_states_for_cycle's
        # docstring for the log evidence this is built from.
        fresh_states = _refresh_states_for_cycle(cand_cycle, states)
        cand_balance = balances[cand_start_asset.key()]
        cand_price = graph_module.price_in_uusd(cand_start_asset, pools, fresh_states, ustc, lunc)
        fresh_amount, fresh_gross, _, fresh_net, _ = evaluate_cycle(
            cand_cycle, fresh_states, lunc_price_uusd, cand_balance, cand_price)
        if fresh_amount <= 0:
            log.info("Skipping %s: no longer sizeable once re-checked against fresh pre-trade "
                      "state (was offer=%d against the loop-start snapshot) — the pool moved "
                      "since discovery.", cand_label, cand_amount)
            _mark_cycle_real_check_failed(cand_cycle)
            continue
        fresh_profit_uusd = int(fresh_net * cand_price)
        if not clears_min_profit(cand_start_asset, fresh_net, fresh_profit_uusd):
            log.info("Skipping %s: no longer clears the profit floor once re-checked against "
                      "fresh pre-trade state (fresh net_in_start_asset=%d %s, fresh "
                      "profit_uusd=%d vs stale candidate profit_uusd=%d) — the pool moved "
                      "since discovery.", cand_label, fresh_net, cand_start_asset,
                      fresh_profit_uusd, cand_profit_uusd)
            _mark_cycle_real_check_failed(cand_cycle)
            continue

        prepared = _prepare_execution(terra, pools, fresh_states, ustc, lunc, cand_cycle,
                                       fresh_amount, fresh_gross, cand_start_asset, cand_label,
                                       bypass_profit_gate=False,
                                       account_number=account_number, sequence=sequence)
        if prepared is None:
            # Real check failed (spread ceiling or real gas simulation) —
            # cool this exact cycle down so it can't immediately dominate
            # the ranking again next loop; see _cycle_in_cooldown's
            # comment for why. A candidate whose REAL check simply didn't
            # clear the profit margin (not a hard failure) still benefits
            # from this — a persistently-thin margin will keep failing the
            # same way, so there's nothing lost by letting other
            # candidates get a turn in the meantime.
            _mark_cycle_real_check_failed(cand_cycle)
        if prepared is not None:
            leg_amounts, leg_params, real_profit_uusd, real_gas_uluna = prepared
            log.info("Timing: real-check loop took %.2fs across %d attempt(s) to find a winner.",
                      time.time() - _t_realcheck_start, tried)
            return _execute_winning_cycle(terra, cand_cycle, fresh_amount, leg_amounts, leg_params,
                                           cand_label, real_profit_uusd, False, fresh_gross,
                                           real_gas_uluna,
                                           pools=pools, states=fresh_states, ustc=ustc, lunc=lunc,
                                           account_number=account_number, sequence=sequence)

    log.info("Tried %d candidate(s) this loop (of %d sizeable and passing their own floor) — "
              "none cleared the real spread/gas checks.", tried,
              sum(1 for c in candidates_sorted if c[5]))
    return


def _check_multi_trip_plan(terra, trips, cycle, states, pools, ustc, lunc, start_asset,
                            lunc_price, start_price, account_number, sequence):
    """
    Builds the flat multi-message plan for a given list of (trip_amount,
    gross_profit, leg_amounts, leg_params) trips, gets ONE real
    simulate_fee call on the whole bundle, and re-runs the full profit
    decision (flat floor + MIN_PROFIT_MARGIN_MULTIPLE) against that real
    gas number — same two gates _prepare_execution already required of
    the single-trip plan, just priced with this bundle's own real cost.

    Factored out of _execute_winning_cycle so the shrinking-fallback loop
    there can call this repeatedly with progressively fewer trips without
    duplicating the simulate_fee + margin-math block each time.

    Returns (flat_plan, real_gas_uluna, real_profit_uusd) on success, or
    (None, None, None) if this specific trip list doesn't clear (a
    failed simulate_fee call, missing price data, or a real profit that
    doesn't clear the margin gate all count as "doesn't clear" here —
    the caller decides what to do next, not this function).
    """
    if len(trips) < 2:
        return None, None, None
    from executor import build_leg_msg
    flat_plan = [(edge, trip_leg_amounts[i], trip_leg_params[i])
                 for _, _, trip_leg_amounts, trip_leg_params in trips
                 for i, edge in enumerate(cycle)]
    msgs = [build_leg_msg(terra.address, edge, trip_amount, params)
            for edge, trip_amount, params in flat_plan]
    try:
        fee = terra.simulate_fee(msgs, account_number=account_number, sequence=sequence)
    except Exception as e:
        log.warning("%d-trip repeated-cycle plan failed real gas simulation (%s).", len(trips), e)
        return None, None, None

    real_gas_uluna = sum(int(c.amount) for c in fee.amount if c.denom == config.GAS_DENOM)
    if real_gas_uluna <= 0:
        return None, None, None

    total_trip_amount = sum(t[0] for t in trips)
    # Sum each trip's OWN gross_profit — already computed inside
    # plan_repeated_cycle_execution against that trip's own post-
    # previous-trade local reserves, so summing it directly is correct
    # (re-simulating every trip against the ORIGINAL unshifted states
    # would overstate profit for trip 2 onward).
    total_gross = sum(t[1] for t in trips)
    if start_asset.id == config.DENOM_LUNC and start_asset.kind == "native":
        real_gas_in_start = real_gas_uluna
    elif start_price > 0:
        real_gas_in_start = int(real_gas_uluna * lunc_price / start_price)
    else:
        return None, None, None

    real_net = total_gross - real_gas_in_start
    real_profit_uusd = int(real_net * start_price)
    required_uusd = float(config.MIN_PROFIT_MARGIN_MULTIPLE) * float(
        graph_module.simulate_cycle_costs_uusd(cycle, total_trip_amount, states, pools, ustc, lunc)
        + Decimal(real_gas_uluna) * lunc_price)

    if (clears_min_profit(start_asset, real_net, real_profit_uusd)
            and real_profit_uusd >= required_uusd):
        return flat_plan, real_gas_uluna, real_profit_uusd
    return None, None, None


def _execute_winning_cycle(terra, cycle, amount, leg_amounts, leg_params, label, profit_uusd,
                            is_forced, gross_profit, real_gas_uluna, pools=None, states=None,
                            ustc=None, lunc=None, account_number=None, sequence=None):
    """
    Executes the one candidate that survived _prepare_execution — the
    LUNC gas reserve floor check plus the actual ATOMIC/sequential
    broadcast. Split out from run_once so both the forced-test-trade path
    and the candidate fallback loop call the exact same execution code
    instead of two copies drifting apart over time.

    ADDED 2026-08-22 (pools/states/ustc/lunc/account_number/sequence,
    all optional): when config.ENABLE_LEG_SPLITTING is on and this is an
    atomic execution, tries to replace the naive one-message-per-leg plan
    with the split-optimized plan from build_split_plan_for_execution
    before broadcasting. leg_amounts/leg_params (the naive plan) are
    ALREADY fully approved by _prepare_execution's checks and are used
    as-is if splitting isn't enabled, isn't applicable (sequential mode),
    or the split plan's own real gas re-check fails — the split path can
    only improve the outcome of an already-good trade, never substitute
    for the safety checks that approved it in the first place.

    real_gas_uluna (ADDED after the 2026-08-31 gas-reserve incident): the
    real, simulate_fee-derived gas cost for the naive plan, as returned
    by _prepare_execution — NOT a flat guess. GAS_RESERVE_ULUNA alone is
    a static floor sized for a typical single-leg trade; a bundled
    atomic cycle (4+ legs, or a multi-trip/split plan on top of that)
    can genuinely cost several times that in real gas, so the flat floor
    can pass this early check while still leaving nowhere near enough
    headroom for the ACTUAL broadcast about to happen. This value (or
    whichever plan-specific real-gas figure supersedes it below) is
    reused right before broadcast for a precise, live-balance check —
    see the hard check just above the try/except broadcast block.
    """
    if not config.DRY_RUN:
        lunc_balance = terra.get_balance(config.DENOM_LUNC)
        if lunc_balance < config.GAS_RESERVE_ULUNA:
            log.error("LUNC balance (%d) is below the gas reserve floor (%d) — refusing to "
                      "trade even though an opportunity was found, to avoid getting stranded "
                      "without gas.", lunc_balance, config.GAS_RESERVE_ULUNA)
            return

    log.info(">>> Executing %s: offer=%d expected_net_profit_uusd=%d (mode=%s%s)",
              label, amount, profit_uusd, "ATOMIC" if config.ATOMIC_EXECUTION else "sequential",
              ", FORCED TEST TRADE" if is_forced else "")

    if config.ATOMIC_EXECUTION:
        if not config.DRY_RUN:
            balances_before = {edge.asset_out.key(): terra.get_asset_balance(edge.asset_out)
                                for edge in cycle}

        split_plan = None
        # Defaults for the hard pre-broadcast check below — the naive
        # single-message-per-leg plan's own already-approved numbers.
        # Overwritten if a multi-trip or leg-split plan replaces it.
        broadcast_total_offer = amount
        broadcast_real_gas = real_gas_uluna
        # ADDED 2026-08-25, built from real evidence (arb_20260825.log):
        # the SAME cycle executed 4 separate times across ~85 seconds,
        # each paying its own gas — 19.2% of total captured profit spent
        # on redundant gas alone. Tries the greedy multi-TRIP extension
        # (repeated full passes through this cycle, each re-sized against
        # local post-trade reserves) BEFORE falling back to per-LEG
        # splitting — these two mechanisms solve different problems
        # (multiple loops' worth of the same opportunity vs. one thin
        # pool's spread-deduction quirk) and are kept mutually exclusive
        # per execution for now rather than combined, to keep the real
        # gas re-check below straightforward.
        if (config.ENABLE_REPEATED_CYCLE_EXECUTION and pools is not None and states is not None
                and not config.DRY_RUN):
            # cycle[0].asset_in IS the last edge's asset_out (that's what
            # makes it a cycle) — balances_before already fetched this
            # exact balance one block above. Re-querying it here cost a
            # second real LCD round-trip (~0.3-0.4s observed in
            # arb_20260830.log) inside the window between an approved
            # decision and broadcast, on a leg whose spread was already
            # sitting at the MAX_SPREAD_CEILING_BPS limit — that's exactly
            # the wrong place to spend extra time on pool drift.
            live_balance = balances_before[cycle[0].asset_in.key()]
        elif config.ENABLE_REPEATED_CYCLE_EXECUTION and pools is not None and states is not None:
            live_balance = amount * 4  # DRY_RUN: no real balance query, just enough headroom to try
        else:
            live_balance = 0

        if config.ENABLE_REPEATED_CYCLE_EXECUTION and pools is not None and states is not None:
            start_asset = cycle[0].asset_in
            lunc_price = graph_module.price_in_uusd(lunc, pools, states, ustc, lunc)
            start_price = graph_module.price_in_uusd(start_asset, pools, states, ustc, lunc)
            # Includes GAS_ADJUSTMENT — the real terra.simulate_fee call this
            # plan gets re-checked against always applies it (see
            # terra_client.py), so a flat guess that omits it systematically
            # under-prices gas relative to what the final real check will
            # see. Confirmed via arb_20260825.log: without this factor, the
            # greedy loop accepted a 5th trip whose real (adjusted) gas cost
            # tipped the WHOLE bundle over into failing its final re-check —
            # throwing away all 5 trips' profit, not just the marginal one.
            gas_guess_per_trip_uluna = int(Decimal(GAS_UNITS_PER_HOP) * len(cycle)
                                            * config.GAS_PRICE * Decimal(str(config.GAS_ADJUSTMENT)))
            if start_asset.id == config.DENOM_LUNC and start_asset.kind == "native":
                gas_guess_per_trip = gas_guess_per_trip_uluna
            elif start_price > 0 and lunc_price > 0:
                gas_guess_per_trip = int(Decimal(gas_guess_per_trip_uluna) * lunc_price / start_price)
            else:
                gas_guess_per_trip = None

            if gas_guess_per_trip is not None:
                trips = plan_repeated_cycle_execution(
                    cycle, states, live_balance, start_asset, lunc_price, start_price,
                    gas_guess_per_trip)
                # SHRINKING FALLBACK — ADDED 2026-08-25: confirmed via
                # arb_20260825.log that the previous all-or-nothing version
                # of this check threw away an entire 5-trip, ~2,060 LUNC
                # bundle just because the LAST (most marginal) trip's real
                # gas share tipped the WHOLE thing under the margin
                # requirement — falling all the way back to a single ~488
                # LUNC trip and losing everything the other 4 trips would
                # have captured. Tries the full trip list first, then drops
                # the last (most marginal, since trips are appended in
                # decreasing-profit order) trip and retries, down to 2 —
                # below 2 there's no bundling benefit over the plain
                # single-trip plan already approved by _prepare_execution.
                for n in range(len(trips), 1, -1):
                    candidate_trips = trips[:n]
                    flat_plan, multi_real_gas, real_profit_uusd_multi = _check_multi_trip_plan(
                        terra, candidate_trips, cycle, states, pools, ustc, lunc, start_asset,
                        lunc_price, start_price, account_number, sequence)
                    if flat_plan is not None:
                        split_plan = flat_plan
                        # The naive single-trip amount/real_gas_uluna no longer describe
                        # what's about to be broadcast — a multi-trip bundle offers the
                        # SUM of every trip's amount and pays gas for the WHOLE bundle in
                        # one tx. Track both so the hard pre-broadcast check below (and
                        # the DRY_RUN log line further down) reasons about the actual
                        # plan being sent, not the plan that was originally approved.
                        broadcast_total_offer = sum(t[0] for t in candidate_trips)
                        broadcast_real_gas = multi_real_gas
                        log.info("%s: using %d-trip repeated-cycle plan (%d messages, amounts=%s) "
                                  "instead of 1 — real_gas_uluna=%d, real_profit_uusd=%d "
                                  "(re-verified)%s.", label, n, len(flat_plan),
                                  [t[0] for t in candidate_trips], multi_real_gas,
                                  real_profit_uusd_multi,
                                  "" if n == len(trips) else
                                  f", shrunk from {len(trips)} trips after the full bundle failed")
                        break
                    log.info("%s: %d-trip repeated-cycle plan didn't clear the profit gate with "
                              "its own real gas — %s.", label, n,
                              "trying fewer trips" if n > 2 else "falling back to the single-trip plan")

        if split_plan is None and config.ENABLE_LEG_SPLITTING and pools is not None and states is not None:
            split_plan, split_real_gas, split_real_profit_uusd = build_split_plan_for_execution(
                terra, pools, states, ustc, lunc, cycle, leg_amounts, gross_profit, cycle[0].asset_in,
                label, account_number=account_number, sequence=sequence)
            if split_plan is not None:
                # Leg-splitting only changes how many messages a leg becomes, not the
                # total amount offered out of the wallet (still `amount`) — but it DOES
                # change real gas, so that must still flow into the pre-broadcast check.
                broadcast_real_gas = split_real_gas
                log.info("%s: using split-optimized plan (%d messages) instead of the naive "
                          "%d-message plan — real_gas_uluna=%d, real_profit_uusd=%d (re-verified).",
                          label, len(split_plan), len(cycle), split_real_gas, split_real_profit_uusd)

        # HARD PRE-BROADCAST CHECK (added after the 2026-08-31 incident):
        # config.GAS_RESERVE_ULUNA is a flat floor sized for a typical trade — it is
        # NOT a substitute for checking the actual numbers about to be broadcast. For
        # a cycle that starts in LUNC, `broadcast_total_offer` and gas both come out of
        # the SAME wallet balance, so the flat floor above can pass while this specific
        # broadcast still can't actually be covered (a 4-leg atomic cycle's real gas
        # runs ~77-90 LUNC — well over the old 20 LUNC flat reserve). Re-fetch balance
        # right here, immediately before broadcast, rather than trusting the value
        # fetched at the top of this function: everything above (multi-trip planning,
        # leg-splitting, its own simulate_fee calls) took real time and pool state or
        # wallet balance may have moved since.
        start_asset_is_lunc = (cycle[0].asset_in.kind == "native"
                                and cycle[0].asset_in.id == config.DENOM_LUNC)
        if not config.DRY_RUN and start_asset_is_lunc:
            live_lunc_balance = terra.get_balance(config.DENOM_LUNC)
            required = broadcast_total_offer + broadcast_real_gas
            if required > live_lunc_balance:
                log.error("Refusing to broadcast %s: offer (%d) + real gas (%d) = %d uluna "
                          "exceeds live LUNC balance (%d) — the flat GAS_RESERVE_ULUNA floor "
                          "(%d) was not enough headroom for this specific broadcast.",
                          label, broadcast_total_offer, broadcast_real_gas, required,
                          live_lunc_balance, config.GAS_RESERVE_ULUNA)
                return

        try:
            if split_plan is not None:
                from executor import execute_plan_atomic
                cycle_result = execute_plan_atomic(terra, split_plan)
            else:
                cycle_result = execute_cycle_atomic(terra, cycle, leg_amounts, leg_params)
        except Exception as e:
            log.error("Atomic cycle %s reverted or failed to broadcast: %s. No funds moved "
                      "beyond gas for this attempt (Cosmos SDK txs are atomic across "
                      "messages) — nothing further to reconcile.", label, e)
            return
        if not config.DRY_RUN:
            final_asset = cycle[-1].asset_out
            balance_after = terra.get_asset_balance(final_asset)
            received = balance_after - balances_before[final_asset.key()]
            if final_asset.kind == "native" and final_asset.id == config.DENOM_LUNC:
                received += cycle_result.gas_fee_uluna
            log.info("Atomic cycle %s complete: txhash=%s final_asset_delta=%d %s",
                      label, cycle_result.txhash, received, final_asset)

            # Realized-vs-predicted check. `amount` is what was offered;
            # `gross_profit` (from evaluate_cycle, same start-asset units)
            # is the predicted NET gain on top of it — so amount+gross_profit
            # is the predicted total output, directly comparable to `received`
            # since a cycle always returns to its starting asset. Added
            # 2026-08-02 specifically as the early-warning signal for
            # MAX_POOL_RESERVE_FRACTION being raised (0.05 -> 0.15, see
            # config.py) — that change reopens the same exposure that caused
            # the 2026-07-12 incident (a real trade realizing only ~24% of
            # predicted profit). This makes that ratio visible on every real
            # trade going forward instead of only discoverable after the fact.
            predicted_total = amount + gross_profit
            if predicted_total > 0:
                realization_pct = (Decimal(received) / Decimal(predicted_total)) * 100
                log.info("Realized vs predicted: got %d, predicted %d (%.1f%% realized)",
                          received, predicted_total, realization_pct)
                if realization_pct < 70:
                    log.warning("Realized profit was under 70%% of predicted for %s — if this "
                                "keeps happening, especially on thin pools, MAX_POOL_RESERVE_"
                                "FRACTION (currently %.2f) may be too high again.",
                                label, config.MAX_POOL_RESERVE_FRACTION)
        return

    # --- Sequential mode (original path) ---
    # Same hard pre-broadcast check as the atomic path above: gas is paid in LUNC
    # regardless of start asset, so a cycle that STARTS in LUNC draws its offer and
    # its gas from the same balance — the flat GAS_RESERVE_ULUNA floor checked at the
    # top of this function is not a substitute for checking this specific broadcast's
    # real numbers against a freshly-fetched balance.
    start_asset_is_lunc = cycle[0].asset_in.kind == "native" and cycle[0].asset_in.id == config.DENOM_LUNC
    if not config.DRY_RUN and start_asset_is_lunc:
        live_lunc_balance = terra.get_balance(config.DENOM_LUNC)
        required = amount + real_gas_uluna
        if required > live_lunc_balance:
            log.error("Refusing to execute %s: offer (%d) + real gas (%d) = %d uluna exceeds "
                      "live LUNC balance (%d) — the flat GAS_RESERVE_ULUNA floor (%d) was not "
                      "enough headroom for this specific execution.",
                      label, amount, real_gas_uluna, required, live_lunc_balance,
                      config.GAS_RESERVE_ULUNA)
            return

    current_amount = amount
    for i, edge in enumerate(cycle):
        if not config.DRY_RUN:
            balance_before = terra.get_asset_balance(edge.asset_out)

        offer_amount = leg_amounts[i] if config.DRY_RUN else current_amount

        lp = leg_params[i]
        leg_result = execute_leg(terra, edge.pool.pair_address, edge.asset_in, offer_amount,
                                  max_spread=lp.max_spread, belief_price=lp.belief_price,
                                  pool_kind=lp.kind, min_receive=lp.min_receive or 0)

        if config.DRY_RUN:
            continue

        balance_after = terra.get_asset_balance(edge.asset_out)
        raw_delta = balance_after - balance_before
        if edge.asset_out.kind == "native" and edge.asset_out.id == config.DENOM_LUNC:
            received = raw_delta + leg_result.gas_fee_uluna
        else:
            received = raw_delta

        if received <= 0:
            log.error("Leg %s -> %s: no real proceeds after accounting for gas "
                      "(raw_delta=%d, gas_fee=%d, adjusted=%d) — aborting remaining legs.",
                      edge.asset_in, edge.asset_out, raw_delta, leg_result.gas_fee_uluna, received)
            return
        current_amount = received


if __name__ == "__main__":
    main()