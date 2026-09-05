"""
Prints the RAW {"simulation": {...}} response from White Whale's LUNC/USTC
pool. pool_client.DexPool._resolve_commission_via_simulation already
confirmed this response is missing a top-level "commission_amount" field
(the shape Terraswap/Astroport use) — this script exists to see what
field names it uses INSTEAD, so _resolve_commission_via_simulation can be
taught to read White Whale's actual shape rather than running on the
unverified 0.003 default forever.

Read-only query, same as every other pool-state fetch this bot already
does constantly — no risk, no funds involved.

Run: python inspect_whitewhale_simulation.py
"""
import json
import config
from pool_client import _query_contract_raw

PROBE_AMOUNT_ULUNA = config.SMOKE_TEST_AMOUNT_ULUNA  # 2 LUNC, arbitrary


def main():
    resp = _query_contract_raw(config.WHITEWHALE_POOL_LUNC_USTC, {
        "simulation": {
            "offer_asset": {
                "info": {"native_token": {"denom": config.DENOM_LUNC}},
                "amount": str(PROBE_AMOUNT_ULUNA),
            }
        }
    })
    print(json.dumps(resp, indent=2))
    print()
    print("Look for whatever field(s) represent the swap fee — common alternate names")
    print("on White Whale-family contracts include 'swap_fee_amount',")
    print("'protocol_fee_amount', or a fee expressed as a rate rather than an amount.")
    print("Paste this whole output back and the commission resolver can be patched")
    print("to read it correctly instead of falling back to the 0.003 default.")


if __name__ == "__main__":
    main()