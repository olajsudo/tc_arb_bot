"""
Probe script for the 14 pools + 4 tokens (LIX, LTK, ELPACO, ROTTI) added to
config.py on 2026-09-01 at the user's request.

None of these are wired into arbitrage_bot.build_pools_and_assets() yet, and
they're deliberately left out of config.validate()'s required list too — per
this project's own established convention (see config.py's JEFF/DFC/JURIS-
GRDX comment from 2026-08-29), nothing new gets attached to the live pool
list until a probe like this one confirms:

  1. Which client class applies — DexPool for a Terraswap/Terraport/
     LuncSwap-family {"pair":{}} + {"assets":[...]} shape, GarudaPool for
     pair_base's {"pool":{}}-only asset1/asset2/reserve1/reserve2 shape
     (see pool_client.GarudaPool's docstring for the exact difference).
  2. That the two assets actually on each pool match what the user's
     labels claim (catches typos/copy errors in the supplied addresses
     before they'd otherwise surface as a confusing runtime KeyError deep
     in DexPool.get_state/GarudaPool.get_state).
  3. Roughly-sane reserves (not a rug-pulled or near-empty pool).

This does NOT confirm the swap ExecuteMsg schema (the write path) — same
caveat as every prior probe script here. LuncSwap JURIS/USDC and LuncSwap
TERRA/USDC both needed the CW20 Send-hook + min_receive handling in
executor.py rather than plain build_swap_msg; don't assume LuncSwap.fun's
LUNC/LIX pool matches either shape without checking after this probe, most
likely by watching the first real (tiny) trade closely — same "trust but
verify" bar as every venue in this bot got before being trusted with size.

Run: python probe_new_pools_20260901.py
Needs a live LCD endpoint (config.LCD_URLS) — this only does read-only
queries, nothing is ever broadcast.
"""
import json
import logging

import config
from pool_client import _query_contract_raw

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("probe")

# (label, pool_address, expected_asset_a_display, expected_asset_a_id,
#  expected_asset_b_display, expected_asset_b_id)
POOLS_TO_PROBE = [
    ("LuncSwap.fun LUNC/LIX", config.LUNCSWAP_POOL_LUNC_LIX,
     "LUNC", config.DENOM_LUNC, "LIX", config.LIX_CW20_ADDRESS),
    ("Garuda LUNC/LIX", config.GARUDA_POOL_LUNC_LIX,
     "LUNC", config.DENOM_LUNC, "LIX", config.LIX_CW20_ADDRESS),
    ("Garuda LTK/LIX", config.GARUDA_POOL_LTK_LIX,
     "LTK", config.LTK_CW20_ADDRESS, "LIX", config.LIX_CW20_ADDRESS),
    ("Terraswap LUNC/LTK", config.TERRASWAP_POOL_LUNC_LTK,
     "LUNC", config.DENOM_LUNC, "LTK", config.LTK_CW20_ADDRESS),
    ("Garuda LUNC/LTK", config.GARUDA_POOL_LUNC_LTK,
     "LUNC", config.DENOM_LUNC, "LTK", config.LTK_CW20_ADDRESS),
    ("Garuda USDC/LTK", config.GARUDA_POOL_USDC_LTK,
     "USDC", config.DENOM_USDC, "LTK", config.LTK_CW20_ADDRESS),
    ("Garuda USTC/LTK", config.GARUDA_POOL_USTC_LTK,
     "USTC", config.DENOM_USTC, "LTK", config.LTK_CW20_ADDRESS),
    ("Garuda LTK/ELPACO", config.GARUDA_POOL_LTK_ELPACO,
     "LTK", config.LTK_CW20_ADDRESS, "ELPACO", config.ELPACO_CW20_ADDRESS),
    ("Garuda LUNC/ELPACO", config.GARUDA_POOL_LUNC_ELPACO,
     "LUNC", config.DENOM_LUNC, "ELPACO", config.ELPACO_CW20_ADDRESS),
    ("Garuda ROTTI/LUNC", config.GARUDA_POOL_ROTTI_LUNC,
     "ROTTI", config.ROTTI_CW20_ADDRESS, "LUNC", config.DENOM_LUNC),
    ("Garuda FUN/ROTTI", config.GARUDA_POOL_FUN_ROTTI,
     "FUN", config.FUN_CW20_ADDRESS, "ROTTI", config.ROTTI_CW20_ADDRESS),
    ("Terraport ROTTI/LUNC", config.TERRAPORT_POOL_ROTTI_LUNC,
     "ROTTI", config.ROTTI_CW20_ADDRESS, "LUNC", config.DENOM_LUNC),
    ("Garuda ROTTI/JURIS", config.GARUDA_POOL_ROTTI_JURIS,
     "ROTTI", config.ROTTI_CW20_ADDRESS, "JURIS", config.JURIS_CW20_ADDRESS),
    ("Garuda ROTTI/GRDX", config.GARUDA_POOL_ROTTI_GRDX,
     "ROTTI", config.ROTTI_CW20_ADDRESS, "GRDX", config.GRDX_CW20_ADDRESS),
]


def _ids_from_pool_response(data: dict):
    """Returns {kind:id -> reserve} for a Garuda-shape {"pool":{}} response."""
    out = {}
    for info_key, reserve_key in (("asset1", "reserve1"), ("asset2", "reserve2")):
        info = data[info_key]
        if "native" in info:
            out[f"native:{info['native']}"] = int(data[reserve_key])
        elif "cw20" in info:
            out[f"cw20:{info['cw20']}"] = int(data[reserve_key])
    return out


def _ids_from_assets_list(assets: list):
    """Returns {kind:id -> amount} for a Terraswap-shape {"assets":[...]} list."""
    out = {}
    for a in assets:
        info = a["info"]
        if "native_token" in info:
            out[f"native:{info['native_token']['denom']}"] = int(a["amount"])
        else:
            out[f"cw20:{info['token']['contract_addr']}"] = int(a["amount"])
    return out


_NATIVE_DENOMS = {config.DENOM_LUNC, config.DENOM_USTC, config.DENOM_USDC, config.DENOM_USDC_AXL}


def probe_one(label, addr, a_disp, a_id, b_disp, b_id):
    log.info("--- %s (%s) ---", label, addr)
    expected_a = f"native:{a_id}" if a_id in _NATIVE_DENOMS else f"cw20:{a_id}"
    expected_b = f"native:{b_id}" if b_id in _NATIVE_DENOMS else f"cw20:{b_id}"

    pair_shape = None
    try:
        pair_resp = _query_contract_raw(addr, {"pair": {}})
        pair_shape = "terraswap-family"
        log.info("  {\"pair\":{}} succeeded -> pair_type=%s",
                  pair_resp.get("pair_type", "<not present>"))
    except Exception as e:
        log.info("  {\"pair\":{}} failed (%s) -- expected for a genuine Garuda "
                  "pair_base contract, not necessarily a problem.", type(e).__name__)

    reserves = None
    recommended = None
    try:
        pool_resp = _query_contract_raw(addr, {"pool": {}})
        if "assets" in pool_resp:
            reserves = _ids_from_assets_list(pool_resp["assets"])
            shape = "terraswap-family ({\"assets\":[...]})"
            recommended = "DexPool"
        elif "asset1" in pool_resp:
            reserves = _ids_from_pool_response(pool_resp)
            shape = "garuda pair_base (asset1/asset2/reserve1/reserve2)"
            recommended = "GarudaPool"
        else:
            shape = f"UNRECOGNIZED shape: keys={list(pool_resp.keys())}"
        log.info("  {\"pool\":{}} shape: %s -> recommended client class: %s",
                  shape, recommended or "NEITHER -- investigate manually")
    except Exception as e:
        log.error("  {\"pool\":{}} FAILED (%s) -- cannot confirm this pool at all.",
                   type(e).__name__)
        return

    if reserves is None:
        return

    log.info("  reserves: %s", json.dumps(reserves, indent=2))
    ok_a = expected_a in reserves
    ok_b = expected_b in reserves
    if ok_a and ok_b:
        log.info("  ASSET MATCH OK: both %s and %s found on this pool.", a_disp, b_disp)
    else:
        log.error("  ASSET MISMATCH: expected %s (%s) and %s (%s) -- got keys %s. "
                   "Do NOT wire this pool in until this is resolved; the address may "
                   "be for a different pair than labeled.",
                   a_disp, expected_a, b_disp, expected_b, list(reserves.keys()))


def main():
    log.info("Probing %d new pool addresses read-only. Nothing will be broadcast.\n", len(POOLS_TO_PROBE))
    for label, addr, a_disp, a_id, b_disp, b_id in POOLS_TO_PROBE:
        probe_one(label, addr, a_disp, a_id, b_disp, b_id)
        log.info("")
    log.info("Next: for every pool that showed ASSET MATCH OK, add a DexPool(...) "
              "entry (if {\"pool\":{}} returned an \"assets\" list) or GarudaPool(...) "
              "entry (if it returned asset1/asset2/reserve1/reserve2) to "
              "arbitrage_bot.build_pools_and_assets(), then add its config constant "
              "to config.validate()'s required list -- same two steps every prior "
              "batch (JEFF/DFC, MOON, GDEX/GRDX) went through. Do this only for pools "
              "that passed; leave mismatched ones out and go back to the user for the "
              "correct address.")


if __name__ == "__main__":
    main()