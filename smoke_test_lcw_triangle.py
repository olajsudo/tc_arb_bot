"""
Triangular smoke test — NOT the arbitrage strategy. Forces one small real
round-trip through the EXACT 3-hop route the bot proposed:

    LUNC -> LCW  (Terraport LCW/LUNC pool)
    LCW  -> USTC (Terraport LCW/USTC pool)
    USTC -> LUNC (Terraswap Pool 1)

This is the first live test of this specific combination — we've proven
native swaps, a 2-hop CW20 leg (TERRA), and a 2-hop CW20 leg with a
transfer tax (LCW) separately, but never this exact 3-hop path for real.

Uses a small, fixed test amount: 1,000,000 uluna = 1 LUNC (all assets
here — LUNC, USTC, TERRA, LCW — use 6 decimals, so 1,000,000 base units
= 1.0 whole token for any of them).

Tracks actual balance changes after each leg (not swap events), same
approach as the main bot now uses, since LCW's real transfer tax means
swap events under-report what the pool sent but not what you actually
end up holding. Also corrects for gas contamination on the final leg
(USTC -> LUNC), since gas is always paid in LUNC and would otherwise
make that leg's balance delta look artificially small or negative.

WILL lose a small amount of real money to fees/tax/slippage. Respects
DRY_RUN — set DRY_RUN=false in .env to actually broadcast.

Run: python smoke_test_lcw_triangle.py
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
log = logging.getLogger("smoke_test_lcw_triangle")

TEST_AMOUNT_ULUNA = 1_000_000  # 1.0 LUNC (6 decimals)


def main():
    config.validate()
    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)
    log.info("DRY_RUN=%s", config.DRY_RUN)

    lunc = Asset(kind="native", id=config.DENOM_LUNC, decimals=6, display="LUNC")
    ustc = Asset(kind="native", id=config.DENOM_USTC, decimals=6, display="USTC")
    lcw_token = Asset(kind="cw20", id=config.LCW_CW20_ADDRESS, decimals=6, display="LCW")

    pool_lcw_lunc = DexPool("Terraport LCW/LUNC", terra, config.TERRAPORT_POOL_LCW_LUNC,
                             lcw_token, lunc, config.TERRAPORT_COMMISSION_RATE)
    pool_lcw_ustc = DexPool("Terraport LCW/USTC", terra, config.TERRAPORT_POOL_LCW_USTC,
                             lcw_token, ustc, config.TERRAPORT_COMMISSION_RATE)
    pool_lunc_ustc = DexPool(config.TERRASWAP_POOL_1_NAME, terra, config.TERRASWAP_POOL_1,
                              lunc, ustc, config.TERRASWAP_COMMISSION_RATE)

    if not config.DRY_RUN:
        balance = terra.get_balance(config.DENOM_LUNC)
        needed = TEST_AMOUNT_ULUNA + config.GAS_RESERVE_ULUNA
        if balance < needed:
            log.error("LUNC balance (%d) looks too low to safely run this test (need at least "
                      "%d: test amount + gas reserve). Aborting.", balance, needed)
            return
        log.warning("DRY_RUN is OFF — about to broadcast 1 REAL 3-leg round-trip: "
                    "%d uluna (1 LUNC) -> LCW -> USTC -> back to LUNC.", TEST_AMOUNT_ULUNA)
        log.warning("This exact route has never been tested live before. "
                    "Starting in 5 seconds — Ctrl+C now to cancel.")
        time.sleep(5)

    current_amount = TEST_AMOUNT_ULUNA
    legs = [
        ("Leg 1: LUNC -> LCW", pool_lcw_lunc, lunc, lcw_token),
        ("Leg 2: LCW -> USTC", pool_lcw_ustc, lcw_token, ustc),
        ("Leg 3: USTC -> LUNC", pool_lunc_ustc, ustc, lunc),
    ]

    for label, pool, asset_in, asset_out in legs:
        log.info("--- %s ---", label)

        if config.DRY_RUN:
            execute_leg(terra, pool.pair_address, asset_in, current_amount)
            log.info("[DRY_RUN] %s simulated only.", label)
            continue

        balance_before = terra.get_asset_balance(asset_out)
        leg_result = execute_leg(terra, pool.pair_address, asset_in, current_amount)
        balance_after = terra.get_asset_balance(asset_out)
        raw_delta = balance_after - balance_before

        # Gas is always paid in LUNC — if this leg's output IS LUNC, the
        # raw balance delta is (swap proceeds - gas fee), not swap
        # proceeds alone. Add the fee back to isolate the true proceeds.
        if asset_out.kind == "native" and asset_out.id == config.DENOM_LUNC:
            received = raw_delta + leg_result.gas_fee_uluna
            log.info("%s: offered %d %s, raw LUNC balance delta %d (gas-contaminated), "
                      "gas paid %d -> true swap proceeds %d",
                      label, current_amount, asset_in, raw_delta, leg_result.gas_fee_uluna, received)
        else:
            received = raw_delta
            log.info("%s: offered %d %s, wallet %s balance went %d -> %d (received %d)",
                      label, current_amount, asset_in, asset_out, balance_before, balance_after, received)

        if received <= 0:
            log.error("%s produced no real balance increase — aborting remaining legs. "
                      "Check the txhash above on an explorer.", label)
            return

        current_amount = received

    if config.DRY_RUN:
        log.info("Smoke test complete (dry run).")
        return

    net = current_amount - TEST_AMOUNT_ULUNA
    log.info("Triangular round-trip complete: started with %d uluna, ended with %d uluna. "
              "Net: %d uluna (%.4f LUNC)", TEST_AMOUNT_ULUNA, current_amount, net, net / 1_000_000)
    log.info("All three legs succeeded, including the shallow LCW/USTC pool and the LCW "
              "transfer tax on both sides. Smoke test complete.")


if __name__ == "__main__":
    main()
