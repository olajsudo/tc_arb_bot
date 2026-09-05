"""
JURIS smoke test — NOT the arbitrage strategy. Ignores profitability
entirely and forces one small real round-trip through the GARUDA DEFI
JURIS/LUNC pool specifically (not the Terraport JURIS/LUNC pool):

    LUNC  -> JURIS (native offer leg)
    JURIS -> LUNC  (CW20 offer leg via Send hook)

This is the important one to run before trusting Garuda with any real
size: every other venue in this codebase (Terraswap, Terraport,
Astroport) has now been confirmed to implement the same
{"pool":{}}/{"simulation":{}} query interface and swap/Send-hook message
shapes that pool_client.py and executor.py assume. Garuda DeFi has not —
config.py's GARUDA_COMMISSION_RATE comment flags this explicitly. This
test either confirms Garuda behaves the same way, or fails in a way that
tells us exactly where it diverges, before the main loop ever sizes a
real trade through it.

Also measures any JURIS transfer tax the same way LCW's ~5% and ASTRO's
~0.50% were discovered — via real balance deltas, not swap events.

Note: there's no JURIS/USTC pool, so unlike the ASTRO/LCW smoke tests
this round-trips through LUNC. Since the final leg's output is LUNC —
the same asset gas is paid in — this corrects for gas contamination on
that leg, same approach as smoke_test_lcw_triangle.py.

WILL lose a small amount of real money to fees/tax/slippage. Respects
DRY_RUN — set DRY_RUN=false in .env to actually broadcast.

Run: python smoke_test_juris.py
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
log = logging.getLogger("smoke_test_juris")


def main():
    config.validate()
    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)
    log.info("DRY_RUN=%s", config.DRY_RUN)

    lunc = Asset(kind="native", id=config.DENOM_LUNC, decimals=6, display="LUNC")
    juris_token = Asset(kind="cw20", id=config.JURIS_CW20_ADDRESS,
                         decimals=config.JURIS_DECIMALS, display="JURIS")

    pool = DexPool("Garuda JURIS/LUNC", terra, config.GARUDA_POOL_JURIS_LUNC,
                    juris_token, lunc, config.GARUDA_COMMISSION_RATE)

    # Small, fixed test amount — not sized for profit, just to prove the
    # message path works and measure any transfer tax. 1,000,000 uluna =
    # 1.0 LUNC.
    amount_uluna = 1_000_000

    if not config.DRY_RUN:
        balance = terra.get_balance(config.DENOM_LUNC)
        # Need the test amount PLUS the configured gas reserve PLUS a
        # margin for two legs' worth of real gas — this test pays gas in
        # LUNC twice (once per leg), unlike the USTC-denominated smoke
        # tests where only one leg touched LUNC via gas.
        buffer_uluna = config.GAS_RESERVE_ULUNA + 20_000_000  # reserve + ~20 LUNC gas margin
        needed = amount_uluna + buffer_uluna
        if balance < needed:
            log.error("Wallet LUNC balance (%d uluna) looks too low for this test (need at "
                       "least %d: test amount + gas reserve + margin). Aborting.",
                       balance, needed)
            return
        log.warning("DRY_RUN is OFF — about to broadcast 1 REAL round-trip through the "
                     "UNVERIFIED Garuda DeFi venue: %d uluna (1 LUNC) -> JURIS -> back to LUNC.",
                     amount_uluna)
        log.warning("This is the first live trade through Garuda DeFi — if the query/message "
                     "interface differs from the Terraswap-family standard, this may fail "
                     "outright rather than just producing a wrong number. That's useful "
                     "information either way.")
        log.warning("Starting in 5 seconds — Ctrl+C now to cancel.")
        time.sleep(5)

    log.info("--- Leg 1: LUNC -> JURIS (native offer, via Garuda DeFi) ---")
    balance_before_juris = terra.get_asset_balance(juris_token)
    leg1_result = execute_leg(terra, pool.pair_address, lunc, amount_uluna)
    if config.DRY_RUN:
        log.info("[DRY_RUN] Leg 1 simulated only, skipping leg 2.")
        log.info("Smoke test complete (dry run).")
        return

    balance_after_juris = terra.get_asset_balance(juris_token)
    received_juris = balance_after_juris - balance_before_juris
    log.info("Leg 1: live JURIS balance went %d -> %d (received %d — this is the REAL amount, "
              "not the swap event, so any hidden transfer tax on the way IN already shows up "
              "here as a shortfall vs. the pool's reported return_amount=%d)",
              balance_before_juris, balance_after_juris, received_juris, leg1_result.received)

    if received_juris <= 0:
        log.error("Leg 1: no real JURIS balance increase — aborting before touching the CW20 "
                   "leg. If the txhash above shows a failure, that's the important signal: "
                   "Garuda's swap message format may not match the Terraswap-family standard "
                   "executor.py assumes. Check the raw tx on an explorer.")
        return

    log.info("--- Leg 2: JURIS -> LUNC (CW20 offer via Send hook, via Garuda DeFi) ---")
    balance_before_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2_result = execute_leg(terra, pool.pair_address, juris_token, received_juris)
    balance_after_lunc = terra.get_balance(config.DENOM_LUNC)

    # Gas is always paid in LUNC, and this leg's OUTPUT is also LUNC — so
    # the raw balance delta is (swap proceeds - gas fee for THIS leg), not
    # proceeds alone. Add the fee back to isolate true proceeds, same as
    # smoke_test_lcw_triangle.py's final leg.
    raw_delta_lunc = balance_after_lunc - balance_before_lunc
    received_lunc = raw_delta_lunc + leg2_result.gas_fee_uluna
    reported_return = leg2_result.received

    log.info("Leg 2: pool reported return_amount=%d uluna; raw LUNC balance delta=%d uluna "
              "(gas-contaminated), gas paid=%d -> true swap proceeds=%d",
              reported_return, raw_delta_lunc, leg2_result.gas_fee_uluna, received_lunc)

    if received_lunc <= 0:
        log.error("Leg 2 (the CW20 leg) produced no real LUNC proceeds. Check your wallet's "
                   "JURIS balance and the tx on an explorer using the txhash logged above — "
                   "this would suggest Garuda's Send-hook path doesn't match the standard "
                   "executor.py assumes, worth investigating before trading this venue further.")
        return

    if reported_return > 0 and received_lunc < reported_return:
        shortfall = reported_return - received_lunc
        implied_tax_bps = (shortfall * 10000) / reported_return
        log.warning("Leg 2: true swap proceeds (%d) are LESS than the pool's reported "
                    "return_amount (%d) — shortfall of %d uluna, implying a JURIS transfer "
                    "tax of roughly %.1f bps (%.3f%%) on top of Garuda's own commission. "
                    "If this is consistent across runs, set JURIS_TRANSFER_TAX_BPS=%d in "
                    ".env (config.py currently defaults it to 0, UNVERIFIED).",
                    received_lunc, reported_return, shortfall, implied_tax_bps,
                    implied_tax_bps / 100, round(implied_tax_bps))
    else:
        log.info("Leg 2: true swap proceeds match (or exceed) the pool's reported "
                  "return_amount — no evidence of a hidden JURIS transfer tax on this leg.")

    net = received_lunc - amount_uluna
    log.info("JURIS round-trip complete (via Garuda DeFi): offered %d uluna, got %d JURIS, "
              "then %d uluna back (true proceeds). Net: %d uluna (%.4f LUNC)",
              amount_uluna, received_juris, received_lunc, net, net / 1_000_000)
    log.info("Both legs succeeded through Garuda DeFi — its query/message interface matches "
              "the Terraswap-family standard this bot assumes. Smoke test complete.")


if __name__ == "__main__":
    main()