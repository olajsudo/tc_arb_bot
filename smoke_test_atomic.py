"""
Standalone smoke test for atomic (single-tx, multi-message) cycle
execution — separate from the main loop so you can validate real
on-chain behavior with a small, deliberately-chosen cycle before trusting
ATOMIC_EXECUTION=true inside arbitrage_bot.py's live decision loop.

Picks ONE real cycle currently available (preferring one that routes
through a REV pool, since REV/LUNC and REV/USTC are newly added and unverified;
falls back to any other LUNC-starting cycle otherwise — capped to a small
SMOKE_TEST_AMOUNT_ULUNA-scale offer regardless of what real sizing would
allow), computes real belief_price/max_spread params, and executes it via
executor.execute_cycle_atomic — as ONE bundled transaction.

Safe by construction:
  - Respects DRY_RUN same as everything else — defaults to a dry run
    unless you've explicitly set DRY_RUN=false in .env.
  - Uses a small fixed offer (config.SMOKE_TEST_AMOUNT_ULUNA, same knob
    your other smoke tests already use), not real sizing/balance-fraction
    math, so a real run risks a known, small, deliberately-chosen amount.
  - Prints the profit_uusd estimate and asks for a final go/no-go before
    ever broadcasting for real, on top of the DRY_RUN gate.

Run: python smoke_test_atomic.py
"""
import logging
from decimal import Decimal

import config
from assets import Asset
from pool_client import DexPool
from terra_client import TerraClient
from executor import execute_cycle_atomic
import graph as graph_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("smoke_test_atomic")


def build_pools_and_assets(terra):
    lunc = Asset(kind="native", id=config.DENOM_LUNC, decimals=6, display="LUNC")
    ustc = Asset(kind="native", id=config.DENOM_USTC, decimals=6, display="USTC")
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
    rev_token = Asset(kind="cw20", id=config.REV_CW20_ADDRESS,
                       decimals=config.REV_DECIMALS, display="REV")

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
        DexPool("Terraport REV/LUNC", terra, config.TERRAPORT_POOL_REV_LUNC,
                 rev_token, lunc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport REV/USTC", terra, config.TERRAPORT_POOL_REV_USTC,
                 rev_token, ustc, config.TERRAPORT_COMMISSION_RATE),
    ]
    return pools, lunc, ustc


def main():
    config.validate()
    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)
    log.info("DRY_RUN=%s", config.DRY_RUN)

    pools, lunc, ustc = build_pools_and_assets(terra)
    edges = graph_module.build_edges(pools)
    states = {id(p): p.get_state() for p in pools}

    cycles = graph_module.find_cycles(edges, lunc, max_hops=3)
    if not cycles:
        log.error("No LUNC-starting cycles found at all (max_hops=3) — nothing to test.")
        return

    # Pick the first cycle with a positive probe edge, ignoring profitability —
    # this is a mechanism test (does atomic execution work at all), not a
    # profit-seeking run. A small fixed offer is used regardless of the
    # cycle's real edge strength.
    #
    # Since the point of THIS run is specifically to confirm the two new
    # REV pools work end-to-end (query shape, commission resolution,
    # correct swap message), prefer a cycle that actually routes through
    # one of them over any other candidate. Falls back to any positive-
    # probe cycle if no REV-touching cycle clears the probe (e.g. thin
    # liquidity), so this script still does something useful either way.
    REV_POOL_NAMES = {"Terraport REV/LUNC", "Terraport REV/USTC"}

    def touches_rev(cycle):
        return any(edge.pool.name in REV_POOL_NAMES for edge in cycle)

    rev_cycles = [c for c in cycles if touches_rev(c)]
    other_cycles = [c for c in cycles if not touches_rev(c)]
    if not rev_cycles:
        log.warning("No cycle found that routes through a REV pool (max_hops=3) — "
                    "falling back to any other sizeable cycle instead. This run will "
                    "NOT actually validate the REV pools.")

    chosen = None
    chosen_kind = None
    for candidate_list, kind in ((rev_cycles, "REV-pool"), (other_cycles, "non-REV fallback")):
        for cycle in candidate_list:
            probe = min(config.SMOKE_TEST_AMOUNT_ULUNA, 100)
            probe_out = graph_module.simulate_cycle(cycle, probe, states)
            if probe_out > 0:
                chosen = cycle
                chosen_kind = kind
                break
        if chosen is not None:
            break

    if chosen is not None:
        log.info("Selected a %s cycle for this run.", chosen_kind)

    if chosen is None:
        log.error("No cycle produced a nonzero simulated result even at a tiny probe — "
                  "nothing safe to test.")
        return

    label = graph_module.cycle_label(chosen)
    offer = config.SMOKE_TEST_AMOUNT_ULUNA
    log.info("Chosen cycle: %s", label)
    log.info("Fixed smoke-test offer: %d uluna (%.2f LUNC) — NOT real sizing math, "
              "deliberately small and fixed.", offer, offer / 1_000_000)

    leg_amounts = graph_module.simulate_cycle_legs(chosen, offer, states)
    leg_params = graph_module.compute_leg_execution_params(
        chosen, leg_amounts, states, config.SPREAD_TOLERANCE_BPS, config.MAX_SPREAD_CEILING_BPS)
    if leg_params is None:
        log.error("Could not compute safe execution params for this cycle "
                  "(spread exceeded MAX_SPREAD_CEILING_BPS on some leg) — aborting test.")
        return

    log.info("Leg amounts: %s", leg_amounts)
    log.info("Leg params (belief_price, max_spread): %s", leg_params)

    simulated_final = graph_module.simulate_cycle(chosen, offer, states)
    simulated_profit = simulated_final - offer
    log.info("Simulated result: start=%d final=%d simulated_profit=%d %s "
              "(this is a MECHANISM test, not a profit-seeking run — expect this to "
              "often be negative or tiny at smoke-test size)",
              offer, simulated_final, simulated_profit, lunc)

    if not config.DRY_RUN:
        log.warning("DRY_RUN is FALSE — this will broadcast a REAL transaction risking "
                    "%.2f LUNC as a bundled %d-leg atomic swap.", offer / 1_000_000, len(chosen))
        confirm = input("Type 'yes' to proceed with a REAL broadcast, anything else to abort: ")
        if confirm.strip().lower() != "yes":
            log.info("Aborted by user — no transaction sent.")
            return

    if not config.DRY_RUN:
        start_asset = chosen[0].asset_in
        balance_before = terra.get_asset_balance(start_asset)

    result = execute_cycle_atomic(terra, chosen, leg_amounts, leg_params)
    log.info("execute_cycle_atomic returned: txhash=%s gas_fee_uluna=%d",
              result.txhash, result.gas_fee_uluna)

    if not config.DRY_RUN:
        start_asset = chosen[0].asset_in
        balance_after = terra.get_asset_balance(start_asset)
        raw_delta = balance_after - balance_before
        if start_asset.kind == "native" and start_asset.id == config.DENOM_LUNC:
            real_change = raw_delta + result.gas_fee_uluna  # add gas back to isolate swap-only effect
        else:
            real_change = raw_delta
        log.info("Real balance delta for %s: raw=%d gas_adjusted=%d (negative = net loss "
                  "on this test trade, expected/likely at smoke-test size once gas is "
                  "included)", start_asset, raw_delta, real_change)


if __name__ == "__main__":
    main()