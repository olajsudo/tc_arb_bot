"""
Verifies two things per pool, against the pool's OWN on-chain {"pair": {}}
response — not against the pasted label it was given:

  1. Curve type: amm_math.simulate_swap ONLY implements xyk (constant-
     product) math. A pair_type of "stable" or "concentrated" (or White
     Whale's own "constant_product" string, which IS xyk under a different
     name) needs to be told apart correctly, or every return_amount/
     spread/commission this bot computes for that pool is silently wrong.

  2. Asset pairing: confirmed the hard way on 2026-08-02 — two of the
     three Astroport addresses in a pasted pool list had their labels
     swapped relative to their real on-chain assets (one supposedly
     "ampLUNC/LUNC" turned out to be a plain LUNC/USTC pool with no CW20
     at all; the address labeled "ampLUNC/USTC (big)" was actually
     ampLUNC/LUNC). A pool with the wrong assets configured doesn't just
     miscalculate — get_state() will KeyError trying to find a reserve key
     that isn't there, or worse, silently key against the wrong asset if
     the mismatch happens to still resolve.

Run: python check_pool_curve_type.py
"""
import json
import config
from pool_client import _query_contract_raw

# (display name, contract address, expected asset_infos as a list of
# (kind, id) tuples) — this is every pool added since 2026-08-01 whose
# labels came from a manually pasted list, INCLUDING the FUTURE pools
# that are already enabled and trading live off unverified labels.
POOLS_TO_CHECK = [
    ("White Whale LUNC/USTC", config.WHITEWHALE_POOL_LUNC_USTC,
     [("native", config.DENOM_LUNC), ("native", config.DENOM_USTC)]),
    ("Astroport LUNC/USTC", config.ASTROPORT_POOL_LUNC_USTC,
     [("native", config.DENOM_LUNC), ("native", config.DENOM_USTC)]),
    ("Astroport ampLUNC/LUNC", config.AMPLUNC_ASTROPORT_POOL_LUNC,
     [("cw20", config.AMPLUNC_CW20_ADDRESS), ("native", config.DENOM_LUNC)]),
    ("Astroport ampLUNC/USTC", config.AMPLUNC_ASTROPORT_POOL_USTC,
     [("cw20", config.AMPLUNC_CW20_ADDRESS), ("native", config.DENOM_USTC)]),
    ("Terraswap ampLUNC/USTC 1", config.AMPLUNC_TERRASWAP_POOL_USTC_1,
     [("cw20", config.AMPLUNC_CW20_ADDRESS), ("native", config.DENOM_USTC)]),
    ("Terraswap ampLUNC/USTC 2", config.AMPLUNC_TERRASWAP_POOL_USTC_2,
     [("cw20", config.AMPLUNC_CW20_ADDRESS), ("native", config.DENOM_USTC)]),
    ("Terraswap ampLUNC/LUNC", config.AMPLUNC_TERRASWAP_POOL_LUNC,
     [("cw20", config.AMPLUNC_CW20_ADDRESS), ("native", config.DENOM_LUNC)]),
    # These three are ALREADY ENABLED in arbitrage_bot.py, trading live off
    # labels that were never independently checked against on-chain data —
    # given what just turned up on the Astroport pools, check these first.
    ("Terraport FUTURE/LUNC (LIVE)", config.FUTUREFLARE_POOL_FUTURE_LUNC,
     [("cw20", config.FUTURE_CW20_ADDRESS), ("native", config.DENOM_LUNC)]),
    ("Terraport FUTURE/TERRA (LIVE)", config.FUTUREFLARE_POOL_FUTURE_TERRA,
     [("cw20", config.FUTURE_CW20_ADDRESS), ("cw20", config.TERRA_CW20_ADDRESS)]),
    ("Terraport FUTURE/TRIT (LIVE)", config.FUTUREFLARE_POOL_FUTURE_TRIT,
     [("cw20", config.FUTURE_CW20_ADDRESS), ("cw20", config.TRIT_CW20_ADDRESS)]),
]


def _actual_assets(resp: dict):
    """Extracts (kind, id) pairs from a {"pair":{}} response's asset_infos,
    in whatever order the contract returned them."""
    out = []
    for info in resp.get("asset_infos", []):
        if "native_token" in info:
            out.append(("native", info["native_token"]["denom"]))
        elif "token" in info:
            out.append(("cw20", info["token"]["contract_addr"]))
        else:
            out.append(("unknown", str(info)))
    return out


def main():
    for name, address, expected in POOLS_TO_CHECK:
        print(f"\n{name}  ({address})")
        print("-" * 70)
        try:
            resp = _query_contract_raw(address, {"pair": {}})
            pair_type = resp.get("pair_type")
            actual = _actual_assets(resp)
            print(json.dumps(resp, indent=2))

            # --- curve type ---
            is_xyk = False
            if pair_type is not None:
                if isinstance(pair_type, str):
                    is_xyk = pair_type == "constant_product"
                else:
                    is_xyk = "xyk" in pair_type
            if pair_type is None:
                print(">>> CURVE: no 'pair_type' field — may not be an Astroport-family "
                      "pair contract at all. Check the venue's own explorer/docs.")
            elif is_xyk:
                print(">>> CURVE: confirmed xyk/constant-product — safe for amm_math.py.")
            else:
                print(f">>> CURVE: NOT xyk (pair_type={pair_type}) — amm_math.py's formula "
                      "will be WRONG for this pool. Do not trade it as-is.")

            # --- asset pairing ---
            if set(actual) == set(expected):
                print(">>> ASSETS: match the configured pairing.")
            else:
                print(f">>> ASSETS: MISMATCH. Configured as {expected}, "
                      f"actual on-chain is {actual}. Fix config.py/arbitrage_bot.py "
                      "before trusting or trading this pool.")
        except Exception as e:
            print(f">>> Query failed: {e}")
            print(">>> Could mean this address isn't a standard pair contract, or "
                  "doesn't implement {\"pair\": {}} — check the venue's own explorer.")


if __name__ == "__main__":
    main()