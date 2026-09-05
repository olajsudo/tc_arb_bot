"""
Directly tests whether White Whale's LUNC/USTC pool accepts the swap
message shape executor.py builds — WITHOUT waiting for a real arbitrage
cycle to organically clear MAX_SPREAD_CEILING_BPS first (which, per a real
log, hasn't happened yet: every cycle touching this pool so far has needed
~2000bps, double the 1000bps ceiling).

Why this matters specifically for White Whale: its commission-simulation
query already failed with a real, confirmed schema mismatch
(KeyError: 'commission_amount' — see pool_client.DexPool._resolve_
commission_via_simulation). That's evidence its query responses genuinely
differ in shape from Terraswap/Astroport's, which raises the odds its
swap ExecuteMsg might too — the same category of surprise Garuda gave
this bot before (see config.GARUDA_COMMISSION_RATE's docstring). This
script isolates that ONE question — does the swap message gas-simulate
cleanly — without needing an actual profitable opportunity to test it.

This ONLY builds and gas-simulates a message (terra.simulate_fee) — it
NEVER broadcasts, regardless of DRY_RUN. No funds move, no matter what
this script finds.

Run: python probe_whitewhale_swap.py
"""
import logging

import config
from assets import Asset
from terra_client import TerraClient
from executor import build_swap_msg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("probe_whitewhale_swap")

# Small, arbitrary probe size — matches the existing SMOKE_TEST_AMOUNT_ULUNA
# convention (2 LUNC). Size doesn't matter for what this script is testing
# (message SHAPE, not price/profitability) — it just needs to be small
# enough that gas simulation doesn't fail for unrelated reasons (e.g.
# tripping the pool's own max_spread if the offer is large relative to
# White Whale's current reserves).
PROBE_AMOUNT_ULUNA = config.SMOKE_TEST_AMOUNT_ULUNA


def main():
    config.validate()
    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)

    lunc = Asset(kind="native", id=config.DENOM_LUNC, decimals=6, display="LUNC")

    log.info("Building a %d uluna swap message against White Whale LUNC/USTC (%s)...",
              PROBE_AMOUNT_ULUNA, config.WHITEWHALE_POOL_LUNC_USTC)
    msg = build_swap_msg(terra.address, config.WHITEWHALE_POOL_LUNC_USTC, lunc,
                          PROBE_AMOUNT_ULUNA, max_spread="0.05")

    log.info("Message built successfully — attempting gas simulation "
              "(read-only, does NOT broadcast)...")
    try:
        fee = terra.simulate_fee([msg])
        log.info("SUCCESS — White Whale accepted this swap message shape. "
                  "Estimated fee: %s", fee.amount)
        log.info("This clears the ExecuteMsg-compatibility question — the pool is "
                  "safe to trade whenever a real opportunity clears the spread ceiling.")
    except Exception as e:
        log.error("FAILED — gas simulation rejected this message: %s", e)
        log.error("This means White Whale's swap ExecuteMsg schema likely does NOT "
                  "match what executor.build_swap_msg builds (the same failure mode "
                  "Garuda hit). Do not trade this pool until the real schema is found "
                  "(check White Whale's own contract docs/source) and executor.py is "
                  "updated to match, the same way it would need to be for any "
                  "genuinely incompatible venue.")


if __name__ == "__main__":
    main()