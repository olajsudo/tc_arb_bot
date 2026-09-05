"""
Smoke test — NOT the arbitrage strategy. This ignores profitability
entirely and just proves the bot can actually sign, broadcast, and
receive funds back: swap a small fixed LUNC amount on Terraswap Pool 1,
then swap whatever USTC came back on Terraswap Pool 2, back to LUNC.
Repeats SMOKE_TEST_COUNT times.

This WILL lose a small amount of real money each round-trip (fees + tax +
slippage) — that's expected and fine for a few tiny test amounts. Not
something you'd want to run indefinitely.

Respects DRY_RUN like the main bot: with DRY_RUN=true (the default) this
just logs what it would have done, with nothing broadcast. Set
DRY_RUN=false in .env to actually test real execution.

Run: python smoke_test.py
"""
import logging
import time

import config
from assets import Asset
from pool_client import DexPool
from terra_client import TerraClient
from executor import execute_leg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("smoke_test")


def main():
    config.validate()
    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)
    log.info("DRY_RUN=%s", config.DRY_RUN)

    lunc = Asset(kind="native", id=config.DENOM_LUNC, decimals=6, display="LUNC")
    ustc = Asset(kind="native", id=config.DENOM_USTC, decimals=6, display="USTC")

    pool_1 = DexPool(config.TERRASWAP_POOL_1_NAME, terra, config.TERRASWAP_POOL_1,
                      lunc, ustc, config.TERRASWAP_COMMISSION_RATE)
    pool_2 = DexPool(config.TERRASWAP_POOL_2_NAME, terra, config.TERRASWAP_POOL_2,
                      lunc, ustc, config.TERRASWAP_COMMISSION_RATE)

    amount = config.SMOKE_TEST_AMOUNT_ULUNA
    count = config.SMOKE_TEST_COUNT
    interval = config.SMOKE_TEST_INTERVAL_SECONDS

    if not config.DRY_RUN:
        balance = terra.get_balance(config.DENOM_LUNC)
        needed = amount + 5_000_000  # rough gas buffer
        if balance < needed:
            log.error("Wallet LUNC balance (%d uluna) looks too low for %d test round-trip(s) "
                       "of %d uluna plus gas. Aborting before broadcasting anything.",
                       balance, count, amount)
            return
        log.warning("DRY_RUN is OFF — about to broadcast %d REAL round-trip swap(s) of %d uluna "
                     "(%.2f LUNC) each. This will lose a small amount to fees/tax/slippage.",
                     count, amount, amount / 1_000_000)
        log.warning("Starting in 5 seconds — Ctrl+C now to cancel.")
        time.sleep(5)

    for i in range(1, count + 1):
        log.info("--- Round-trip %d/%d ---", i, count)

        received_ustc = execute_leg(terra, pool_1.pair_address, lunc, amount).received
        if config.DRY_RUN:
            log.info("[DRY_RUN] Round-trip %d: leg 1 simulated only, skipping leg 2.", i)
            continue

        if received_ustc <= 0:
            log.error("Round-trip %d: leg 1 (LUNC->USTC on %s) reported 0 received, "
                       "aborting this round-trip.", i, pool_1.name)
            continue

        received_lunc = execute_leg(terra, pool_2.pair_address, ustc, received_ustc).received
        if received_lunc <= 0:
            log.error("Round-trip %d: leg 2 (USTC->LUNC on %s) reported 0 received. "
                       "You may be holding USTC from leg 1 — check your wallet balance.",
                       i, pool_2.name)
            continue

        net = received_lunc - amount
        log.info("Round-trip %d complete: offered %d uluna, got %d uusd back, then %d uluna back. "
                  "Net: %d uluna (%.4f LUNC)", i, amount, received_ustc, received_lunc,
                  net, net / 1_000_000)

        if i < count:
            time.sleep(interval)

    log.info("Smoke test complete.")


if __name__ == "__main__":
    main()
