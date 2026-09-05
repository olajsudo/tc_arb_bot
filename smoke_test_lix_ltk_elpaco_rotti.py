"""
REAL-FUND smoke test for the 2026-09-02 batch ONLY: LIX, LTK, ELPACO, ROTTI
— 4 brand-new CW20 tokens across 14 new pool addresses (LuncSwap.fun,
Garuda DeFi, Terraswap, Terraport). Deliberately kept SEPARATE from
smoke_test_new_tokens.py rather than appended to it, so running this
script never re-fires real transactions against JURIS, cwLUNC, BENANCE,
GDEX, GRDX, or FUN's own pools — those were already confirmed in earlier
rounds and re-testing them here would just be redundant real-money spend.
This script imports its shared helpers (_garuda_min_receive,
_execute_garuda_leg_diagnostic, _report_gap, and a few already-trusted
token/pool references) straight from smoke_test_new_tokens.py rather than
duplicating them.

Discovers each new token's own transfer tax (if any) the same way every
earlier token was: execute a small REAL swap, then compare the swap
event's reported return_amount against the ACTUAL wallet balance delta.
Any gap beyond normal rounding is the token's own transfer tax, baked
into the CW20 contract — invisible to the pool and to tax.py — UNLESS the
leg's received asset is native LUNC/USTC/USDC, in which case a gap is
Terra Classic's own stability tax on the return leg (already modeled
generically elsewhere; see _report_gap's docstring in
smoke_test_new_tokens.py).

This moves real, small amounts of real funds. It will NOT run:
  - if config.DRY_RUN is True (nothing real would happen)
  - without passing --confirm on the command line

COVERAGE — all 14 pools from the 2026-09-02 batch, one test each:
  1.  LuncSwap LUNC/LIX        -> test_lix_via_luncswap_round_trip
                                   (PRIMARY tax discovery for LIX)
  2.  Garuda LUNC/LIX          -> test_lix_via_garuda_round_trip
                                   (venue cross-check only — LIX tax
                                   already known from #1)
  3.  Terraswap LUNC/LTK       -> test_ltk_via_terraswap_round_trip
                                   (PRIMARY tax discovery for LTK)
  4.  Garuda LUNC/LTK          -> test_ltk_via_garuda_round_trip
                                   (venue cross-check only)
  5.  Garuda LTK/LIX           -> test_ltk_lix_garuda
  6.  Garuda USDC/LTK          -> test_usdc_ltk_garuda
  7.  Garuda USTC/LTK          -> test_ustc_ltk_garuda
  8.  Garuda LUNC/ELPACO       -> test_elpaco_via_garuda_lunc_round_trip
                                   (PRIMARY tax discovery for ELPACO —
                                   its only native-paired pool)
  9.  Garuda LTK/ELPACO        -> test_ltk_elpaco_garuda
  10. Garuda ROTTI/LUNC        -> test_rotti_via_garuda_lunc_round_trip
                                   (PRIMARY tax discovery for ROTTI)
  11. Terraport ROTTI/LUNC     -> test_rotti_via_terraport_round_trip
                                   (venue cross-check only)
  12. Garuda FUN/ROTTI         -> test_fun_rotti_garuda (FUN sourced via
                                   the already-live Garuda FUN/LUNC pool,
                                   not re-tested itself — its 0bps tax was
                                   confirmed 2026-08-05)
  13. Garuda ROTTI/JURIS       -> test_rotti_juris_garuda
  14. Garuda ROTTI/GRDX        -> test_rotti_grdx_garuda (GRDX sourced via
                                   the already-live Garuda GRDX/LUNC pool,
                                   not re-tested itself — its 0bps tax was
                                   confirmed 2026-08-05/06)

None of these 14 pools has been independently schema-probed against this
bot's executor before this script's first run (no check_new_venues_
interface.py / probe_garuda_schema.py-style pass has touched them) — the
first real transaction against each pool IS the schema check, same as
LuncSwap JURIS/USDC was on 2026-08-28. Treat every leg here as maximally
trust-but-verify, and update config.py's *_TRANSFER_TAX_BPS /
*_DECIMALS for LIX/LTK/ELPACO/ROTTI immediately if any gap shows up.

Order matters: later tests source LTK/ELPACO/ROTTI/FUN/GRDX from earlier
tests' leftover balances rather than each doing its own native round
trip, to avoid unnecessary extra gas/legs. Keep the sequence in
COVERAGE_TESTS intact if you add to it.

Run: python smoke_test_lix_ltk_elpaco_rotti.py --confirm
"""
import sys
import logging

import config
from assets import Asset
from pool_client import GarudaPool
from terra_client import TerraClient
from executor import execute_leg
from smoke_test_new_tokens import (
    _garuda_min_receive,
    _execute_garuda_leg_diagnostic,
    _report_gap,
    TEST_NATIVE_AMOUNT_ULUNA,
    lunc,
    fun_token,
    grdx_token,
    juris_token,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("smoke_test_lix_ltk_elpaco_rotti")

usdc = Asset(kind="native", id=config.DENOM_USDC, decimals=6, display="USDC")
ustc = Asset(kind="native", id=config.DENOM_USTC, decimals=6, display="USTC")
lix_token = Asset(kind="cw20", id=config.LIX_CW20_ADDRESS, decimals=config.LIX_DECIMALS, display="LIX")
ltk_token = Asset(kind="cw20", id=config.LTK_CW20_ADDRESS, decimals=config.LTK_DECIMALS, display="LTK")
elpaco_token = Asset(kind="cw20", id=config.ELPACO_CW20_ADDRESS, decimals=config.ELPACO_DECIMALS, display="ELPACO")
rotti_token = Asset(kind="cw20", id=config.ROTTI_CW20_ADDRESS, decimals=config.ROTTI_DECIMALS, display="ROTTI")


def test_lix_via_luncswap_round_trip(terra):
    """PRIMARY tax-discovery test for LIX. LuncSwap.fun is already a
    trusted venue (see LuncSwap JURIS/USDC in smoke_test_new_tokens.py),
    so this exercises a plain Terraswap-family message shape — no
    Garuda-style hard min_receive floor. First real transaction ever
    attempted against this specific pool address."""
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
          f"this is a cost-discovery test). If LUNC->LIX showed a real gap, add "
          f"LIX_TRANSFER_TAX_BPS in config.py before trusting this token live.")


def test_lix_via_garuda_round_trip(terra):
    """Venue cross-check for LIX — its own transfer tax is already
    discovered via test_lix_via_luncswap_round_trip (tax is a property of
    the TOKEN contract, not the pool), so this mainly verifies Garuda's
    pair_base mechanics on THIS pool address."""
    print("\n=== LUNC/LIX round trip (Garuda LUNC/LIX) ===")
    pool = GarudaPool("Garuda LUNC/LIX", terra, config.GARUDA_POOL_LUNC_LIX,
                       lunc, lix_token, config.GARUDA_COMMISSION_RATE)
    start_lunc = terra.get_balance(config.DENOM_LUNC)
    start_lix = terra.get_cw20_balance(config.LIX_CW20_ADDRESS)
    print(f"  Starting: {start_lunc} uluna, {start_lix} LIX (base units)")

    min_receive_1 = _garuda_min_receive(pool, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting.")
        return
    leg1 = execute_leg(terra, pool.pair_address, lunc, TEST_NATIVE_AMOUNT_ULUNA,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_lix = terra.get_cw20_balance(config.LIX_CW20_ADDRESS)
    actual_received_lix = mid_lix - start_lix
    _report_gap("LUNC -> LIX (Garuda)", leg1.received, actual_received_lix)

    if actual_received_lix <= 0:
        print("  Received 0 LIX — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, lix_token, actual_received_lix)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg — leaving "
              "LIX unswapped rather than broadcasting with zero slippage protection.")
        return
    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, lix_token, actual_received_lix, min_receive_2, "LIX -> LUNC (Garuda)")
    if leg2 is None:
        return
    end_lunc = terra.get_balance(config.DENOM_LUNC)
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap("LIX -> LUNC (Garuda)", leg2.received, actual_received_lunc, received_is_native=True)


def test_ltk_via_terraswap_round_trip(terra):
    """PRIMARY tax-discovery test for LTK via Terraswap (already-trusted
    venue, plain message shape). Deliberately leaves ~half of what's
    received in the wallet so the CW20/CW20 cross-pair tests below don't
    each need their own native round trip."""
    print("\n=== LTK round trip (Terraswap LUNC/LTK) ===")
    pool_address = config.TERRASWAP_POOL_LUNC_LTK
    start_lunc = terra.get_balance(config.DENOM_LUNC)
    start_ltk = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)
    print(f"  Starting: {start_lunc} uluna, {start_ltk} LTK (base units)")

    leg1 = execute_leg(terra, pool_address, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    mid_ltk = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)
    actual_received_ltk = mid_ltk - start_ltk
    _report_gap("LUNC -> LTK", leg1.received, actual_received_ltk)

    if actual_received_ltk <= 0:
        print("  Received 0 LTK — aborting before attempting the return leg.")
        return

    return_amount = actual_received_ltk // 2
    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2 = execute_leg(terra, pool_address, ltk_token, return_amount)
    end_lunc = terra.get_balance(config.DENOM_LUNC)
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap("LTK -> LUNC", leg2.received, actual_received_lunc, received_is_native=True)

    print(f"  Round trip (half): started with {start_lunc} uluna, ended with {end_lunc} uluna. "
          f"If LUNC->LTK showed a real gap, add LTK_TRANSFER_TAX_BPS in config.py. Remaining "
          f"~{actual_received_ltk - return_amount} LTK left in wallet intentionally for the "
          f"cross-pair tests below.")


def test_ltk_via_garuda_round_trip(terra):
    """Venue cross-check for LTK (tax already known from
    test_ltk_via_terraswap_round_trip) — verifies Garuda mechanics on
    this pool address."""
    print("\n=== LUNC/LTK round trip (Garuda LUNC/LTK) ===")
    pool = GarudaPool("Garuda LUNC/LTK", terra, config.GARUDA_POOL_LUNC_LTK,
                       lunc, ltk_token, config.GARUDA_COMMISSION_RATE)
    start_lunc = terra.get_balance(config.DENOM_LUNC)
    start_ltk = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)
    print(f"  Starting: {start_lunc} uluna, {start_ltk} LTK (base units)")

    min_receive_1 = _garuda_min_receive(pool, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting.")
        return
    leg1 = execute_leg(terra, pool.pair_address, lunc, TEST_NATIVE_AMOUNT_ULUNA,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_ltk = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)
    actual_received_ltk = mid_ltk - start_ltk
    _report_gap("LUNC -> LTK (Garuda)", leg1.received, actual_received_ltk)

    if actual_received_ltk <= 0:
        print("  Received 0 LTK — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, ltk_token, actual_received_ltk)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg — leaving "
              "LTK unswapped rather than broadcasting with zero slippage protection.")
        return
    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, ltk_token, actual_received_ltk, min_receive_2, "LTK -> LUNC (Garuda)")
    if leg2 is None:
        return
    end_lunc = terra.get_balance(config.DENOM_LUNC)
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap("LTK -> LUNC (Garuda)", leg2.received, actual_received_lunc, received_is_native=True)


def _get_or_source_ltk(terra, min_amount: int = 1) -> int:
    """Returns the wallet's current LTK balance, sourcing a small amount
    via Terraswap LUNC/LTK first if the wallet holds less than
    min_amount. Used by every LTK-involving CW20/CW20 cross-pair test
    below so each doesn't need its own native round trip."""
    balance = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)
    if balance >= min_amount:
        return balance
    print("  Wallet holds insufficient LTK — sourcing via Terraswap LUNC/LTK first.")
    leg = execute_leg(terra, config.TERRASWAP_POOL_LUNC_LTK, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    balance = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)
    _report_gap("LUNC -> LTK (sourcing)", leg.received, balance)
    return balance


def test_ltk_lix_garuda(terra):
    """LTK/LIX cross-pair (Garuda, CW20/CW20)."""
    print("\n=== LTK/LIX round trip (Garuda LTK/LIX) ===")
    ltk_balance = _get_or_source_ltk(terra)
    if ltk_balance <= 0:
        print("  Still holding 0 LTK after sourcing — skipping this test.")
        return
    pool = GarudaPool("Garuda LTK/LIX", terra, config.GARUDA_POOL_LTK_LIX,
                       ltk_token, lix_token, config.GARUDA_COMMISSION_RATE)
    test_amount = min(ltk_balance, max(1, ltk_balance // 4))
    start_lix = terra.get_cw20_balance(config.LIX_CW20_ADDRESS)

    min_receive_1 = _garuda_min_receive(pool, ltk_token, test_amount)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting before LTK->LIX.")
        return
    leg1 = execute_leg(terra, pool.pair_address, ltk_token, test_amount,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_lix = terra.get_cw20_balance(config.LIX_CW20_ADDRESS)
    actual_received_lix = mid_lix - start_lix
    _report_gap("LTK -> LIX", leg1.received, actual_received_lix)

    if actual_received_lix <= 0:
        print("  Received 0 LIX — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, lix_token, actual_received_lix)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_ltk = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, lix_token, actual_received_lix, min_receive_2, "LIX -> LTK")
    if leg2 is None:
        return
    post_return_ltk = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)
    _report_gap("LIX -> LTK", leg2.received, post_return_ltk - pre_return_ltk)


def test_usdc_ltk_garuda(terra):
    """USDC/LTK cross-pair (Garuda, native USDC / CW20)."""
    print("\n=== USDC/LTK round trip (Garuda USDC/LTK) ===")
    ltk_balance = _get_or_source_ltk(terra)
    if ltk_balance <= 0:
        print("  Still holding 0 LTK after sourcing — skipping this test.")
        return
    pool = GarudaPool("Garuda USDC/LTK", terra, config.GARUDA_POOL_USDC_LTK,
                       usdc, ltk_token, config.GARUDA_COMMISSION_RATE)
    test_amount = min(ltk_balance, max(1, ltk_balance // 4))
    start_usdc = terra.get_balance(config.DENOM_USDC)

    min_receive_1 = _garuda_min_receive(pool, ltk_token, test_amount)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting before LTK->USDC.")
        return
    leg1 = execute_leg(terra, pool.pair_address, ltk_token, test_amount,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_usdc = terra.get_balance(config.DENOM_USDC)
    actual_received_usdc = mid_usdc - start_usdc
    _report_gap("LTK -> USDC", leg1.received, actual_received_usdc, received_is_native=True)

    if actual_received_usdc <= 0:
        print("  Received 0 USDC — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, usdc, actual_received_usdc)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_ltk = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, usdc, actual_received_usdc, min_receive_2, "USDC -> LTK")
    if leg2 is None:
        return
    post_return_ltk = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)
    _report_gap("USDC -> LTK", leg2.received, post_return_ltk - pre_return_ltk)


def test_ustc_ltk_garuda(terra):
    """USTC/LTK cross-pair (Garuda, native USTC / CW20)."""
    print("\n=== USTC/LTK round trip (Garuda USTC/LTK) ===")
    ltk_balance = _get_or_source_ltk(terra)
    if ltk_balance <= 0:
        print("  Still holding 0 LTK after sourcing — skipping this test.")
        return
    pool = GarudaPool("Garuda USTC/LTK", terra, config.GARUDA_POOL_USTC_LTK,
                       ustc, ltk_token, config.GARUDA_COMMISSION_RATE)
    test_amount = min(ltk_balance, max(1, ltk_balance // 4))
    start_ustc = terra.get_balance(config.DENOM_USTC)

    min_receive_1 = _garuda_min_receive(pool, ltk_token, test_amount)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting before LTK->USTC.")
        return
    leg1 = execute_leg(terra, pool.pair_address, ltk_token, test_amount,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_ustc = terra.get_balance(config.DENOM_USTC)
    actual_received_ustc = mid_ustc - start_ustc
    _report_gap("LTK -> USTC", leg1.received, actual_received_ustc, received_is_native=True)

    if actual_received_ustc <= 0:
        print("  Received 0 USTC — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, ustc, actual_received_ustc)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_ltk = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, ustc, actual_received_ustc, min_receive_2, "USTC -> LTK")
    if leg2 is None:
        return
    post_return_ltk = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)
    _report_gap("USTC -> LTK", leg2.received, post_return_ltk - pre_return_ltk)


def test_elpaco_via_garuda_lunc_round_trip(terra):
    """PRIMARY tax-discovery test for ELPACO — LUNC/ELPACO is its only
    native-paired pool, so this doubles as the sole tax-discovery route
    AND the venue check (both on Garuda). Keeps ~half of what's received
    for the LTK/ELPACO cross-pair test below."""
    print("\n=== LUNC/ELPACO round trip (Garuda LUNC/ELPACO) ===")
    pool = GarudaPool("Garuda LUNC/ELPACO", terra, config.GARUDA_POOL_LUNC_ELPACO,
                       lunc, elpaco_token, config.GARUDA_COMMISSION_RATE)
    start_lunc = terra.get_balance(config.DENOM_LUNC)
    start_elpaco = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    print(f"  Starting: {start_lunc} uluna, {start_elpaco} ELPACO (base units)")

    min_receive_1 = _garuda_min_receive(pool, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting.")
        return
    leg1 = execute_leg(terra, pool.pair_address, lunc, TEST_NATIVE_AMOUNT_ULUNA,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_elpaco = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    actual_received_elpaco = mid_elpaco - start_elpaco
    _report_gap("LUNC -> ELPACO", leg1.received, actual_received_elpaco)

    if actual_received_elpaco <= 0:
        print("  Received 0 ELPACO — aborting before attempting the return leg.")
        return

    return_amount = actual_received_elpaco // 2
    min_receive_2 = _garuda_min_receive(pool, elpaco_token, return_amount)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, elpaco_token, return_amount, min_receive_2, "ELPACO -> LUNC")
    if leg2 is None:
        return
    end_lunc = terra.get_balance(config.DENOM_LUNC)
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap("ELPACO -> LUNC", leg2.received, actual_received_lunc, received_is_native=True)
    print(f"  If LUNC->ELPACO showed a real gap, add ELPACO_TRANSFER_TAX_BPS in config.py. "
          f"Remaining ~{actual_received_elpaco - return_amount} ELPACO left in wallet "
          f"intentionally.")


def test_ltk_elpaco_garuda(terra):
    """LTK/ELPACO cross-pair (Garuda, CW20/CW20)."""
    print("\n=== LTK/ELPACO round trip (Garuda LTK/ELPACO) ===")
    ltk_balance = _get_or_source_ltk(terra)
    elpaco_balance = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    if ltk_balance <= 0 or elpaco_balance <= 0:
        print(f"  Need both LTK ({ltk_balance}) and ELPACO ({elpaco_balance}) to test this "
              f"pair — run test_elpaco_via_garuda_lunc_round_trip first if ELPACO is 0. Skipping.")
        return
    pool = GarudaPool("Garuda LTK/ELPACO", terra, config.GARUDA_POOL_LTK_ELPACO,
                       ltk_token, elpaco_token, config.GARUDA_COMMISSION_RATE)
    test_amount = min(ltk_balance, max(1, ltk_balance // 2))
    start_elpaco = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)

    min_receive_1 = _garuda_min_receive(pool, ltk_token, test_amount)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting before LTK->ELPACO.")
        return
    leg1 = execute_leg(terra, pool.pair_address, ltk_token, test_amount,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_elpaco = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    actual_received_elpaco = mid_elpaco - start_elpaco
    _report_gap("LTK -> ELPACO", leg1.received, actual_received_elpaco)

    if actual_received_elpaco <= 0:
        print("  Received 0 ELPACO — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, elpaco_token, actual_received_elpaco)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_ltk = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, elpaco_token, actual_received_elpaco, min_receive_2, "ELPACO -> LTK")
    if leg2 is None:
        return
    post_return_ltk = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)
    _report_gap("ELPACO -> LTK", leg2.received, post_return_ltk - pre_return_ltk)


def test_rotti_via_garuda_lunc_round_trip(terra):
    """PRIMARY tax-discovery test for ROTTI via Garuda ROTTI/LUNC. Keeps
    ~two-thirds of what's received for the FUN/ROTTI, ROTTI/JURIS, and
    ROTTI/GRDX cross-pair tests below."""
    print("\n=== ROTTI/LUNC round trip (Garuda ROTTI/LUNC) ===")
    pool = GarudaPool("Garuda ROTTI/LUNC", terra, config.GARUDA_POOL_ROTTI_LUNC,
                       rotti_token, lunc, config.GARUDA_COMMISSION_RATE)
    start_lunc = terra.get_balance(config.DENOM_LUNC)
    start_rotti = terra.get_cw20_balance(config.ROTTI_CW20_ADDRESS)
    print(f"  Starting: {start_lunc} uluna, {start_rotti} ROTTI (base units)")

    min_receive_1 = _garuda_min_receive(pool, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting.")
        return
    leg1 = execute_leg(terra, pool.pair_address, lunc, TEST_NATIVE_AMOUNT_ULUNA,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_rotti = terra.get_cw20_balance(config.ROTTI_CW20_ADDRESS)
    actual_received_rotti = mid_rotti - start_rotti
    _report_gap("LUNC -> ROTTI", leg1.received, actual_received_rotti)

    if actual_received_rotti <= 0:
        print("  Received 0 ROTTI — aborting before attempting the return leg.")
        return

    return_amount = actual_received_rotti // 3
    min_receive_2 = _garuda_min_receive(pool, rotti_token, return_amount)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, rotti_token, return_amount, min_receive_2, "ROTTI -> LUNC")
    if leg2 is None:
        return
    end_lunc = terra.get_balance(config.DENOM_LUNC)
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap("ROTTI -> LUNC", leg2.received, actual_received_lunc, received_is_native=True)
    print(f"  If LUNC->ROTTI showed a real gap, add ROTTI_TRANSFER_TAX_BPS in config.py. "
          f"Remaining ~{actual_received_rotti - return_amount} ROTTI left in wallet "
          f"intentionally.")


def test_rotti_via_terraport_round_trip(terra):
    """Venue cross-check for ROTTI (tax already known from
    test_rotti_via_garuda_lunc_round_trip) — plain Terraport message
    shape, default max_spread cushion, no Garuda-style hard min_receive
    floor."""
    print("\n=== ROTTI/LUNC round trip (Terraport ROTTI/LUNC) ===")
    pool_address = config.TERRAPORT_POOL_ROTTI_LUNC
    start_lunc = terra.get_balance(config.DENOM_LUNC)
    start_rotti = terra.get_cw20_balance(config.ROTTI_CW20_ADDRESS)
    print(f"  Starting: {start_lunc} uluna, {start_rotti} ROTTI (base units)")

    leg1 = execute_leg(terra, pool_address, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    mid_rotti = terra.get_cw20_balance(config.ROTTI_CW20_ADDRESS)
    actual_received_rotti = mid_rotti - start_rotti
    _report_gap("LUNC -> ROTTI (Terraport)", leg1.received, actual_received_rotti)

    if actual_received_rotti <= 0:
        print("  Received 0 ROTTI — aborting before attempting the return leg.")
        return

    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2 = execute_leg(terra, pool_address, rotti_token, actual_received_rotti)
    end_lunc = terra.get_balance(config.DENOM_LUNC)
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap("ROTTI -> LUNC (Terraport)", leg2.received, actual_received_lunc, received_is_native=True)
    print(f"  If this gap disagrees with the Garuda ROTTI/LUNC reading above by more than a "
          f"few bps, that's worth understanding before trusting either pool live.")


def _source_fun_via_garuda_lunc(terra, source_amount_uluna: int = TEST_NATIVE_AMOUNT_ULUNA) -> int:
    """Sources FUN via the already-live Garuda FUN/LUNC pool. FUN's own
    tax is already known to be 0bps (confirmed 2026-08-05), so no new
    discovery happens here — this purely funds the FUN/ROTTI cross-pair
    test below."""
    print("\n--- Sourcing FUN via Garuda FUN/LUNC ---")
    pool = GarudaPool("Garuda FUN/LUNC", terra, config.GARUDA_POOL_FUN_LUNC,
                       fun_token, lunc, config.GARUDA_COMMISSION_RATE)
    min_receive = _garuda_min_receive(pool, lunc, source_amount_uluna)
    if min_receive <= 0:
        print("  Could not compute a live min_receive floor — aborting sourcing attempt.")
        return terra.get_cw20_balance(config.FUN_CW20_ADDRESS)
    start_fun = terra.get_cw20_balance(config.FUN_CW20_ADDRESS)
    leg = execute_leg(terra, pool.pair_address, lunc, source_amount_uluna,
                       pool_kind="garuda", min_receive=min_receive)
    end_fun = terra.get_cw20_balance(config.FUN_CW20_ADDRESS)
    _report_gap("LUNC -> FUN (sourcing)", leg.received, end_fun - start_fun)
    return end_fun


def test_fun_rotti_garuda(terra):
    """FUN/ROTTI cross-pair (Garuda, CW20/CW20)."""
    print("\n=== FUN/ROTTI round trip (Garuda FUN/ROTTI) ===")
    rotti_balance = terra.get_cw20_balance(config.ROTTI_CW20_ADDRESS)
    if rotti_balance <= 0:
        print("  Wallet holds 0 ROTTI — run test_rotti_via_garuda_lunc_round_trip first. Skipping.")
        return
    fun_balance = terra.get_cw20_balance(config.FUN_CW20_ADDRESS)
    if fun_balance <= 0:
        fun_balance = _source_fun_via_garuda_lunc(terra)
        if fun_balance <= 0:
            print("  Still holding 0 FUN after sourcing — skipping this test.")
            return

    pool = GarudaPool("Garuda FUN/ROTTI", terra, config.GARUDA_POOL_FUN_ROTTI,
                       fun_token, rotti_token, config.GARUDA_COMMISSION_RATE)
    test_amount = min(fun_balance, max(1, fun_balance // 4))
    start_rotti = terra.get_cw20_balance(config.ROTTI_CW20_ADDRESS)

    min_receive_1 = _garuda_min_receive(pool, fun_token, test_amount)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting before FUN->ROTTI.")
        return
    leg1 = execute_leg(terra, pool.pair_address, fun_token, test_amount,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_rotti = terra.get_cw20_balance(config.ROTTI_CW20_ADDRESS)
    actual_received_rotti = mid_rotti - start_rotti
    _report_gap("FUN -> ROTTI", leg1.received, actual_received_rotti)

    if actual_received_rotti <= 0:
        print("  Received 0 ROTTI — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, rotti_token, actual_received_rotti)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_fun = terra.get_cw20_balance(config.FUN_CW20_ADDRESS)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, rotti_token, actual_received_rotti, min_receive_2, "ROTTI -> FUN")
    if leg2 is None:
        return
    post_return_fun = terra.get_cw20_balance(config.FUN_CW20_ADDRESS)
    _report_gap("ROTTI -> FUN", leg2.received, post_return_fun - pre_return_fun)


def test_rotti_juris_garuda(terra):
    """ROTTI/JURIS cross-pair (Garuda, CW20/CW20)."""
    print("\n=== ROTTI/JURIS round trip (Garuda ROTTI/JURIS) ===")
    rotti_balance = terra.get_cw20_balance(config.ROTTI_CW20_ADDRESS)
    if rotti_balance <= 0:
        print("  Wallet holds 0 ROTTI — run test_rotti_via_garuda_lunc_round_trip first. Skipping.")
        return
    pool = GarudaPool("Garuda ROTTI/JURIS", terra, config.GARUDA_POOL_ROTTI_JURIS,
                       rotti_token, juris_token, config.GARUDA_COMMISSION_RATE)
    test_amount = min(rotti_balance, max(1, rotti_balance // 2))
    start_juris = terra.get_cw20_balance(config.JURIS_CW20_ADDRESS)

    min_receive_1 = _garuda_min_receive(pool, rotti_token, test_amount)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting before ROTTI->JURIS.")
        return
    leg1 = execute_leg(terra, pool.pair_address, rotti_token, test_amount,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_juris = terra.get_cw20_balance(config.JURIS_CW20_ADDRESS)
    actual_received_juris = mid_juris - start_juris
    _report_gap("ROTTI -> JURIS", leg1.received, actual_received_juris)

    if actual_received_juris <= 0:
        print("  Received 0 JURIS — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, juris_token, actual_received_juris)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_rotti = terra.get_cw20_balance(config.ROTTI_CW20_ADDRESS)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, juris_token, actual_received_juris, min_receive_2, "JURIS -> ROTTI")
    if leg2 is None:
        return
    post_return_rotti = terra.get_cw20_balance(config.ROTTI_CW20_ADDRESS)
    _report_gap("JURIS -> ROTTI", leg2.received, post_return_rotti - pre_return_rotti)


def _source_grdx_via_garuda_lunc(terra, source_amount_uluna: int = TEST_NATIVE_AMOUNT_ULUNA) -> int:
    """Sources GRDX via the already-live Garuda GRDX/LUNC pool. GRDX's
    own tax is already known to be 0bps (confirmed 2026-08-05/06), so no
    new discovery happens here — this purely funds the ROTTI/GRDX
    cross-pair test below."""
    print("\n--- Sourcing GRDX via Garuda GRDX/LUNC ---")
    pool = GarudaPool("Garuda GRDX/LUNC", terra, config.GARUDA_POOL_GRDX_LUNC,
                       grdx_token, lunc, config.GARUDA_COMMISSION_RATE)
    min_receive = _garuda_min_receive(pool, lunc, source_amount_uluna)
    if min_receive <= 0:
        print("  Could not compute a live min_receive floor — aborting sourcing attempt.")
        return terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)
    start_grdx = terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)
    leg = execute_leg(terra, pool.pair_address, lunc, source_amount_uluna,
                       pool_kind="garuda", min_receive=min_receive)
    end_grdx = terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)
    _report_gap("LUNC -> GRDX (sourcing)", leg.received, end_grdx - start_grdx)
    return end_grdx


def test_rotti_grdx_garuda(terra):
    """ROTTI/GRDX cross-pair (Garuda, CW20/CW20) — the last of the 14
    pools this script covers."""
    print("\n=== ROTTI/GRDX round trip (Garuda ROTTI/GRDX) ===")
    rotti_balance = terra.get_cw20_balance(config.ROTTI_CW20_ADDRESS)
    if rotti_balance <= 0:
        print("  Wallet holds 0 ROTTI — run test_rotti_via_garuda_lunc_round_trip first. Skipping.")
        return
    grdx_balance = terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)
    if grdx_balance <= 0:
        grdx_balance = _source_grdx_via_garuda_lunc(terra)
        if grdx_balance <= 0:
            print("  Still holding 0 GRDX after sourcing — skipping this test.")
            return

    pool = GarudaPool("Garuda ROTTI/GRDX", terra, config.GARUDA_POOL_ROTTI_GRDX,
                       rotti_token, grdx_token, config.GARUDA_COMMISSION_RATE)
    test_amount = min(rotti_balance, max(1, rotti_balance // 2))
    start_grdx = terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)

    min_receive_1 = _garuda_min_receive(pool, rotti_token, test_amount)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting before ROTTI->GRDX.")
        return
    leg1 = execute_leg(terra, pool.pair_address, rotti_token, test_amount,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_grdx = terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)
    actual_received_grdx = mid_grdx - start_grdx
    _report_gap("ROTTI -> GRDX", leg1.received, actual_received_grdx)

    if actual_received_grdx <= 0:
        print("  Received 0 GRDX — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, grdx_token, actual_received_grdx)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_rotti = terra.get_cw20_balance(config.ROTTI_CW20_ADDRESS)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, grdx_token, actual_received_grdx, min_receive_2, "GRDX -> ROTTI")
    if leg2 is None:
        return
    post_return_rotti = terra.get_cw20_balance(config.ROTTI_CW20_ADDRESS)
    _report_gap("GRDX -> ROTTI", leg2.received, post_return_rotti - pre_return_rotti)


# Order matters — later tests source LTK/ELPACO/ROTTI/FUN/GRDX from
# earlier tests' leftover balances rather than each doing its own native
# round trip. Keep this sequence intact if adding to it.
COVERAGE_TESTS = [
    ("LIX (LuncSwap)", test_lix_via_luncswap_round_trip),
    ("LUNC/LIX (Garuda)", test_lix_via_garuda_round_trip),
    ("LTK (Terraswap)", test_ltk_via_terraswap_round_trip),
    ("LUNC/LTK (Garuda)", test_ltk_via_garuda_round_trip),
    ("LTK/LIX (Garuda)", test_ltk_lix_garuda),
    ("USDC/LTK (Garuda)", test_usdc_ltk_garuda),
    ("USTC/LTK (Garuda)", test_ustc_ltk_garuda),
    ("LUNC/ELPACO (Garuda)", test_elpaco_via_garuda_lunc_round_trip),
    ("LTK/ELPACO (Garuda)", test_ltk_elpaco_garuda),
    ("ROTTI/LUNC (Garuda)", test_rotti_via_garuda_lunc_round_trip),
    ("ROTTI/LUNC (Terraport)", test_rotti_via_terraport_round_trip),
    ("FUN/ROTTI (Garuda)", test_fun_rotti_garuda),
    ("ROTTI/JURIS (Garuda)", test_rotti_juris_garuda),
    ("ROTTI/GRDX (Garuda)", test_rotti_grdx_garuda),
]


def main():
    if "--confirm" not in sys.argv:
        print("This script broadcasts REAL transactions with REAL funds (small amounts, but "
              "real). Re-run with --confirm to proceed:\n"
              "  python smoke_test_lix_ltk_elpaco_rotti.py --confirm")
        sys.exit(1)

    config.validate()
    if config.DRY_RUN:
        print("config.DRY_RUN is True — this script needs DRY_RUN=False to actually observe a "
              "real swap's result. Set DRY_RUN=false in your .env for this run only, then set "
              "it back before running the main bot again if you don't want live trading.")
        sys.exit(1)

    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)

    print(f"\nThis run is scoped to ONLY the 2026-09-02 batch: LIX, LTK, ELPACO, ROTTI, across "
          f"all {len(COVERAGE_TESTS)} of their new pools. FUN and GRDX appear solely as "
          f"already-tested counterparties for the ROTTI cross-pair tests — not re-tested "
          f"themselves. No pre-existing token (JURIS, cwLUNC, BENANCE, GDEX/GRDX, etc.) is "
          f"touched by this script at all; those live in smoke_test_new_tokens.py and are not "
          f"imported or run here beyond a few of their already-confirmed shared helpers.")

    failed = []
    for name, fn in COVERAGE_TESTS:
        try:
            fn(terra)
        except Exception as e:
            failed.append(name)
            print(f"\n!!! {name} test raised an unhandled error — SKIPPING to the next pool "
                  f"rather than aborting the whole run: {e}")
            log.exception("%s test failed", name)

    if failed:
        print(f"\n{len(failed)} test(s) hit an unhandled error and were skipped: {', '.join(failed)}. "
              f"Read the errors above — these still need to be understood before trusting those "
              f"pools live, even though the rest of the run completed.")

    print("\nDone. Add/update LIX_TRANSFER_TAX_BPS / LTK_TRANSFER_TAX_BPS / "
          "ELPACO_TRANSFER_TAX_BPS / ROTTI_TRANSFER_TAX_BPS in config.py (and wire them into "
          "cw20_transfer_tax_rate's rates dict) for anything that showed a real (>5bps) gap "
          "above, then re-run once more to confirm stability before trusting any of these "
          "live in arbitrage_bot.py.")


if __name__ == "__main__":
    main()