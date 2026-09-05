"""
Builds MsgExecuteContract swap messages for a given offer Asset (native or
CW20) against a pair contract, and handles broadcasting (or, in DRY_RUN,
just logs what would have been sent).

Native offer: swap executed directly on the pair contract, with the offer
coin attached as `funds`.

CW20 offer: the pair contract can't pull CW20 funds itself (no allowance
model in play here) — instead you call `Send` on the *token* contract,
which forwards the tokens to the pair contract along with an embedded
Cw20HookMsg::Swap, base64-encoded in the `msg` field.
"""
import json
import base64
import logging
from dataclasses import dataclass
from collections import namedtuple
from terra_classic_sdk.core.wasm import MsgExecuteContract
from terra_classic_sdk.core.coins import Coin, Coins

import config
from assets import Asset

log = logging.getLogger("executor")

# Per-leg execution parameters, produced by graph.compute_leg_execution_params
# and consumed here to build the right message for the pool's actual
# contract type. kind is "terraswap" (belief_price/max_spread apply,
# min_receive is None) or "garuda" (min_receive applies, belief_price/
# max_spread are None) — see build_leg_msg below. Defined here (not in
# graph.py) since this is fundamentally about message-building, not
# cycle-finding; graph.py imports this rather than the reverse, so there's
# no import cycle.
LegParams = namedtuple("LegParams", ["kind", "belief_price", "max_spread", "min_receive"])


@dataclass
class LegResult:
    received: int        # from the swap event — informational, do NOT
                          # trust this alone for CW20s with hidden taxes
    gas_fee_uluna: int    # actual fee paid for this tx (0 in DRY_RUN)


def build_swap_msg(sender: str, pair_address: str, offer_asset: Asset,
                    offer_amount: int, max_spread: str = "0.02",
                    belief_price: str = None):
    if offer_asset.kind == "native":
        execute_msg = {
            "swap": {
                "offer_asset": {
                    "info": offer_asset.info(),
                    "amount": str(offer_amount),
                },
                "max_spread": max_spread,
            }
        }
        if belief_price is not None:
            execute_msg["swap"]["belief_price"] = belief_price

        return MsgExecuteContract(
            sender,
            pair_address,
            execute_msg,
            Coins([Coin(offer_asset.id, offer_amount)]),
        )

    # CW20 path: Send{contract, amount, msg} on the token contract itself.
    inner_msg = {"swap": {"max_spread": max_spread}}
    if belief_price is not None:
        inner_msg["swap"]["belief_price"] = belief_price
    encoded_hook = base64.b64encode(json.dumps(inner_msg).encode()).decode()

    execute_msg = {
        "send": {
            "contract": pair_address,
            "amount": str(offer_amount),
            "msg": encoded_hook,
        }
    }
    return MsgExecuteContract(
        sender,
        offer_asset.id,  # the CW20 token contract, not the pair
        execute_msg,
        None,
    )


def build_swap_msg_garuda(sender: str, pair_address: str, offer_asset: Asset,
                           offer_amount: int, min_receive: int = 0):
    """
    CONFIRMED 2026-08-04 via probe_garuda_schema.py against real Garuda
    `pair_base` contracts (zero-cost — TerraClient.simulate_fee only,
    never broadcasts). Both paths were reverse-engineered from the chain's
    own rejection messages, not from contract source/docs:

      - native offer: offer_asset is {"native": "<denom>"} — a BARE DENOM
        STRING, not an amount and not a {denom, amount} struct (both of
        those were tried and rejected: the struct/tuple shapes failed to
        parse at all with "Invalid type"; a bare amount string parsed but
        was rejected as "Invalid offer asset"). The actual amount comes
        entirely from the attached `funds` Coin, same as every other
        native offer in this bot. min_receive is REQUIRED at the top
        level (confirmed via a "missing field `min_receive`" rejection
        when omitted) — there is no optional max_spread/belief_price
        fallback the way Terraswap-family pairs have.
      - cw20 offer (Send hook): {"swap": {"min_receive": "<amt>"}} —
        confirmed accepted with a real held balance (JURIS, via the
        BENANCE/JURIS pool). max_spread is accepted alongside it but not
        required.

    min_receive defaults to 0 ONLY as a safe-to-call default for further
    schema/interface probing (0 = "accept absolutely any output", so it
    never itself causes a rejection here). It provides ZERO real slippage
    protection and must NEVER be used for an actual broadcast — a real
    caller needs to compute this from live reserves the same way
    graph.compute_leg_execution_params derives belief_price/max_spread
    for Terraswap-family pools, before this is ever wired into
    execute_leg/execute_cycle_atomic for real trading.
    """
    if offer_asset.kind == "native":
        execute_msg = {
            "swap": {
                "offer_asset": {"native": offer_asset.id},
                "min_receive": str(min_receive),
            }
        }
        return MsgExecuteContract(
            sender,
            pair_address,
            execute_msg,
            Coins([Coin(offer_asset.id, offer_amount)]),
        )

    inner_msg = {"swap": {"min_receive": str(min_receive)}}
    encoded_hook = base64.b64encode(json.dumps(inner_msg).encode()).decode()
    execute_msg = {
        "send": {
            "contract": pair_address,
            "amount": str(offer_amount),
            "msg": encoded_hook,
        }
    }
    return MsgExecuteContract(
        sender,
        offer_asset.id,
        execute_msg,
        None,
    )


def execute_leg(terra, pair_address: str, offer_asset: Asset, offer_amount: int,
                 max_spread: str = "0.02", belief_price: str = None,
                 pool_kind: str = "terraswap", min_receive: int = 0) -> LegResult:
    """
    Executes a single swap leg. In DRY_RUN mode, logs the intended action
    and returns a zeroed LegResult without broadcasting.

    Kept on this (pair_address, offer_asset, ...) shape rather than
    taking a graph.Edge directly — smoke_test_new_tokens.py and other
    scripts already call it this way and don't have an Edge to hand it.
    pool_kind/min_receive default to the original Terraswap-family
    behavior, so every existing caller keeps working unchanged; a caller
    that knows it's dealing with a Garuda leg passes pool_kind="garuda"
    and a real min_receive (see build_leg_msg/LegParams in this same
    module for the arbitrage_bot.py call site that does this from
    graph.compute_leg_execution_params's output).

    belief_price/max_spread should normally come from
    graph.compute_leg_execution_params(), anchored to the same snapshot
    reserves the cycle was sized and priced against — the old flat
    max_spread=0.02 with no belief_price gave the pair contract nothing
    real to check the fill against.

    IMPORTANT: `.received` comes from the swap event and can be wrong for
    CW20 tokens with a hidden transfer tax (confirmed for LCW) — callers
    that need the true amount should measure a balance delta instead, and
    use `.gas_fee_uluna` to correct that delta when the output asset is
    LUNC (gas is always paid in uluna, so a LUNC-denominated leg's raw
    balance delta is swap proceeds minus gas, not swap proceeds alone).
    """
    if pool_kind == "garuda":
        msg = build_swap_msg_garuda(terra.address, pair_address, offer_asset, offer_amount,
                                     min_receive=min_receive)
    else:
        msg = build_swap_msg(terra.address, pair_address, offer_asset, offer_amount,
                              max_spread, belief_price)

    if config.DRY_RUN:
        log.info("[DRY_RUN] Would swap %s %s via %s (pool_kind=%s max_spread=%s "
                  "belief_price=%s min_receive=%s)",
                  offer_amount, offer_asset, pair_address, pool_kind, max_spread,
                  belief_price, min_receive)
        return LegResult(received=0, gas_fee_uluna=0)

    result = terra.sign_and_broadcast([msg], memo=config.TX_MEMO)
    received = _parse_return_amount(result)
    gas_fee = _parse_gas_fee(result)
    log.info("Swap executed: txhash=%s offered=%s%s received=%s gas_fee_uluna=%s",
              result.txhash, offer_amount, offer_asset, received, gas_fee)
    return LegResult(received=received, gas_fee_uluna=gas_fee)


def build_leg_msg(sender: str, edge, offer_amount: int, leg_params: LegParams):
    """
    Single dispatch point for building a leg's swap message FROM AN EDGE
    (graph.Edge) — used by arbitrage_bot.build_cycle_msgs and
    execute_cycle_atomic below, which already have edge objects on hand
    (unlike execute_leg above, which predates LegParams and is kept on
    its original pair_address/offer_asset signature for backward
    compatibility with existing callers). Picks build_swap_msg or
    build_swap_msg_garuda based on leg_params.kind, which graph.
    compute_leg_execution_params sets from edge.pool.pool_kind.
    """
    if leg_params.kind == "garuda":
        return build_swap_msg_garuda(sender, edge.pool.pair_address, edge.asset_in,
                                      offer_amount, min_receive=leg_params.min_receive)
    return build_swap_msg(sender, edge.pool.pair_address, edge.asset_in, offer_amount,
                           max_spread=leg_params.max_spread, belief_price=leg_params.belief_price)

@dataclass
class CycleResult:
    txhash: str
    gas_fee_uluna: int   # actual fee paid for the WHOLE bundled tx (0 in DRY_RUN)


def execute_cycle_atomic(terra, cycle, leg_amounts, leg_params) -> CycleResult:
    """
    Builds all len(cycle) leg messages and broadcasts them as ONE
    transaction. Cosmos SDK txs are atomic across messages by default —
    if any leg's message would fail on-chain (spread exceeded,
    insufficient funds from a mismatched prior-leg amount, etc.), the
    ENTIRE tx reverts, including legs that would have succeeded. Nothing
    partially executes.

    This trades the sequential executor's "adaptive mid-cycle balance-
    delta correction" (which safely absorbed LCW's undocumented transfer
    tax by re-measuring before sizing the next leg) for "hard revert on
    any mismatch" — a surprise on any leg costs only gas, never stranded
    principal in the middle of a cycle. It also removes the multi-second
    window between sequential broadcasts where pool state could move
    between legs (the leading theory for the 2026-07-12 loss).

    leg_amounts/leg_params MUST come from the same snapshot
    (graph.simulate_cycle_legs / compute_leg_execution_params) already
    used to size, price, and gas-check this cycle — this function does
    not re-derive them, to guarantee the broadcast tx is byte-for-byte
    what was already approved and gas-simulated. leg_params[i] is a
    LegParams — build_leg_msg picks the right message shape (Terraswap-
    family vs Garuda) per leg from its .kind.
    """
    msgs = [
        build_leg_msg(terra.address, edge, leg_amounts[i], leg_params[i])
        for i, edge in enumerate(cycle)
    ]

    if config.DRY_RUN:
        for i, edge in enumerate(cycle):
            log.info("[DRY_RUN] (atomic leg %d/%d) Would swap %s %s via %s "
                      "(kind=%s max_spread=%s belief_price=%s min_receive=%s)",
                      i + 1, len(cycle), leg_amounts[i], edge.asset_in, edge.pool.pair_address,
                      leg_params[i].kind, leg_params[i].max_spread, leg_params[i].belief_price,
                      leg_params[i].min_receive)
        return CycleResult(txhash="DRY_RUN", gas_fee_uluna=0)

    result = terra.sign_and_broadcast(msgs, memo=config.TX_MEMO)
    gas_fee = _parse_gas_fee(result)
    log.info("Atomic cycle executed: txhash=%s legs=%d gas_fee_uluna=%s",
              result.txhash, len(cycle), gas_fee)
    return CycleResult(txhash=result.txhash, gas_fee_uluna=gas_fee)

    
def execute_plan_atomic(terra, plan) -> CycleResult:
    """
    Like execute_cycle_atomic, but takes a FLAT plan — a list of
    (edge, trip_amount, LegParams) tuples, as produced by
    graph.plan_cycle_execution — instead of one (edge, amount, params)
    per cycle leg. A leg that graph.find_optimal_leg_split decided is
    worth splitting contributes MULTIPLE entries here (same edge/pool,
    each a smaller sequential same-direction trade); a leg where
    splitting wasn't worth the extra gas contributes exactly one, same
    as execute_cycle_atomic's original per-leg behavior. Every entry
    still becomes one MsgExecuteContract in ONE bundled, atomic
    broadcast — this only changes how many messages a single logical
    leg turns into, not the atomicity guarantee: if any message in the
    plan would fail on-chain, the entire tx reverts, same as
    execute_cycle_atomic.

    Kept as a separate function (not folded into execute_cycle_atomic)
    so existing callers building one-message-per-leg plans are
    completely unaffected.
    """
    msgs = [build_leg_msg(terra.address, edge, trip_amount, params)
            for edge, trip_amount, params in plan]

    if config.DRY_RUN:
        for i, (edge, trip_amount, params) in enumerate(plan):
            log.info("[DRY_RUN] (plan msg %d/%d) Would swap %s %s via %s "
                      "(kind=%s max_spread=%s belief_price=%s min_receive=%s)",
                      i + 1, len(plan), trip_amount, edge.asset_in, edge.pool.pair_address,
                      params.kind, params.max_spread, params.belief_price, params.min_receive)
        return CycleResult(txhash="DRY_RUN", gas_fee_uluna=0)

    result = terra.sign_and_broadcast(msgs, memo=config.TX_MEMO)
    gas_fee = _parse_gas_fee(result)
    log.info("Atomic plan executed: txhash=%s messages=%d gas_fee_uluna=%s",
              result.txhash, len(msgs), gas_fee)
    return CycleResult(txhash=result.txhash, gas_fee_uluna=gas_fee)


def _parse_return_amount(result) -> int:
    """Pulls return_amount out of the wasm event logs, if present."""
    try:
        for log_entry in result.logs:
            for ev in log_entry.events:
                if ev["type"] == "wasm":
                    attrs = {a["key"]: a["value"] for a in ev["attributes"]}
                    if "return_amount" in attrs:
                        return int(attrs["return_amount"])
    except Exception as e:
        log.warning("Could not parse return_amount from tx result: %s", e)
    return 0


def _parse_gas_fee(result) -> int:
    """Pulls the actual uluna fee paid for this tx out of the confirmed
    tx result. Tries a couple of attribute paths since the exact shape
    isn't guaranteed across SDK versions — falls back to 0 (with a
    warning) if none match, which callers should treat conservatively."""
    try:
        fee_coins = result.tx.auth_info.fee.amount
        for c in fee_coins:
            if c.denom == config.GAS_DENOM:
                return int(c.amount)
    except Exception:
        pass
    try:
        # some SDK versions expose gas_used/gas_wanted but not fee directly;
        # fall back to gas_wanted * configured gas price as an estimate.
        gas_wanted = int(getattr(result, "gas_wanted", 0))
        if gas_wanted > 0:
            return int(gas_wanted * float(config.GAS_PRICE))
    except Exception:
        pass
    log.warning("Could not determine actual gas fee paid for txhash=%s — "
                "balance-delta checks on LUNC-denominated legs may be unreliable this time.",
                getattr(result, "txhash", "?"))
    return 0