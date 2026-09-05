"""
REAL-FUND smoke test for the 6 new CW20 tokens added 2026-08-04 (cwLUNC,
cwUSTC, BENANCE, GDEX, GRDX, FUN) — discovers each token's own transfer
tax (if any) the same way LCW's 5%, ASTRO's 0.50%, and REV's 1.00% were
originally found: execute a small REAL swap, then compare the swap
event's reported return_amount against the ACTUAL wallet balance delta.
Any gap beyond normal rounding is the token's own transfer tax, baked
into the CW20 contract, invisible to the pool and to tax.py.

This moves real, small amounts of real funds. It will NOT run:
  - if config.DRY_RUN is True (nothing real would happen, so there'd be
    nothing to discover — this script exists specifically to observe a
    REAL swap's real result)
  - without passing --confirm on the command line

ROUTING — UPDATED 2026-08-05 after check_new_venues_interface.py and
probe_garuda_schema.py both confirmed the Garuda `pair_base` schema
(executor.build_swap_msg_garuda) and all 5 Garuda pools showed PASS:

  cwLUNC  -> testable now, via Terraswap cwLUNC/LUNC (native LUNC leg)
  BENANCE -> testable now, via Garuda BENANCE/LUNC (native LUNC leg,
             CONFIRMED schema — this is the first real-money test of the
             Garuda venue for this bot, uses min_receive slippage
             protection computed live, same tolerance as the main bot's
             SPREAD_TOLERANCE_BPS)
  GDEX    -> testable now even from a ZERO starting balance: sourced via
             Garuda GDEX/LUNC (native leg) if the wallet doesn't already
             hold some — previously blocked entirely since Terraport
             GDEX/GRDX (CW20/CW20) had no enabled native-paired route to
             acquire GDEX from scratch. That gap is now closed.
  GRDX    -> same as GDEX, tested via the same GDEX<->GRDX round trip
  FUN     -> testable now: GDEX is sourced the same way (Garuda GDEX/LUNC)
             then swapped for FUN via Garuda FUN/GDEX (CW20/CW20,
             CONFIRMED schema)
  cwUSTC  -> STILL BLOCKED: only WESO cwLUNC/cwUSTC exists (no native
             cwUSTC/USTC pairing found yet), and it's a pegged pair
             (wrapped vs. underlying) that hasn't had a curve-type check
             the way ampLUNC/LUNC needed — see arbitrage_bot.py's WESO
             section. Not tested here until that's resolved.

Garuda legs in this script use execute_leg(..., pool_kind="garuda",
min_receive=...) — min_receive is computed live from the pool's own
current reserves via amm_math.simulate_swap, the same math graph.
compute_leg_execution_params uses for the main bot, since Garuda's
pair_base contract has no max_spread/belief_price field to fall back on
(see executor.build_swap_msg_garuda's docstring) — min_receive is the
ONLY on-chain slippage protection it accepts.

Run: python smoke_test_new_tokens.py --confirm
"""
import sys
import logging
from decimal import Decimal

import config
import tax as tax_module
from assets import Asset
from amm_math import simulate_swap
from pool_client import GarudaPool
from terra_client import TerraClient
from executor import execute_leg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("smoke_test_new_tokens")

TEST_NATIVE_AMOUNT_ULUNA = 2_000_000  # 2 LUNC — matches the REV precedent's test size
GARUDA_MIN_RECEIVE_TOLERANCE_BPS = getattr(config, "SPREAD_TOLERANCE_BPS", 300)  # same
# real-world movement buffer the main bot grants Terraswap-family legs via
# max_spread — expressed as an absolute floor here since Garuda has no
# spread field. Falls back to 300bps if config doesn't define this (it
# does, via SPREAD_TOLERANCE_BPS — kept as a getattr for safety since this
# script's only job right now is observing real swaps, not depending on a
# config name staying exactly this).

# ADDED 2026-08-05, round 2: the BENANCE return leg (BENANCE->LUNC, a CW20
# offer via Garuda's Send-hook) reverted with "Insufficient return amount"
# even AFTER _garuda_min_receive started tax-adjusting the offer by the
# CONFIRMED 500bps found on the LUNC->BENANCE (incoming) leg. That means
# the outgoing-direction tax is either higher than 500bps or genuinely
# asymmetric (cwLUNC already showed 0bps one way / 150bps the other way —
# BENANCE may be the same shape, just worse). Garuda's min_receive is a
# hard on-chain floor with none of Terraswap-family's built-in 2%
# max_spread cushion, so a still-UNVERIFIED CW20-outgoing tax can revert
# before this script ever gets to observe and report the real gap.
# DISCOVERY_TOLERANCE_BPS is a much wider buffer used ONLY for a CW20
# offer_asset in THIS diagnostic script — it exists purely so an unknown
# real-world tax doesn't hard-revert the transaction before its size can
# be measured. This has NO effect on the live bot's actual trading
# tolerance (graph.compute_leg_execution_params / SPREAD_TOLERANCE_BPS
# are untouched) — it only lets this one-off smoke test survive long
# enough to report the real number, which you should then hardcode as
# the appropriate _TRANSFER_TAX_BPS in config.py before trusting the
# live bot with this token.
DISCOVERY_TOLERANCE_BPS = 3000  # 30% — generous on purpose; see comment above

lunc = Asset(kind="native", id=config.DENOM_LUNC, decimals=6, display="LUNC")
cwlunc_token = Asset(kind="cw20", id=config.CWLUNC_CW20_ADDRESS, decimals=config.CWLUNC_DECIMALS, display="cwLUNC")
gdex_token = Asset(kind="cw20", id=config.GDEX_CW20_ADDRESS, decimals=config.GDEX_DECIMALS, display="GDEX")
grdx_token = Asset(kind="cw20", id=config.GRDX_CW20_ADDRESS, decimals=config.GRDX_DECIMALS, display="GRDX")
benance_token = Asset(kind="cw20", id=config.BENANCE_CW20_ADDRESS, decimals=config.BENANCE_DECIMALS, display="BENANCE")
fun_token = Asset(kind="cw20", id=config.FUN_CW20_ADDRESS, decimals=config.FUN_DECIMALS, display="FUN")
juris_token = Asset(kind="cw20", id=config.JURIS_CW20_ADDRESS, decimals=config.JURIS_DECIMALS, display="JURIS")


def _garuda_min_receive(pool: GarudaPool, offer_asset: Asset, offer_amount: int,
                         tolerance_bps: int = None) -> int:
    """
    Live min_receive floor for a Garuda leg — queries this pool's CURRENT
    reserves (not a stale snapshot) and runs amm_math.simulate_swap, the
    same xyk math graph.py uses, then floors the result by tolerance_bps
    for real movement between this query and the broadcast. Returns 0
    (accept anything) if reserves can't be read, which callers should
    treat as a reason to abort rather than proceed with zero protection.

    tolerance_bps defaults to None, which now means "pick automatically":
    DISCOVERY_TOLERANCE_BPS for a CW20 offer_asset (its own transfer tax
    on the outgoing/Send side is exactly what this script is often still
    trying to discover — see DISCOVERY_TOLERANCE_BPS's comment above),
    or the tighter GARUDA_MIN_RECEIVE_TOLERANCE_BPS for a native offer
    (whose tax — Terra Classic's stability tax — tax_module already
    fetches live and correctly, so no wide discovery buffer is needed
    there). A caller can still pass an explicit value to override this.

    FIXED 2026-08-05: previously ran simulate_swap on the raw offer_amount
    with no tax adjustment — for a CW20 offer_asset with its own transfer
    tax (e.g. BENANCE, confirmed 500 bps 2026-08-05), the pool actually
    RECEIVES less than offer_amount, so the real return lands under the
    floor this computed and the contract reverts with "Insufficient
    return amount" (exactly what happened on the first BENANCE->LUNC
    return leg). Now mirrors graph.simulate_cycle's taxed_offer step —
    apply tax_module.calculate_tax on the offer side first, same as the
    real bot's cycle simulation already does.
    """
    state = pool.get_state()
    reserve_in = state.reserves.get(offer_asset.key())
    other_asset = pool.other_asset(offer_asset)
    reserve_out = state.reserves.get(other_asset.key())
    if not reserve_in or not reserve_out:
        log.warning("%s: could not read reserves for min_receive calc — treating as 0 "
                    "(NO slippage protection). Caller should abort instead of trading blind.",
                    pool.name)
        return 0
    if tolerance_bps is None:
        tolerance_bps = (DISCOVERY_TOLERANCE_BPS if offer_asset.kind == "cw20"
                          else GARUDA_MIN_RECEIVE_TOLERANCE_BPS)
    taxed_offer = offer_amount - tax_module.calculate_tax(offer_amount, offer_asset, direction="out")
    result = simulate_swap(taxed_offer, reserve_in, reserve_out, state.commission_rate)
    min_receive = int(Decimal(result.return_amount)
                       * (Decimal(10000) - Decimal(tolerance_bps)) / Decimal(10000))
    return max(0, min_receive)


def _execute_garuda_leg_diagnostic(terra, pool: GarudaPool, offer_asset: Asset,
                                    offer_amount: int, min_receive: int, leg_label: str):
    """
    Wraps execute_leg for a Garuda leg with a one-time diagnostic retry if
    the protected attempt reverts on-chain. ADDED 2026-08-05 (round 3)
    after the BENANCE->LUNC return leg reverted with "Insufficient return
    amount" even at the wide DISCOVERY_TOLERANCE_BPS (30%) floor — Garuda's
    min_receive is a hard on-chain floor with none of Terraswap-family's
    built-in max_spread cushion, so an unknown, possibly-asymmetric,
    possibly-larger-than-expected outgoing CW20 tax (or an outright
    sell-side restriction) can revert before this script ever gets to
    observe and report the real number.

    On a revert, retries EXACTLY ONCE with min_receive=1 (accept whatever
    comes back) purely to observe the actual result — this carries ZERO
    real slippage protection and must never be used for anything but this
    one-off diagnostic. It's safe to do here because the first attempt's
    revert means nothing was actually transferred (a reverted tx is a
    no-op on-chain besides its own gas cost), so this isn't risking a
    second real position on top of the first — just gas for one more tx.

    Returns (leg_result_or_None, was_diagnostic_retry: bool). A None
    result means even the min_receive=1 retry failed — read the raw error
    printed here, since that's no longer a slippage problem (nothing left
    to loosen) and points at something more fundamental (e.g. a sell-side
    restriction rather than a plain tax).
    """
    try:
        result = execute_leg(terra, pool.pair_address, offer_asset, offer_amount,
                              pool_kind="garuda", min_receive=min_receive)
        return result, False
    except Exception as e:
        print(f"  {leg_label}: protected attempt (min_receive={min_receive}) reverted on-chain: {e}")
        print(f"  {leg_label}: retrying ONCE with min_receive=1 (NO slippage protection — "
              f"diagnostic only, purely to observe the real result) ...")
        try:
            result = execute_leg(terra, pool.pair_address, offer_asset, offer_amount,
                                  pool_kind="garuda", min_receive=1)
            print(f"  {leg_label}: diagnostic retry succeeded — the gap below reflects a bigger "
                  f"(or differently-shaped) tax than DISCOVERY_TOLERANCE_BPS assumed. Do NOT trust "
                  f"this token live until the real number is hardcoded and re-confirmed.")
            return result, True
        except Exception as e2:
            print(f"  {leg_label}: diagnostic retry ALSO reverted even with zero slippage "
                  f"protection: {e2}")
            print(f"  {leg_label}: this points at something beyond a plain transfer tax (e.g. a "
                  f"sell-side restriction/honeypot-style block) — do not trade this token or "
                  f"direction until this is understood outside this script.")
            return None, True


def _report_gap(label, event_amount, actual_delta, received_is_native: bool = False):
    """
    received_is_native flags a leg where the asset actually landing in the
    wallet is a NATIVE coin (e.g. any "X -> LUNC" leg) rather than a CW20.
    CORRECTED 2026-08-05 (round 5): a real run showed ~150bps gaps on
    EVERY X->LUNC leg tested (cwLUNC, BENANCE, JURIS, GRDX — 4 unrelated
    tokens, 3 venues) — a CW20 contract has no way to tax a native coin,
    so a gap here can only be Terra Classic's own native stability tax on
    the LUNC being sent back, which graph.py ALREADY models generically
    (tax.calculate_tax's native branch fires on edge.asset_out whenever
    it's LUNC, regardless of the CW20 on the other side). Treating that
    as a NEW per-token tax and hardcoding it as a directional CW20 rate
    would double-count it. A gap on a CW20-received leg (received_is_native
    =False, the default) has no such explanation available and IS a real,
    token-specific finding worth a config change — see BENANCE's confirmed
    500bps buy-tax.
    """
    if event_amount <= 0:
        print(f"  {label}: swap event reported 0 — can't compute a gap.")
        return
    gap = event_amount - actual_delta
    gap_bps = (gap / event_amount) * 10000
    print(f"  {label}: event return_amount={event_amount}, actual balance delta={actual_delta}, "
          f"gap={gap} ({gap_bps:.2f} bps)")
    if abs(gap_bps) < 5:
        print(f"    -> looks like normal rounding noise, not a real transfer tax.")
    elif received_is_native:
        print(f"    -> looks like Terra Classic's NATIVE stability tax on the returned LUNC "
              f"(~{gap_bps/100:.2f}%, currently {getattr(config, 'TAX_RATE_FAILSAFE_DEFAULT', '?')} "
              f"failsafe / fetched live via tax.get_tax_rate()) — this is ALREADY modeled "
              f"generically in graph.py (fires on any native asset_out, independent of the "
              f"CW20 on the other side of the trade). Do NOT set a new CW20 _TRANSFER_TAX_BPS "
              f"for this — that would double-count the same tax. Only worth investigating "
              f"further if this drifts noticeably from tax.get_tax_rate()'s current live value.")
    else:
        print(f"    -> looks like a REAL transfer tax (~{gap_bps/100:.2f}%). "
              f"Set the corresponding _TRANSFER_TAX_BPS in config.py to {round(gap_bps)} "
              f"and re-run this test once more to confirm it's stable before trusting it live.")


def test_cwlunc_round_trip(terra):
    print("\n=== cwLUNC round trip (Terraswap cwLUNC/LUNC) ===")
    pool_address = config.TERRASWAP_POOL_CWLUNC_LUNC
    start_lunc = terra.get_balance(config.DENOM_LUNC)
    start_cwlunc = terra.get_cw20_balance(config.CWLUNC_CW20_ADDRESS)
    print(f"  Starting: {start_lunc} uluna, {start_cwlunc} cwLUNC (base units)")

    leg1 = execute_leg(terra, pool_address, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    mid_cwlunc = terra.get_cw20_balance(config.CWLUNC_CW20_ADDRESS)
    actual_received_cwlunc = mid_cwlunc - start_cwlunc
    _report_gap("LUNC -> cwLUNC", leg1.received, actual_received_cwlunc)

    if actual_received_cwlunc <= 0:
        print("  Received 0 cwLUNC — aborting before attempting the return leg.")
        return

    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2 = execute_leg(terra, pool_address, cwlunc_token, actual_received_cwlunc)
    end_lunc = terra.get_balance(config.DENOM_LUNC)
    # actual LUNC gained on the return leg, net of that leg's own gas fee
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap("cwLUNC -> LUNC", leg2.received, actual_received_lunc, received_is_native=True)

    print(f"  Round trip: started with {start_lunc} uluna, ended with {end_lunc} uluna "
          f"(difference is 2 legs' worth of AMM commission + native stability tax + gas — "
          f"expected to be negative, this is a cost-discovery test, not a profit attempt).")


def test_gdex_grdx(terra):
    print("\n=== GDEX/GRDX (Terraport GDEX/GRDX) ===")
    pool_address = config.TERRAPORT_POOL_GDEX_GRDX
    gdex_balance = terra.get_cw20_balance(config.GDEX_CW20_ADDRESS)
    grdx_balance = terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)
    print(f"  Current balances: {gdex_balance} GDEX, {grdx_balance} GRDX (base units)")

    if gdex_balance <= 0 and grdx_balance <= 0:
        gdex_balance = source_gdex_via_garuda(terra)
        if gdex_balance <= 0:
            print("  Still holding 0 GDEX after the sourcing attempt above — skipping this test.")
            return

    if gdex_balance > 0:
        test_amount = min(gdex_balance, max(1, gdex_balance // 10))  # small slice, not the whole balance
        pre = terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)
        leg = execute_leg(terra, pool_address, gdex_token, test_amount)
        post = terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)
        _report_gap("GDEX -> GRDX", leg.received, post - pre)
    else:
        test_amount = min(grdx_balance, max(1, grdx_balance // 10))
        pre = terra.get_cw20_balance(config.GDEX_CW20_ADDRESS)
        leg = execute_leg(terra, pool_address, grdx_token, test_amount)
        post = terra.get_cw20_balance(config.GDEX_CW20_ADDRESS)
        _report_gap("GRDX -> GDEX", leg.received, post - pre)


def test_benance_round_trip(terra):
    """
    First real-money test of the Garuda venue for this bot. Round trip
    LUNC -> BENANCE -> LUNC via Garuda BENANCE/LUNC (native leg both
    directions), using the CONFIRMED pair_base schema (executor.
    build_swap_msg_garuda) and a live-computed min_receive floor on both
    legs — see _garuda_min_receive's docstring for why that's the only
    slippage protection available here, unlike the Terraswap-family tests
    above.
    """
    print("\n=== BENANCE round trip (Garuda BENANCE/LUNC) ===")
    pool = GarudaPool("Garuda BENANCE/LUNC", terra, config.GARUDA_POOL_BENANCE_LUNC,
                       benance_token, lunc, config.GARUDA_COMMISSION_RATE)
    start_lunc = terra.get_balance(config.DENOM_LUNC)
    start_benance = terra.get_cw20_balance(config.BENANCE_CW20_ADDRESS)
    print(f"  Starting: {start_lunc} uluna, {start_benance} BENANCE (base units)")

    min_receive_1 = _garuda_min_receive(pool, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor (see warning above) — aborting "
              "before risking a real broadcast with zero slippage protection.")
        return
    leg1 = execute_leg(terra, pool.pair_address, lunc, TEST_NATIVE_AMOUNT_ULUNA,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_benance = terra.get_cw20_balance(config.BENANCE_CW20_ADDRESS)
    actual_received_benance = mid_benance - start_benance
    _report_gap("LUNC -> BENANCE", leg1.received, actual_received_benance)

    if actual_received_benance <= 0:
        print("  Received 0 BENANCE — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, benance_token, actual_received_benance)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg — leaving "
              "BENANCE unswapped rather than broadcasting with zero slippage protection. "
              "Re-run this script to retry the return leg.")
        return
    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, benance_token, actual_received_benance, min_receive_2, "BENANCE -> LUNC")
    if leg2 is None:
        return
    end_lunc = terra.get_balance(config.DENOM_LUNC)
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap("BENANCE -> LUNC", leg2.received, actual_received_lunc, received_is_native=True)

    print(f"  Round trip: started with {start_lunc} uluna, ended with {end_lunc} uluna "
          f"(difference is 2 legs' worth of AMM commission + native stability tax + gas — "
          f"expected to be negative, this is a cost-discovery test, not a profit attempt).")


def test_grdx_lunc_round_trip(terra):
    """
    ADDED 2026-08-05 (round 4). Garuda GRDX/LUNC just showed PASS on
    check_new_venues_interface.py's zero-cost schema/reserve check — this
    is the mandatory real-fund follow-up before it can be trusted, same
    process every other Garuda pool went through. Round trip LUNC ->
    GRDX -> LUNC directly via this new pool (separate from the existing
    GDEX/GRDX and GDEX/LUNC-sourcing tests — this is GRDX's OWN native
    pairing, not sourced through GDEX). Uses the diagnostic-retry wrapper
    on the return leg since Garuda's min_receive is a hard on-chain floor
    with no built-in cushion, same risk BENANCE's return leg hit.
    """
    print("\n=== GRDX/LUNC round trip (Garuda GRDX/LUNC) ===")
    pool = GarudaPool("Garuda GRDX/LUNC", terra, config.GARUDA_POOL_GRDX_LUNC,
                       grdx_token, lunc, config.GARUDA_COMMISSION_RATE)
    start_lunc = terra.get_balance(config.DENOM_LUNC)
    start_grdx = terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)
    print(f"  Starting: {start_lunc} uluna, {start_grdx} GRDX (base units)")

    min_receive_1 = _garuda_min_receive(pool, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor (see warning above) — aborting "
              "before risking a real broadcast with zero slippage protection.")
        return
    leg1 = execute_leg(terra, pool.pair_address, lunc, TEST_NATIVE_AMOUNT_ULUNA,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_grdx = terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)
    actual_received_grdx = mid_grdx - start_grdx
    _report_gap("LUNC -> GRDX", leg1.received, actual_received_grdx)

    if actual_received_grdx <= 0:
        print("  Received 0 GRDX — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, grdx_token, actual_received_grdx)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg — leaving "
              "GRDX unswapped rather than broadcasting with zero slippage protection. "
              "Re-run this script to retry the return leg.")
        return
    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, grdx_token, actual_received_grdx, min_receive_2, "GRDX -> LUNC")
    if leg2 is None:
        return
    end_lunc = terra.get_balance(config.DENOM_LUNC)
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap("GRDX -> LUNC", leg2.received, actual_received_lunc, received_is_native=True)

    print(f"  Round trip: started with {start_lunc} uluna, ended with {end_lunc} uluna "
          f"(difference is 2 legs' worth of AMM commission + native stability tax + gas — "
          f"expected to be negative, this is a cost-discovery test, not a profit attempt). "
          f"GDEX/GRDX and GDEX/LUNC already showed 0bps on every direction tested — if this "
          f"pool shows a real gap instead, that's new information specific to GRDX's native "
          f"pairing, not a contradiction of the earlier readings.")


def source_gdex_via_garuda(terra, source_amount_uluna: int = TEST_NATIVE_AMOUNT_ULUNA) -> int:
    """
    Swaps a small amount of LUNC for GDEX via Garuda GDEX/LUNC (native
    leg, CONFIRMED schema) — closes the "no enabled native-paired route
    to acquire GDEX from scratch" gap that used to block test_gdex_grdx
    and test_fun_via_gdex from a zero starting balance. Returns the
    wallet's GDEX balance after the swap (0 if it failed or received 0).
    """
    print("\n--- Sourcing GDEX via Garuda GDEX/LUNC (wallet holds neither GDEX nor GRDX) ---")
    pool = GarudaPool("Garuda GDEX/LUNC", terra, config.GARUDA_POOL_GDEX_LUNC,
                       gdex_token, lunc, config.GARUDA_COMMISSION_RATE)
    min_receive = _garuda_min_receive(pool, lunc, source_amount_uluna)
    if min_receive <= 0:
        print("  Could not compute a live min_receive floor — aborting sourcing attempt.")
        return 0
    start_gdex = terra.get_cw20_balance(config.GDEX_CW20_ADDRESS)
    leg = execute_leg(terra, pool.pair_address, lunc, source_amount_uluna,
                       pool_kind="garuda", min_receive=min_receive)
    end_gdex = terra.get_cw20_balance(config.GDEX_CW20_ADDRESS)
    _report_gap("LUNC -> GDEX (sourcing)", leg.received, end_gdex - start_gdex)
    return end_gdex


def test_fun_via_gdex(terra):
    """
    FUN/GDEX round trip via Garuda FUN/GDEX (CW20/CW20, CONFIRMED
    schema). Sources GDEX first via source_gdex_via_garuda if the wallet
    doesn't already hold enough — GDEX itself isn't the asset under test
    here, so this doesn't re-report its gap (test_gdex_grdx/
    source_gdex_via_garuda already do).
    """
    print("\n=== FUN round trip (Garuda FUN/GDEX) ===")
    gdex_balance = terra.get_cw20_balance(config.GDEX_CW20_ADDRESS)
    if gdex_balance <= 0:
        gdex_balance = source_gdex_via_garuda(terra)
        if gdex_balance <= 0:
            print("  Still holding 0 GDEX after the sourcing attempt above — skipping this test.")
            return

    pool = GarudaPool("Garuda FUN/GDEX", terra, config.GARUDA_POOL_FUN_GDEX,
                       fun_token, gdex_token, config.GARUDA_COMMISSION_RATE)
    test_amount = min(gdex_balance, max(1, gdex_balance // 10))
    start_fun = terra.get_cw20_balance(config.FUN_CW20_ADDRESS)

    min_receive_1 = _garuda_min_receive(pool, gdex_token, test_amount)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting before the GDEX->FUN leg.")
        return
    leg1 = execute_leg(terra, pool.pair_address, gdex_token, test_amount,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_fun = terra.get_cw20_balance(config.FUN_CW20_ADDRESS)
    actual_received_fun = mid_fun - start_fun
    _report_gap("GDEX -> FUN", leg1.received, actual_received_fun)

    if actual_received_fun <= 0:
        print("  Received 0 FUN — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, fun_token, actual_received_fun)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg — leaving "
              "FUN unswapped rather than broadcasting with zero slippage protection.")
        return
    pre_return_gdex = terra.get_cw20_balance(config.GDEX_CW20_ADDRESS)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, fun_token, actual_received_fun, min_receive_2, "FUN -> GDEX")
    if leg2 is None:
        return
    post_return_gdex = terra.get_cw20_balance(config.GDEX_CW20_ADDRESS)
    _report_gap("FUN -> GDEX", leg2.received, post_return_gdex - pre_return_gdex)


def test_juris_round_trip(terra):
    """
    ADDED 2026-08-05 (round 4): JURIS has been trading LIVE in
    arbitrage_bot.py's pool list since 2026-07-14 (Terraport JURIS/LUNC,
    Terraport JURIS/TERRA) and, more recently, WESO JURIS/cwLUNC and
    Garuda BENANCE/JURIS — FOUR live pools — on nothing more than
    config.JURIS_TRANSFER_TAX_BPS's own "UNVERIFIED — assumed 0" comment.
    Unlike cwLUNC/BENANCE (discovered specifically because they were NEW
    and untrusted), JURIS's own transfer tax was simply never empirically
    checked the way LCW/ASTRO/REV were, despite already being live. This
    closes that gap using the same real-swap-vs-actual-balance method,
    via Terraport (an already-trusted venue, plain Terraswap-family
    message shape, default max_spread cushion — no Garuda-style hard
    min_receive floor risk).
    """
    print("\n=== JURIS round trip (Terraport JURIS/LUNC) ===")
    pool_address = config.TERRAPORT_POOL_JURIS_LUNC
    start_lunc = terra.get_balance(config.DENOM_LUNC)
    start_juris = terra.get_cw20_balance(config.JURIS_CW20_ADDRESS)
    print(f"  Starting: {start_lunc} uluna, {start_juris} JURIS (base units)")

    leg1 = execute_leg(terra, pool_address, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    mid_juris = terra.get_cw20_balance(config.JURIS_CW20_ADDRESS)
    actual_received_juris = mid_juris - start_juris
    _report_gap("LUNC -> JURIS", leg1.received, actual_received_juris)

    if actual_received_juris <= 0:
        print("  Received 0 JURIS — aborting before attempting the return leg.")
        return

    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2 = execute_leg(terra, pool_address, juris_token, actual_received_juris)
    end_lunc = terra.get_balance(config.DENOM_LUNC)
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap("JURIS -> LUNC", leg2.received, actual_received_lunc, received_is_native=True)

    print(f"  Round trip: started with {start_lunc} uluna, ended with {end_lunc} uluna "
          f"(difference is 2 legs' worth of AMM commission + native stability tax + gas — "
          f"expected to be negative, this is a cost-discovery test, not a profit attempt). "
          f"If either gap above showed a real (>5bps) tax, JURIS has been mispriced on "
          f"EVERY live cycle touching it since 2026-07-14 — treat this as high priority "
          f"to fix in config.py regardless of what else this run found.")


def test_juris_lunc_garuda_round_trip(terra):
    """
    ADDED 2026-08-06 (round 6). Garuda JURIS/LUNC just showed PASS on
    check_new_venues_interface.py — same mandatory real-fund follow-up
    every other Garuda pool got before going live.

    Note: JURIS's own CW20 transfer tax is ALREADY known (0bps either
    direction, confirmed via the Terraport JURIS/LUNC round trip in
    test_juris_round_trip) — tax is a property of the TOKEN contract, not
    the pool, so it doesn't need re-discovering here. What this test
    actually verifies is the Garuda VENUE mechanics on this specific new
    pool address: GarudaPool's {"pool":{}} reserve parsing and the
    min_receive floor computation, both unverified for this address until
    a real swap actually clears. Worth doing carefully — this pool's
    reserves are enormous (reserve1 ~1.6e15 in the interface check) even
    relative to the other Garuda pools, closer in scale to BENANCE/LUNC
    than GDEX/LUNC, so this also exercises that scale case again.
    """
    print("\n=== JURIS/LUNC round trip (Garuda JURIS/LUNC) ===")
    pool = GarudaPool("Garuda JURIS/LUNC", terra, config.GARUDA_POOL_JURIS_LUNC,
                       juris_token, lunc, config.GARUDA_COMMISSION_RATE)
    start_lunc = terra.get_balance(config.DENOM_LUNC)
    start_juris = terra.get_cw20_balance(config.JURIS_CW20_ADDRESS)
    print(f"  Starting: {start_lunc} uluna, {start_juris} JURIS (base units)")

    min_receive_1 = _garuda_min_receive(pool, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor (see warning above) — aborting "
              "before risking a real broadcast with zero slippage protection.")
        return
    leg1 = execute_leg(terra, pool.pair_address, lunc, TEST_NATIVE_AMOUNT_ULUNA,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_juris = terra.get_cw20_balance(config.JURIS_CW20_ADDRESS)
    actual_received_juris = mid_juris - start_juris
    _report_gap("LUNC -> JURIS (Garuda)", leg1.received, actual_received_juris)

    if actual_received_juris <= 0:
        print("  Received 0 JURIS — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, juris_token, actual_received_juris)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg — leaving "
              "JURIS unswapped rather than broadcasting with zero slippage protection. "
              "Re-run this script to retry the return leg.")
        return
    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, juris_token, actual_received_juris, min_receive_2, "JURIS -> LUNC (Garuda)")
    if leg2 is None:
        return
    end_lunc = terra.get_balance(config.DENOM_LUNC)
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap("JURIS -> LUNC (Garuda)", leg2.received, actual_received_lunc, received_is_native=True)

    print(f"  Round trip: started with {start_lunc} uluna, ended with {end_lunc} uluna "
          f"(difference is 2 legs' worth of AMM commission + native stability tax + gas — "
          f"expected to be negative, this is a cost-discovery test, not a profit attempt). "
          f"If either gap above showed a real (>5bps) CW20-side tax (not the already-expected "
          f"native-tax pattern on the return leg), that would contradict the Terraport JURIS/LUNC "
          f"reading and is worth a closer look before trusting this pool live.")


def main():
    if "--confirm" not in sys.argv:
        print("This script broadcasts REAL transactions with REAL funds (small amounts, but "
              "real). Re-run with --confirm to proceed:\n  python smoke_test_new_tokens.py --confirm")
        sys.exit(1)

    config.validate()
    if config.DRY_RUN:
        print("config.DRY_RUN is True — this script needs DRY_RUN=False to actually observe a "
              "real swap's result. Set DRY_RUN=false in your .env for this run only, then set "
              "it back before running the main bot again if you don't want live trading.")
        sys.exit(1)

    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)

    print("\ncwUSTC is still BLOCKED — no native cwUSTC/USTC pairing exists yet, and the only "
          "real WESO pool for it (cwLUNC/cwUSTC) is a pegged pair that hasn't had a curve-type "
          "check. Not tested here.")

    tests = [
        ("JURIS", test_juris_round_trip),
        ("cwLUNC", test_cwlunc_round_trip),
        ("BENANCE", test_benance_round_trip),
        ("GDEX/GRDX", test_gdex_grdx),
        ("FUN", test_fun_via_gdex),
        ("GRDX/LUNC", test_grdx_lunc_round_trip),
        ("JURIS/LUNC (Garuda)", test_juris_lunc_garuda_round_trip),
    ]
    failed = []
    for name, fn in tests:
        try:
            fn(terra)
        except Exception as e:
            failed.append(name)
            print(f"\n!!! {name} test raised an unhandled error — SKIPPING to the next token "
                  f"rather than aborting the whole run: {e}")
            log.exception("%s test failed", name)

    if failed:
        print(f"\n{len(failed)} test(s) hit an unhandled error and were skipped: {', '.join(failed)}. "
              f"Read the errors above — these still need to be understood before trusting those "
              f"tokens live, even though the rest of the run completed.")

    print("\nDone. Update the relevant _TRANSFER_TAX_BPS constants in config.py for anything "
          "that showed a real (>5bps) gap above, then re-run once more to confirm stability "
          "before trusting the number live.")


if __name__ == "__main__":
    main()