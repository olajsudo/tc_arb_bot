"""
Smoke test — NOT the arbitrage strategy. Ignores profitability entirely
and forces one small real round-trip through each of six pools flagged
during the 2026-07-14 USDC.eth.axl investigation, independently:

    1. Terraport USDC.eth.axl/LUNC   (LUNC  -> USDC.eth.axl -> LUNC)
    2. Terraport USDC.eth.axl/USTC   (USTC  -> USDC.eth.axl -> USTC)
    3. Terraport USDC.eth.axl/TERRA  (TERRA -> USDC.eth.axl -> TERRA)   [LOW LIQUIDITY]
    4. Terraswap USTC/USDC.eth.axl   (USTC  -> USDC.eth.axl -> USTC)
    5. Terraport JURIS/TERRA         (TERRA -> JURIS -> TERRA)          [first CW20/CW20 pair]
    6. Terraport JURIS/LUNC          (LUNC  -> JURIS -> LUNC)

Why these six: pools 1-3 are the only pools this bot trades where the
offer asset in commission-simulation is unavoidably native (see
pool_client.py's _resolve_commission_via_simulation docstring) — the
confirmed condition that triggers Terraport's fee-on-input
commission_amount bug. That bug is in the *simulation query* path only;
this test validates the *actual execution* path instead, by comparing
each leg's real balance-delta proceeds against the pool's own reported
return_amount from the swap event — a different code path from the
commission-simulation bug, and the one that matters for real trades.
Pool 4 (Terraswap, not Terraport) and pool 5 (the only CW20/CW20 pair in
the bot) are included because, like 1-3, neither has any real execution
history yet, even though they're not part of the commission-simulation
bug's mechanism specifically. Pool 6 confirms Terraport's (not Garuda's)
JURIS/LUNC pool implements the standard interface for real trades.

Each round-trip starts and ends in an asset the wallet is expected to
already hold (LUNC, USTC, or TERRA) — never USDC.eth.axl or JURIS — so
this never depends on a pre-existing balance of the asset being tested.

Test amounts are small and fixed, chosen to stay well under 1% of the
shallowest reserve involved in each pool (see per-test comments below);
they are NOT sized for profit. Pool 3 in particular is low-liquidity
(~9.7 USDC.eth.axl total reserve per the last observed pool state), so
its test amount is deliberately tiny — expect a very small, possibly
near-zero real profit/loss figure there; the point is confirming the
message path and tax measurement work, not economic significance.

Each test's own balance check happens live, immediately before that
test — so gas/balance spent by an earlier test in this same run is
already reflected in the next test's check.

WILL lose a small amount of real money to fees/tax/slippage, 6 times
over (up to 12 real broadcasts total). Respects DRY_RUN — set
DRY_RUN=false in .env to actually broadcast. In DRY_RUN, each test's
leg 2 is skipped (mirrors smoke_test_juris.py) since leg 1 produces no
real received amount to feed into it.

Run: python smoke_test_usdcaxl_and_juris.py
"""
import logging
import time

import config
from assets import Asset
from terra_client import TerraClient
from executor import execute_leg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("smoke_test_usdcaxl_juris")

# Same per-round-trip gas margin smoke_test_juris.py uses: reserve floor
# plus headroom for two legs' worth of real gas, paid in LUNC regardless
# of which assets are actually being swapped.
GAS_MARGIN_ULUNA = 20_000_000


def is_lunc(asset: Asset) -> bool:
    return asset.kind == "native" and asset.id == config.DENOM_LUNC


def check_balance(terra, asset: Asset, amount: int) -> bool:
    """Live balance check, run immediately before each test."""
    lunc_balance = terra.get_balance(config.DENOM_LUNC)
    needed_gas = config.GAS_RESERVE_ULUNA + GAS_MARGIN_ULUNA

    if is_lunc(asset):
        needed = amount + needed_gas
        if lunc_balance < needed:
            log.error("Wallet LUNC balance (%d uluna) too low for this test (need >= %d: "
                       "test amount + gas reserve + margin). Skipping this pool.",
                       lunc_balance, needed)
            return False
        return True

    if lunc_balance < needed_gas:
        log.error("Wallet LUNC balance (%d uluna) too low to cover gas reserve + margin "
                   "(need >= %d) for this test. Skipping this pool.", lunc_balance, needed_gas)
        return False

    asset_balance = terra.get_asset_balance(asset)
    if asset_balance < amount:
        log.error("Wallet %s balance (%d) too low for this test (need >= %d). "
                   "Skipping this pool.", asset, asset_balance, amount)
        return False
    return True


def execute_and_measure(terra, pair_address: str, offer_asset: Asset, offer_amount: int,
                         receive_asset: Asset):
    """Executes one leg and returns (true_received, leg_result). true_received
    is corrected for gas contamination when receive_asset is LUNC, same as
    smoke_test_juris.py's leg 2."""
    balance_before = terra.get_asset_balance(receive_asset)
    leg_result = execute_leg(terra, pair_address, offer_asset, offer_amount)
    if config.DRY_RUN:
        return None, leg_result

    balance_after = terra.get_asset_balance(receive_asset)
    raw_delta = balance_after - balance_before
    received = raw_delta + leg_result.gas_fee_uluna if is_lunc(receive_asset) else raw_delta
    return received, leg_result


def report_leg(leg_label: str, reported_return: int, received: int, receive_asset: Asset):
    log.info("%s: pool reported return_amount=%d %s; true received (balance-delta, gas-"
              "corrected if applicable)=%d %s", leg_label, reported_return, receive_asset,
              received, receive_asset)
    if reported_return > 0 and received < reported_return:
        shortfall = reported_return - received
        implied_bps = (shortfall * 10000) / reported_return
        log.warning("%s: true proceeds are LESS than reported return_amount — shortfall of "
                    "%d %s, implying roughly %.1f bps (%.3f%%) of extra loss beyond the "
                    "pool's own commission (hidden transfer tax, or a real-vs-simulated "
                    "execution gap). Worth investigating if this repeats across runs.",
                    leg_label, shortfall, receive_asset, implied_bps, implied_bps / 100)
    elif reported_return > 0:
        log.info("%s: true proceeds match (or exceed) reported return_amount — no evidence "
                  "of a hidden shortfall on this leg.", leg_label)


def round_trip_test(terra, label: str, pair_address: str, start_asset: Asset,
                     other_asset: Asset, start_amount: int, note: str = ""):
    print()
    log.info("=" * 90)
    log.info("%s%s", label, f"  [{note}]" if note else "")
    log.info("=" * 90)

    if not config.DRY_RUN and not check_balance(terra, start_asset, start_amount):
        return

    try:
        log.info("--- Leg 1: %s -> %s (offer via %s) ---", start_asset, other_asset, label)
        received_other, leg1 = execute_and_measure(terra, pair_address, start_asset,
                                                     start_amount, other_asset)
        if config.DRY_RUN:
            log.info("[DRY_RUN] Leg 1 simulated only, skipping leg 2 for this pool.")
            return

        if received_other is None or received_other <= 0:
            log.error("Leg 1: no real %s balance increase — aborting this pool's test before "
                       "touching leg 2. Check the txhash above on an explorer if it looks like "
                       "the broadcast itself failed.", other_asset)
            return
        report_leg("Leg 1", leg1.received, received_other, other_asset)

        log.info("--- Leg 2: %s -> %s (offer via %s) ---", other_asset, start_asset, label)
        received_start, leg2 = execute_and_measure(terra, pair_address, other_asset,
                                                     received_other, start_asset)
    except Exception as e:
        log.error("%s: unhandled error during this pool's test — SKIPPING to the next pool "
                   "rather than aborting the whole run. If this happened on leg 1, no funds "
                   "moved for THIS pool (broadcast either didn't happen or was rejected "
                   "before/at confirmation). If it happened on leg 2, leg 1 already succeeded "
                   "and this pool's start_asset is short by leg 1's offer amount until you "
                   "manually swap the received %s back — check your wallet balance for %s.",
                   label, e, other_asset, other_asset)
        return

    if received_start is None or received_start <= 0:
        log.error("Leg 2: no real %s balance increase — aborting this pool's test. Check "
                   "the txhash above on an explorer.", start_asset)
        return
    report_leg("Leg 2", leg2.received, received_start, start_asset)

    net = received_start - start_amount
    log.info("%s round-trip complete: offered %d %s, got %d %s, then %d %s back. "
              "Net: %d %s", label, start_amount, start_asset, received_other, other_asset,
              received_start, start_asset, net, start_asset)


def main():
    config.validate()
    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)
    log.info("DRY_RUN=%s", config.DRY_RUN)

    lunc = Asset(kind="native", id=config.DENOM_LUNC, decimals=6, display="LUNC")
    ustc = Asset(kind="native", id=config.DENOM_USTC, decimals=6, display="USTC")
    usdc_axl = Asset(kind="native", id=config.DENOM_USDC_AXL, decimals=6, display="USDC.eth.axl")
    terra_token = Asset(kind="cw20", id=config.TERRA_CW20_ADDRESS,
                         decimals=config.TERRA_DECIMALS, display="TERRA")
    juris_token = Asset(kind="cw20", id=config.JURIS_CW20_ADDRESS,
                         decimals=config.JURIS_DECIMALS, display="JURIS")

    # (label, pair_address, start_asset, other_asset, start_amount, note)
    # Amounts chosen to stay well under 1% of the shallowest reserve on
    # each pool, per the last observed pool state (2026-07-14 ~21:37 UTC
    # block 29497834) — see per-line comments. Re-check current reserves
    # via inspect_pools.py if it's been a while since that snapshot.
    tests = [
        # LUNC-side reserve ~22.8B uluna. This pool prices ~17,000 LUNC per
        # USDC.eth.axl, so a "normal" 1 LUNC test (like every other pool
        # here) produces only ~58 raw USDC.eth.axl base units back — too
        # close to dust, and swapping that back in leg 2 triggered an
        # "invalid coins" (zero-net-output) rejection during fee estimation
        # on 2026-07-15. 900 LUNC (~4% of reserve, still economically
        # trivial given LUNC's price) produces roughly 52,000 base units,
        # safely clear of that edge.
        ("Terraport USDC.eth.axl/LUNC", config.TERRAPORT_POOL_USDCAXL_LUNC,
         lunc, usdc_axl, 900_000_000, "native/native — commission-sim bug pool"),
        # USTC-side reserve ~44.6B uusd; 1 USTC is negligible.
        ("Terraport USDC.eth.axl/USTC", config.TERRAPORT_POOL_USDCAXL_USTC,
         ustc, usdc_axl, 1_000_000, "native/native — commission-sim bug pool"),
        # USDC.eth.axl-side reserve only ~9.7M base units (~9.7 tokens) —
        # the shallowest pool in the whole bot. 5 TERRA in produces well
        # under 0.1% of that reserve per probe_usdcaxl_commission.py's
        # earlier reading (offer=5,000,000 TERRA -> return ~4320 USDC.eth.axl
        # base units). Expect a tiny, possibly near-zero net figure.
        ("Terraport USDC.eth.axl/TERRA", config.TERRAPORT_POOL_USDCAXL_TERRA,
         terra_token, usdc_axl, 5_000_000, "LOW LIQUIDITY — commission-sim bug pool"),
        # USTC-side reserve ~182B uusd; 1 USTC is negligible.
        ("Terraswap USTC/USDC.eth.axl", config.TERRASWAP_POOL_USTC_USDCAXL,
         ustc, usdc_axl, 1_000_000, "native/native, Terraswap (not Terraport)"),
        # TERRA-side reserve ~8.1T, JURIS-side ~2.87Q — 1 TERRA is negligible.
        ("Terraport JURIS/TERRA", config.TERRAPORT_POOL_JURIS_TERRA,
         terra_token, juris_token, 1_000_000, "first CW20/CW20 pair traded live"),
        # LUNC-side reserve ~539.6T uluna; 1 LUNC is negligible.
        ("Terraport JURIS/LUNC", config.TERRAPORT_POOL_JURIS_LUNC,
         lunc, juris_token, 1_000_000, "Terraport (not Garuda) — confirms standard interface"),
    ]

    if not config.DRY_RUN:
        log.warning("DRY_RUN is OFF — about to broadcast up to %d REAL transactions across "
                     "%d pools, none of which have prior real execution history.",
                     len(tests) * 2, len(tests))
        log.warning("Each pool's test is independent — a failure or shortfall on one does "
                     "not stop the others from running.")
        log.warning("Starting in 5 seconds — Ctrl+C now to cancel.")
        time.sleep(5)

    for label, pair_address, start_asset, other_asset, start_amount, note in tests:
        round_trip_test(terra, label, pair_address, start_asset, other_asset,
                         start_amount, note)

    print()
    log.info("=" * 90)
    log.info("All tests complete. Review each pool's section above for shortfalls or errors "
              "before trusting it with real arbitrage sizing.")
    log.info("=" * 90)


if __name__ == "__main__":
    main()
    