"""
ASTRO smoke test — NOT the arbitrage strategy. Ignores profitability
entirely and forces one small real round-trip through the Astroport
ASTRO/USTC pool:

    USTC  -> ASTRO (native offer leg)
    ASTRO -> USTC  (CW20 offer leg via Send hook)

Same purpose as smoke_test_lcw.py (which proved LCW's real ~5% transfer
tax) and smoke_test_cw20.py (which proved the CW20 Send-hook path itself
using TERRA) — ASTRO is a different token contract, and config.py is
explicit that its transfer tax has never been empirically verified:

    ASTRO_TRANSFER_TAX_BPS = _int("ASTRO_TRANSFER_TAX_BPS", "0")  # UNVERIFIED — assumed 0

This exists specifically because a live 3-leg cycle through
LUNC -[Astroport ASTRO/LUNC]-> ASTRO -[Astroport ASTRO/USTC]-> USTC
-[Terraswap Pool 1]-> LUNC repeatedly failed simulate_fee() at leg 3 with
an insufficient-funds mismatch of ~0.35% — the right size to be an
unaccounted-for ASTRO transfer tax, the same shape of bug LCW had. This
test isolates just the ASTRO/USTC leg pair to measure it directly,
instead of guessing a number from arbitrage-loop logs.

WILL lose a small amount of real money to fees/tax/slippage. Respects
DRY_RUN — set DRY_RUN=false in .env to actually broadcast.

Run: python smoke_test_astro.py
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
log = logging.getLogger("smoke_test_astro")


def main():
    config.validate()
    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)
    log.info("DRY_RUN=%s", config.DRY_RUN)

    ustc = Asset(kind="native", id=config.DENOM_USTC, decimals=6, display="USTC")
    astro_token = Asset(kind="cw20", id=config.ASTRO_CW20_ADDRESS,
                         decimals=config.ASTRO_DECIMALS, display="ASTRO")

    pool = DexPool("Astroport ASTRO/USTC", terra, config.ASTROPORT_POOL_ASTRO_USTC,
                    astro_token, ustc, config.ASTROPORT_COMMISSION_RATE)

    # Small, fixed test amount — not sized for profit, just to prove the
    # message path works and measure any transfer tax. Deliberately much
    # smaller than the TERRA/LCW tests' 500,000 uusd: this wallet's live
    # USTC balance is small right now (it holds most of its value in
    # LUNC), and a percentage-based transfer tax is scale-invariant, so a
    # smaller probe measures it just as validly. 20,000 uusd = 0.02 USTC.
    amount_uusd = 20_000

    if not config.DRY_RUN:
        balance = terra.get_balance(config.DENOM_USTC)
        buffer_uusd = 10_000  # small margin so the tx isn't the wallet's entire USTC balance
        if balance < amount_uusd + buffer_uusd:
            log.error("Wallet USTC balance (%d uusd) looks too low for this test (need at "
                       "least %d: test amount + buffer). Aborting. If you want to run the "
                       "test at a larger size, swap a small amount of LUNC to USTC first — "
                       "the wallet is holding most of its value in LUNC right now.",
                       balance, amount_uusd + buffer_uusd)
            return
        log.warning("DRY_RUN is OFF — about to broadcast 1 REAL round-trip through a CW20 (ASTRO) "
                     "leg: %d uusd -> ASTRO -> back to USTC.", amount_uusd)
        log.warning("Starting in 5 seconds — Ctrl+C now to cancel.")
        time.sleep(5)

    log.info("--- Leg 1: USTC -> ASTRO (native offer) ---")
    balance_before = terra.get_asset_balance(astro_token)
    execute_leg(terra, pool.pair_address, ustc, amount_uusd)
    if config.DRY_RUN:
        log.info("[DRY_RUN] Leg 1 simulated only, skipping leg 2.")
        log.info("Smoke test complete (dry run).")
        return

    balance_after = terra.get_asset_balance(astro_token)
    received_astro = balance_after - balance_before
    log.info("Leg 1: live ASTRO balance went %d -> %d (received %d — this is the REAL amount, "
              "not the swap event, so any hidden transfer tax on the way IN already shows up "
              "here as a shortfall vs. the pool's reported return_amount)",
              balance_before, balance_after, received_astro)

    if received_astro <= 0:
        log.error("Leg 1: no real ASTRO balance increase — aborting before touching the CW20 leg.")
        return

    log.info("--- Leg 2: ASTRO -> USTC (CW20 offer via Send hook) ---")
    balance_before_ustc = terra.get_balance(config.DENOM_USTC)
    leg_result = execute_leg(terra, pool.pair_address, astro_token, received_astro)
    balance_after_ustc = terra.get_balance(config.DENOM_USTC)

    # Gas is always paid in LUNC, not USTC, so no gas-contamination
    # correction is needed on this leg's USTC balance delta.
    raw_delta_ustc = balance_after_ustc - balance_before_ustc
    reported_return = leg_result.received

    log.info("Leg 2: pool reported return_amount=%d uusd; real wallet USTC balance delta=%d uusd",
              reported_return, raw_delta_ustc)

    if raw_delta_ustc <= 0:
        log.error("Leg 2 (the CW20 leg) produced no real USTC balance increase. Check your "
                   "wallet's ASTRO balance and the tx on an explorer using the txhash logged "
                   "above — this would suggest ASTRO behaves differently from TERRA/LCW on the "
                   "sell side (e.g. a transfer restriction), worth investigating before trading "
                   "it further.")
        return

    if reported_return > 0 and raw_delta_ustc < reported_return:
        shortfall = reported_return - raw_delta_ustc
        implied_tax_bps = (shortfall * 10000) / reported_return
        log.warning("Leg 2: real USTC received (%d) is LESS than the pool's reported "
                    "return_amount (%d) — shortfall of %d uusd, implying an ASTRO transfer "
                    "tax of roughly %.1f bps (%.3f%%) on top of the AMM's own commission. "
                    "If this is consistent across runs, set ASTRO_TRANSFER_TAX_BPS=%d in "
                    ".env (config.py currently defaults it to 0, UNVERIFIED).",
                    raw_delta_ustc, reported_return, shortfall, implied_tax_bps,
                    implied_tax_bps / 100, round(implied_tax_bps))
    else:
        log.info("Leg 2: real USTC received matches (or exceeds) the pool's reported "
                  "return_amount — no evidence of a hidden ASTRO transfer tax on this leg.")

    net = raw_delta_ustc - amount_uusd
    log.info("ASTRO round-trip complete: offered %d uusd, got %d ASTRO, then %d uusd back "
              "(real balance delta). Net: %d uusd (%.4f USTC)",
              amount_uusd, received_astro, raw_delta_ustc, net, net / 1_000_000)
    log.info("Both legs succeeded, including the CW20 Send-hook path for ASTRO. Smoke test complete.")


if __name__ == "__main__":
    main()