"""
Recovery utility for the LCW smoke test that got interrupted mid-way
(leg 1 succeeded, leg 2 failed because LCW applies its own transfer tax
that the pool's swap event doesn't account for).

Queries your ACTUAL current LCW balance directly from the token contract
(not from any swap event), and sells that exact real amount back to
USTC. Safe to run even if you're not sure exactly how much LCW you're
holding — it always uses the live balance.

Run: python recover_lcw.py
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
log = logging.getLogger("recover_lcw")


def main():
    config.validate()
    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)
    log.info("DRY_RUN=%s", config.DRY_RUN)

    ustc = Asset(kind="native", id=config.DENOM_USTC, decimals=6, display="USTC")
    lcw_token = Asset(kind="cw20", id=config.LCW_CW20_ADDRESS,
                       decimals=config.LCW_DECIMALS, display="LCW")

    live_balance = terra.get_cw20_balance(config.LCW_CW20_ADDRESS)
    log.info("Live LCW balance on-chain: %d", live_balance)

    if live_balance <= 0:
        log.info("No LCW balance to recover — nothing to do.")
        return

    pool = DexPool("Terraport LCW/USTC", terra, config.TERRAPORT_POOL_LCW_USTC,
                    lcw_token, ustc, config.TERRAPORT_COMMISSION_RATE)

    if config.DRY_RUN:
        log.info("[DRY_RUN] Would sell %d LCW (live balance) back to USTC via %s",
                  live_balance, pool.pair_address)
        return

    log.warning("About to sell your full live LCW balance (%d) back to USTC.", live_balance)
    log.warning("Starting in 5 seconds — Ctrl+C now to cancel.")
    time.sleep(5)

    received_ustc = execute_leg(terra, pool.pair_address, lcw_token, live_balance).received
    if received_ustc <= 0:
        log.error("Sell-back failed or returned 0 — check the txhash above on an explorer, "
                   "and check your live LCW balance again before retrying (it may have "
                   "changed if this partially succeeded).")
        return

    log.info("Recovered %d uusd (%.4f USTC) back from %d LCW.",
              received_ustc, received_ustc / 1_000_000, live_balance)


if __name__ == "__main__":
    main()
