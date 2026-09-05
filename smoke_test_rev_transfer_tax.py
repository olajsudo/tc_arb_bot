"""
One-off measurement script for REV's real CW20 transfer tax — follows the
exact pattern already used to discover LCW's ~5.00% and ASTRO's ~0.50%
(see config.py's CW20 transfer tax comments): broadcast one small REAL
swap, then compare the swap event's reported return_amount against the
actual wallet balance delta. Any gap is the token's own hidden transfer
tax, not something the chain or pool query exposes.

Why this exists: the 2026-07-30 atomic smoke test
(smoke_test_atomic.py) failed on the REV->USTC leg with an on-chain
"Cannot Sub" overflow — leg 1 tried to offer 595916 REV but the wallet's
simulated balance after leg 0 was only 590049, a ~0.98% shortfall. That's
the exact signature of an undocumented CW20 transfer tax, currently
assumed 0% for REV in config.py (it isn't in cw20_transfer_tax_rate()'s
table yet). That failed atomic run never broadcast anything (it failed
during the SDK's own gas-estimation call, before broadcast_sync), so no
gas or funds were spent — but it's also not a clean measurement, since
spread/rounding could distort a number inferred from it. This script
gets a clean one instead: a single real swap, sequential (not atomic),
so there's nothing else in the same tx that could fail or confuse the
comparison.

Respects DRY_RUN like every other script here — set DRY_RUN=false only
once you're ready to actually broadcast this small measurement swap.

Run: python smoke_test_rev_transfer_tax.py
"""
import logging
from decimal import Decimal

import config
from assets import Asset
from pool_client import DexPool
from terra_client import TerraClient
from executor import execute_leg
from amm_math import simulate_swap

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("smoke_test_rev_tax")


def main():
    config.validate()
    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)
    log.info("DRY_RUN=%s", config.DRY_RUN)

    lunc = Asset(kind="native", id=config.DENOM_LUNC, decimals=6, display="LUNC")
    rev_token = Asset(kind="cw20", id=config.REV_CW20_ADDRESS,
                       decimals=config.REV_DECIMALS, display="REV")
    pool = DexPool("Terraport REV/LUNC", terra, config.TERRAPORT_POOL_REV_LUNC,
                    rev_token, lunc, config.TERRAPORT_COMMISSION_RATE)

    state = pool.get_state()
    reserve_in = state.reserves[lunc.key()]
    reserve_out = state.reserves[rev_token.key()]

    offer = config.SMOKE_TEST_AMOUNT_ULUNA
    sim = simulate_swap(offer, reserve_in, reserve_out, state.commission_rate)

    belief_price = str((Decimal(reserve_in) / Decimal(reserve_out)).quantize(Decimal("0.000001")))
    # Wide max_spread deliberately — this run is measuring REV's OUTPUT-side
    # transfer tax specifically, not testing normal trade-time slippage
    # protection, so a generous tolerance here just avoids an unrelated
    # spread rejection getting in the way of the measurement.
    max_spread = "0.05"

    log.info("AMM-only simulated return (does NOT include any REV transfer tax, since "
              "it's unmeasured/assumed 0 today): offer=%d uluna -> return_amount=%d REV "
              "(this is the figure the swap event itself will report if it executes)",
              offer, sim.return_amount)

    if config.DRY_RUN:
        log.info("DRY_RUN — nothing will be broadcast, so there's nothing real to measure. "
                  "Set DRY_RUN=false in .env and rerun when you're ready to actually measure "
                  "REV's transfer tax with a small real swap.")
        return

    balance_before = terra.get_cw20_balance(config.REV_CW20_ADDRESS)
    log.warning("DRY_RUN is FALSE — this will broadcast a REAL swap of %.2f LUNC into REV "
                "purely to measure REV's transfer tax (this swap is one-directional — it "
                "does NOT swap back to LUNC/USTC afterward).", offer / 1_000_000)
    confirm = input("Type 'yes' to proceed with a REAL broadcast, anything else to abort: ")
    if confirm.strip().lower() != "yes":
        log.info("Aborted by user — no transaction sent.")
        return

    leg_result = execute_leg(terra, config.TERRAPORT_POOL_REV_LUNC, lunc, offer,
                              max_spread=max_spread, belief_price=belief_price)

    balance_after = terra.get_cw20_balance(config.REV_CW20_ADDRESS)
    actual_received = balance_after - balance_before
    reported = leg_result.received

    if reported <= 0:
        log.error("Swap event reported return_amount=0 (couldn't parse it) — check the "
                  "txhash manually on a Terra Classic explorer instead.")
        return

    shortfall = reported - actual_received
    tax_bps = Decimal(shortfall * 10000) / Decimal(reported) if reported else Decimal(0)
    log.info("Swap event reported return_amount=%d REV; actual wallet balance delta=%d REV; "
              "shortfall=%d REV (%.2f bps, ~%.3f%%) — this is REV's real CW20 transfer tax "
              "on arrival, the same thing LCW's ~5.00%% and ASTRO's ~0.50%% turned out to be.",
              reported, actual_received, shortfall, tax_bps, tax_bps / 100)
    log.info("Before trusting REV in any atomic cycle again: round this to a sane value and "
              "add it to config.py as REV_TRANSFER_TAX_BPS, plus REV_CW20_ADDRESS: "
              "REV_TRANSFER_TAX_BPS in cw20_transfer_tax_rate()'s table — same pattern as "
              "LCW_TRANSFER_TAX_BPS / ASTRO_TRANSFER_TAX_BPS.")


if __name__ == "__main__":
    main()