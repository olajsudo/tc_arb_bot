"""
READ-ONLY interface/curve-type probe for the 3 disabled WESO pools and 5
disabled Garuda pools added 2026-08-04. SAFE to run any time — the
{"pair":{}} / {"pool":{}} checks cost nothing (pure LCD queries), and the
swap-message check only calls TerraClient.simulate_fee(), which signs a
tx LOCALLY and asks the chain to estimate gas for it — it never
broadcasts, so no funds or gas are spent even if it fails. This is the
exact method smoke_test_juris.py originally used to catch Garuda JURIS/
LUNC's "pair_base" schema mismatch, at zero cost, before any real swap
was attempted.

Run this BEFORE running smoke_test_new_tokens.py (which does spend real,
tiny amounts) and BEFORE uncommenting any of these pools in
arbitrage_bot.py's pool list.

A pool only "PASS"es if all checks succeed AND the asset_infos in check 1
actually match what we think this pool contains — see the 2026-08-02
pasted-label mixups on the ampLUNC/Astroport pools in config.py for why
the pairing itself, not just the interface, needs independent
confirmation.

IMPORTANT CAVEAT for check 3 on CW20/CW20 pools (WESO JURIS/cwLUNC,
Garuda BENANCE/JURIS, GDEX/GRDX, FUN/GDEX): the chain-side simulation
checks the sender actually holds enough of the offered CW20 to cover the
Send amount, same as a real broadcast would. If the wallet holds zero of
the probe asset, check 3 will fail with an insufficient-balance-shaped
error that has NOTHING to do with schema compatibility — this script
tries to detect that case and label it INCONCLUSIVE rather than FAIL, but
always read the raw error text yourself before concluding a pool is
actually broken.

GARUDA-SPECIFIC HANDLING (added 2026-08-04, round 2, after
probe_garuda_schema.py confirmed the real message schema): Garuda's
`pair_base` contracts are NOT Terraswap/Astroport-compatible —
confirmed via real queries:
  - No {"pair":{}} query exists at all (QueryMsg only has `pool`,
    `simulate_provide_liquidity`, `simulate_withdraw_liquidity`,
    `user_position`, `simulate_swap`) — check 1 below uses {"pool":{}}
    for Garuda pools instead, which returns asset1/asset2/reserve1/
    reserve2 rather than Terraswap's assets:[{info,amount}] shape.
  - There is no pair_type field either, so curve type can't be
    independently confirmed here the way check_pool_curve_type.py does
    for other venues — Garuda pools are trusted to be xyk based on
    Garuda's own docs (see config.GARUDA_COMMISSION_RATE's comment),
    not on-chain confirmation. Flagged in the PASS output as a caveat.
  - The swap message is built via executor.build_swap_msg_garuda (CONFIRMED
    schema, see that function's docstring) instead of build_swap_msg.

Run: python check_new_venues_interface.py
"""
import json
import logging

import config
from assets import Asset
from terra_client import TerraClient
from executor import build_swap_msg, build_swap_msg_garuda

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("check_new_venues")

lunc = Asset(kind="native", id=config.DENOM_LUNC, decimals=6, display="LUNC")
ustc = Asset(kind="native", id=config.DENOM_USTC, decimals=6, display="USTC")
cwlunc_token = Asset(kind="cw20", id=config.CWLUNC_CW20_ADDRESS, decimals=config.CWLUNC_DECIMALS, display="cwLUNC")
cwustc_token = Asset(kind="cw20", id=config.CWUSTC_CW20_ADDRESS, decimals=config.CWUSTC_DECIMALS, display="cwUSTC")
juris_token = Asset(kind="cw20", id=config.JURIS_CW20_ADDRESS, decimals=config.JURIS_DECIMALS, display="JURIS")
benance_token = Asset(kind="cw20", id=config.BENANCE_CW20_ADDRESS, decimals=config.BENANCE_DECIMALS, display="BENANCE")
gdex_token = Asset(kind="cw20", id=config.GDEX_CW20_ADDRESS, decimals=config.GDEX_DECIMALS, display="GDEX")
grdx_token = Asset(kind="cw20", id=config.GRDX_CW20_ADDRESS, decimals=config.GRDX_DECIMALS, display="GRDX")
fun_token = Asset(kind="cw20", id=config.FUN_CW20_ADDRESS, decimals=config.FUN_DECIMALS, display="FUN")

# (pool_address, name, asset_a, asset_b, probe_asset, is_garuda)
# probe_asset is chosen to be the side most likely to actually be in the
# wallet already, to avoid a false "insufficient balance" reading on
# check 3 — native sides are always safe; for CW20/CW20 pools this picks
# whichever token this bot already trades elsewhere (JURIS, GDEX), but
# even that isn't guaranteed non-zero, hence the INCONCLUSIVE handling.
# CORRECTED 2026-08-04 from the first real run's results:
#   - The originally-supplied "cwLUNC/LUNC" address is actually the WESO swap
#     ROUTER contract (wesoswap_router — confirmed via its real QueryMsg variant
#     list: simulate_swap_operations, execute_swap_operations, route, etc, not a
#     pair contract at all). There is nothing to check here with pair/pool
#     queries — a router needs a completely different integration than
#     pool_client.py's direct-pair-query architecture. Left out of this list
#     entirely; if a genuine WESO cwLUNC/LUNC PAIR contract exists, its address
#     still needs to be found before it can be tested this way.
#   - The originally-supplied "cwUSTC/USTC" address's real {"pair":{}} response
#     shows it is actually a cwLUNC/cwUSTC pool (both CW20, no native USTC in it
#     at all) — same kind of pasted-label mixup the ampLUNC/Astroport pools had.
#     Corrected below to reflect its real contents; the ORIGINAL native cwUSTC/
#     USTC pairing (if it exists on WESO) still needs its own real address.
POOLS_TO_CHECK = [
    (config.WESO_POOL_CWLUNC_CWUSTC, "WESO cwLUNC/cwUSTC (was mislabeled cwUSTC/USTC)",
     cwlunc_token, cwustc_token, cwlunc_token, False),
    (config.WESO_POOL_JURIS_CWLUNC, "WESO JURIS/cwLUNC", juris_token, cwlunc_token, juris_token, False),
    (config.GARUDA_POOL_BENANCE_LUNC, "Garuda BENANCE/LUNC", benance_token, lunc, lunc, True),
    (config.GARUDA_POOL_BENANCE_JURIS, "Garuda BENANCE/JURIS", benance_token, juris_token, juris_token, True),
    (config.GARUDA_POOL_GDEX_LUNC, "Garuda GDEX/LUNC", gdex_token, lunc, lunc, True),
    (config.GARUDA_POOL_GDEX_GRDX, "Garuda GDEX/GRDX", gdex_token, grdx_token, gdex_token, True),
    (config.GARUDA_POOL_FUN_GDEX, "Garuda FUN/GDEX", fun_token, gdex_token, gdex_token, True),
    (config.GARUDA_POOL_GRDX_LUNC, "Garuda GRDX/LUNC", grdx_token, lunc, lunc, True),
    (config.GARUDA_POOL_JURIS_LUNC, "Garuda JURIS/LUNC", juris_token, lunc, lunc, True),
]

PROBE_AMOUNT = 2_000_000  # base units — bumped 2026-08-04: WESO JURIS/cwLUNC rejected the
# original 1000-unit probe with "minimum offer amount not met... minimum=1000000", a
# business-logic rejection, not a schema one. Simulate_fee never broadcasts regardless of
# amount, so there's no extra cost to using a bigger probe — this comfortably clears that
# observed minimum with margin.
_BALANCE_ERROR_HINTS = ("insufficient", "balance", "cannot sub", "overflow")
_MIN_ORDER_SIZE_HINTS = ("minimum offer amount", "minimum=")


def _asset_from_garuda_info(info: dict) -> Asset:
    """Parses a Garuda pair_base {"pool":{}} asset1/asset2 entry — see
    pool_client._asset_from_garuda_info for the confirmed shape this
    mirrors (kept as a local, dependency-free copy for this script)."""
    if "native" in info:
        denom = info["native"]
        return Asset(kind="native", id=denom, decimals=6, display=denom)
    if "cw20" in info:
        addr = info["cw20"]
        return Asset(kind="cw20", id=addr, decimals=6, display=addr)
    raise ValueError(f"Unrecognized Garuda pair_base asset info shape: {info}")


def check_pool_garuda(terra, account_number, sequence, pool_address, name, asset_a, asset_b, probe_asset):
    print(f"\n=== {name} ({pool_address}) [Garuda pair_base] ===")
    status = "PASS"

    try:
        pool_info = terra.query_contract(pool_address, {"pool": {}})
        print("  [1/2] {\"pool\":{}} OK:", json.dumps(pool_info)[:300])
        seen = {_asset_from_garuda_info(pool_info["asset1"]).key(),
                _asset_from_garuda_info(pool_info["asset2"]).key()}
        expected = {asset_a.key(), asset_b.key()}
        if seen != expected:
            print(f"  \u26a0\ufe0f  asset mismatch — expected {expected}, got {seen}. "
                  f"This pool may not contain the pair we think it does.")
            status = "FAIL"
        print("  \u26a0\ufe0f  Garuda's pair_base exposes no pair_type field — curve type "
              "(assumed xyk per Garuda's own docs, see config.GARUDA_COMMISSION_RATE) "
              "is NOT independently confirmed here, unlike Terraswap-family pools.")
    except Exception as e:
        print(f"  [1/2] {{\"pool\":{{}}}} FAILED: {e}")
        status = "FAIL"

    try:
        msg = build_swap_msg_garuda(terra.address, pool_address, probe_asset, PROBE_AMOUNT, min_receive=0)
        terra.simulate_fee([msg], account_number=account_number, sequence=sequence)
        print("  [2/2] Garuda-schema swap message ACCEPTED by gas simulation.")
    except Exception as e:
        err_text = str(e).lower()
        if any(hint in err_text for hint in _MIN_ORDER_SIZE_HINTS):
            print(f"  [2/2] Garuda-schema swap message REJECTED: {e}")
            print("        Own minimum order size rejected the probe amount — the message "
                  "format was understood and parsed correctly. Increase PROBE_AMOUNT and re-run.")
            if status == "PASS":
                status = "INCONCLUSIVE (min order size — likely fine)"
        elif any(hint in err_text for hint in _BALANCE_ERROR_HINTS):
            print(f"  [2/2] Garuda-schema swap message REJECTED: {e}")
            print("        Looks balance-related, not schema-related (probe asset may not be "
                  "held in this wallet) — INCONCLUSIVE, not a confirmed schema failure.")
            if status == "PASS":
                status = "INCONCLUSIVE (balance)"
        else:
            print(f"  [2/2] Garuda-schema swap message REJECTED by gas simulation: {e}")
            print("        The CONFIRMED build_swap_msg_garuda schema still doesn't parse/pass "
                  "here — this specific pool may have its own quirk beyond the base pair_base "
                  "schema. Read the raw error text before assuming every Garuda pool shares "
                  "this fate.")
            status = "FAIL"

    print(f"  RESULT: {status}")
    return status


def check_pool(terra, account_number, sequence, pool_address, name, asset_a, asset_b, probe_asset):
    print(f"\n=== {name} ({pool_address}) ===")
    status = "PASS"

    try:
        pair_info = terra.query_contract(pool_address, {"pair": {}})
        print("  [1/3] {\"pair\":{}} OK:", json.dumps(pair_info)[:300])
        seen_ids = {Asset.from_chain_info(info).id for info in pair_info.get("asset_infos", [])}
        expected_ids = {asset_a.id, asset_b.id}
        if seen_ids != expected_ids:
            print(f"  \u26a0\ufe0f  asset_infos mismatch — expected {expected_ids}, got {seen_ids}. "
                  f"This pool may not contain the pair we think it does.")
            status = "FAIL"
        pair_type = str(pair_info.get("pair_type", "")).lower()
        if pair_type and "xyk" not in pair_type and "constant_product" not in pair_type:
            print(f"  \u26a0\ufe0f  pair_type={pair_info.get('pair_type')} — does NOT look like plain xyk. "
                  f"amm_math.simulate_swap only implements xyk; do not enable until resolved.")
            status = "FAIL"
        elif pair_type:
            print(f"  \u2713 curve type CONFIRMED xyk (pair_type={pair_info.get('pair_type')}).")
        else:
            print(f"  \u26a0\ufe0f  no pair_type field in this pool's response — curve type NOT "
                  f"independently confirmed (may still be xyk, just not self-reported by this "
                  f"contract). Treat as unconfirmed, not as a pass, until checked another way.")
    except Exception as e:
        print(f"  [1/3] {{\"pair\":{{}}}} FAILED: {e}")
        status = "FAIL"

    try:
        pool_info = terra.query_contract(pool_address, {"pool": {}})
        print("  [2/3] {\"pool\":{}} OK:", json.dumps(pool_info)[:300])
    except Exception as e:
        print(f"  [2/3] {{\"pool\":{{}}}} FAILED: {e}")
        status = "FAIL"

    try:
        msg = build_swap_msg(terra.address, pool_address, probe_asset, PROBE_AMOUNT)
        terra.simulate_fee([msg], account_number=account_number, sequence=sequence)
        print("  [3/3] Standard swap message ACCEPTED by gas simulation — schema looks Terraswap-compatible.")
    except Exception as e:
        err_text = str(e).lower()
        if any(hint in err_text for hint in _MIN_ORDER_SIZE_HINTS):
            print(f"  [3/3] Standard swap message REJECTED: {e}")
            print("        This pool's OWN minimum order size rejected the probe amount — "
                  "the message format itself was understood and parsed correctly (that's a "
                  "GOOD sign, not a schema failure). Increase PROBE_AMOUNT further and re-run "
                  "to get a clean pass, or treat this as schema-confirmed and move straight to "
                  "smoke_test_new_tokens.py with a real amount above this pool's minimum.")
            if status == "PASS":
                status = "INCONCLUSIVE (min order size — likely fine)"
        elif any(hint in err_text for hint in _BALANCE_ERROR_HINTS):
            print(f"  [3/3] Standard swap message REJECTED: {e}")
            print("        Looks balance-related, not schema-related (probe asset may not be held "
                  "in this wallet) — INCONCLUSIVE, not a confirmed schema failure. Re-run after "
                  "acquiring a tiny amount of the probe asset, or inspect the raw error yourself.")
            if status == "PASS":
                status = "INCONCLUSIVE (balance)"
        else:
            print(f"  [3/3] Standard swap message REJECTED by gas simulation: {e}")
            print("        This is the SAME failure mode that caught Garuda JURIS/LUNC's pair_base "
                  "schema mismatch — no funds or gas were spent, but this pool needs a custom "
                  "message-building path in executor.py before it can be traded.")
            status = "FAIL"

    print(f"  RESULT: {status}")
    return status


def main():
    config.validate()
    terra = TerraClient()
    account_number, sequence = terra.get_account_number_and_sequence()
    results = {}
    for pool_address, name, asset_a, asset_b, probe_asset, is_garuda in POOLS_TO_CHECK:
        if not pool_address:
            print(f"\n=== {name} === SKIPPED (no address configured)")
            continue
        checker = check_pool_garuda if is_garuda else check_pool
        results[name] = checker(terra, account_number, sequence, pool_address,
                                 name, asset_a, asset_b, probe_asset)

    print("\n\n=== SUMMARY ===")
    for name, status in results.items():
        print(f"  {status:<12} {name}")
    print("\nOnly uncomment a pool in arbitrage_bot.py's pool list after it shows PASS here. "
          "INCONCLUSIVE means re-check manually — don't treat it as a pass. A CW20/CW20 pool's "
          "own transfer tax still needs smoke_test_new_tokens.py afterward, even after a PASS "
          "here. For Garuda pools specifically, PASS here confirms interface/schema only — "
          "curve type (xyk) is trusted from Garuda's docs, not independently verified on-chain.")


if __name__ == "__main__":
    main()