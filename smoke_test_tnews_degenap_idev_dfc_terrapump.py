"""
REAL-FUND smoke test for the 2026-09-03 batch: TNEWS, DEGENAP, IDEV — 12
active pools across Terraport and Garuda DeFi (both already-trusted
venues) — PLUS two schema probes at the user's request for the pools that
were deliberately left out of arbitrage_bot.py:

  1. LUNC_DFC_POOL_UNKNOWN ("DFC/LUNC, no dex visible") — already on file
     since 2026-08-29, previously investigated via manual query and found
     to have extra {"pair":{}} response fields ("fee_rate",
     "lp_stake_contract") no plain Terraswap/Terraport pool has. This
     script re-queries it fresh and, ONLY if the shape now looks standard,
     attempts a real tiny compatibility swap.
  2. TERRAPUMP_POOL_TNEWS_LUNC ("Terra.pump") — a venue this bot has never
     traded on before. Same treatment: query first, swap only if the
     shape matches what DexPool actually assumes.

This is NOT the same as the tax-discovery round trips below. A probe on
an unconfirmed venue queries the contract's {"pair":{}} and {"pool":{}}
responses BEFORE risking a real swap message, and only attempts the swap
if the response shape matches the standard Terraswap-family
assets=[{info,amount},{info,amount}] shape DexPool.get_state() expects
with no unexpected extra top-level fields. If the query already looks
wrong, the swap is skipped entirely — sending an ExecuteMsg to a schema
you don't understand is how the Garuda USDC/LTK "Invalid fee amount"
surprise happened, and this avoids repeating that blind guess where it
can be avoided by looking first. CosmWasm's strict message deserialization
means a genuinely mismatched swap message gets rejected before any state
change (gas spent, no funds lost) rather than executing with wrong
semantics — but "probably safe to attempt" is not the same bar as "already
trusted", hence probing before executing rather than skipping the check
altogether.

COVERAGE — all 12 active pools from the 2026-09-03 batch, one test each,
plus the two probes:
  0a. probe_dfc_unknown_pool          — LUNC_DFC_POOL_UNKNOWN
  0b. probe_terrapump_tnews_lunc      — TERRAPUMP_POOL_TNEWS_LUNC (parked)
  1.  Terraport DEGENAP/LUNC          — PRIMARY tax discovery for DEGENAP
  2.  Terraport DEGENAP/USTC          — cross-pair
  3.  Garuda USTC/DEGENAP             — venue cross-check vs #2
  4.  Terraport DEGENAP/GRDX          — cross-pair (GRDX sourced)
  5.  Terraport TNEWS/LUNC            — PRIMARY tax discovery for TNEWS
  6.  Terraport DEGENAP/TNEWS         — cross-pair (both sourced above)
  7.  Garuda ELPACO/IDEV              — PRIMARY tax discovery for IDEV
                                         (ELPACO sourced first; ELPACO's
                                         own tax already confirmed 0bps
                                         2026-09-02, not re-discovered)
  8.  Garuda ELPACO/USTC              — re-confirms ELPACO still 0bps
  9.  Garuda ELPACO/JURIS             — cross-pair
  10. Garuda ELPACO/GRDX              — cross-pair (GRDX sourced)
  11. Garuda IDEV/LUNC                — venue cross-check vs #7 for IDEV
  12. Garuda IDEV/LTK                 — cross-pair (LTK sourced)

Order matters — later tests source DEGENAP/TNEWS/ELPACO/IDEV/GRDX/LTK
from earlier tests' leftover balances. Keep the sequence intact if adding
to it.

This moves real, small amounts of real funds (and the probes broadcast a
SMALLER real amount — see PROBE_NATIVE_AMOUNT_ULUNA — specifically
because their venue compatibility isn't confirmed yet). It will NOT run:
  - if config.DRY_RUN is True (nothing real would happen)
  - without passing --confirm on the command line

Run: python smoke_test_tnews_degenap_idev_dfc_terrapump.py --confirm
"""
import sys
import json
import logging

import config
from assets import Asset
from pool_client import GarudaPool, _query_contract_raw
from terra_client import TerraClient
from executor import execute_leg
from smoke_test_new_tokens import (
    _garuda_min_receive,
    _execute_garuda_leg_diagnostic,
    _report_gap,
    TEST_NATIVE_AMOUNT_ULUNA,
    lunc,
    grdx_token,
    juris_token,
)
from smoke_test_lix_ltk_elpaco_rotti import (
    ltk_token,
    elpaco_token,
    usdc,
    ustc,
    _get_or_source_ltk,
    _source_grdx_via_garuda_lunc,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("smoke_test_tnews_degenap_idev_dfc_terrapump")

dfc_token = Asset(kind="cw20", id=config.DFC_CW20_ADDRESS, decimals=config.DFC_DECIMALS, display="DFC")
tnews_token = Asset(kind="cw20", id=config.TNEWS_CW20_ADDRESS, decimals=config.TNEWS_DECIMALS, display="TNEWS")
degenap_token = Asset(kind="cw20", id=config.DEGENAP_CW20_ADDRESS, decimals=config.DEGENAP_DECIMALS, display="DEGENAP")
idev_token = Asset(kind="cw20", id=config.IDEV_CW20_ADDRESS, decimals=config.IDEV_DECIMALS, display="IDEV")

# Smaller than TEST_NATIVE_AMOUNT_ULUNA (2 LUNC) — used ONLY for the two
# unconfirmed-venue probes below, to limit exposure while the message
# schema is still unverified. Everything else in this file uses the
# normal TEST_NATIVE_AMOUNT_ULUNA.
PROBE_NATIVE_AMOUNT_ULUNA = 200_000  # 0.2 LUNC

# What a standard Terraswap/Astroport-family {"pool":{}} / {"pair":{}}
# response is expected to contain. Any KEY beyond these sets is the same
# signal that flagged LUNC_DFC_POOL_UNKNOWN as a probably-different fork
# (its {"pair":{}} response carries "fee_rate" and "lp_stake_contract" on
# top of the standard fields).
EXPECTED_POOL_QUERY_KEYS = {"assets", "total_share"}
EXPECTED_PAIR_QUERY_KEYS = {"asset_infos", "contract_addr", "liquidity_token", "asset_decimals", "pair_type"}


def _probe_unknown_venue(terra, label, pool_address, cw20_token):
    """
    Query-first schema probe for a venue this bot has never confirmed.
    Prints the raw {"pair":{}} and {"pool":{}} responses, flags any
    unexpected top-level fields, and ONLY attempts a real (small) swap if
    the {"pool":{}} response matches the standard Terraswap-family
    assets=[{info,amount},{info,amount}] shape with no unexpected fields.
    Mirrors how LUNC_DFC_POOL_UNKNOWN was originally investigated (see
    arbitrage_bot.py's comment above that parked pool) and how WESO's
    "reflective" pair_type was discovered — extra/unexpected response
    fields are the signal, not a specific expected failure mode.
    """
    print(f"\n=== Schema probe: {label} ({pool_address}) ===")
    pair_extra = None
    try:
        pair_resp = _query_contract_raw(pool_address, {"pair": {}})
        print(f"  {{'pair':{{}}}} response:\n{json.dumps(pair_resp, indent=2)}")
        pair_extra = set(pair_resp.keys()) - EXPECTED_PAIR_QUERY_KEYS
        if pair_extra:
            print(f"  ! UNEXPECTED top-level fields in {{'pair':{{}}}} response: "
                  f"{sorted(pair_extra)} — this is exactly the signal that flagged "
                  f"LUNC_DFC_POOL_UNKNOWN as a probably-different fork.")
    except Exception as e:
        print(f"  {{'pair':{{}}}} query failed (not necessarily disqualifying — some forks "
              f"don't implement this query at all): {e}")

    try:
        pool_resp = _query_contract_raw(pool_address, {"pool": {}})
        print(f"  {{'pool':{{}}}} response:\n{json.dumps(pool_resp, indent=2)}")
    except Exception as e:
        print(f"  {{'pool':{{}}}} query failed: {e} — cannot proceed to a swap probe without "
              f"this succeeding, since DexPool.get_state() depends on it.")
        print(f"  RECOMMENDATION: KEEP PARKED.")
        return

    pool_extra = set(pool_resp.keys()) - EXPECTED_POOL_QUERY_KEYS
    has_assets_shape = ("assets" in pool_resp and isinstance(pool_resp["assets"], list)
                         and len(pool_resp["assets"]) == 2
                         and all(isinstance(a, dict) and "info" in a and "amount" in a
                                 for a in pool_resp["assets"]))

    if pool_extra:
        print(f"  ! UNEXPECTED top-level fields in {{'pool':{{}}}} response: {sorted(pool_extra)}")
    if not has_assets_shape:
        print(f"  ! {{'pool':{{}}}} response does NOT match the standard "
              f"assets=[{{info,amount}},{{info,amount}}] shape DexPool.get_state() expects.")

    if pool_extra or not has_assets_shape or pair_extra:
        print(f"  RECOMMENDATION: KEEP PARKED. Query shape doesn't cleanly match what this "
              f"bot's DexPool code assumes — sending a real swap message here would be guessing "
              f"at a schema, same risk category LUNC_DFC_POOL_UNKNOWN was parked for. Do not "
              f"enable {label} without understanding the flagged fields first.")
        return

    print(f"  Query shape looks standard — no unexpected fields, assets=[...] present as "
          f"expected. Attempting a tiny ({PROBE_NATIVE_AMOUNT_ULUNA} uluna) real compatibility "
          f"swap...")

    start_lunc = terra.get_balance(config.DENOM_LUNC)
    start_token = terra.get_cw20_balance(cw20_token.id)
    try:
        leg1 = execute_leg(terra, pool_address, lunc, PROBE_NATIVE_AMOUNT_ULUNA)
    except Exception as e:
        print(f"  ! Real swap FAILED: {e}")
        print(f"  RECOMMENDATION: KEEP PARKED. Query shape looked standard but the actual swap "
              f"message was rejected on-chain — same surprise category as Garuda USDC/LTK's "
              f"'Invalid fee amount' error. Needs its own investigation before enabling.")
        return

    mid_token = terra.get_cw20_balance(cw20_token.id)
    actual_received = mid_token - start_token
    _report_gap(f"LUNC -> {cw20_token.display} ({label})", leg1.received, actual_received)

    if actual_received <= 0:
        print(f"  Received 0 {cw20_token.display} — stopping before the return leg.")
        print(f"  RECOMMENDATION: investigate further before enabling — the swap succeeded "
              f"on-chain but produced nothing.")
        return

    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    try:
        leg2 = execute_leg(terra, pool_address, cw20_token, actual_received)
    except Exception as e:
        print(f"  ! Return swap FAILED: {e} — {cw20_token.display} from the first leg is now "
              f"sitting in the wallet (not lost, just needs a different route back to LUNC).")
        print(f"  RECOMMENDATION: investigate further before enabling.")
        return

    end_lunc = terra.get_balance(config.DENOM_LUNC)
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap(f"{cw20_token.display} -> LUNC ({label})", leg2.received, actual_received_lunc,
                received_is_native=True)

    print(f"  RECOMMENDATION: {label} completed a full real round trip cleanly through the "
          f"standard DexPool code path — safe to un-park and wire in as a normal DexPool, same "
          f"bar as any other confirmed venue. This was ONE small round trip though; consider a "
          f"second confirmation run (and the normal TEST_NATIVE_AMOUNT_ULUNA size) before "
          f"sizing real trades through it, same as any freshly un-parked pool.")


def probe_dfc_unknown_pool(terra):
    """LUNC_DFC_POOL_UNKNOWN — parked since 2026-08-29. DFC already trades
    fine via the confirmed Garuda and Terraswap LUNC/DFC pools, so this is
    a low-urgency re-check, not a blocker for anything."""
    _probe_unknown_venue(terra, "LUNC/DFC unknown-DEX pool", config.LUNC_DFC_POOL_UNKNOWN, dfc_token)


def probe_terrapump_tnews_lunc(terra):
    """TERRAPUMP_POOL_TNEWS_LUNC — parked since 2026-09-03, first time
    this bot has looked at a Terra.pump contract. TNEWS/LUNC already
    trades via the confirmed Terraport pool, so this is also low-urgency."""
    _probe_unknown_venue(terra, "Terra.pump TNEWS/LUNC", config.TERRAPUMP_POOL_TNEWS_LUNC, tnews_token)


def test_degenap_via_terraport_round_trip(terra):
    """PRIMARY tax-discovery test for DEGENAP via Terraport DEGENAP/LUNC.
    Keeps ~two-thirds of what's received for the USTC/GRDX/TNEWS
    cross-pair tests below."""
    print("\n=== DEGENAP/LUNC round trip (Terraport) ===")
    pool_address = config.TERRAPORT_POOL_DEGENAP_LUNC
    start_lunc = terra.get_balance(config.DENOM_LUNC)
    start_degenap = terra.get_cw20_balance(config.DEGENAP_CW20_ADDRESS)
    print(f"  Starting: {start_lunc} uluna, {start_degenap} DEGENAP (base units)")

    leg1 = execute_leg(terra, pool_address, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    mid_degenap = terra.get_cw20_balance(config.DEGENAP_CW20_ADDRESS)
    actual_received_degenap = mid_degenap - start_degenap
    _report_gap("LUNC -> DEGENAP", leg1.received, actual_received_degenap)

    if actual_received_degenap <= 0:
        print("  Received 0 DEGENAP — aborting before attempting the return leg.")
        return

    return_amount = actual_received_degenap // 3
    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2 = execute_leg(terra, pool_address, degenap_token, return_amount)
    end_lunc = terra.get_balance(config.DENOM_LUNC)
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap("DEGENAP -> LUNC", leg2.received, actual_received_lunc, received_is_native=True)

    print(f"  If LUNC->DEGENAP showed a real gap, add DEGENAP_TRANSFER_TAX_BPS in config.py. "
          f"Remaining ~{actual_received_degenap - return_amount} DEGENAP left in wallet "
          f"intentionally for the cross-pair tests below.")


def _get_or_source_degenap(terra, min_amount: int = 1) -> int:
    balance = terra.get_cw20_balance(config.DEGENAP_CW20_ADDRESS)
    if balance >= min_amount:
        return balance
    print("  Wallet holds insufficient DEGENAP — sourcing via Terraport DEGENAP/LUNC first.")
    leg = execute_leg(terra, config.TERRAPORT_POOL_DEGENAP_LUNC, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    balance = terra.get_cw20_balance(config.DEGENAP_CW20_ADDRESS)
    _report_gap("LUNC -> DEGENAP (sourcing)", leg.received, balance)
    return balance


def test_degenap_terraport_ustc(terra):
    """DEGENAP/USTC cross-pair (Terraport)."""
    print("\n=== DEGENAP/USTC round trip (Terraport) ===")
    degenap_balance = _get_or_source_degenap(terra)
    if degenap_balance <= 0:
        print("  Still holding 0 DEGENAP after sourcing — skipping this test.")
        return
    pool_address = config.TERRAPORT_POOL_DEGENAP_USTC
    test_amount = min(degenap_balance, max(1, degenap_balance // 3))
    start_ustc = terra.get_balance(config.DENOM_USTC)

    leg1 = execute_leg(terra, pool_address, degenap_token, test_amount)
    mid_ustc = terra.get_balance(config.DENOM_USTC)
    actual_received_ustc = mid_ustc - start_ustc
    _report_gap("DEGENAP -> USTC", leg1.received, actual_received_ustc, received_is_native=True)

    if actual_received_ustc <= 0:
        print("  Received 0 USTC — aborting before attempting the return leg.")
        return

    pre_return_degenap = terra.get_cw20_balance(config.DEGENAP_CW20_ADDRESS)
    leg2 = execute_leg(terra, pool_address, ustc, actual_received_ustc)
    post_return_degenap = terra.get_cw20_balance(config.DEGENAP_CW20_ADDRESS)
    _report_gap("USTC -> DEGENAP", leg2.received, post_return_degenap - pre_return_degenap)


def test_degenap_garuda_ustc(terra):
    """USTC/DEGENAP cross-pair (Garuda) — venue cross-check against the
    Terraport DEGENAP/USTC reading above."""
    print("\n=== USTC/DEGENAP round trip (Garuda) ===")
    degenap_balance = _get_or_source_degenap(terra)
    if degenap_balance <= 0:
        print("  Still holding 0 DEGENAP after sourcing — skipping this test.")
        return
    pool = GarudaPool("Garuda USTC/DEGENAP", terra, config.GARUDA_POOL_USTC_DEGENAP,
                       ustc, degenap_token, config.GARUDA_COMMISSION_RATE)
    test_amount = min(degenap_balance, max(1, degenap_balance // 2))
    start_ustc = terra.get_balance(config.DENOM_USTC)

    min_receive_1 = _garuda_min_receive(pool, degenap_token, test_amount)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting before DEGENAP->USTC.")
        return
    leg1 = execute_leg(terra, pool.pair_address, degenap_token, test_amount,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_ustc = terra.get_balance(config.DENOM_USTC)
    actual_received_ustc = mid_ustc - start_ustc
    _report_gap("DEGENAP -> USTC (Garuda)", leg1.received, actual_received_ustc, received_is_native=True)

    if actual_received_ustc <= 0:
        print("  Received 0 USTC — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, ustc, actual_received_ustc)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_degenap = terra.get_cw20_balance(config.DEGENAP_CW20_ADDRESS)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, ustc, actual_received_ustc, min_receive_2, "USTC -> DEGENAP (Garuda)")
    if leg2 is None:
        return
    post_return_degenap = terra.get_cw20_balance(config.DEGENAP_CW20_ADDRESS)
    _report_gap("USTC -> DEGENAP (Garuda)", leg2.received, post_return_degenap - pre_return_degenap)


def test_degenap_terraport_grdx(terra):
    """DEGENAP/GRDX cross-pair (Terraport). Sources GRDX via the
    already-live Garuda GRDX/LUNC pool if needed."""
    print("\n=== DEGENAP/GRDX round trip (Terraport) ===")
    degenap_balance = _get_or_source_degenap(terra)
    if degenap_balance <= 0:
        print("  Still holding 0 DEGENAP after sourcing — skipping this test.")
        return
    pool_address = config.TERRAPORT_POOL_DEGENAP_GRDX
    test_amount = min(degenap_balance, max(1, degenap_balance // 2))
    start_grdx = terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)

    leg1 = execute_leg(terra, pool_address, degenap_token, test_amount)
    mid_grdx = terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)
    actual_received_grdx = mid_grdx - start_grdx
    _report_gap("DEGENAP -> GRDX", leg1.received, actual_received_grdx)

    if actual_received_grdx <= 0:
        print("  Received 0 GRDX — aborting before attempting the return leg.")
        return

    pre_return_degenap = terra.get_cw20_balance(config.DEGENAP_CW20_ADDRESS)
    leg2 = execute_leg(terra, pool_address, grdx_token, actual_received_grdx)
    post_return_degenap = terra.get_cw20_balance(config.DEGENAP_CW20_ADDRESS)
    _report_gap("GRDX -> DEGENAP", leg2.received, post_return_degenap - pre_return_degenap)


def test_tnews_via_terraport_round_trip(terra):
    """PRIMARY tax-discovery test for TNEWS via Terraport TNEWS/LUNC (the
    confirmed venue — NOT the parked Terra.pump pool). Keeps ~half of
    what's received for the DEGENAP/TNEWS cross-pair test below."""
    print("\n=== TNEWS/LUNC round trip (Terraport) ===")
    pool_address = config.TERRAPORT_POOL_TNEWS_LUNC
    start_lunc = terra.get_balance(config.DENOM_LUNC)
    start_tnews = terra.get_cw20_balance(config.TNEWS_CW20_ADDRESS)
    print(f"  Starting: {start_lunc} uluna, {start_tnews} TNEWS (base units)")

    leg1 = execute_leg(terra, pool_address, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    mid_tnews = terra.get_cw20_balance(config.TNEWS_CW20_ADDRESS)
    actual_received_tnews = mid_tnews - start_tnews
    _report_gap("LUNC -> TNEWS", leg1.received, actual_received_tnews)

    if actual_received_tnews <= 0:
        print("  Received 0 TNEWS — aborting before attempting the return leg.")
        return

    return_amount = actual_received_tnews // 2
    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2 = execute_leg(terra, pool_address, tnews_token, return_amount)
    end_lunc = terra.get_balance(config.DENOM_LUNC)
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap("TNEWS -> LUNC", leg2.received, actual_received_lunc, received_is_native=True)

    print(f"  If LUNC->TNEWS showed a real gap, add TNEWS_TRANSFER_TAX_BPS in config.py. "
          f"Remaining ~{actual_received_tnews - return_amount} TNEWS left in wallet "
          f"intentionally for the cross-pair test below.")


def test_degenap_tnews_terraport(terra):
    """DEGENAP/TNEWS cross-pair (Terraport). Needs both tokens sourced
    from the two primary tests above."""
    print("\n=== DEGENAP/TNEWS round trip (Terraport) ===")
    degenap_balance = _get_or_source_degenap(terra)
    tnews_balance = terra.get_cw20_balance(config.TNEWS_CW20_ADDRESS)
    if degenap_balance <= 0 or tnews_balance <= 0:
        print(f"  Need both DEGENAP ({degenap_balance}) and TNEWS ({tnews_balance}) — run "
              f"test_tnews_via_terraport_round_trip first if TNEWS is 0. Skipping.")
        return
    pool_address = config.TERRAPORT_POOL_DEGENAP_TNEWS
    test_amount = min(degenap_balance, max(1, degenap_balance // 2))
    start_tnews = terra.get_cw20_balance(config.TNEWS_CW20_ADDRESS)

    leg1 = execute_leg(terra, pool_address, degenap_token, test_amount)
    mid_tnews = terra.get_cw20_balance(config.TNEWS_CW20_ADDRESS)
    actual_received_tnews = mid_tnews - start_tnews
    _report_gap("DEGENAP -> TNEWS", leg1.received, actual_received_tnews)

    if actual_received_tnews <= 0:
        print("  Received 0 TNEWS — aborting before attempting the return leg.")
        return

    pre_return_degenap = terra.get_cw20_balance(config.DEGENAP_CW20_ADDRESS)
    leg2 = execute_leg(terra, pool_address, tnews_token, actual_received_tnews)
    post_return_degenap = terra.get_cw20_balance(config.DEGENAP_CW20_ADDRESS)
    _report_gap("TNEWS -> DEGENAP", leg2.received, post_return_degenap - pre_return_degenap)


def _source_elpaco_via_garuda_lunc(terra, source_amount_uluna: int = TEST_NATIVE_AMOUNT_ULUNA) -> int:
    """Sources ELPACO via the already-live Garuda LUNC/ELPACO pool.
    ELPACO's own tax is already confirmed 0bps (2026-09-02), so no new
    discovery happens here — this purely funds the IDEV/ELPACO cross-pair
    tests below."""
    print("\n--- Sourcing ELPACO via Garuda LUNC/ELPACO ---")
    pool = GarudaPool("Garuda LUNC/ELPACO", terra, config.GARUDA_POOL_LUNC_ELPACO,
                       lunc, elpaco_token, config.GARUDA_COMMISSION_RATE)
    min_receive = _garuda_min_receive(pool, lunc, source_amount_uluna)
    if min_receive <= 0:
        print("  Could not compute a live min_receive floor — aborting sourcing attempt.")
        return terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    start_elpaco = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    leg = execute_leg(terra, pool.pair_address, lunc, source_amount_uluna,
                       pool_kind="garuda", min_receive=min_receive)
    end_elpaco = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    _report_gap("LUNC -> ELPACO (sourcing)", leg.received, end_elpaco - start_elpaco)
    return end_elpaco


def test_elpaco_idev_garuda(terra):
    """PRIMARY tax-discovery test for IDEV via Garuda ELPACO/IDEV. Keeps
    ~half of what's received for the IDEV/LUNC and IDEV/LTK tests below."""
    print("\n=== ELPACO/IDEV round trip (Garuda) ===")
    elpaco_balance = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    if elpaco_balance <= 0:
        elpaco_balance = _source_elpaco_via_garuda_lunc(terra)
        if elpaco_balance <= 0:
            print("  Still holding 0 ELPACO after sourcing — skipping this test.")
            return

    pool = GarudaPool("Garuda ELPACO/IDEV", terra, config.GARUDA_POOL_ELPACO_IDEV,
                       elpaco_token, idev_token, config.GARUDA_COMMISSION_RATE)
    test_amount = min(elpaco_balance, max(1, elpaco_balance // 3))
    start_idev = terra.get_cw20_balance(config.IDEV_CW20_ADDRESS)

    min_receive_1 = _garuda_min_receive(pool, elpaco_token, test_amount)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting before ELPACO->IDEV.")
        return
    leg1 = execute_leg(terra, pool.pair_address, elpaco_token, test_amount,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_idev = terra.get_cw20_balance(config.IDEV_CW20_ADDRESS)
    actual_received_idev = mid_idev - start_idev
    _report_gap("ELPACO -> IDEV", leg1.received, actual_received_idev)

    if actual_received_idev <= 0:
        print("  Received 0 IDEV — aborting before attempting the return leg.")
        return

    return_amount = actual_received_idev // 2
    min_receive_2 = _garuda_min_receive(pool, idev_token, return_amount)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_elpaco = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, idev_token, return_amount, min_receive_2, "IDEV -> ELPACO")
    if leg2 is None:
        return
    post_return_elpaco = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    _report_gap("IDEV -> ELPACO", leg2.received, post_return_elpaco - pre_return_elpaco)
    print(f"  If ELPACO->IDEV showed a real gap, add IDEV_TRANSFER_TAX_BPS in config.py. "
          f"Remaining ~{actual_received_idev - return_amount} IDEV left in wallet "
          f"intentionally for the tests below.")


def test_elpaco_ustc_garuda(terra):
    """ELPACO/USTC cross-pair (Garuda) — re-confirms ELPACO is still
    0bps via a third route (LUNC and LTK already confirmed it 2026-09-02)."""
    print("\n=== ELPACO/USTC round trip (Garuda) ===")
    elpaco_balance = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    if elpaco_balance <= 0:
        elpaco_balance = _source_elpaco_via_garuda_lunc(terra)
        if elpaco_balance <= 0:
            print("  Still holding 0 ELPACO after sourcing — skipping this test.")
            return
    pool = GarudaPool("Garuda ELPACO/USTC", terra, config.GARUDA_POOL_ELPACO_USTC,
                       elpaco_token, ustc, config.GARUDA_COMMISSION_RATE)
    test_amount = min(elpaco_balance, max(1, elpaco_balance // 2))
    start_ustc = terra.get_balance(config.DENOM_USTC)

    min_receive_1 = _garuda_min_receive(pool, elpaco_token, test_amount)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting before ELPACO->USTC.")
        return
    leg1 = execute_leg(terra, pool.pair_address, elpaco_token, test_amount,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_ustc = terra.get_balance(config.DENOM_USTC)
    actual_received_ustc = mid_ustc - start_ustc
    _report_gap("ELPACO -> USTC", leg1.received, actual_received_ustc, received_is_native=True)

    if actual_received_ustc <= 0:
        print("  Received 0 USTC — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, ustc, actual_received_ustc)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_elpaco = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, ustc, actual_received_ustc, min_receive_2, "USTC -> ELPACO")
    if leg2 is None:
        return
    post_return_elpaco = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    _report_gap("USTC -> ELPACO", leg2.received, post_return_elpaco - pre_return_elpaco)


def test_elpaco_juris_garuda(terra):
    """ELPACO/JURIS cross-pair (Garuda)."""
    print("\n=== ELPACO/JURIS round trip (Garuda) ===")
    elpaco_balance = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    if elpaco_balance <= 0:
        elpaco_balance = _source_elpaco_via_garuda_lunc(terra)
        if elpaco_balance <= 0:
            print("  Still holding 0 ELPACO after sourcing — skipping this test.")
            return
    pool = GarudaPool("Garuda ELPACO/JURIS", terra, config.GARUDA_POOL_ELPACO_JURIS,
                       elpaco_token, juris_token, config.GARUDA_COMMISSION_RATE)
    test_amount = min(elpaco_balance, max(1, elpaco_balance // 2))
    start_juris = terra.get_cw20_balance(config.JURIS_CW20_ADDRESS)

    min_receive_1 = _garuda_min_receive(pool, elpaco_token, test_amount)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting before ELPACO->JURIS.")
        return
    leg1 = execute_leg(terra, pool.pair_address, elpaco_token, test_amount,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_juris = terra.get_cw20_balance(config.JURIS_CW20_ADDRESS)
    actual_received_juris = mid_juris - start_juris
    _report_gap("ELPACO -> JURIS", leg1.received, actual_received_juris)

    if actual_received_juris <= 0:
        print("  Received 0 JURIS — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, juris_token, actual_received_juris)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_elpaco = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, juris_token, actual_received_juris, min_receive_2, "JURIS -> ELPACO")
    if leg2 is None:
        return
    post_return_elpaco = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    _report_gap("JURIS -> ELPACO", leg2.received, post_return_elpaco - pre_return_elpaco)


def test_elpaco_grdx_garuda(terra):
    """ELPACO/GRDX cross-pair (Garuda). Sources GRDX via the already-live
    Garuda GRDX/LUNC pool if needed."""
    print("\n=== ELPACO/GRDX round trip (Garuda) ===")
    elpaco_balance = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    if elpaco_balance <= 0:
        elpaco_balance = _source_elpaco_via_garuda_lunc(terra)
        if elpaco_balance <= 0:
            print("  Still holding 0 ELPACO after sourcing — skipping this test.")
            return
    grdx_balance = terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)
    if grdx_balance <= 0:
        grdx_balance = _source_grdx_via_garuda_lunc(terra)
        if grdx_balance <= 0:
            print("  Still holding 0 GRDX after sourcing — skipping this test.")
            return

    pool = GarudaPool("Garuda ELPACO/GRDX", terra, config.GARUDA_POOL_ELPACO_GRDX,
                       elpaco_token, grdx_token, config.GARUDA_COMMISSION_RATE)
    test_amount = min(elpaco_balance, max(1, elpaco_balance // 2))
    start_grdx = terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)

    min_receive_1 = _garuda_min_receive(pool, elpaco_token, test_amount)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting before ELPACO->GRDX.")
        return
    leg1 = execute_leg(terra, pool.pair_address, elpaco_token, test_amount,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_grdx = terra.get_cw20_balance(config.GRDX_CW20_ADDRESS)
    actual_received_grdx = mid_grdx - start_grdx
    _report_gap("ELPACO -> GRDX", leg1.received, actual_received_grdx)

    if actual_received_grdx <= 0:
        print("  Received 0 GRDX — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, grdx_token, actual_received_grdx)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_elpaco = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, grdx_token, actual_received_grdx, min_receive_2, "GRDX -> ELPACO")
    if leg2 is None:
        return
    post_return_elpaco = terra.get_cw20_balance(config.ELPACO_CW20_ADDRESS)
    _report_gap("GRDX -> ELPACO", leg2.received, post_return_elpaco - pre_return_elpaco)


def test_idev_lunc_garuda(terra):
    """IDEV/LUNC round trip (Garuda) — venue cross-check for IDEV's tax.
    LUNC->IDEV here is a SECOND independent read of IDEV's buy tax
    (first was ELPACO->IDEV above); IDEV->LUNC should show only the usual
    native stability tax."""
    print("\n=== IDEV/LUNC round trip (Garuda) ===")
    idev_balance = terra.get_cw20_balance(config.IDEV_CW20_ADDRESS)
    if idev_balance <= 0:
        print("  Wallet holds 0 IDEV — run test_elpaco_idev_garuda first. Skipping.")
        return
    pool = GarudaPool("Garuda IDEV/LUNC", terra, config.GARUDA_POOL_IDEV_LUNC,
                       idev_token, lunc, config.GARUDA_COMMISSION_RATE)
    start_lunc = terra.get_balance(config.DENOM_LUNC)

    min_receive_1 = _garuda_min_receive(pool, lunc, TEST_NATIVE_AMOUNT_ULUNA)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting before LUNC->IDEV.")
        return
    start_idev = terra.get_cw20_balance(config.IDEV_CW20_ADDRESS)
    leg1 = execute_leg(terra, pool.pair_address, lunc, TEST_NATIVE_AMOUNT_ULUNA,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_idev = terra.get_cw20_balance(config.IDEV_CW20_ADDRESS)
    actual_received_idev = mid_idev - start_idev
    _report_gap("LUNC -> IDEV (Garuda)", leg1.received, actual_received_idev)

    if actual_received_idev <= 0:
        print("  Received 0 IDEV — aborting before attempting the return leg.")
        return

    return_amount = idev_balance + actual_received_idev  # everything on hand now
    min_receive_2 = _garuda_min_receive(pool, idev_token, return_amount)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_lunc = terra.get_balance(config.DENOM_LUNC)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, idev_token, return_amount, min_receive_2, "IDEV -> LUNC (Garuda)")
    if leg2 is None:
        return
    end_lunc = terra.get_balance(config.DENOM_LUNC)
    actual_received_lunc = (end_lunc - pre_return_lunc) + leg2.gas_fee_uluna
    _report_gap("IDEV -> LUNC (Garuda)", leg2.received, actual_received_lunc, received_is_native=True)


def test_idev_ltk_garuda(terra):
    """IDEV/LTK cross-pair (Garuda). Sources IDEV fresh (since the prior
    test likely swept the wallet's IDEV back to LUNC) and LTK via the
    already-live Terraswap LUNC/LTK pool."""
    print("\n=== IDEV/LTK round trip (Garuda) ===")
    idev_balance = terra.get_cw20_balance(config.IDEV_CW20_ADDRESS)
    if idev_balance <= 0:
        print("  Sourcing fresh IDEV via Garuda ELPACO/IDEV (via LUNC->ELPACO first)...")
        elpaco_balance = _source_elpaco_via_garuda_lunc(terra)
        if elpaco_balance <= 0:
            print("  Could not source ELPACO — skipping this test.")
            return
        idev_pool_for_sourcing = GarudaPool("Garuda ELPACO/IDEV", terra, config.GARUDA_POOL_ELPACO_IDEV,
                                             elpaco_token, idev_token, config.GARUDA_COMMISSION_RATE)
        min_receive = _garuda_min_receive(idev_pool_for_sourcing, elpaco_token, elpaco_balance)
        if min_receive <= 0:
            print("  Could not compute a live min_receive floor for sourcing IDEV — skipping.")
            return
        leg = execute_leg(terra, idev_pool_for_sourcing.pair_address, elpaco_token, elpaco_balance,
                           pool_kind="garuda", min_receive=min_receive)
        idev_balance = terra.get_cw20_balance(config.IDEV_CW20_ADDRESS)
        _report_gap("ELPACO -> IDEV (sourcing)", leg.received, idev_balance)
        if idev_balance <= 0:
            print("  Still holding 0 IDEV after sourcing — skipping this test.")
            return

    ltk_balance = _get_or_source_ltk(terra)
    if ltk_balance <= 0:
        print("  Still holding 0 LTK after sourcing — skipping this test.")
        return

    pool = GarudaPool("Garuda IDEV/LTK", terra, config.GARUDA_POOL_IDEV_LTK,
                       idev_token, ltk_token, config.GARUDA_COMMISSION_RATE)
    test_amount = min(idev_balance, max(1, idev_balance // 2))
    start_ltk = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)

    min_receive_1 = _garuda_min_receive(pool, idev_token, test_amount)
    if min_receive_1 <= 0:
        print("  Could not compute a live min_receive floor — aborting before IDEV->LTK.")
        return
    leg1 = execute_leg(terra, pool.pair_address, idev_token, test_amount,
                        pool_kind="garuda", min_receive=min_receive_1)
    mid_ltk = terra.get_cw20_balance(config.LTK_CW20_ADDRESS)
    actual_received_ltk = mid_ltk - start_ltk
    _report_gap("IDEV -> LTK", leg1.received, actual_received_ltk)

    if actual_received_ltk <= 0:
        print("  Received 0 LTK — aborting before attempting the return leg.")
        return

    min_receive_2 = _garuda_min_receive(pool, ltk_token, actual_received_ltk)
    if min_receive_2 <= 0:
        print("  Could not compute a live min_receive floor for the return leg.")
        return
    pre_return_idev = terra.get_cw20_balance(config.IDEV_CW20_ADDRESS)
    leg2, was_diagnostic = _execute_garuda_leg_diagnostic(
        terra, pool, ltk_token, actual_received_ltk, min_receive_2, "LTK -> IDEV")
    if leg2 is None:
        return
    post_return_idev = terra.get_cw20_balance(config.IDEV_CW20_ADDRESS)
    _report_gap("LTK -> IDEV", leg2.received, post_return_idev - pre_return_idev)


# Probes run first (independent of the sourcing chain below, and cheap —
# query-only unless the shape looks safe). Then the 12 active pools in
# dependency order: later tests source DEGENAP/TNEWS/ELPACO/IDEV/GRDX/LTK
# from earlier tests' leftover balances. Keep this sequence intact if
# adding to it.
COVERAGE_TESTS = [
    ("Probe: LUNC/DFC unknown DEX", probe_dfc_unknown_pool),
    ("Probe: Terra.pump TNEWS/LUNC", probe_terrapump_tnews_lunc),
    ("DEGENAP/LUNC (Terraport)", test_degenap_via_terraport_round_trip),
    ("DEGENAP/USTC (Terraport)", test_degenap_terraport_ustc),
    ("USTC/DEGENAP (Garuda)", test_degenap_garuda_ustc),
    ("DEGENAP/GRDX (Terraport)", test_degenap_terraport_grdx),
    ("TNEWS/LUNC (Terraport)", test_tnews_via_terraport_round_trip),
    ("DEGENAP/TNEWS (Terraport)", test_degenap_tnews_terraport),
    ("ELPACO/IDEV (Garuda)", test_elpaco_idev_garuda),
    ("ELPACO/USTC (Garuda)", test_elpaco_ustc_garuda),
    ("ELPACO/JURIS (Garuda)", test_elpaco_juris_garuda),
    ("ELPACO/GRDX (Garuda)", test_elpaco_grdx_garuda),
    ("IDEV/LUNC (Garuda)", test_idev_lunc_garuda),
    ("IDEV/LTK (Garuda)", test_idev_ltk_garuda),
]


def main():
    if "--confirm" not in sys.argv:
        print("This script broadcasts REAL transactions with REAL funds (small amounts, but "
              "real — including two schema probes against UNCONFIRMED venues). Re-run with "
              "--confirm to proceed:\n"
              "  python smoke_test_tnews_degenap_idev_dfc_terrapump.py --confirm")
        sys.exit(1)

    config.validate()
    if config.DRY_RUN:
        print("config.DRY_RUN is True — this script needs DRY_RUN=False to actually observe a "
              "real swap's result. Set DRY_RUN=false in your .env for this run only, then set "
              "it back before running the main bot again if you don't want live trading.")
        sys.exit(1)

    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)

    print(f"\nThis run covers the 2026-09-03 batch: TNEWS, DEGENAP, IDEV across their "
          f"{len(COVERAGE_TESTS) - 2} active pools, PLUS two schema probes for the venues that "
          f"were deliberately left parked (LUNC_DFC_POOL_UNKNOWN and Terra.pump TNEWS/LUNC). "
          f"The probes query before they swap — they will NOT send a real transaction if the "
          f"contract's response shape doesn't match what DexPool assumes. ELPACO and GRDX "
          f"appear only as already-tax-confirmed counterparties for sourcing — not re-tested "
          f"themselves.")

    failed = []
    for name, fn in COVERAGE_TESTS:
        try:
            fn(terra)
        except Exception as e:
            failed.append(name)
            print(f"\n!!! {name} test raised an unhandled error — SKIPPING to the next item "
                  f"rather than aborting the whole run: {e}")
            log.exception("%s test failed", name)

    if failed:
        print(f"\n{len(failed)} test(s) hit an unhandled error and were skipped: {', '.join(failed)}. "
              f"Read the errors above — these still need to be understood before trusting those "
              f"pools live, even though the rest of the run completed.")

    print("\nDone. For the round-trip tests: add/update TNEWS_TRANSFER_TAX_BPS / "
          "DEGENAP_TRANSFER_TAX_BPS / IDEV_TRANSFER_TAX_BPS in config.py (and wire them into "
          "cw20_transfer_tax_rate's rates dict, or CW20_DIRECTIONAL_TAX_BPS if in/out differ) "
          "for anything that showed a real (>5bps) gap above, then re-run once more to confirm "
          "stability. For the two probes: follow each one's printed RECOMMENDATION — un-park "
          "and wire into arbitrage_bot.py only if it says the venue is confirmed clean.")


if __name__ == "__main__":
    main()