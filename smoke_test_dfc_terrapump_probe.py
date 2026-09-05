"""
REAL-FUND (potentially) re-probe for JUST the two still-unconfirmed
venues from the 2026-09-03 batch: LUNC_DFC_POOL_UNKNOWN and the parked
Terra.pump TNEWS/LUNC pool. All 12 active DEGENAP/TNEWS/IDEV pools
already got a clean real round trip in
smoke_test_tnews_degenap_idev_dfc_terrapump.py's 2026-09-03 run (11 of 12
confirmed directly; the 12th, Garuda USTC/DEGENAP, is untested only
because its on-chain reserves are dust — 38 uusd — not because of
anything wrong with the pool or DEGENAP's tax, which is separately
confirmed via its other 4 legs) — none of that needs repeating.

This reuses _probe_unknown_venue / probe_dfc_unknown_pool /
probe_terrapump_tnews_lunc from that script directly rather than
duplicating the probe logic. See that script's module docstring for the
full reasoning behind query-first probing: check {"pair":{}} and
{"pool":{}} responses BEFORE risking a real swap message, and only swap
if the shape matches what DexPool actually assumes.

Last run (2026-09-03) results, for reference:
  - LUNC_DFC_POOL_UNKNOWN: re-confirmed the same fee_rate=3300/
    lp_stake_contract anomaly as the original 2026-08-29 finding.
    KEEP PARKED, no swap attempted (correctly).
  - Terra.pump TNEWS/LUNC: BOTH {"pair":{}} and {"pool":{}} queries
    returned a flat HTTP 500 from the LCD — a different, arguably more
    informative signal than DFC's case (extra fields vs. no response at
    all). Could mean this contract doesn't implement either standard
    query (plausible for a pump.fun-style bonding curve with a custom
    schema), or could be a transient/LCD-specific issue. This script
    re-queries fresh to help tell those apart.

Run: python smoke_test_dfc_terrapump_probe.py --confirm
"""
import sys
import logging

import config
from terra_client import TerraClient
from smoke_test_tnews_degenap_idev_dfc_terrapump import (
    probe_dfc_unknown_pool,
    probe_terrapump_tnews_lunc,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("smoke_test_dfc_terrapump_probe")

COVERAGE_TESTS = [
    ("Probe: LUNC/DFC unknown DEX", probe_dfc_unknown_pool),
    ("Probe: Terra.pump TNEWS/LUNC", probe_terrapump_tnews_lunc),
]


def main():
    if "--confirm" not in sys.argv:
        print("This script MAY broadcast a real transaction (only if a probe's query shape "
              "looks clean — see each probe's printed RECOMMENDATION). Re-run with --confirm "
              "to proceed:\n"
              "  python smoke_test_dfc_terrapump_probe.py --confirm")
        sys.exit(1)

    config.validate()
    if config.DRY_RUN:
        print("config.DRY_RUN is True — this script needs DRY_RUN=False in case a probe's "
              "query shape looks clean and it wants to attempt a real compatibility swap. Set "
              "DRY_RUN=false in your .env for this run only, then set it back before running "
              "the main bot again if you don't want live trading.")
        sys.exit(1)

    terra = TerraClient()
    log.info("Wallet address: %s", terra.address)

    print("\nThis run ONLY re-probes LUNC_DFC_POOL_UNKNOWN and Terra.pump TNEWS/LUNC — nothing "
          "else from the 2026-09-03 batch is touched here; the 12 active DEGENAP/TNEWS/IDEV "
          "pools already completed real round trips and don't need repeating.")

    failed = []
    for name, fn in COVERAGE_TESTS:
        try:
            fn(terra)
        except Exception as e:
            failed.append(name)
            print(f"\n!!! {name} raised an unhandled error: {e}")
            log.exception("%s failed", name)

    if failed:
        print(f"\n{len(failed)} probe(s) hit an unhandled error: {', '.join(failed)}.")

    print("\nDone. Follow each probe's printed RECOMMENDATION — un-park and wire into "
          "arbitrage_bot.py only if it says the venue is confirmed clean.")


if __name__ == "__main__":
    main()