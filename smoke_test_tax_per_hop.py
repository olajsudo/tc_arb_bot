"""
REAL-FUND smoke test: walks ONE real multi-hop cycle leg-by-leg with a
small offer, and for EACH leg separately measures whether tax was really
applied on the OUTGOING side (the amount sent as funds into the pool) and
the INCOMING side (the amount the pool sends back) — then compares that
observed count against what tax.py/graph.py currently PREDICT for the
same cycle.

WHY: graph.simulate_cycle calls tax.calculate_tax() twice per hop —
once on asset_in (direction="out") and once on asset_out (direction="in")
— and tax.py's native branch fires on BOTH calls whenever the asset is
native, regardless of direction. For a LUNC-rooted cycle that means the
model expects at least 2 native tax hits per round trip (leaving LUNC,
returning to LUNC), and more for every additional native leg in between.
If real trades are only showing tax applied once or twice on a cycle
where the model expects more, this test tells you exactly which leg(s)
and which side(s) (offer-in vs return-out) the mismatch is on, using
real on-chain data instead of guessing.

HOW EACH LEG IS MEASURED:
  - OUTGOING tax (on offer_amount, before the swap): not directly
    observable from wallet balance (we send exactly offer_amount
    regardless of tax — the tax reduces what the CONTRACT receives, not
    what leaves our wallet). Instead, this computes two predictions
    BEFORE executing — what the AMM should return if the full untaxed
    offer_amount reached the pool, vs. what it should return if only
    (offer_amount - predicted_outgoing_tax) reached the pool — and after
    executing, checks which prediction the real swap event's
    return_amount actually landed closer to.
  - INCOMING tax (on the pool's return_amount): directly observable —
    compare the swap event's reported return_amount against the ACTUAL
    wallet balance delta for asset_out (gas-adjusted if asset_out is
    LUNC). This is the same technique smoke_test_new_tokens.py uses.

This moves real, small amounts of real funds, sequentially (not atomic —
we need to observe each leg's real result before sizing the next one,
the same reason arbitrage_bot.py's sequential execution mode measures
balance deltas between legs). It will NOT run:
  - if config.DRY_RUN is True (nothing real would happen, so there'd be
    nothing to observe)
  - without passing --confirm on the command line

Run: python smoke_test_tax_per_hop.py --confirm
     python smoke_test_tax_per_hop.py --confirm --start LUNC --hops 3
     python smoke_test_tax_per_hop.py --list                 (no funds moved)
"""
import sys
import argparse
import logging
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP

import config
import tax as tax_module
from assets import Asset
from amm_math import simulate_swap
from terra_client import TerraClient
from executor import execute_leg
import graph as graph_module
import sizing
from arbitrage_bot import build_pools_and_assets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("smoke_test_tax_per_hop")

# Wide on purpose, same reasoning as smoke_test_new_tokens.py's
# DISCOVERY_TOLERANCE_BPS: this script's whole point is to observe
# whatever tax is REALLY there, so it must not risk reverting on-chain
# before it gets to measure and report the real number. Has no effect on
# the live bot's actual trading tolerance (SPREAD_TOLERANCE_BPS is
# untouched) — this only loosens the throwaway messages THIS script
# builds for its own diagnostic legs.
DISCOVERY_TOLERANCE_BPS = 3000  # 30%

# Below this, a measured gap is treated as rounding noise rather than a
# real tax hit — same 5bps threshold smoke_test_new_tokens.py's
# _report_gap uses, kept consistent so results from both scripts read
# the same way.
NOISE_FLOOR_BPS = 5


# Same rounding convention graph.compute_leg_execution_params uses
# (_DECIMAL_PLACES=6) — raw Decimal division can carry 40+ fractional
# digits, which Terraport/Terraswap-family contracts reject outright with
# a decimal-parsing error rather than truncating it silently. Duplicated
# here (not imported from graph.py) since graph._fmt_decimal is a private
# helper and this script's belief_price/max_spread strings are built
# independently of graph.compute_leg_execution_params's own snapshot.
_DECIMAL_PLACES = 6


def _fmt_decimal(d: Decimal, places: int = _DECIMAL_PLACES, rounding=ROUND_HALF_UP) -> str:
    quant = Decimal(1).scaleb(-places)
    return str(d.quantize(quant, rounding=rounding))


def _predict_leg(offer_amount: int, asset_in: Asset, asset_out: Asset, reserve_in: int,
                  reserve_out: int, commission_rate: Decimal):
    """
    Returns (untaxed_predicted_return, taxed_predicted_return,
    predicted_outgoing_tax) for one leg, BEFORE it's executed.
    untaxed_predicted_return assumes the pool receives the full
    offer_amount (no outgoing tax); taxed_predicted_return assumes the
    pool only receives offer_amount minus tax.py's currently-predicted
    outgoing tax. Comparing the real swap event against these two after
    the fact is what isolates whether outgoing tax is real for this leg.
    """
    predicted_outgoing_tax = tax_module.calculate_tax(offer_amount, asset_in, direction="out")
    untaxed = simulate_swap(offer_amount, reserve_in, reserve_out, commission_rate).return_amount
    taxed = simulate_swap(max(0, offer_amount - predicted_outgoing_tax), reserve_in, reserve_out,
                           commission_rate).return_amount
    return untaxed, taxed, predicted_outgoing_tax


def _classify_outgoing(event_return: int, untaxed_pred: int, taxed_pred: int) -> str:
    """
    Classifies whether the REAL swap event's return_amount looks like it
    came from a taxed or untaxed offer, by checking which prediction it
    lands closer to (within NOISE_FLOOR_BPS of either counts as a match;
    landing closer to neither — e.g. reserves moved a lot between our
    snapshot and the real broadcast — is reported as INCONCLUSIVE rather
    than guessed at).
    """
    if untaxed_pred <= 0:
        return "INCONCLUSIVE (no valid untaxed prediction)"
    gap_to_untaxed_bps = abs(event_return - untaxed_pred) / untaxed_pred * 10000
    gap_to_taxed_bps = (abs(event_return - taxed_pred) / taxed_pred * 10000) if taxed_pred > 0 else Decimal("inf")
    if gap_to_untaxed_bps <= NOISE_FLOOR_BPS and gap_to_taxed_bps > NOISE_FLOOR_BPS:
        return "NOT DETECTED (event matches untaxed prediction)"
    if gap_to_taxed_bps <= NOISE_FLOOR_BPS and gap_to_untaxed_bps > NOISE_FLOOR_BPS:
        return "DETECTED (event matches taxed prediction)"
    if gap_to_untaxed_bps <= NOISE_FLOOR_BPS and gap_to_taxed_bps <= NOISE_FLOOR_BPS:
        return "INCONCLUSIVE (predicted tax is itself ~0 for this leg — both predictions agree)"
    return (f"INCONCLUSIVE (event matches neither prediction cleanly — "
            f"{gap_to_untaxed_bps:.1f}bps from untaxed, {gap_to_taxed_bps:.1f}bps from taxed; "
            f"reserves likely moved between snapshot and broadcast)")


def _model_predicted_hits(cycle) -> int:
    """
    How many tax HITS (outgoing + incoming, summed across every leg)
    tax.py/graph.py currently predict for this cycle, using a tiny probe
    amount purely to check which side of calculate_tax() returns > 0 —
    not to predict real bps, just to count how many of the 2*len(cycle)
    calculate_tax() calls graph.simulate_cycle makes for this cycle
    actually fire at all under the current model.
    """
    hits = 0
    probe = 1_000_000
    for edge in cycle:
        if tax_module.calculate_tax(probe, edge.asset_in, direction="out") > 0:
            hits += 1
        if tax_module.calculate_tax(probe, edge.asset_out, direction="in") > 0:
            hits += 1
    return hits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true",
                         help="Actually broadcast. Without this, only --list works.")
    parser.add_argument("--list", action="store_true",
                         help="List candidate real cycles and exit — moves no funds.")
    parser.add_argument("--start", default="LUNC",
                         help="Display name of the asset to root cycle-search from (default LUNC).")
    parser.add_argument("--hops", type=int, default=2,
                         help="Exact number of hops the chosen cycle must have (default 2).")
    parser.add_argument("--cycle-index", type=int, default=0,
                         help="Which matching cycle to test, 0-based, in the order found (default 0).")
    args = parser.parse_args()

    if not args.list and not args.confirm:
        print("Refusing to run without --confirm (this moves real funds). "
              "Use --list first to see candidate cycles without moving anything.")
        sys.exit(1)
    if not args.list and config.DRY_RUN:
        print("config.DRY_RUN is True — there would be nothing real to observe. "
              "Set DRY_RUN=false in .env for this script specifically (it does NOT "
              "affect any other run of arbitrage_bot.py using the same .env, since "
              "DRY_RUN is only read once per process).")
        sys.exit(1)

    config.validate()
    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)

    pools, assets_to_check, lunc, ustc = build_pools_and_assets(terra)
    states = {id(p): p.get_state() for p in pools}
    edges = graph_module.build_edges(pools)

    start_asset = next((a for a in assets_to_check if str(a).upper() == args.start.upper()), None)
    if start_asset is None:
        print(f"--start {args.start!r} isn't one of this bot's known assets_to_check: "
              f"{[str(a) for a in assets_to_check]}")
        sys.exit(1)

    all_cycles = graph_module.find_cycles(edges, start_asset, max_hops=4)
    cycles = [c for c in all_cycles if len(c) == args.hops]
    if not cycles:
        print(f"No {args.hops}-hop cycles found starting from {start_asset} right now "
              f"(found {len(all_cycles)} cycles at other hop counts). Try a different "
              f"--hops or --start.")
        sys.exit(1)

    print(f"Found {len(cycles)} {args.hops}-hop cycle(s) starting from {start_asset}:")
    for i, c in enumerate(cycles):
        marker = " <-- selected" if i == args.cycle_index else ""
        print(f"  [{i}] {graph_module.cycle_label(c)}{marker}")

    if args.list:
        return

    if args.cycle_index >= len(cycles):
        print(f"--cycle-index {args.cycle_index} is out of range (only {len(cycles)} found).")
        sys.exit(1)
    cycle = cycles[args.cycle_index]
    label = graph_module.cycle_label(cycle)

    model_hits = _model_predicted_hits(cycle)
    print(f"\nModel currently predicts {model_hits} tax hit(s) across this cycle's "
          f"{len(cycle)} hop(s) (out of {2 * len(cycle)} possible offer-in/return-out sides).")

    # Raw fetched tax_rate/tax_cap for every native denom this cycle
    # actually touches — printed explicitly (not just used internally) so
    # a cap that's suspiciously 0 is visible immediately instead of only
    # showing up indirectly as "0 predicted tax" on individual legs. This
    # is what caught the 2026-08-08 finding that live tax_cap for both
    # uluna and uusd was fetching as 0 while real swaps still showed an
    # uncapped 1.50% deducted on every native receive (see config.py's
    # TAX_CAP_ZERO_CONFIRMED_DENOMS for the fix that came out of this).
    native_denoms_in_cycle = sorted({a.id for edge in cycle for a in (edge.asset_in, edge.asset_out)
                                      if a.kind == "native"})
    if native_denoms_in_cycle:
        rate = tax_module.get_tax_rate()
        print(f"Live tax_rate: {rate} ({rate * 100}%)")
        for denom in native_denoms_in_cycle:
            cap = tax_module.get_tax_cap(denom)
            confirmed = denom in config.TAX_CAP_ZERO_CONFIRMED_DENOMS
            flag = ""
            if cap == 0 and not confirmed:
                flag = "  <-- 0 and NOT in TAX_CAP_ZERO_CONFIRMED_DENOMS: calculate_tax will now treat this as suspicious and NOT apply it"
            print(f"  tax_cap[{denom}] = {cap}{flag}")

    balance = terra.get_asset_balance(start_asset)
    probe_amount = sizing.probe_amount_for(balance)
    offer_amount = sizing.liquidity_cap_for_cycle(cycle, states, probe_amount)
    if offer_amount <= 0:
        print(f"No usable balance/liquidity ceiling for {start_asset} right now "
              f"(balance={balance}) — nothing safe to test.")
        sys.exit(1)

    print(f"\n=== Testing {label} ===")
    print(f"Offer size: {offer_amount} {start_asset} (small, discovery-tolerance size — "
          f"NOT real profit-based sizing)")

    print("\nWARNING: this broadcasts REAL transactions with REAL funds, one leg at a "
          "time, using a wide (30%) discovery tolerance so an unexpectedly large real "
          "tax doesn't revert the tx before it can be measured.")
    confirm = input("Type 'yes' to proceed, anything else to abort: ")
    if confirm.strip().lower() != "yes":
        print("Aborted by user — no transaction sent.")
        return

    current_amount = offer_amount
    observed_hits = 0
    leg_summaries = []

    for i, edge in enumerate(cycle):
        pool = edge.pool
        pool_kind = getattr(pool, "pool_kind", "terraswap")
        leg_label = f"leg {i+1}/{len(cycle)}: {edge.asset_in} -[{pool.name}]-> {edge.asset_out}"
        print(f"\n--- {leg_label} ---")

        # Fresh reserves right before this leg, not the initial batch —
        # sequential execution takes real wall-clock time between legs,
        # and staleness here would corrupt the untaxed-vs-taxed
        # prediction this leg's OUTGOING classification depends on.
        state = pool.get_state()
        reserve_in = state.reserves.get(edge.asset_in.key())
        reserve_out = state.reserves.get(edge.asset_out.key())
        if not reserve_in or not reserve_out:
            print(f"  Could not read fresh reserves for {pool.name} — aborting before "
                  f"sending real funds into an unknown state.")
            return

        untaxed_pred, taxed_pred, out_tax_pred = _predict_leg(
            current_amount, edge.asset_in, edge.asset_out, reserve_in, reserve_out,
            state.commission_rate)
        print(f"  Offering {current_amount} {edge.asset_in}")
        print(f"  Predicted return if OUTGOING tax does NOT apply: {untaxed_pred}")
        print(f"  Predicted return if OUTGOING tax DOES apply ({out_tax_pred} predicted tax): {taxed_pred}")

        balance_before_out = terra.get_asset_balance(edge.asset_out)

        if pool_kind == "garuda":
            min_receive = int(Decimal(taxed_pred) * (Decimal(10000) - Decimal(DISCOVERY_TOLERANCE_BPS))
                               / Decimal(10000))
            try:
                leg_result = execute_leg(terra, pool.pair_address, edge.asset_in, current_amount,
                                          pool_kind="garuda", min_receive=max(0, min_receive))
            except Exception as e:
                print(f"  Reverted on-chain even at {DISCOVERY_TOLERANCE_BPS/100:.0f}% tolerance: {e}")
                print(f"  Stopping here — {i} leg(s) already executed for real; check wallet "
                      f"balances before re-running.")
                return
        else:
            belief_price = Decimal(reserve_in) / Decimal(reserve_out) if reserve_out else None
            leg_result = execute_leg(terra, pool.pair_address, edge.asset_in, current_amount,
                                      max_spread=_fmt_decimal(Decimal(DISCOVERY_TOLERANCE_BPS) / Decimal(10000),
                                                               rounding=ROUND_UP),
                                      belief_price=_fmt_decimal(belief_price) if belief_price else None)

        event_return = leg_result.received
        outgoing_verdict = _classify_outgoing(event_return, untaxed_pred, taxed_pred)
        print(f"  Real swap event return_amount: {event_return}")
        print(f"  OUTGOING tax on this leg: {outgoing_verdict}")

        balance_after_out = terra.get_asset_balance(edge.asset_out)
        raw_delta = balance_after_out - balance_before_out
        if edge.asset_out.kind == "native" and edge.asset_out.id == config.DENOM_LUNC:
            actual_received = raw_delta + leg_result.gas_fee_uluna
        else:
            actual_received = raw_delta

        incoming_detected = False
        if event_return > 0:
            gap = event_return - actual_received
            gap_bps = Decimal(gap) / Decimal(event_return) * 10000
            print(f"  Actual wallet balance delta for {edge.asset_out}: {actual_received} "
                  f"(event said {event_return}, gap={gap}, {gap_bps:.2f} bps)")
            if abs(gap_bps) > NOISE_FLOOR_BPS:
                incoming_detected = True
                print(f"  INCOMING tax on this leg: DETECTED (~{gap_bps/100:.2f}%)")
            else:
                print(f"  INCOMING tax on this leg: NOT DETECTED (gap is rounding noise)")
        else:
            print(f"  Event reported 0 return — can't measure incoming tax for this leg.")
            actual_received = max(0, actual_received)

        outgoing_detected = outgoing_verdict.startswith("DETECTED")
        observed_hits += int(outgoing_detected) + int(incoming_detected)
        leg_summaries.append((leg_label, outgoing_verdict, incoming_detected))

        if actual_received <= 0:
            print(f"  No real proceeds after this leg — stopping here rather than "
                  f"offering 0 into the next pool.")
            return
        current_amount = actual_received

    print(f"\n=== Summary: {label} ===")
    for leg_label, outgoing_verdict, incoming_detected in leg_summaries:
        print(f"  {leg_label}")
        print(f"    outgoing: {outgoing_verdict}")
        print(f"    incoming: {'DETECTED' if incoming_detected else 'not detected'}")
    print(f"\nModel predicted {model_hits} tax hit(s) across this cycle; "
          f"observed {observed_hits} real hit(s) (INCONCLUSIVE legs are not counted "
          f"either way above — re-run if any leg came back inconclusive to get a clean read).")
    if observed_hits != model_hits:
        print("MISMATCH — the model's tax count does not match what was actually observed "
              "on-chain for this cycle. Re-run this same cycle once more to rule out a "
              "one-off (reserves moving between snapshot and broadcast can produce a false "
              "INCONCLUSIVE or a borderline classification); if it repeats, the specific "
              "leg(s) flagged above are where tax.py/graph.py's model and reality have "
              "diverged for this route.")
    else:
        print("MATCH — the model's tax count agrees with what was actually observed for "
              "this cycle.")


if __name__ == "__main__":
    main()