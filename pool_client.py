"""
One AMM pair contract, addressed directly (no factory lookup — you already
know the pair addresses). Works for native/native, native/cw20, or (in
principle) cw20/cw20 pairs, since everything is keyed off Asset objects
rather than raw denom strings.
"""
import time
import json
import base64
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict

import config
from assets import Asset

log = logging.getLogger("pool_client")

# A shared, connection-pooled Session instead of a bare requests.get() per
# call. Plain requests.get() opens and tears down a fresh HTTPS connection
# (full TLS handshake) EVERY call — firing ~20 of those simultaneously via
# ThreadPoolExecutor overwhelmed the handshake stage itself and produced
# real "SSL handshake timed out" failures under concurrent load (2026-07-31).
# A shared Session with a real connection pool lets threads reuse
# already-established connections instead of each opening its own from
# scratch. requests.Session is documented as thread-safe for this usage
# (each request still gets its own underlying connection from the pool).
# Retry policy: retry a couple of times on connection-level failures
# (timeouts, connection resets) with a short backoff — these are transient
# under concurrent load, not evidence of a real data problem, and retrying
# here is cheaper than letting one slow pool abort the WHOLE loop
# iteration (which is what happened before this was caught in run_once).
_session = requests.Session()
# CHANGED 2026-08-07: total=2 same-endpoint retries (with backoff) used to
# mean a single bad endpoint could eat several retries × up to 10s each
# before this ever gave up on it — exactly what turned two loops into 26s
# and 42s ones in a real run (arb_20260807.log). Retrying is now handled by
# failing over to a DIFFERENT endpoint (see _get_with_failover below)
# instead of hammering the same possibly-stuck one; total=0 here means no
# same-endpoint retry at the connection-pool level, just a fast move to the
# next LCD_URLS entry.
_retry = Retry(total=0, status_forcelist=[502, 503, 504])
# pool_maxsize raised 20->40 on 2026-08-29, same day as the state-fetch
# ThreadPoolExecutor's max_workers going 12->16 (arbitrage_bot.py). This
# session is shared across run_once's state-fetch, the background
# commission-refresh thread (max_workers=6), and the balance-fetch thread
# (max_workers=12) — their concurrent demand can now exceed 20 in bursts,
# which is exactly what produced 3 "Connection pool is full, discarding
# connection" warnings within the same half-second in arb_20260829.log's
# very first loop right after the max_workers change. Not fatal (the
# request still completes, just without connection reuse) but a real,
# measurable sign of pressure at the old limit — and this exact codebase's
# history is that this kind of pressure is what escalates into genuine SSL
# handshake failures under more contention. 40 gives headroom above the
# 16+6+12=34 combined worst case; if "Connection pool is full" warnings
# still show up after this, the fix is lowering max_workers back down, not
# raising this further.
_adapter = HTTPAdapter(pool_connections=40, pool_maxsize=40, max_retries=_retry)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


def _get_with_failover(path: str, params: dict = None, timeout: float = None) -> requests.Response:
    """
    GETs `path` (appended to each base URL in config.LCD_URLS in turn)
    against the first endpoint that succeeds, instead of only ever trying
    config.LCD_URL. On any failure (timeout, connection error, or a 5xx
    left after the adapter's own retry policy) this moves on to the next
    endpoint immediately rather than waiting out the full timeout on a
    dead one more than once. Raises the LAST endpoint's exception if every
    one fails — callers see one real error either way, not a partial or
    silently-wrong result.

    ADDED 2026-08-07 — see config.LCD_URLS's comment for the real
    incident that motivated this (a single endpoint stalling ~12
    concurrent pool queries at once).
    """
    timeout = config.LCD_QUERY_TIMEOUT if timeout is None else timeout
    last_exc = None
    for i, base_url in enumerate(config.LCD_URLS):
        try:
            resp = _session.get(f"{base_url}{path}", params=params, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_exc = e
            if i + 1 < len(config.LCD_URLS):
                log.warning("LCD endpoint %s failed (%s) — trying fallback %s.",
                            base_url, e, config.LCD_URLS[i + 1])
    raise last_exc


def get_all_native_balances_raw(address: str, timeout: float = None) -> Dict[str, int]:
    """
    Fetches EVERY native-denom balance for `address` in one LCD round trip
    (the bank module's list-all-balances endpoint), instead of one
    by_denom call per denom. Used when several native assets need their
    balance in the same loop iteration (currently LUNC, USTC, USDC.eth.axl)
    — turns N round trips into 1. Assumes the wallet's balances fit on a
    single page (a handful of denoms for a wallet actively used by this
    bot) — not using pagination.key, so if the wallet ever accumulates
    enough distinct denoms to paginate, a genuinely-held denom past the
    first page would silently read as 0 here.
    """
    resp = _get_with_failover(f"/cosmos/bank/v1beta1/balances/{address}", timeout=timeout)
    return {c["denom"]: int(c["amount"]) for c in resp.json().get("balances", [])}


def _query_bank_balance_raw(address: str, denom: str, timeout: float = None) -> int:
    """
    Thread-safe raw REST equivalent of TerraClient.get_balance() — same
    rationale as _query_contract_raw below: the SDK's LCDClient wraps a
    single shared asyncio event loop (nest_asyncio), so calling
    TerraClient's SDK-wrapped balance methods from multiple OS threads
    concurrently risks the same "cannot enter context ... already
    entered" failure that concurrent pool queries hit before this file
    switched to raw requests. Plain `requests` has no shared async state,
    so it's safe to fire from many threads at once — this lets balance
    fetching be parallelized across assets the same way pool-state
    fetching already is, instead of staying sequential.
    """
    resp = _get_with_failover(f"/cosmos/bank/v1beta1/balances/{address}/by_denom",
                               params={"denom": denom}, timeout=timeout)
    amount = resp.json().get("balance", {}).get("amount", "0")
    return int(amount)


def _query_cw20_balance_raw(contract_addr: str, address: str, timeout: float = None) -> int:
    """Thread-safe raw REST equivalent of TerraClient.get_cw20_balance()."""
    data = _query_contract_raw(contract_addr, {"balance": {"address": address}}, timeout=timeout)
    return int(data["balance"])


def get_asset_balance_raw(address: str, asset: "Asset", timeout: float = None) -> int:
    """
    Thread-safe raw-REST balance lookup for any Asset (native or cw20).
    Use this instead of TerraClient.get_asset_balance() whenever balances
    for several assets are being fetched concurrently (see the two
    helpers above for why the SDK path isn't safe to parallelize).
    """
    if asset.kind == "native":
        return _query_bank_balance_raw(address, asset.id, timeout=timeout)
    return _query_cw20_balance_raw(asset.id, address, timeout=timeout)


def get_account_number_and_sequence_raw(address: str, timeout: float = None):
    """
    Failover-safe equivalent of the SDK's wallet.account_number_and_sequence()
    — a plain READ (auth module account query), not a broadcast, so there's
    none of sign_and_broadcast's double-send risk in failing this over
    across config.LCD_URLS. ADDED 2026-08-07, same incident as
    _get_with_failover above — this was one of the SDK-based calls
    deliberately left on the single primary endpoint when failover was
    first added for pool/balance queries; a stall here blocks EVERY
    subsequent simulate_fee call in the same loop (they reuse this result
    — see TerraClient.get_account_number_and_sequence's docstring), so
    it's worth the same protection.
    Returns (account_number, sequence) as plain ints.
    """
    resp = _get_with_failover(f"/cosmos/auth/v1beta1/accounts/{address}", timeout=timeout)
    account = resp.json()["account"]
    # BaseAccount fields are top-level; some account types (vesting, etc.)
    # nest them under "base_account" — handle both shapes defensively.
    if "account_number" not in account and "base_account" in account:
        account = account["base_account"]
    return int(account["account_number"]), int(account["sequence"])


def get_latest_block_height_raw(timeout: float = None) -> int:
    """
    Failover-safe equivalent of terra.get_latest_block_height(). Diagnostic-
    only (see that method's docstring) but cheap to make consistent with
    every other read in this file now that the failover helper exists.
    """
    resp = _get_with_failover("/cosmos/base/tendermint/v1beta1/blocks/latest", timeout=timeout)
    return int(resp.json()["block"]["header"]["height"])


def _query_contract_raw(contract_addr: str, query_msg: dict, timeout: float = None) -> dict:
    """
    Thread-safe alternative to terra_client.TerraClient.query_contract().

    Added after running pool queries concurrently via ThreadPoolExecutor
    against the SDK's shared client produced real asyncio failures
    ("RuntimeError: cannot enter context ... already entered") — the
    SDK wraps async calls around ONE shared event loop (nest_asyncio),
    which multiple OS threads driving `run_until_complete` concurrently
    can corrupt. Plain `requests` calls have no shared async state at
    all, so they're safe to fire from many threads at once — this is
    the same pattern tax.py already uses for treasury queries, just
    extended to contract queries specifically so pool-state fetching can
    be parallelized without touching the SDK's event loop.

    Uses the standard Cosmos SDK / CosmWasm LCD REST path for smart
    contract queries. This is the standard path across CosmWasm chains,
    but hasn't been independently cross-checked result-for-result
    against the SDK's own query_contract() on THIS specific LCD — if
    reserves/commission numbers look wrong after this change (vs. what
    the SDK-based path previously returned), that mismatch, not thread
    safety, would be the first thing to check.
    """
    query_b64 = base64.b64encode(json.dumps(query_msg).encode()).decode()
    resp = _get_with_failover(f"/cosmwasm/wasm/v1/contract/{contract_addr}/smart/{query_b64}",
                               timeout=timeout)
    return resp.json()["data"]


# How often to re-probe a pool's live commission rate. Not "resolve once and
# cache forever": config.py's TERRAPORT_COMMISSION_RATE note is explicit that
# Terraport's current 2% is a TEMPORARY governance-controlled override, and
# it may revert to their target 0.3% at some unknown future point. A short
# TTL means that change gets picked up automatically instead of requiring a
# code deploy.
_COMMISSION_TTL_SECONDS = 600

# Probe size for the simulation query, in the OFFER asset's own base units.
# Deliberately small and generic (not tied to any one asset's decimals or
# typical pool depth) — this is only used to read back commission_amount /
# offer_amount, not to size a real trade, and every pair contract we're
# using has vastly more liquidity than this.
_COMMISSION_PROBE_AMOUNT = 1_000_000


@dataclass
class PoolState:
    name: str
    pair_address: str
    reserves: Dict[str, int]  # Asset.key() -> reserve amount
    commission_rate: Decimal


def _asset_from_garuda_info(info: dict) -> Asset:
    """
    Parses one side of a Garuda `pair_base` {"pool":{}} response —
    `{"native": "uluna"}` or `{"cw20": "terra1..."}` — which is NOT the
    Terraswap/Astroport asset_infos shape (`{"native_token": {"denom":
    ...}}` / `{"token": {"contract_addr": ...}}`) every other pool in this
    file uses. Confirmed against real Garuda BENANCE/LUNC, BENANCE/JURIS,
    GDEX/LUNC, GDEX/GRDX, FUN/GDEX responses on 2026-08-04 (see
    check_new_venues_interface.py's raw output).

    Only kind+id are trustworthy from this — decimals/display are NOT
    known from this response alone, so GarudaPool.get_state() below uses
    this purely to figure out which side is which, matching against the
    already-correctly-built Asset objects passed into GarudaPool's
    constructor, never as the Asset actually used downstream.
    """
    if "native" in info:
        denom = info["native"]
        return Asset(kind="native", id=denom, decimals=6, display=denom)
    if "cw20" in info:
        addr = info["cw20"]
        return Asset(kind="cw20", id=addr, decimals=6, display=addr)
    raise ValueError(f"Unrecognized Garuda pair_base asset info shape: {info}")


class GarudaPool:
    """
    DexPool-compatible wrapper for Garuda DeFi's `pair_base` contracts.
    NOT a DexPool subclass — deliberately duck-typed against the same
    interface (name/pair_address/asset_x/asset_y/commission_rate/assets()/
    other_asset()/get_state()/refresh_commission_if_due()) that graph.py
    and sizing.py already consume, so nothing downstream needs to know
    this pool is different, EXCEPT executor.py, which needs to route it
    through build_swap_msg_garuda() instead of build_swap_msg() — see
    that function's docstring for why the schema there is still
    UNCONFIRMED and gated behind probe_garuda_schema.py.

    Two confirmed (2026-08-04, real on-chain queries) differences from
    every Terraswap/Astroport-family pool already in this bot:

      1. {"pool":{}} response shape is {"asset1": {native|cw20},
         "asset2": {...}, "reserve1": "...", "reserve2": "..."} — NOT
         {"assets": [{"info": {...}, "amount": ...}, ...]}.
      2. There is no {"pair":{}} query at all — QueryMsg only exposes
         `pool`, `simulate_provide_liquidity`, `simulate_withdraw_
         liquidity`, `user_position`, `simulate_swap`. That also means
         there's no pair_type field to confirm xyk-vs-something-else the
         way check_pool_curve_type.py does for other venues, and no
         {"simulation":{}} query to live-resolve commission the way
         DexPool._resolve_commission_via_simulation does — this class
         always uses the fixed config.GARUDA_COMMISSION_RATE (0.5%, per
         Garuda's own docs) instead of a live-simulated rate.
    """

    def __init__(self, name: str, terra, pair_address: str,
                 asset_x: Asset, asset_y: Asset, default_commission: Decimal,
                 scan_interval: int = 1):
        self.name = name
        self.terra = terra
        self.pair_address = pair_address
        self.asset_x = asset_x
        self.asset_y = asset_y
        self.default_commission = default_commission
        self.commission_rate = default_commission
        self.scan_interval = max(1, scan_interval)
        self._commission_resolved_at = 0.0
        self.pool_kind = "garuda"  # see DexPool.pool_kind's comment

    def assets(self):
        return (self.asset_x, self.asset_y)

    def other_asset(self, asset: Asset) -> Asset:
        return self.asset_y if asset.key() == self.asset_x.key() else self.asset_x

    def refresh_commission_if_due(self) -> bool:
        # No live-simulation query exists on pair_base contracts (see
        # class docstring) — commission_rate stays pinned to
        # default_commission (config.GARUDA_COMMISSION_RATE) forever.
        # Returning True (not attempting a doomed query every TTL cycle)
        # mirrors DexPool.refresh_commission_if_due's return contract.
        return True

    def get_state(self) -> PoolState:
        resp = _query_contract_raw(self.pair_address, {"pool": {}})
        parsed1 = _asset_from_garuda_info(resp["asset1"])
        parsed2 = _asset_from_garuda_info(resp["asset2"])
        reserve1 = int(resp["reserve1"])
        reserve2 = int(resp["reserve2"])

        reserves = {}
        for parsed, amount in ((parsed1, reserve1), (parsed2, reserve2)):
            if parsed.kind == self.asset_x.kind and parsed.id == self.asset_x.id:
                reserves[self.asset_x.key()] = amount
            elif parsed.kind == self.asset_y.kind and parsed.id == self.asset_y.id:
                reserves[self.asset_y.key()] = amount
            else:
                raise ValueError(
                    f"{self.name}: on-chain asset {parsed.kind}:{parsed.id} matches "
                    f"neither configured asset_x ({self.asset_x}) nor asset_y "
                    f"({self.asset_y}) — pool list is mislabeled, the same class of "
                    f"bug check_pool_curve_type.py exists to catch for other venues."
                )
        if len(reserves) != 2:
            raise ValueError(f"{self.name}: only matched {len(reserves)}/2 reserves against "
                              f"asset_x/asset_y — check the pool list entry for this pair.")

        return PoolState(
            name=self.name,
            pair_address=self.pair_address,
            reserves=reserves,
            commission_rate=self.commission_rate,
        )


class DexPool:
    def __init__(self, name: str, terra, pair_address: str,
                 asset_x: Asset, asset_y: Asset, default_commission: Decimal,
                 scan_interval: int = 1, commission_probe_amount: int = None):
        """
        CONFIRMED 2026-07-14 (see _resolve_commission_via_simulation's
        docstring for the full finding): for Terraport pairs, commission
        resolution only reads back a trustworthy rate when asset_x (the
        asset THIS class probes with) is the CW20 side of a CW20/native
        pair. If asset_x is native — including for any native/native pair,
        which has no CW20 side to use instead — commission resolution will
        reliably fail its anomaly check and fall back to
        default_commission. That fallback is safe (not silently wrong),
        but it means default_commission must be set to the pair's REAL
        rate up front for such pools, not a guess — it will never
        self-correct via live simulation the way every CW20-offered pool
        does. When adding a new Terraport native/native or native-as-
        asset_x pool, either confirm default_commission is right (e.g. via
        probe_usdcaxl_commission.py) or swap asset_x/asset_y if the pair
        has a CW20 side.

        commission_probe_amount: overrides the module-level
        _COMMISSION_PROBE_AMOUNT (1,000,000 raw units) for THIS pool's
        commission-simulation probe. ADDED 2026-08-28 after LuncSwap
        JURIS/USDC showed the same failure every single refresh, forever
        (not intermittently like WESO's originally-too-small probe was):
        1,000,000 raw JURIS units is worth a small fraction of a cent on
        that pool, so the simulated return/commission rounded to ~1 raw
        unit each — nowhere near enough precision for the rate math below
        to produce anything but a rejected read. Whether the default probe
        size is "big enough" depends on the offered asset's real value per
        raw unit, which varies enormously across tokens — a fixed global
        constant works for most pools here by coincidence, not by design.
        Set this explicitly for any new pool where asset_x is a very
        low-unit-value token, rather than assuming the global default
        works.

        scan_interval: how many loop iterations between times this pool
        is fed into cycle-scanning (find_cycles/evaluate_cycle). 1 (the
        default) means every loop, same as before this parameter existed.
        A pool with scan_interval=5 is only included in cycle generation
        on loops where (loop_counter % 5 == 0) — arbitrage_bot.run_once
        does this filtering; get_state() itself is unaffected, so the
        pool's reserves are still fetched and its commission still
        refreshed on the normal schedule regardless. This does NOT make a
        pool untradeable — it's still fully tradeable (including via a
        manual/forced trade) on any loop it's included, just not
        evaluated on EVERY loop. Intended for pools you want to keep
        around for occasional real opportunities or manual testing
        without paying their CPU cost (see sizing.spread_cap_for_cycle's
        docstring) on every single loop.
        """
        self.name = name
        self.terra = terra
        self.pair_address = pair_address
        self.asset_x = asset_x
        self.asset_y = asset_y
        self.default_commission = default_commission
        self.commission_rate = default_commission
        self.scan_interval = max(1, scan_interval)
        self.commission_probe_amount = (
            commission_probe_amount if commission_probe_amount is not None
            else _COMMISSION_PROBE_AMOUNT)
        self._commission_resolved_at = 0.0  # epoch seconds; 0 = never
        self.pool_kind = "terraswap"  # see GarudaPool.pool_kind for the other value —
        # lets graph.compute_leg_execution_params and executor.build_leg_msg pick the
        # right message-building path per pool without isinstance checks.

    def assets(self):
        return (self.asset_x, self.asset_y)

    def other_asset(self, asset: Asset) -> Asset:
        return self.asset_y if asset.key() == self.asset_x.key() else self.asset_x

    def _resolve_commission_via_simulation(self) -> bool:
        """
        Reads the pair contract's ACTUAL, currently-in-effect commission by
        asking it to simulate a small real swap and reading back
        commission_amount — instead of guessing which config query and
        top-level field name this particular fork uses.

        IMPORTANT: commission_amount and spread_amount in a Terraswap/
        Astroport-family SimulationResponse are documented as being
        denominated in the ASK (output) asset, not the offer asset —
        return_amount is the final net output, and the pre-fee/pre-spread
        raw AMM output is return_amount + spread_amount + commission_amount.
        The true rate is commission_amount divided by THAT raw output, not
        by offer_amount — offer_amount is in a different asset's units
        entirely whenever offer/ask have different exchange rates, which
        silently produced wildly wrong rates (a 2% pool read back as 30%,
        a 0.3% pool read back as 0.003%) rather than erroring.

        CONFIRMED 2026-07-14 (probe_usdcaxl_commission.py, 10 Terraport
        pools, 3 probe sizes each): that "ask-denominated" assumption is
        FALSE specifically when the OFFER asset is native (LUNC/USTC).
        In that case Terraport's simulation query reports commission_amount
        as exactly 2% of the offer_amount instead — fee-on-INPUT, not
        fee-on-output — with zero exceptions across every native-offer
        probe run (three USDC.eth.axl pools plus TERRA/LUNC, TERRA/USTC,
        LCW/LUNC, LCW/USTC, TRIT/LUNC, TRIT/USTC, JURIS/LUNC). When the
        offer asset is CW20 instead, commission_amount behaves exactly as
        documented (ask-denominated). return_amount itself still appears
        correctly net of the real ~2% fee either way (cross-checked against
        raw reserve ratios), so this is a mislabeled/miscomputed field in
        Terraport's simulation response, not an error in what the contract
        actually settles.

        Practical consequence — READ THIS BEFORE ADDING A NEW POOL: this
        resolver always probes by offering self.asset_x. Every existing
        Terraport CW20/native pool in this bot happens to have asset_x set
        to the CW20 side (see arbitrage_bot.py's pool list), so this
        resolver has only ever exercised the correct, ask-denominated path
        for them — their live-resolved rates are trustworthy, but that's a
        property of argument ORDER, not of anything this code enforces. Any
        new Terraport pool constructed with the native asset as asset_x
        (including any future native/native pool, which has no CW20 side
        to fall back on at all) WILL hit the fee-on-input path and get a
        bogus rate ~2 orders of magnitude too low. The anomaly check below
        rejects that specific failure mode today, but if you add a pool
        like this, don't rely on the reject-and-fallback catching it
        silently forever — confirm the resolved rate explicitly (or run
        probe_usdcaxl_commission.py against it) before trusting it live.
        """
        offer_asset = self.asset_x
        try:
            resp = _query_contract_raw(self.pair_address, {
                "simulation": {
                    "offer_asset": {
                        "info": offer_asset.info(),
                        "amount": str(self.commission_probe_amount),
                    }
                }
            })
            return_amount = Decimal(str(resp["return_amount"]))
            spread_amount = Decimal(str(resp["spread_amount"]))
            if "commission_amount" in resp:
                # Terraswap/Astroport-family shape: one combined fee field.
                commission_amount = Decimal(str(resp["commission_amount"]))
            elif "swap_fee_amount" in resp:
                # White Whale shape (CONFIRMED 2026-08-02 via
                # inspect_whitewhale_simulation.py against the live LUNC/USTC
                # pool): no single "commission_amount" field at all — instead
                # THREE separate fee components, each of which reduces the
                # trader's return_amount the same way a combined commission
                # would (return_amount = raw_return - spread - swap_fee -
                # protocol_fee - burn_fee). Summing all three that are
                # present gives the same "total value extracted from this
                # swap" figure commission_amount represents everywhere else
                # in this file — amm_math.simulate_swap doesn't care how
                # many components a venue splits its fee into, only the
                # total rate. protocol_fee_amount/burn_fee_amount may not
                # always be present (0 or genuinely absent on some pairs),
                # default to 0 rather than KeyError.
                commission_amount = (
                    Decimal(str(resp.get("swap_fee_amount", "0")))
                    + Decimal(str(resp.get("protocol_fee_amount", "0")))
                    + Decimal(str(resp.get("burn_fee_amount", "0")))
                )
            else:
                log.warning("%s: simulation response has neither 'commission_amount' "
                            "nor 'swap_fee_amount' — unrecognized response shape, "
                            "keeping previous rate %s. Raw response: %s",
                            self.name, self.commission_rate, resp)
                return False
        except Exception as e:
            log.warning("%s: commission simulation query failed (%s) — keeping "
                        "previous rate %s.", self.name, e, self.commission_rate)
            return False

        raw_output = return_amount + spread_amount + commission_amount
        if raw_output <= 0:
            log.warning("%s: simulation returned zero/negative raw output "
                        "(return=%s spread=%s commission=%s) — keeping previous "
                        "rate %s.", self.name, return_amount, spread_amount,
                        commission_amount, self.commission_rate)
            return False

        rate = commission_amount / raw_output

        # Anomaly check: a resolved rate dramatically BELOW the configured
        # default is suspicious the same way one dramatically above it is.
        # This used to only log and fall through, on the theory that a
        # near-zero rate isn't obviously impossible (some pools genuinely
        # run near-zero fees) — but this is now a CONFIRMED failure mode
        # (see the fee-on-input finding in this method's docstring above),
        # not just a one-off: any pool probed with a native offer asset
        # will trip this every time. Reject and keep the previous/default
        # rate, same as the ceiling check below. Surfaces the raw response
        # components directly so a case like this doesn't require
        # reconstructing them by hand from separate log lines again.
        if self.default_commission > 0 and rate < self.default_commission / Decimal(20):
            log.warning("%s: resolved commission rate %s is over 20x BELOW the configured "
                        "default (%s) — rejecting as a likely bad read (CONFIRMED cause for "
                        "Terraport specifically: it reports commission_amount as fee-on-INPUT "
                        "for native offer assets, not fee-on-output as documented; see this "
                        "method's docstring — other venues sharing this check may have a "
                        "different root cause, but the same 20x-below signal is still worth "
                        "rejecting rather than trusting) and keeping previous rate %s. Raw "
                        "simulation response: return_amount=%s spread_amount=%s "
                        "commission_amount=%s (probe_amount=%d %s).", self.name, rate,
                        self.default_commission, self.commission_rate, return_amount,
                        spread_amount, commission_amount, self.commission_probe_amount, offer_asset)
            return False

        # Sanity clamp: a real AMM swap fee on these venues is never
        # anywhere near 20%. If the math above ever goes wrong again in a
        # way I haven't anticipated, fail loud and keep the previous
        # (presumably saner) rate rather than silently feeding a garbage
        # number into real trade sizing/profit math.
        if rate < 0 or rate > Decimal("0.20"):
            log.warning("%s: computed commission rate %s is outside sane bounds "
                        "(0-20%%) — treating as a bad read and keeping previous "
                        "rate %s. Raw sim response: return=%s spread=%s "
                        "commission=%s", self.name, rate, self.commission_rate,
                        return_amount, spread_amount, commission_amount)
            return False

        if rate != self.commission_rate:
            log.info("%s: commission rate from live simulation = %s (was %s)",
                      self.name, rate, self.commission_rate)
        self.commission_rate = rate
        return True

    def refresh_commission_if_due(self) -> bool:
        """
        Re-resolves commission_rate via simulation if _COMMISSION_TTL_SECONDS
        has elapsed since the last successful resolution. Returns True if a
        (successful or not-yet-due) check ran, False only on an unexpected
        error.

        IMPORTANT: this is NOT called from get_state() anymore. It used to
        be — but that meant any pool whose TTL had just expired paid for
        an extra LCD round trip (the commission simulation query) INLINE
        inside get_state(), right when the main loop is fetching reserves
        as fast as possible to catch a live gap. A due refresh landing on
        several pools in the same 8-worker fetch batch (e.g. right after
        startup, when every pool's TTL is expired at once) could roughly
        double that batch's latency. Callers should instead invoke this
        periodically from a separate, lower-frequency background thread
        (see arbitrage_bot.commission_refresh_loop) — get_state() just
        reads whatever commission_rate happens to be cached, which is at
        most one background-refresh-interval stale, well within the
        staleness the 600s TTL already tolerated anyway.

        PARKED 2026-08-24: config.COMMISSION_SIMULATION_PARKED names pools
        whose live simulation query is known to NEVER succeed — not a
        transient network issue, a structural one (e.g. Terraport GDEX/GRDX,
        confirmed permanently reporting fee-on-input for its specific asset
        pairing, tripping the anomaly check on every single attempt — see
        _resolve_commission_via_simulation's docstring). Before this fix, a
        parked pool's _commission_resolved_at stayed 0.0 forever (never
        successfully resolved), which made the "not yet due" TTL check
        below always false — so instead of retrying once per 600s like a
        normal pool, it retried on EVERY commission_refresh_loop tick (every
        30s, forever, for the life of the process) — confirmed via
        arb_20260824.log. A parked pool now skips the query entirely and
        pins to default_commission, exactly like GarudaPool.
        """
        if self.name in config.COMMISSION_SIMULATION_PARKED:
            return True
        now = time.time()
        if (now - self._commission_resolved_at) < _COMMISSION_TTL_SECONDS:
            return True
        resolved = self._resolve_commission_via_simulation()
        if resolved:
            self._commission_resolved_at = now
        elif self._commission_resolved_at == 0.0:
            # Never successfully resolved even once — fall back to the
            # configured default, but keep retrying on every call (don't
            # set _commission_resolved_at) rather than freezing on a
            # possibly-wrong guess for the full TTL window.
            log.info("%s: using default commission rate %s (simulation query "
                      "not yet successful)", self.name, self.default_commission)
        return True

    def get_state(self) -> PoolState:
        # No commission refresh here anymore — see refresh_commission_if_due's
        # docstring. This method is now reserve-fetch only: one round trip,
        # every time, so it stays fast and predictable in the main loop's
        # parallel fetch batch regardless of TTL state.
        resp = _query_contract_raw(self.pair_address, {"pool": {}})
        reserves = {}
        for asset in resp["assets"]:
            a = Asset.from_chain_info(asset["info"])
            reserves[a.key()] = int(asset["amount"])
        return PoolState(
            name=self.name,
            pair_address=self.pair_address,
            reserves=reserves,
            commission_rate=self.commission_rate,
        )