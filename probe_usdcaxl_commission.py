"""
Diagnostic probe for the USDC.eth.axl commission-simulation anomaly found
on 2026-07-14: pool_client.py's _resolve_commission_via_simulation()
computes rate = commission_amount / (return_amount + spread_amount +
commission_amount), i.e. it assumes commission_amount is denominated in
the ASK (output) asset — the documented behavior for Terraswap/Astroport-
family SimulationResponse. For all three Terraport USDC.eth.axl pools,
that formula produced commission rates 20x+ below every other Terraport
pool's confirmed ~2% (e.g. 0.0000012 instead of ~0.02).

Working hypothesis: commission_amount for these pools is actually 2% of
the OFFER amount (fee-on-input), not the ask/raw_output — every observed
reading was EXACTLY 2% of the 1,000,000 probe_amount (20,000), regardless
of how wildly return_amount/spread_amount varied across the three pools.
That's inconsistent with a proportional fee on output unless the pool's
own internal accounting is different from every other pool in this bot.

This script does NOT change any bot behavior — it only issues read-only
`simulation` queries (same query pool_client.py already uses) at several
probe sizes and in both swap directions, so we can see directly whether:

  (a) commission_amount == round(offer_amount * 0.02) holds across
      different probe sizes (confirms fee-on-input, and that it's an
      exact 2% relationship rather than noise), and

  (b) whether that pattern is specific to USDC.eth.axl as the OFFER
      asset, or holds for the pool generally (i.e. also appears when
      offering the pool's OTHER asset — LUNC, USTC, or TERRA).

  (c) whether the implied spot price (return_amount / taxed-offer, for
      a small probe) is consistent with the pool's raw reserve ratio —
      a sanity check against a decimals-mismatch explanation instead of
      a fee-accounting one.

Also extended to probe the native-offer direction on every OTHER
Terraport pool this bot trades (TERRA/LUNC, TERRA/USTC, LCW/LUNC,
LCW/USTC, Terraport TRIT/LUNC, Terraport TRIT/USTC, JURIS/LUNC).
pool_client.py's resolver always probes by offering asset_x, which for
every one of these pools is the CW20 side by construction — so the
native-offer direction has never actually been checked against any of
them in production. If the same commission_amount-is-2%-of-input pattern
shows up there too, it means this bot has been relying on
default_commission being correct for native-offer legs on ALL of these
pools by accident, not just the three USDC.eth.axl ones.

Nothing here is used for trade sizing — it's read-only, informational,
and safe to run at any time, including while run_bot.sh is also running
(these are simulation queries, not state-changing).

Run: python probe_usdcaxl_commission.py
"""
from decimal import Decimal

import config
from assets import Asset
from terra_client import TerraClient

# Multiple probe sizes per offer asset, in that asset's own base units.
# Chosen to stay well under 5% of the shallowest reserve in play
# (USDC.eth.axl/TERRA has ~9.7M base units on the USDC.eth.axl side per
# the last observed pool state) while still spanning two orders of
# magnitude, so a fee-on-input relationship (which should hold exactly
# at ANY size, since it doesn't depend on pool curvature) is easy to
# tell apart from a fee-on-output relationship (which would also look
# ~linear at these sizes, but at a completely different ratio).
USDC_PROBE_AMOUNTS = [200_000, 1_000_000, 5_000_000]          # 0.2 / 1 / 5 USDC.eth.axl
NATIVE_PROBE_AMOUNTS = [1_000_000, 10_000_000, 100_000_000]    # in uluna/uusd
CW20_PROBE_AMOUNTS = [1_000_000, 10_000_000, 100_000_000]      # in TERRA base units


def simulate(terra, pair_address: str, offer_asset: Asset, offer_amount: int) -> dict:
    resp = terra.query_contract(pair_address, {
        "simulation": {
            "offer_asset": {
                "info": offer_asset.info(),
                "amount": str(offer_amount),
            }
        }
    })
    return {
        "return_amount": Decimal(str(resp["return_amount"])),
        "spread_amount": Decimal(str(resp["spread_amount"])),
        "commission_amount": Decimal(str(resp["commission_amount"])),
    }


def probe_direction(terra, pool_name: str, pair_address: str,
                     offer_asset: Asset, probe_amounts: list):
    print(f"\n  Offering {offer_asset} into {pool_name}:")
    print(f"  {'offer_amount':>14} {'return':>18} {'spread':>14} {'commission':>12} "
          f"{'comm/offer':>12} {'comm/raw_out':>13}")
    for amt in probe_amounts:
        try:
            r = simulate(terra, pair_address, offer_asset, amt)
        except Exception as e:
            print(f"  {amt:>14}  SIMULATION FAILED: {e}")
            continue
        raw_output = r["return_amount"] + r["spread_amount"] + r["commission_amount"]
        comm_over_offer = r["commission_amount"] / Decimal(amt) if amt else Decimal(0)
        comm_over_raw = (r["commission_amount"] / raw_output) if raw_output > 0 else Decimal(0)
        print(f"  {amt:>14} {int(r['return_amount']):>18} {int(r['spread_amount']):>14} "
              f"{int(r['commission_amount']):>12} {comm_over_offer:>12.6f} {comm_over_raw:>13.8f}")


def main():
    config.validate()
    terra = TerraClient()

    lunc = Asset(kind="native", id=config.DENOM_LUNC, decimals=6, display="LUNC")
    ustc = Asset(kind="native", id=config.DENOM_USTC, decimals=6, display="USTC")
    usdc_axl = Asset(kind="native", id=config.DENOM_USDC_AXL, decimals=6, display="USDC.eth.axl")
    terra_token = Asset(kind="cw20", id=config.TERRA_CW20_ADDRESS,
                         decimals=config.TERRA_DECIMALS, display="TERRA")

    pools = [
        ("Terraport USDC.eth.axl/LUNC", config.TERRAPORT_POOL_USDCAXL_LUNC, usdc_axl, lunc, NATIVE_PROBE_AMOUNTS),
        ("Terraport USDC.eth.axl/USTC", config.TERRAPORT_POOL_USDCAXL_USTC, usdc_axl, ustc, NATIVE_PROBE_AMOUNTS),
        ("Terraport USDC.eth.axl/TERRA", config.TERRAPORT_POOL_USDCAXL_TERRA, usdc_axl, terra_token, CW20_PROBE_AMOUNTS),
    ]

    # The other Terraport pools this bot trades. pool_client.py's resolver
    # always probes by offering asset_x, and for every one of these,
    # asset_x is the CW20 side (TERRA/LCW/TRIT/JURIS) by construction —
    # so the native-offer direction (LUNC or USTC in) has NEVER actually
    # been probed for any of them. That's exactly the direction where the
    # USDC.eth.axl pools showed commission_amount = 2% of the OFFER
    # amount instead of the ask/raw_output the resolver assumes. This
    # section closes that gap: same query, same NATIVE_PROBE_AMOUNTS,
    # just aimed at the untested side of each pool. (JURIS/TERRA is
    # excluded — it's CW20/CW20, no native side to probe.)
    other_terraport_native_offer_pools = [
        ("Terraport TERRA/LUNC", config.TERRAPORT_POOL_TERRA_LUNC, lunc),
        ("Terraport TERRA/USTC", config.TERRAPORT_POOL_TERRA_USTC, ustc),
        ("Terraport LCW/LUNC", config.TERRAPORT_POOL_LCW_LUNC, lunc),
        ("Terraport LCW/USTC", config.TERRAPORT_POOL_LCW_USTC, ustc),
        ("Terraport TRIT/LUNC", config.TERRAPORT_POOL_TRIT_LUNC, lunc),
        ("Terraport TRIT/USTC", config.TERRAPORT_POOL_TRIT_USTC, ustc),
        ("Terraport JURIS/LUNC", config.TERRAPORT_POOL_JURIS_LUNC, lunc),
    ]

    print("=" * 100)
    print("USDC.eth.axl commission anomaly probe")
    print("If commission/offer stays ~constant at 0.02 across all sizes AND both")
    print("directions -> fee-on-input, contract-wide (needs a resolver fix, not just")
    print("a reject-and-fallback). If it only shows up when USDC.eth.axl is the offer")
    print("asset -> asset-specific (possibly a decimals/encoding issue worth checking")
    print("directly against the token's registered decimals). If commission/raw_out")
    print("is ~constant instead -> the pool is standard after all and something else")
    print("(e.g. a stale/cached response) explains the original bad reads.")
    print("=" * 100)

    for pool_name, pair_address, offer_asset, other_asset, other_probes in pools:
        print(f"\n{'-' * 100}\n{pool_name}  ({pair_address})\n{'-' * 100}")
        # Direction 1: offer USDC.eth.axl (the direction that read anomalously).
        probe_direction(terra, pool_name, pair_address, usdc_axl, USDC_PROBE_AMOUNTS)
        # Direction 2: offer the pool's other asset, to see if the same
        # fee-on-input pattern shows up regardless of which asset is offered.
        probe_direction(terra, pool_name, pair_address, other_asset, other_probes)

    print("\n" + "=" * 100)
    print("Other Terraport pools — native-offer direction (previously untested)")
    print("Each of these has only ever been probed via its CW20-offered side in")
    print("production, since that's asset_x for all of them. If comm/offer reads")
    print("~0.02 here too, the fee-on-input pattern is Terraport-wide for native")
    print("offers, not USDC.eth.axl-specific -- meaning every one of these pools has")
    print("been relying on default_commission (now 0.02) being correct for native-")
    print("offer legs, same as the three pools above, purely by accident until now.")
    print("=" * 100)

    for pool_name, pair_address, native_asset in other_terraport_native_offer_pools:
        print(f"\n{'-' * 100}\n{pool_name}  ({pair_address})\n{'-' * 100}")
        probe_direction(terra, pool_name, pair_address, native_asset, NATIVE_PROBE_AMOUNTS)

    print("\nDone. Compare comm/offer vs comm/raw_out columns across sizes and")
    print("directions per the header above to identify the mechanism.")


if __name__ == "__main__":
    main()