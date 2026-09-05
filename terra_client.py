"""
Thin wrapper around terra_classic_sdk's LCDClient for Terra Classic (columbus-5).
Keeps signing/broadcasting logic in one place.
"""
import logging
import time
import concurrent.futures

import ripemd160_patch
ripemd160_patch.apply()  # must run before terra_classic_sdk's key module is imported

from terra_classic_sdk.client.lcd import LCDClient
from terra_classic_sdk.client.lcd.api.tx import CreateTxOptions
from terra_classic_sdk.key.mnemonic import MnemonicKey
from terra_classic_sdk.core.fee import Fee
from terra_classic_sdk.core.coins import Coins

import config
import pool_client

log = logging.getLogger("terra_client")

# How long a cached (account_number, sequence) pair is trusted before
# get_account_number_and_sequence() re-fetches from the chain — see that
# method's docstring. Generous on purpose: the sequence only actually
# changes when THIS process broadcasts a real tx (handled by bumping the
# cache locally right after a successful broadcast, not by waiting for
# this TTL), so this window is really just a safety net against external
# drift (e.g. the same mnemonic being used from somewhere else too).
_ACCOUNT_CACHE_TTL_SECONDS = 120

# ADDED 2026-08-26 after arb_20260826.log traced a real 15.49s stall in the
# real-check path to simulate_fee() specifically — 7.04s of it AFTER an
# unrelated background query had already failed, meaning that time was
# genuinely spent waiting on this SDK call, not on anything else. Checked
# terra_classic_sdk's public docs: LCDClient's constructor is
# `LCDClient(url, chain_id=None, gas_prices=None, gas_adjustment=None)` —
# no timeout parameter — and it shares its architecture with the upstream
# terra_sdk it's forked from, whose synchronous LCDClient wraps an asyncio/
# aiohttp client under the hood (via nest_asyncio). aiohttp's own default
# total timeout, if nothing overrides it, is 5 MINUTES. So today, a slow
# (not dead) LCD response on ANY SDK call in this file can sit for a very
# long time without ever tripping the failover loops that already exist —
# those only trigger on an exception, and "slow but still answering"
# never raises one.
#
# Rather than guess at aiohttp/nest_asyncio internals that may differ
# between terra_sdk and this fork (or between SDK versions), this enforces
# a deadline from OUTSIDE the SDK call entirely: run it in a worker thread
# and give up waiting after SIMULATE_FEE_TIMEOUT_SECONDS, treating that the
# same as any other failure — which means it flows into the SAME per-
# endpoint failover loops simulate_fee/_wait_for_tx already have. This
# works no matter what HTTP library the SDK uses internally, since it
# never touches the SDK's own timeout machinery. One real limitation: the
# abandoned thread can't be forcibly killed (Python threads can't be), so
# a timed-out call keeps running in the background until it finishes or
# errors on its own — acceptable here since this is only meant to fire
# occasionally (a genuinely slow endpoint), not as a routine path, and the
# small shared pool below caps how many such orphaned calls can pile up
# at once.
_deadline_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="lcd_deadline")


def _call_with_deadline(fn, timeout_seconds: float, *args, **kwargs):
    """Runs fn(*args, **kwargs) with a hard wall-clock deadline. Raises
    TimeoutError (not caught here) if it doesn't finish in time — callers
    treat that exactly like any other exception from the underlying call,
    so it feeds into existing per-endpoint failover/retry logic unchanged."""
    future = _deadline_executor.submit(fn, *args, **kwargs)
    return future.result(timeout=timeout_seconds)


class TerraClient:
    def __init__(self):
        # ADDED 2026-08-07: one LCDClient (+ derived wallet) per
        # config.LCD_URLS entry, not just the primary — lets simulate_fee
        # and tx-confirmation polling fail over across endpoints, the same
        # protection pool_client.py already has for pool/balance reads.
        # self.lcd / self.wallet (index 0) remain the PRIMARY and are used
        # UNCHANGED for the actual broadcast in sign_and_broadcast — see
        # that method and config.LCD_FALLBACK_URLS's comment for why
        # broadcast deliberately does NOT fail over (double-send risk if
        # the first attempt actually succeeded despite a client-side
        # error). Every other SDK call below is read-only/non-mutating, so
        # failover carries none of that risk.
        self._lcd_clients = [
            LCDClient(
                url=url,
                chain_id=config.CHAIN_ID,
                gas_prices=Coins.from_str(f"{config.GAS_PRICE}{config.GAS_DENOM}"),
                gas_adjustment=config.GAS_ADJUSTMENT,
            )
            for url in config.LCD_URLS
        ]
        self.key = MnemonicKey(mnemonic=config.MNEMONIC)
        self._wallets = [lcd.wallet(self.key) for lcd in self._lcd_clients]
        self.lcd = self._lcd_clients[0]
        self.wallet = self._wallets[0]
        self.address = self.key.acc_address
        # See get_account_number_and_sequence's docstring — cached across
        # loop iterations, not just within one, since the sequence only
        # actually changes when THIS process broadcasts a real tx.
        self._cached_account_number = None
        self._cached_sequence = None
        self._account_cache_ts = 0.0

    def query_contract(self, contract_addr: str, query_msg: dict) -> dict:
        """Failover-safe (delegates to pool_client's raw REST path — see
        pool_client._query_contract_raw). Previously went through
        self.lcd.wasm.contract_query, pinned to the single primary
        endpoint; a plain contract query has no broadcast/double-send
        risk, so there's no reason not to fail over here too."""
        return pool_client._query_contract_raw(contract_addr, query_msg)

    def get_latest_block_height(self) -> int:
        """
        Diagnostic helper: log this alongside pool reserves each loop. If
        reserves are identical across several loops AND this height isn't
        advancing (~6s/block on Terra Classic), the LCD endpoint itself is
        serving stale/cached data — not a bug in this bot. If the height
        IS advancing while reserves stay flat, the pools genuinely just
        had no other trades in that window (plausible for the thinner
        pairs here), and there's nothing to fix. Failover-safe (see
        pool_client.get_latest_block_height_raw).
        """
        return pool_client.get_latest_block_height_raw()

    def get_balance(self, denom: str) -> int:
        """Failover-safe (see pool_client._query_bank_balance_raw)."""
        return pool_client._query_bank_balance_raw(self.address, denom)

    def get_cw20_balance(self, token_address: str) -> int:
        """Live balance query for a CW20 token — use this instead of
        trusting a pool's swap-event return_amount when the token might
        apply its own transfer tax (return_amount only reflects what the
        pool sent, not what a taxed token actually delivers). Failover-
        safe (see pool_client._query_cw20_balance_raw)."""
        return pool_client._query_cw20_balance_raw(token_address, self.address)

    def get_asset_balance(self, asset) -> int:
        """Live balance for any Asset (native or cw20)."""
        if asset.kind == "native":
            return self.get_balance(asset.id)
        return self.get_cw20_balance(asset.id)

    def sign_and_broadcast(self, msgs: list, memo: str = ""):
        """
        Creates, signs, and broadcasts a transaction, then polls for the
        confirmed on-chain result.

        Uses `sync` broadcast mode rather than `block` mode: many public
        LCD endpoints (including the default one in .env) reject block
        mode outright. Sync mode only confirms the tx was accepted into
        the mempool (passed CheckTx) — it does NOT mean the transaction
        succeeded on-chain. So after broadcasting, this polls tx_info by
        hash until the transaction is actually included in a block, and
        raises if it failed there (DeliverTx failure), which is the real
        pass/fail signal.
        """
        options = CreateTxOptions(msgs=msgs, memo=memo)
        tx = self.wallet.create_and_sign_tx(options)

        sync_result = self.lcd.tx.broadcast_sync(tx)
        if getattr(sync_result, "code", 0) not in (0, None):
            raise RuntimeError(
                f"Broadcast rejected before entering mempool "
                f"(code={sync_result.code}): {sync_result.raw_log}"
            )

        txhash = sync_result.txhash
        log.info("Broadcast accepted into mempool: txhash=%s — waiting for confirmation...", txhash)

        tx_info = self._wait_for_tx(txhash)
        if getattr(tx_info, "code", 0) not in (0, None):
            raise RuntimeError(
                f"Transaction failed on-chain (code={tx_info.code}): {tx_info.raw_log}"
            )
        # The cached (account_number, sequence) from get_account_number_
        # and_sequence() is now stale — this broadcast just consumed a
        # sequence number. Force a fresh fetch so the NEXT loop (or the
        # next leg, for sequential execution) starts from the real
        # on-chain value instead of a locally-guessed increment, which
        # could drift if this tx didn't consume exactly one sequence the
        # way expected.
        try:
            self.get_account_number_and_sequence(force_refresh=True)
        except Exception as e:
            log.warning("Could not refresh account_number/sequence after broadcast (%s) — "
                        "next loop's cached value may be stale until its TTL expires.", e)
        return tx_info

    def _wait_for_tx(self, txhash: str, max_attempts: int = 15, delay_seconds: float = 2.0):
        """Polls tx_info until the transaction is found (included in a
        block) or we give up. Terra Classic blocks are ~6s, so this
        default (15 x 2s = 30s) gives a couple of blocks of margin.

        FAILOVER — ADDED 2026-08-07: tries EVERY endpoint in
        self._lcd_clients on each attempt (not just the one that received
        the broadcast) before sleeping and retrying. This is a pure read
        of already-broadcast, already-public chain state — any synced
        node that's seen the same block returns the identical result, so
        this is safe the same way query_contract's failover is, and
        specifically protects against the broadcasting endpoint itself
        going stale/unreachable WHILE we're polling it for confirmation
        (the tx already exists on-chain by then; we just need any node
        willing to say so). Still keeps the SDK's own TxInfo object shape
        (not a raw dict) since executor.py's _parse_return_amount/
        _parse_gas_fee depend on that exact attribute structure — this
        only changes WHICH client issues the (identical) SDK call.

        TIMEOUT — ADDED 2026-08-26: same _call_with_deadline wrapper as
        simulate_fee, same reason (this SDK call has no timeout of its
        own). A pure read with no double-send risk, so bounding it and
        moving to the next endpoint in the SAME attempt is strictly safe
        — worst case it just means trying every endpoint takes at most
        len(self._lcd_clients) * SIMULATE_FEE_TIMEOUT_SECONDS instead of
        an unbounded wait on whichever one happens to be slow.
        """
        last_error = None
        for attempt in range(1, max_attempts + 1):
            for lcd in self._lcd_clients:
                try:
                    return _call_with_deadline(lcd.tx.tx_info,
                                                config.SIMULATE_FEE_TIMEOUT_SECONDS, txhash)
                except Exception as e:
                    last_error = e
            time.sleep(delay_seconds)
        raise RuntimeError(
            f"Gave up waiting for tx {txhash} to confirm after "
            f"{max_attempts * delay_seconds:.0f}s across {len(self._lcd_clients)} endpoint(s). "
            f"It may still land — check the txhash on a Terra Classic explorer. "
            f"Last error: {last_error}"
        )

    def get_account_number_and_sequence(self, force_refresh: bool = False):
        """
        Returns (account_number, sequence) as a plain (int, int) tuple.

        Cached across LOOP ITERATIONS (not just within one run_once call)
        for up to _ACCOUNT_CACHE_TTL_SECONDS — confirmed from a real log
        that this call costs ~840ms on its own. Paying that EVERY loop was
        pure waste: the sequence only actually changes when this process
        broadcasts a real tx — sign_and_broadcast() below bumps the cache
        locally right after a successful broadcast, so the common case (no
        trade this loop, or DRY_RUN) never needs a fresh fetch at all. The
        TTL is just a safety net against external drift, not the primary
        invalidation mechanism.

        Pass force_refresh=True to bypass the cache (e.g. after a
        broadcast failure that might indicate the locally-bumped sequence
        drifted from the chain's real value).

        FAILOVER — CHANGED 2026-08-07: now uses pool_client's raw REST
        path (see get_account_number_and_sequence_raw) instead of the
        SDK's wallet.account_number_and_sequence(), which was pinned to
        the single primary endpoint. This is a plain account read, no
        broadcast/double-send risk — and a stall here used to block every
        simulate_fee call in the same loop (they all reuse this result).
        """
        now = time.time()
        if (not force_refresh and self._cached_sequence is not None
                and (now - self._account_cache_ts) < _ACCOUNT_CACHE_TTL_SECONDS):
            return self._cached_account_number, self._cached_sequence

        account_number, sequence = pool_client.get_account_number_and_sequence_raw(self.address)

        self._cached_account_number = account_number
        self._cached_sequence = sequence
        self._account_cache_ts = now
        return account_number, sequence

    def simulate_fee(self, msgs: list, account_number: int = None, sequence: int = None) -> Fee:
        """
        Best-effort gas/fee estimate for a not-yet-broadcast set of
        messages. Used by arbitrage_bot.real_gas_cost_uluna() as the
        pre-flight check before committing real funds — the flat
        per-hop gas guess is only a fallback used to rank candidate
        cycles cheaply, and when this simulation itself fails, the main
        loop refuses to fall back to it for the actual go/no-go decision.

        Pass account_number/sequence (from get_account_number_and_
        sequence(), fetched once per loop) to skip the SDK's own implicit
        per-call lookup — see that method's docstring. Falls back to the
        SDK's default (fetch-every-call) behavior if either is omitted,
        so this stays backward compatible with any other caller.

        FAILOVER — ADDED 2026-08-07: tries every (LCDClient, wallet) pair
        in turn, same as query_contract. create_and_sign_tx here NEVER
        broadcasts (it only asks the chain to estimate gas for a locally-
        signed tx it never submits) — there is no double-send risk in
        retrying this against a different endpoint, unlike the real
        broadcast in sign_and_broadcast below. Raises the last endpoint's
        exception if every one fails.

        TIMEOUT — ADDED 2026-08-26: each attempt now gets at most
        config.SIMULATE_FEE_TIMEOUT_SECONDS via _call_with_deadline (see
        that helper's comment for why — this SDK call has no timeout of
        its own). A timeout is treated exactly like any other failure
        here: it moves on to the next endpoint in this same loop. Built
        directly from arb_20260826.log, where this exact call was the
        single largest chunk (7.04s) of a 15.49s real-check stall.
        """
        options_kwargs = dict(
            msgs=msgs,
            gas_prices=Coins.from_str(f"{config.GAS_PRICE}{config.GAS_DENOM}"),
            gas_adjustment=config.GAS_ADJUSTMENT,
        )
        if account_number is not None and sequence is not None:
            options_kwargs["account_number"] = account_number
            options_kwargs["sequence"] = sequence
        options = CreateTxOptions(**options_kwargs)

        last_exc = None
        for i, wallet in enumerate(self._wallets):
            try:
                tx = _call_with_deadline(wallet.create_and_sign_tx,
                                          config.SIMULATE_FEE_TIMEOUT_SECONDS, options)
                return tx.auth_info.fee
            except concurrent.futures.TimeoutError as e:
                last_exc = TimeoutError(
                    f"simulate_fee against {config.LCD_URLS[i]} exceeded "
                    f"{config.SIMULATE_FEE_TIMEOUT_SECONDS}s — treating as a failure "
                    f"(endpoint may just be slow, not down)")
                if i + 1 < len(self._wallets):
                    log.warning("simulate_fee against %s timed out after %.1fs — trying "
                                "fallback %s.", config.LCD_URLS[i],
                                config.SIMULATE_FEE_TIMEOUT_SECONDS, config.LCD_URLS[i + 1])
            except Exception as e:
                last_exc = e
                if i + 1 < len(self._wallets):
                    log.warning("simulate_fee against %s failed (%s) — trying fallback %s.",
                                config.LCD_URLS[i], e, config.LCD_URLS[i + 1])
        raise last_exc