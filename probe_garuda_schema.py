"""
Zero-cost schema probe for Garuda's `pair_base` contracts.

executor.build_swap_msg_garuda is currently a GUESS, inferred from the
error text check_new_venues_interface.py surfaced on 2026-08-04 — not
from Garuda's contract source or docs. This script tries several
candidate ExecuteMsg/Cw20HookMsg shapes via TerraClient.simulate_fee(),
which signs a tx LOCALLY and asks the chain to estimate gas for it — it
NEVER broadcasts, so no funds or gas are spent even if every candidate
fails. This is the same zero-cost method check_new_venues_interface.py
uses.

Run this BEFORE trusting build_swap_msg_garuda for anything real. If a
candidate shows ACCEPTED, update build_swap_msg_garuda to match it
exactly, then re-run this script once more against the updated function
to confirm. If NONE are accepted, read the raw rejection text yourself —
it usually names the next field it wanted, the same way the original two
rejections named "native"/"cw20" and "min_receive".

CAVEAT: the CW20-hook probe (try_cw20_hook) needs the wallet to actually
hold a little BENANCE, or a genuine schema-acceptance will be masked by
the same balance-shaped "Cannot Sub" error check_new_venues_interface.py
already flagged as INCONCLUSIVE elsewhere — read the raw error text if
every cw20 candidate here "rejects" identically.

Run: python probe_garuda_schema.py
"""
import json
import base64
import time
import logging

import config
from assets import Asset
from terra_client import TerraClient
from terra_classic_sdk.core.wasm import MsgExecuteContract
from terra_classic_sdk.core.coins import Coin, Coins

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("probe_garuda_schema")

PROBE_AMOUNT = 2_000_000
NATIVE_POOL = config.GARUDA_POOL_BENANCE_LUNC   # native-offer probe target
CW20_POOL = config.GARUDA_POOL_BENANCE_JURIS    # cw20-offer/hook probe target

# CHANGED 2026-08-04 (round 2): first run showed the wallet holds no BENANCE
# (every hook candidate rejected with an identical "Cannot Sub" balance
# error, masking any schema signal). BENANCE/JURIS is a real Garuda pool, so
# offering the JURIS side instead of BENANCE tests the exact same
# Cw20HookMsg schema, just against a token this bot already holds from
# trading it on Terraport/other venues elsewhere in the pool list.
CW20_HOOK_OFFER_ASSET_ADDRESS = config.JURIS_CW20_ADDRESS
CW20_HOOK_OFFER_DECIMALS = config.JURIS_DECIMALS

_CONNECTION_ERROR_HINTS = ("connection refused", "transport:", "dial tcp")


def _classify(e: Exception) -> str:
    """Distinguishes a real on-chain schema rejection from a transport-level
    failure (dropped connection, refused dial, etc) — round 1 showed the
    latter can happen mid-run and must NOT be read as a schema signal."""
    text = str(e).lower()
    if any(hint in text for hint in _CONNECTION_ERROR_HINTS):
        return "CONNECTION_ERROR"
    return "REJECTED"


def _simulate_with_retry(terra, msgs, account_number, sequence, attempts=3, delay=2.0):
    """Retries only on CONNECTION_ERROR (transport-level) — a real on-chain
    REJECTED result is returned immediately, since retrying that would just
    get the same answer."""
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            terra.simulate_fee(msgs, account_number=account_number, sequence=sequence)
            return None  # ACCEPTED
        except Exception as e:
            kind = _classify(e)
            if kind == "REJECTED":
                return e
            last_exc = e
            if attempt < attempts:
                time.sleep(delay)
    return last_exc  # exhausted retries, still a connection error


# CHANGED 2026-08-04 (round 3): round 2 showed {"native": {denom,amount}}
# and {"native": [denom,amount]} both fail with "Invalid type" — the
# payload is NOT a struct/tuple, it's a single scalar (the bare amount
# string {"native": "2000000"} parsed successfully and got all the way to
# a LOGIC-level "Invalid offer asset" rejection, not a parse error). That
# rules out amount-as-the-scalar being semantically correct even though it
# parses, since something about it still reads as "invalid" to the
# contract. Testing what else that lone scalar could validly be: the pool's
# native denom itself, or the Cosmos SDK Coin.String() format
# (amount+denom concatenated, no separator) — plus two single-field struct
# shapes in case it's a struct after all, just not a two-field one.
NATIVE_CANDIDATES = [
    ("native: bare amount string (round-2 baseline — parses, but 'Invalid offer asset')",
     lambda amt: {"swap": {"offer_asset": {"native": str(amt)}, "min_receive": "1"}}),
    ("native: bare denom string",
     lambda amt: {"swap": {"offer_asset": {"native": config.DENOM_LUNC}, "min_receive": "1"}}),
    ("native: Coin.String() format, amount+denom concatenated",
     lambda amt: {"swap": {"offer_asset": {"native": f"{amt}{config.DENOM_LUNC}"}, "min_receive": "1"}}),
    ("native: single-field struct, amount only",
     lambda amt: {"swap": {"offer_asset": {"native": {"amount": str(amt)}}, "min_receive": "1"}}),
    ("native: single-field struct, denom only",
     lambda amt: {"swap": {"offer_asset": {"native": {"denom": config.DENOM_LUNC}}, "min_receive": "1"}}),
]

# CONFIRMED 2026-08-04 (round 2, against BENANCE/JURIS offering JURIS with a
# real held balance): {"swap": {"min_receive": "<amt>"}} is accepted, with
# or without max_spread — the CW20 Send-hook side needs no further probing.
# min_receive IS required (round 3's native probe separately confirmed this
# for the top-level Swap struct too, via a "missing field" rejection when
# omitted). Kept here only as a smoke check that a code change didn't
# regress it.
CW20_HOOK_CANDIDATES = [
    ("min_receive only (confirmed shape)", lambda: {"swap": {"min_receive": "1"}}),
]


def try_native(terra, account_number, sequence):
    print(f"\n--- Native-offer candidates against {NATIVE_POOL} ---")
    any_accepted = False
    for label, builder in NATIVE_CANDIDATES:
        execute_msg = builder(PROBE_AMOUNT)
        msg = MsgExecuteContract(terra.address, NATIVE_POOL, execute_msg,
                                  Coins([Coin(config.DENOM_LUNC, PROBE_AMOUNT)]))
        exc = _simulate_with_retry(terra, [msg], account_number, sequence)
        if exc is None:
            print(f"  ACCEPTED: {label}\n    msg={json.dumps(execute_msg)}")
            any_accepted = True
        elif _classify(exc) == "CONNECTION_ERROR":
            print(f"  UNTESTED (connection error, not a schema result — retried and still failed "
                  f"to reach the chain) ({label}): {exc}")
        else:
            print(f"  rejected ({label}): {exc}")
    if not any_accepted:
        print("  None accepted — read the raw rejection text above for the next field it wants "
              "(ignore any UNTESTED lines — those never reached the chain).")


def try_cw20_hook(terra, account_number, sequence):
    print(f"\n--- CW20 Send-hook candidates against {CW20_POOL} (offering JURIS, not BENANCE — "
          f"see CW20_HOOK_OFFER_ASSET_ADDRESS comment) ---")
    juris_balance = terra.get_cw20_balance(CW20_HOOK_OFFER_ASSET_ADDRESS)
    print(f"  Wallet JURIS balance: {juris_balance} base units "
          f"({juris_balance / (10 ** CW20_HOOK_OFFER_DECIMALS):.6f} JURIS)")
    if juris_balance < PROBE_AMOUNT:
        print(f"  WARNING: balance is below PROBE_AMOUNT ({PROBE_AMOUNT}) — results below may "
              f"still be balance-masked, not schema-confirmed. Read the raw text carefully.")

    any_accepted = False
    for label, builder in CW20_HOOK_CANDIDATES:
        inner_msg = builder()
        encoded_hook = base64.b64encode(json.dumps(inner_msg).encode()).decode()
        execute_msg = {"send": {"contract": CW20_POOL, "amount": str(PROBE_AMOUNT), "msg": encoded_hook}}
        msg = MsgExecuteContract(terra.address, CW20_HOOK_OFFER_ASSET_ADDRESS, execute_msg, None)
        exc = _simulate_with_retry(terra, [msg], account_number, sequence)
        if exc is None:
            print(f"  ACCEPTED: {label}\n    inner_msg={json.dumps(inner_msg)}")
            any_accepted = True
        elif _classify(exc) == "CONNECTION_ERROR":
            print(f"  UNTESTED (connection error, not a schema result) ({label}): {exc}")
        else:
            print(f"  rejected ({label}): {exc}")
    if not any_accepted:
        print("  None accepted — if these still show \"Cannot Sub\"/balance-shaped errors despite "
              "a nonzero JURIS balance printed above, re-read the raw text; otherwise it's a real "
              "schema miss and the next field name it wants should be in the error text.")


def main():
    config.validate()
    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)
    account_number, sequence = terra.get_account_number_and_sequence()

    try_native(terra, account_number, sequence)
    try_cw20_hook(terra, account_number, sequence)

    print("\nNothing above broadcast anything or spent gas. Update "
          "executor.build_swap_msg_garuda to match whichever candidate(s) showed "
          "ACCEPTED, then re-run check_new_venues_interface.py (after adding these "
          "5 Garuda pools' entries there back with the FIXED builder) before "
          "uncommenting any Garuda pool in arbitrage_bot.py.")


if __name__ == "__main__":
    main()