"""
Investigates the multi-hop router contract before this bot builds any real
execution path around it. Two questions matter here, in order of
importance:

1. Does execute_swap_operations support a route-level minimum_receive (or
   max_spread) parameter? The example transaction that prompted this
   script had NEITHER field set — that's a red flag about THAT specific
   trade (it was submitted with zero slippage protection at the route
   level), not proof the contract lacks the capability. Most Astroport-
   Router-family contracts (which this looks closely modeled on — explicit
   pool_address + pool_type per hop, no factory lookup) support
   minimum_receive as an OPTIONAL field alongside operations. We need to
   know for sure before this bot ever omits it the way that example did.

2. Does the router expose a read-only simulate_swap_operations query? If
   so, that's strong secondary confirmation of the standard Astroport
   Router schema (which does support minimum_receive on the execute side),
   AND it's independently useful — it would let the bot price a WHOLE
   route in one query instead of the current per-pool simulate_swap()
   math, which could also help with latency.

This script is READ-ONLY — queries only, no execute message is built or
simulated here. No risk, no funds involved, nothing broadcasts.

Run: python inspect_router_contract.py
"""
import json
import config
from pool_client import _query_contract_raw

ROUTER_ADDRESS = "terra10nsx5r4rlls46dljmvynnffkz8meqr7kvteec4cx5xa9dyteetnqvq9zhd"

# Same two-hop route shape from the example transaction (LUNC -> TERRA ->
# some CW20), reused here just to probe the simulate query if one exists.
# Amount is irrelevant for what we're testing — small and arbitrary.
PROBE_OPERATIONS = [
    {
        "ask_asset_info": {"token": {"contract_addr": "terra1ex0hjv3wurhj4wgup4jzlzaqj4av6xqd8le4etml7rg9rs207y4s8cdvrp"}},
        "offer_asset_info": {"native_token": {"denom": "uluna"}},
        "pool_address": "terra1rlfns43umzqszm52txxmnseevffx2pe408c99m7cnvd828tdj67q9ftjs2",
        "pool_type": "terraport",
    },
]
PROBE_AMOUNT = config.SMOKE_TEST_AMOUNT_ULUNA  # 2 LUNC


def try_query(label, msg):
    print(f"\n{label}")
    print(f"  query: {json.dumps(msg)}")
    print("-" * 70)
    try:
        resp = _query_contract_raw(ROUTER_ADDRESS, msg)
        print(json.dumps(resp, indent=2))
        return resp
    except Exception as e:
        print(f">>> FAILED: {e}")
        return None


def main():
    print(f"Router contract: {ROUTER_ADDRESS}\n")

    # Common Astroport-Router-family introspection query — may or may not
    # exist on this specific contract, worth trying regardless.
    try_query("Attempt 1: {\"config\": {}}", {"config": {}})

    # The most informative one: if this succeeds with output resembling
    # the execute message's shape, it strongly suggests this is a standard
    # Astroport Router fork, which supports minimum_receive on the execute
    # side even though the example transaction didn't set it.
    try_query(
        "Attempt 2: {\"simulate_swap_operations\": {...}}",
        {"simulate_swap_operations": {
            "offer_amount": str(PROBE_AMOUNT),
            "operations": PROBE_OPERATIONS,
        }},
    )

    print("\n" + "=" * 70)
    print("If Attempt 2 succeeded: this is almost certainly a standard Astroport")
    print("Router fork, meaning execute_swap_operations should accept an optional")
    print("minimum_receive (Uint128) field alongside 'operations' — the example tx")
    print("just chose not to set it. Paste this whole output back and I'll build the")
    print("execution path WITH minimum_receive derived from the same snapshot math")
    print("already used per-leg today, applied to the whole route instead.")
    print()
    print("If Attempt 2 failed (e.g. unrecognized query variant): this contract may")
    print("use a different schema than standard Astroport Router. Paste the error —")
    print("it'll tell us what query variant IS expected, the same way White Whale's")
    print("KeyError told us its actual response shape.")


if __name__ == "__main__":
    main()