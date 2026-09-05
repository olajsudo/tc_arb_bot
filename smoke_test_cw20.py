"""
CW20 smoke test — NOT the arbitrage strategy. Ignores profitability
entirely and forces one small real round-trip through a single pool:

    USTC -> TERRA (native offer leg — already proven working)
    TERRA -> USTC (CW20 offer leg — the untested path: Send hook)

This is the first real test of the CW20 swap message path (executor.py's
`Send{contract, amount, msg}` branch), which the main arbitrage loop
hasn't exercised yet because gas cost dominates at any size small enough
to test cheaply.

WILL lose a small amount of real money to fees/tax/slippage, same as the
native smoke test. Respects DRY_RUN like everything else — set
DRY_RUN=false in .env to actually broadcast.

Run: python smoke_test_cw20.py
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
log = logging.getLogger("smoke_test_cw20")


def main():
    config.validate()
    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)
    log.info("DRY_RUN=%s", config.DRY_RUN)

    ustc = Asset(kind="native", id=config.DENOM_USTC, decimals=6, display="USTC")
    terra_token = Asset(kind="cw20", id=config.TERRA_CW20_ADDRESS,
                         decimals=config.TERRA_DECIMALS, display="TERRA")

    pool = DexPool("Terraport TERRA/USTC", terra, config.TERRAPORT_POOL_TERRA_USTC,
                    terra_token, ustc, config.TERRAPORT_COMMISSION_RATE)

    # Small, fixed test amount — not sized for profit, just to prove the
    # message path works. 500,000 uusd = 0.5 USTC.
    amount_uusd = 500_000

    if not config.DRY_RUN:
        balance = terra.get_balance(config.DENOM_USTC)
        if balance < amount_uusd + 100_000:
            log.error("Wallet USTC balance (%d uusd) looks too low for this test. Aborting.", balance)
            return
        log.warning("DRY_RUN is OFF — about to broadcast 1 REAL round-trip through a CW20 (TERRA) "
                     "leg: %d uusd -> TERRA -> back to USTC. This is the first live test of the "
                     "CW20 Send-hook swap path.", amount_uusd)
        log.warning("Starting in 5 seconds — Ctrl+C now to cancel.")
        time.sleep(5)

    log.info("--- Leg 1: USTC -> TERRA (native offer, already-proven path) ---")
    received_terra = execute_leg(terra, pool.pair_address, ustc, amount_uusd).received
    if config.DRY_RUN:
        log.info("[DRY_RUN] Leg 1 simulated only, skipping leg 2.")
        log.info("Smoke test complete (dry run).")
        return

    if received_terra <= 0:
        log.error("Leg 1 reported 0 TERRA received — aborting before touching the CW20 leg.")
        return

    log.info("--- Leg 2: TERRA -> USTC (CW20 offer via Send hook — THE path being tested) ---")
    received_ustc = execute_leg(terra, pool.pair_address, terra_token, received_terra).received

    if received_ustc <= 0:
        log.error("Leg 2 (the CW20 leg) reported 0 USTC received. This is the important failure "
                   "mode to investigate — check your wallet's TERRA balance and the tx on an "
                   "explorer using the txhash logged above.")
        return

    net = received_ustc - amount_uusd
    log.info("CW20 round-trip complete: offered %d uusd, got %d TERRA, then %d uusd back. "
              "Net: %d uusd (%.4f USTC)", amount_uusd, received_terra, received_ustc,
              net, net / 1_000_000)
    log.info("Both legs succeeded, including the CW20 Send-hook path. Smoke test complete.")


if __name__ == "__main__":
    main()
