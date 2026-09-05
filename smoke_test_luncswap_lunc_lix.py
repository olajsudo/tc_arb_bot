"""
REAL-FUND smoke test for JUST the LuncSwap LUNC/LIX pool — the one pool
out of the 2026-09-02 LIX/LTK/ELPACO/ROTTI batch that never got a real
confirmed transaction. The 2026-09-02 run of
smoke_test_lix_ltk_elpaco_rotti.py hit a transient DNS resolution error
on this test specifically, before broadcasting anything, so this pool's
own venue mechanics (message shape, fee estimation, event parsing) are
still unconfirmed — even though LIX's own transfer tax is already known
(200bps in / 0bps out, confirmed via the Garuda LUNC/LIX and LTK/LIX
legs that DID complete that run).

Deliberately kept to this ONE pool — LTK, ELPACO, ROTTI, and all the
other 13 pools from that batch already completed real, clean round trips
and don't need re-testing. See config.py's comments above
LIX_TRANSFER_TAX_IN_BPS for the full confirmed picture.

This moves real, small amounts of real funds. It will NOT run:
  - if config.DRY_RUN is True (nothing real would happen)
  - without passing --confirm on the command line

Run: python smoke_test_luncswap_lunc_lix.py --confirm
"""
import sys
import logging

import config
from terra_client import TerraClient
from executor import execute_leg
from smoke_test_new_tokens import _report_gap, TEST_NATIVE_AMOUNT_ULUNA, lunc
from smoke_test_lix_ltk_elpaco_rotti import lix_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("smoke_test_luncswap_lunc_lix")


def test_lix_via_luncswap_round_trip(terra):
    """
    First real transaction ever attempted against this specific pool
    address. LuncSwap.fun is already a trusted venue (see LuncSwap
    JURIS/USDC in smoke_test_new_tokens.py), so this exercises a plain
    Terraswap-family message shape — no Garuda-style hard min_receive
    floor. LIX's own transfer tax is already confirmed elsewhere
    (200bps in / 0bps out) — this test is about confirming THIS pool's
    mechanics, not re-discovering the tax, though a gap here would still
    be worth cross-checking against that number.
    """
    print("\n=== LIX round trip (LuncSwap LUNC/LIX) ===")
    pool_address = config.LUNCSWAP_POOL_LUNC_LIX
    start_lunc = terra.get_balance(config.DENOM_LUNC)
    start_lix = terra.get_cw20_balance(config.LIX_CW20_ADDRESS)
    print(f"  Starting: {start_lunc} uluna, {start_lix} LIX (base units)")

    leg1 = execute_leg(terra, pool_address, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    mid_lix = terra.get_cw20_balance(config.LIX_CW20_ADDRESS)
    actual_received_lix = mid_lix - start_lix
    _report_gap("LUNC -> LIX", leg1.received, actual_received_lix)

    if actual_received_lix <= 0:
        print("  Received 0 LIX — aborting before attempting the return leg.")
        return

    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2 = execute_leg(terra, pool_address, lix_token, actual_received_lix)
    end_lunc = terra.get_balance(config.DENOM_LUNC)
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap("LIX -> LUNC", leg2.received, actual_received_lunc, received_is_native=True)

    print(f"  Round trip: started with {start_lunc} uluna, ended with {end_lunc} uluna "
          f"(2 legs' worth of commission + native stability tax + gas — negative is expected, "
          f"this is a venue-confirmation test, not a profit attempt). If the LUNC->LIX gap here "
          f"disagrees with the already-confirmed 200bps LIX_TRANSFER_TAX_IN_BPS by more than a "
          f"few bps, that's worth understanding before trusting this pool live.")


def main():
    if "--confirm" not in sys.argv:
        print("This script broadcasts a REAL transaction with REAL funds (small amount, but "
              "real). Re-run with --confirm to proceed:\n"
              "  python smoke_test_luncswap_lunc_lix.py --confirm")
        sys.exit(1)

    config.validate()
    if config.DRY_RUN:
        print("config.DRY_RUN is True — this script needs DRY_RUN=False to actually observe a "
              "real swap's result. Set DRY_RUN=false in your .env for this run only, then set "
              "it back before running the main bot again if you don't want live trading.")
        sys.exit(1)

    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)

    print("\nThis run tests ONLY the LuncSwap LUNC/LIX pool — nothing else from the "
          "2026-09-02 batch (LTK, ELPACO, ROTTI, and the other Garuda/Terraswap/Terraport "
          "pools) is touched here; those already completed clean real round trips.")

    try:
        test_lix_via_luncswap_round_trip(terra)
    except Exception as e:
        print(f"\n!!! LuncSwap LUNC/LIX test raised an unhandled error: {e}")
        log.exception("LuncSwap LUNC/LIX test failed")
        sys.exit(1)

    print("\nDone. If a real (>5bps) LIX-side gap showed up on the LUNC->LIX leg and it "
          "disagrees with the existing LIX_TRANSFER_TAX_IN_BPS=200 in config.py, investigate "
          "before trusting this specific pool live — otherwise this just confirms the venue's "
          "mechanics and it's good to go.")


if __name__ == "__main__":
    main()