"""
LCW smoke test — NOT the arbitrage strategy. Ignores profitability
entirely and forces one small real round-trip through the LCW/USTC pool:

    USTC -> LCW (native offer leg)
    LCW -> USTC (CW20 offer leg via Send hook)

Same purpose as smoke_test_cw20.py (which proved this path works for
TERRA) — LCW is a different token contract, so this confirms the same
Send-hook path also works for LCW specifically before trusting it with
real size in the main arbitrage loop.

WILL lose a small amount of real money to fees/tax/slippage. Respects
DRY_RUN — set DRY_RUN=false in .env to actually broadcast.

Run: python smoke_test_lcw.py
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
log = logging.getLogger("smoke_test_lcw")


def main():
    config.validate()
    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)
    log.info("DRY_RUN=%s", config.DRY_RUN)

    ustc = Asset(kind="native", id=config.DENOM_USTC, decimals=6, display="USTC")
    lcw_token = Asset(kind="cw20", id=config.LCW_CW20_ADDRESS,
                       decimals=config.LCW_DECIMALS, display="LCW")

    pool = DexPool("Terraport LCW/USTC", terra, config.TERRAPORT_POOL_LCW_USTC,
                    lcw_token, ustc, config.TERRAPORT_COMMISSION_RATE)

    # Small, fixed test amount — not sized for profit, just to prove the
    # message path works for this specific token. 500,000 uusd = 0.5 USTC.
    amount_uusd = 500_000

    if not config.DRY_RUN:
        balance = terra.get_balance(config.DENOM_USTC)
        if balance < amount_uusd + 100_000:
            log.error("Wallet USTC balance (%d uusd) looks too low for this test. Aborting.", balance)
            return
        log.warning("DRY_RUN is OFF — about to broadcast 1 REAL round-trip through a CW20 (LCW) "
                     "leg: %d uusd -> LCW -> back to USTC.", amount_uusd)
        log.warning("Note: the LCW/USTC pool is shallow relative to the others (~7,137 USTC "
                     "reserve, per an earlier inspect_pools.py run) — expect noticeably more "
                     "slippage on this round-trip than the TERRA test.")
        log.warning("Starting in 5 seconds — Ctrl+C now to cancel.")
        time.sleep(5)

    log.info("--- Leg 1: USTC -> LCW (native offer) ---")
    balance_before = terra.get_asset_balance(lcw_token)
    execute_leg(terra, pool.pair_address, ustc, amount_uusd)
    if config.DRY_RUN:
        log.info("[DRY_RUN] Leg 1 simulated only, skipping leg 2.")
        log.info("Smoke test complete (dry run).")
        return

    balance_after = terra.get_asset_balance(lcw_token)
    received_lcw = balance_after - balance_before
    log.info("Leg 1: live LCW balance went %d -> %d (received %d — this is the REAL amount, "
              "not the swap event, since LCW's transfer tax makes the event over-report)",
              balance_before, balance_after, received_lcw)

    if received_lcw <= 0:
        log.error("Leg 1: no real LCW balance increase — aborting before touching the CW20 leg.")
        return

    log.info("--- Leg 2: LCW -> USTC (CW20 offer via Send hook) ---")
    received_ustc = execute_leg(terra, pool.pair_address, lcw_token, received_lcw).received

    if received_ustc <= 0:
        log.error("Leg 2 (the CW20 leg) reported 0 USTC received. Check your wallet's LCW "
                   "balance and the tx on an explorer using the txhash logged above — this "
                   "would suggest LCW behaves differently from TERRA on the sell side "
                   "(e.g. a transfer restriction), worth investigating before trading it further.")
        return

    net = received_ustc - amount_uusd
    log.info("LCW round-trip complete: offered %d uusd, got %d LCW, then %d uusd back. "
              "Net: %d uusd (%.4f USTC)", amount_uusd, received_lcw, received_ustc,
              net, net / 1_000_000)
    log.info("Both legs succeeded, including the CW20 Send-hook path for LCW. Smoke test complete.")


if __name__ == "__main__":
    main()
