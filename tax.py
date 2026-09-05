"""
Terra Classic still runs the `x/treasury` stability tax on native-coin
transfers (the tax that gets charged when native funds move into a
contract, e.g. as `funds` on MsgExecuteContract). It's a percentage of
the transferred amount, capped per-denom by `tax_cap`.

    tax = min(amount * tax_rate, tax_cap)

Both `tax_rate` and `tax_cap` are on-chain governance parameters and can
change, so we always fetch them fresh (with a short cache) rather than
hardcoding them.

CHANGED 2026-08-02: the community passed a vote raising the stability tax
from 0.5% to 1.5% (3x) — confirmed via a real bot transaction that lost
meaningfully more to tax than expected. This module used to fail safe on
a fetch error by treating tax as 0, on the theory that a temporary LCD
hiccup shouldn't block trading and 0% was "safe" in the sense of not
blocking anything. That was ALWAYS technically wrong (0% is never the
real rate), but it was cheap-wrong at 0.5% and is expensive-wrong at 1.5%
— a fetch outage now means the bot could size and approve cycles believing
native transfers are free when they actually cost 1.5% per leg, across
potentially multiple native legs in one cycle. That's a real-money
mispricing, not just a missed-opportunity one.

Fail-safe direction is now: on a fetch failure, use the LAST successfully
fetched value (even if past its TTL) instead of resetting to 0 — stale-
but-real beats a guess. Only if NO fetch has EVER succeeded (a fetch
failure on the very first call, before any real value is known) does this
fall back to TAX_RATE_FAILSAFE_DEFAULT / TAX_CAP_FAILSAFE_DEFAULT in
config.py — hardcoded conservative floors, not 0, so a bad cold start
errs toward missing trades rather than mispricing them.
"""
import time
import logging
import requests
from decimal import Decimal

import config

log = logging.getLogger("tax")

_CACHE_TTL = 60  # seconds

# rate/caps hold the LAST SUCCESSFULLY FETCHED values indefinitely (not
# cleared on TTL expiry or fetch failure) — ts tracks freshness for
# deciding whether to re-fetch, but a stale value is still used as the
# fail-safe fallback rather than being discarded. None means "never
# successfully fetched even once."
_cache = {"rate": None, "caps": {}, "ts": 0, "caps_ts": {},
          "rate_fail_ts": 0, "caps_fail_ts": {},
          "rate_fail_log_ts": 0, "caps_fail_log_ts": {}}

# ADDED 2026-08-06: a FAILED fetch used to be retried on every single call
# with no memory of the failure at all — fine for a one-off LCD hiccup, but
# confirmed via arb_20260806.log to be a real problem for a denom whose
# tax_cap endpoint is PERMANENTLY broken on this LCD provider (DENOM_USDC =
# ibc/0BB9D851... 501s unconditionally, every time, on
# terra-classic-lcd.publicnode.com). With no failure caching, that 501 was
# being re-requested for every leg of every cycle that touched USDC — 8+
# identical failing round trips inside 20 seconds in one loop alone — and
# the log shows the bot going silent for ~4 minutes shortly after, restarted
# fresh by its wrapper. _FAIL_RETRY_INTERVAL makes a failure "sticky" for a
# short cooldown so a dead endpoint costs one slow lookup per cooldown
# window instead of one per leg. This does NOT change what gets returned on
# failure — still last-known-good, or the FAILSAFE_DEFAULT — it only limits
# how often we're willing to re-ask a server that just told us no.
_FAIL_RETRY_INTERVAL = 30  # seconds

# ADDED 2026-08-22: the cooldown branches below used to log a DEBUG line on
# EVERY call, not just once per cooldown window — cheap in isolation, but
# calculate_tax() is invoked repeatedly per candidate (once per leg, times
# every offer amount tried by find_optimal_trade_size's ternary search and
# spread_cap_for_cycle's bisection, times every candidate cycle touching a
# given denom). For a denom with a permanently-broken tax_cap endpoint,
# arb_20260822.log showed this single log line firing ~9,160 times across
# the run (25,872 log lines total inside one 145s-long loop, most of them
# this line) — the formatting+I/O cost of that flood, not any pricing
# logic, is what stalled that loop for over two minutes instead of ~8-18s.
# _FAIL_LOG_THROTTLE makes the cooldown-branch log fire at most once per
# window per denom instead of once per calculate_tax() call — the RETURNED
# VALUE and retry timing are completely unchanged, only how often we're
# willing to re-log an already-known fact.
_FAIL_LOG_THROTTLE = 5  # seconds


def _rate_fresh():
    return (time.time() - _cache["ts"]) < _CACHE_TTL and _cache["rate"] is not None


def _rate_recently_failed():
    return (time.time() - _cache["rate_fail_ts"]) < _FAIL_RETRY_INTERVAL


def _cap_fresh(denom: str):
    ts = _cache["caps_ts"].get(denom, 0)
    return (time.time() - ts) < _CACHE_TTL and denom in _cache["caps"]


def _cap_recently_failed(denom: str):
    ts = _cache["caps_fail_ts"].get(denom, 0)
    return (time.time() - ts) < _FAIL_RETRY_INTERVAL


def get_tax_rate() -> Decimal:
    if _rate_fresh():
        return _cache["rate"]
    if _rate_recently_failed():
        # Already tried and failed within the last _FAIL_RETRY_INTERVAL
        # seconds — don't hit the network again, just fall back the same
        # way the except block below would. Kept as a separate branch
        # (not a try/except) specifically so this path never issues a
        # request at all.
        now = time.time()
        should_log = (now - _cache["rate_fail_log_ts"]) >= _FAIL_LOG_THROTTLE
        if should_log:
            _cache["rate_fail_log_ts"] = now
        if _cache["rate"] is not None:
            if should_log:
                log.debug("tax_rate fetch failed recently — reusing last known rate %s "
                          "(cooldown) instead of assuming 0.", _cache["rate"])
            return _cache["rate"]
        if should_log:
            log.debug("tax_rate fetch failed recently and no previous value is known yet "
                      "(cooldown) — using TAX_RATE_FAILSAFE_DEFAULT (%s) instead of 0.",
                      config.TAX_RATE_FAILSAFE_DEFAULT)
        return Decimal(str(config.TAX_RATE_FAILSAFE_DEFAULT))
    try:
        r = requests.get(f"{config.LCD_URL}/terra/treasury/v1beta1/tax_rate", timeout=5)
        r.raise_for_status()
        rate = Decimal(r.json()["tax_rate"])
        # RESTORED 2026-08-27 — this check existed before config.py got
        # rebuilt from a stale re-upload that day (see config.py's
        # SIMULATE_FEE_TIMEOUT_SECONDS comment for the same incident) and
        # was silently dropped along with it. A live tax_rate fetch that
        # comes back as exactly 0 with a 200 OK is the SAME failure mode
        # already confirmed for get_tax_cap on this LCD (a structurally
        # broken treasury-module response, not a transient error) — trust
        # it blindly here and calculate_tax's `if rate <= 0: return 0`
        # zeroes out tax on EVERY native leg, in EITHER direction, for the
        # full _CACHE_TTL window, every time this endpoint hiccups this
        # way. Since LUNC/USTC are the two hub assets nearly every cycle
        # touches, that's not an edge case — arb_20260827.log (22:15-22:21
        # capture) shows this firing roughly once per TTL window across
        # the whole session.
        #
        # FIXED same day, second pass: the first version of this check
        # returned early WITHOUT setting _cache["rate_fail_ts"], so
        # _rate_recently_failed()'s 30s cooldown never engaged for this
        # path — every single calculate_tax() call (once per leg, per
        # candidate size, per cycle) re-hit this endpoint fresh, at
        # ~700ms/round-trip, instead of being throttled the same way a
        # real fetch exception already is below. A later capture of
        # arb_20260827.log (23:14-23:16) showed 60+ tax_rate fetches in
        # ~80 seconds against only 2 real cycle evaluations — the exact
        # "hammering a structurally-broken endpoint" problem
        # _FAIL_RETRY_INTERVAL/_FAIL_LOG_THROTTLE already exist to prevent
        # for get_tax_cap, just reintroduced here. Setting rate_fail_ts
        # here routes subsequent calls through the SAME
        # _rate_recently_failed() cooldown branch above (including its own
        # log throttling), so a persistently-zero endpoint costs one slow
        # lookup per _FAIL_RETRY_INTERVAL window, not one per leg.
        if rate == 0 and not config.TAX_RATE_ZERO_CONFIRMED:
            _cache["rate_fail_ts"] = time.time()
            log.warning("tax_rate fetched as 0 (HTTP 200 — not a failure) but "
                        "config.TAX_RATE_ZERO_CONFIRMED is False — treating this as a "
                        "suspicious/unconfirmed zero rather than a genuine 0%% rate, and "
                        "using %s instead. Will not re-fetch for %ds. Cross-check with "
                        "smoke_test_tax_per_hop.py before setting that flag to True.",
                        _cache["rate"] if _cache["rate"] is not None
                        else f"TAX_RATE_FAILSAFE_DEFAULT ({config.TAX_RATE_FAILSAFE_DEFAULT})",
                        _FAIL_RETRY_INTERVAL)
            if _cache["rate"] is not None:
                return _cache["rate"]
            return Decimal(str(config.TAX_RATE_FAILSAFE_DEFAULT))
        _cache["rate"] = rate
        _cache["ts"] = time.time()
        return rate
    except Exception as e:
        _cache["rate_fail_ts"] = time.time()
        if _cache["rate"] is not None:
            log.warning("Could not fetch tax_rate (%s) — using last known rate %s "
                        "(stale, but real) instead of assuming 0. Will not retry for %ds.",
                        e, _cache["rate"], _FAIL_RETRY_INTERVAL)
            return _cache["rate"]
        log.warning("Could not fetch tax_rate (%s) and no previous value is known yet "
                    "(this looks like the very first call) — using the conservative "
                    "TAX_RATE_FAILSAFE_DEFAULT (%s) instead of 0. This errs toward "
                    "missing trades, not mispricing them. Will not retry for %ds.",
                    e, config.TAX_RATE_FAILSAFE_DEFAULT, _FAIL_RETRY_INTERVAL)
        return Decimal(str(config.TAX_RATE_FAILSAFE_DEFAULT))


def get_tax_cap(denom: str) -> int:
    if _cap_fresh(denom):
        return _cache["caps"][denom]
    if _cap_recently_failed(denom):
        # Same reasoning as get_tax_rate's cooldown branch above — this is
        # the branch that matters most in practice, since it's a per-denom
        # tax_cap fetch (not the single tax_rate) that gets called once per
        # leg, and it's the one confirmed (2026-08-06) to be PERMANENTLY
        # broken for at least one real denom on the configured LCD, so this
        # branch is hit constantly for that denom rather than being a rare
        # edge case.
        now = time.time()
        last_logged = _cache["caps_fail_log_ts"].get(denom, 0)
        should_log = (now - last_logged) >= _FAIL_LOG_THROTTLE
        if should_log:
            _cache["caps_fail_log_ts"][denom] = now
        if denom in _cache["caps"]:
            if should_log:
                log.debug("tax_cap fetch for %s failed recently — reusing last known cap %s "
                          "(cooldown) instead of assuming 0.", denom, _cache["caps"][denom])
            return _cache["caps"][denom]
        if should_log:
            log.debug("tax_cap fetch for %s failed recently and no previous value is known "
                      "yet (cooldown) — using TAX_CAP_FAILSAFE_DEFAULT (%s uluna-equivalent) "
                      "instead of 0.", denom, config.TAX_CAP_FAILSAFE_DEFAULT)
        return config.TAX_CAP_FAILSAFE_DEFAULT
    try:
        r = requests.get(f"{config.LCD_URL}/terra/treasury/v1beta1/tax_caps/{denom}", timeout=5)
        r.raise_for_status()
        cap = int(r.json()["tax_cap"])
        _cache["caps"][denom] = cap
        _cache["caps_ts"][denom] = time.time()
        return cap
    except Exception as e:
        _cache["caps_fail_ts"][denom] = time.time()
        if denom in _cache["caps"]:
            log.warning("Could not fetch tax_cap for %s (%s) — using last known cap %s "
                        "(stale, but real) instead of assuming 0. Will not retry for %ds.",
                        denom, e, _cache["caps"][denom], _FAIL_RETRY_INTERVAL)
            return _cache["caps"][denom]
        log.warning("Could not fetch tax_cap for %s (%s) and no previous value is known "
                    "yet — using the conservative TAX_CAP_FAILSAFE_DEFAULT (%s uluna-"
                    "equivalent) instead of 0. Will not retry for %ds.",
                    denom, e, config.TAX_CAP_FAILSAFE_DEFAULT, _FAIL_RETRY_INTERVAL)
        return config.TAX_CAP_FAILSAFE_DEFAULT


def calculate_tax(amount: int, asset, direction: str = None) -> int:
    """
    Tax owed (in base units) if `amount` of `asset` moves — either as
    `funds` into a contract (native coins, Terra Classic's stability tax)
    or as a CW20 transfer (some tokens, like LCW, apply their own
    transfer tax baked into the token contract itself — this is NOT
    something the chain or the pool exposes; it was found empirically by
    comparing a real swap's reported return_amount against the actual
    balance received, and is configured per-token in config.py's
    CW20_TRANSFER_TAX_BPS / CW20_DIRECTIONAL_TAX_BPS).

    direction is "in" (asset moving INTO the wallet — a swap's return
    leg), "out" (asset moving OUT of the wallet to a pool — a swap's
    offer leg), or None (ignored for native tax; falls back to a CW20's
    flat legacy rate). cwLUNC and BENANCE are CONFIRMED asymmetric
    (2026-08-05) — omitting direction for those two now uses the wrong
    (flat legacy) rate instead of their real in/out-specific ones.
    """
    if getattr(asset, "kind", "native") == "native":
        denom = asset.id if hasattr(asset, "id") else asset  # accept raw denom str too
        rate = get_tax_rate()
        cap = get_tax_cap(denom)
        if rate <= 0:
            return 0
        tax = (Decimal(amount) * rate).to_integral_value(rounding="ROUND_UP")
        # min(tax, cap) — but ONLY trust a live-fetched cap of exactly 0 for
        # denoms in config.TAX_CAP_ZERO_CONFIRMED_DENOMS, where it's been
        # independently cross-checked against real on-chain behavior (see
        # that set's definition in config.py). For every other denom, a
        # live cap of 0 is now treated as SUSPICIOUS rather than a genuine
        # exemption — CHANGED 2026-08-08 after smoke_test_tax_per_hop.py
        # caught exactly this failure mode for DENOM_LUNC/DENOM_USTC: cap
        # was fetched as 0 for both, silently zeroing every native-asset
        # leg's tax to nothing, while 5/5 real swaps in the same session
        # showed an uncapped, exact 1.50% deducted on every native receive
        # (matching tax_rate precisely — the cap was never actually
        # binding on-chain, only in this bot's own math). A cap of 0
        # trusted blindly is indistinguishable from "this fetch is wrong"
        # until independently confirmed the way USDC.eth.axl was — so
        # until a denom has that confirmation on file, this falls back to
        # config.TAX_CAP_FAILSAFE_DEFAULT (effectively uncapped) instead,
        # the same conservative direction this module's fail-safe design
        # already uses everywhere else: understating tax is the expensive
        # mistake, not overstating it.
        if cap == 0 and denom not in config.TAX_CAP_ZERO_CONFIRMED_DENOMS:
            log.warning("tax_cap for %s came back as 0 but this denom is NOT in "
                        "config.TAX_CAP_ZERO_CONFIRMED_DENOMS — treating this as a "
                        "suspicious/unconfirmed zero rather than a genuine exemption, "
                        "and NOT applying it (using TAX_CAP_FAILSAFE_DEFAULT instead, "
                        "i.e. effectively uncapped). Cross-check this denom with "
                        "smoke_test_tax_per_hop.py before adding it to that set.",
                        denom)
            cap = config.TAX_CAP_FAILSAFE_DEFAULT
        tax = min(tax, Decimal(cap))
        return int(tax)

    # CW20 path
    rate = config.cw20_transfer_tax_rate(asset.id, direction)
    if rate <= 0:
        return 0
    return int((Decimal(amount) * rate).to_integral_value(rounding="ROUND_UP"))