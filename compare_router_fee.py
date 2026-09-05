"""
Empirically measures the router contract's real fee impact by comparing
its simulate_swap_operations output against this bot's OWN direct-pool
math (amm_math.simulate_swap + tax.calculate_tax) for the EXACT SAME hop,
same reserves snapshot, same moment. The delta between them, if any, IS
the router's fee_percent (confirmed present in its config query — "0.05",
meaning unconfirmed whether that's 5% or 0.05%) actually being applied.

Why compare instead of trust the config field's number directly: the field
name alone doesn't tell us the string's convention (0.05 could mean 5% or
0.05% depending on this contract's own convention, which we don't know),
NOR whether it even applies to every route or only specific token flows
(the "terrapump_factory" field in that same config suggests this router
may be launchpad-oriented infrastructure with general routing as a
secondary feature — its fee behavior might not be uniform). An empirical
side-by-side comparison sidesteps all of that: whatever the router's
number turns out to be, this shows exactly how many uluna/utoken it costs
in practice for a real hop, right now.

Read-only — two queries, no execute message built, nothing broadcasts.

Run: python compare_router_fee.py
"""
import json
from decimal import Decimal

import config
from assets import Asset
from pool_client import DexPool, _query_contract_raw
from terra_client import TerraClient
from amm_math import simulate_swap
import tax as tax_module

ROUTER_ADDRESS = "terra10nsx5r4rlls46dljmvynnffkz8meqr7kvteec4cx5xa9dyteetnqvq9zhd"
# Multiple probe sizes — a genuine percentage-based fee (e.g. 0.05%) could
# easily round away to 0 difference at a tiny 2 LUNC probe but would show
# up unmistakably at a size actually comparable to what this bot really
# trades. The first exact-match result at 2 LUNC was good news, but not
# enough on its own to rule out a small % fee hiding in integer rounding.
PROBE_AMOUNTS_ULUNA = [
    config.SMOKE_TEST_AMOUNT_ULUNA,   # 2 LUNC — original probe
    500_000_000,                       # 500 LUNC
    50_000_000_000,                    # 50,000 LUNC — comparable to real cycle sizes seen in logs
]

# Same hop as the example transaction: LUNC -> TERRA via Terraport's
# TERRA/LUNC pool.
TERRA_LUNC_POOL = config.TERRAPORT_POOL_TERRA_LUNC


def check_one_size(pool, state, reserve_lunc, reserve_terra, lunc, terra_token, probe_amount):
    taxed_offer = probe_amount - tax_module.calculate_tax(probe_amount, lunc)
    direct_result = simulate_swap(taxed_offer, reserve_lunc, reserve_terra, pool.commission_rate)
    direct_received = direct_result.return_amount - tax_module.calculate_tax(
        direct_result.return_amount, terra_token)

    router_resp = _query_contract_raw(ROUTER_ADDRESS, {
        "simulate_swap_operations": {
            "offer_amount": str(probe_amount),
            "operations": [{
                "ask_asset_info": {"token": {"contract_addr": config.TERRA_CW20_ADDRESS}},
                "offer_asset_info": {"native_token": {"denom": config.DENOM_LUNC}},
                "pool_address": TERRA_LUNC_POOL,
                "pool_type": "terraport",
            }],
        }
    })
    router_received = int(router_resp["amount"])

    print(f"\nProbe: {probe_amount} uluna ({probe_amount / 1_000_000:,.2f} LUNC)")
    print(f"  Direct pool math: {direct_received} uTERRA")
    print(f"  Router output:    {router_received} uTERRA")
    if router_received >= direct_received:
        print(f"  >>> No fee detected at this size.")
    else:
        diff = direct_received - router_received
        pct = (Decimal(diff) / Decimal(direct_received)) * 100 if direct_received > 0 else Decimal(0)
        print(f"  >>> Router LOWER by {diff} uTERRA ({pct:.4f}%)")


def main():
    config.validate()
    terra = TerraClient()

    lunc = Asset(kind="native", id=config.DENOM_LUNC, decimals=6, display="LUNC")
    terra_token = Asset(kind="cw20", id=config.TERRA_CW20_ADDRESS,
                         decimals=config.TERRA_DECIMALS, display="TERRA")

    pool = DexPool("Terraport TERRA/LUNC", terra, TERRA_LUNC_POOL,
                    terra_token, lunc, config.TERRAPORT_COMMISSION_RATE)
    state = pool.get_state()
    reserve_lunc = state.reserves[lunc.key()]
    reserve_terra = state.reserves[terra_token.key()]

    print(f"Live TERRA/LUNC reserves right now: {reserve_lunc} uluna / {reserve_terra} uTERRA")
    print(f"Pool's own commission_rate: {pool.commission_rate}")

    for probe_amount in PROBE_AMOUNTS_ULUNA:
        check_one_size(pool, state, reserve_lunc, reserve_terra, lunc, terra_token, probe_amount)

    print("\n" + "=" * 70)
    print("If NONE of the sizes above showed a router shortfall: the 'fee_percent' in")
    print("its config genuinely doesn't apply to plain Terraport-pool hops like this")
    print("one — likely a fee specific to terrapump-launched token flows instead,")
    print("given the 'terrapump_factory' field in that same config. Safe to move")
    print("forward on the router integration.")
    print()
    print("If a shortfall showed up ONLY at the larger sizes: that's the 0.05%-style")
    print("percentage fee that was hiding in integer rounding at 2 LUNC. Paste this")
    print("whole output back either way and I'll act on exactly what it shows.")


if __name__ == "__main__":
    main()