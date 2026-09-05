<!-- # Terra Classic Arbitrage Bot — LUNC / USTC / TERRA / LCW, 6 pools

Watches six pools across four assets and trades any profitable cycle it
finds, after fees, tax, gas, and a slippage buffer:

- **Terraswap** LUNC/USTC — `terra1l7vy20x940je7lskm6x9s839vjsmekz9k9mv7g`
- **Terraswap** LUNC/USTC — `terra1tndcaqxkpc5ce9qee5ggqf430mr2z3pefe5wj6`
- **Terraport** TERRA/LUNC — `terra1rlfns43umzqszm52txxmnseevffx2pe408c99m7cnvd828tdj67q9ftjs2`
- **Terraport** TERRA/USTC — `terra1p3xuq64hh47hdt0uf0ncy6pzfwplme62nu22vqmv0sk86zwy87uqpl0egn`
- **Terraport** LCW/LUNC — `terra13mg8pvsrhutgw06khvq43pcuhra8cwmxqftlfpxscfqlwxt6yxuqdtqp2f`
- **Terraport** LCW/USTC — `terra17lfxezdehx09g6pker4umyw46w3ssgptxauv8ek0zrd3arpswasq84uvxu`

TERRA (`terra1ex0hjv3wurhj4wgup4jzlzaqj4av6xqd8le4etml7rg9rs207y4s8cdvrp`) and
LCW (`terra1wx48f5g06l9qxw8k4gn3200lmseu85vkgangwyn9xdu66mtfsfxq60hkdt`) are
CW20 tokens; LUNC and USTC are native coins. With 6 pools now checked each
loop across 4 assets, the graph search finds ~36 candidate cycles per loop
(2 to 4 hops each) — timing this comfortably fits inside
`POLL_INTERVAL_SECONDS` (the cycle search itself takes well under a
second; the pool RPC queries dominate loop time, not the math).

## How it's structured now

With 4 pools over 3 assets, the profitable move isn't always "buy here,
sell there" on one pair anymore — it can be triangular, e.g.
`USTC -[Terraswap]-> LUNC -[Terraport]-> TERRA -[Terraport]-> USTC`. So
instead of hardcoding directions, the bot builds a small graph and
searches it:

1. **`assets.py`** — an `Asset` is either a native coin or a CW20 token;
   everything downstream (pool queries, tax, swap messages) is keyed off
   this instead of raw denom strings.
2. **`pool_client.py`** — `DexPool` wraps one pair contract by address
   (no factory lookups) and reports reserves keyed by `Asset`.
3. **`graph.py`** — treats every pool as two directed edges (swap A→B and
   B→A) and does a bounded DFS to find every cycle (2 to 4 hops) that
   returns to its starting asset without reusing a pool. Also simulates
   a cycle's output given a starting amount, and estimates any asset's
   price in uusd terms (directly if a USTC pool exists, otherwise routed
   through LUNC) — used only for comparing profit across different
   starting assets, not for the swap math itself.
4. **`amm_math.py`** — the on-chain constant-product swap formula, plus
   ternary search for the profit-maximizing trade size (works the same
   whether the cycle is 2 hops or 4).
5. **`tax.py`** — Terra Classic's stability tax applies to native coin
   legs only; CW20 (TERRA) legs are correctly exempt.
6. **`executor.py`** — builds the right message shape per leg: native
   offers attach `funds` directly to the pair contract; CW20 offers call
   `Send` on the *token* contract with an embedded swap hook, since a
   pair contract can't pull CW20 funds on its own.

Each loop, the bot fetches all 4 pools' reserves once, enumerates every
cycle starting from LUNC, USTC, and TERRA, sizes and prices each one, and
executes the best if it clears `MIN_PROFIT_UUSD`.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

`.env` already has all four pool addresses and the TERRA token address
filled in from what you gave me. Still worth a quick sanity check on a
Terra Classic explorer that each is what you expect before running with
real funds. You'll need to fill in:

- `MNEMONIC` — the wallet that pays for and signs swaps.
- `DRY_RUN` — defaults to `true`. Leave it there until you're happy with
  what a few cycles of logged (but not broadcast) output look like.

Notable knobs:
- `MAX_TRADE_AMOUNT_ULUNA` / `MAX_TRADE_AMOUNT_UUSD` / `MAX_TRADE_AMOUNT_TERRA`
  — hard per-asset caps on a single leg's offer size, regardless of what
  the model thinks a larger trade would net.
- `MIN_PROFIT_UUSD` — minimum net profit (converted to uusd terms) to fire.
- `TERRAPORT_COMMISSION_RATE` / `TERRASWAP_COMMISSION_RATE` — fallbacks
  used only if the bot can't confirm the fee on-chain per pool at startup.

## Run

```bash
python arbitrage_bot.py
```

Each cycle logs the best opportunity found (even if not profitable
enough to fire), e.g.:

```
Best opportunity: USTC-[Terraport TERRA/USTC]->TERRA-[Terraport TERRA/LUNC]->LUNC-[Terraswap Pool 1]->USTC offer=... profit_uusd=...
```

## Things worth checking before going live with real funds

- **Gas estimate is a rough per-hop guess** (`GAS_UNITS_PER_HOP` in
  `arbitrage_bot.py`), scaled by the number of legs in a cycle — not a
  real simulation per opportunity. For tighter margins on triangular
  routes (which cost more gas than a 2-hop trade), consider replacing it
  with `terra.simulate_fee(msgs)` on the actual constructed messages.
- **No atomic/same-block execution.** Legs are broadcast sequentially.
  For a 3-hop cycle that's three separate transactions where the market
  (or another arbitrageur) can move between each one. `SLIPPAGE_BUFFER_BPS`
  and `max_spread` in `executor.py` are your safety margins — consider
  tightening them for longer cycles, since more hops means more exposure.
- **Commission rate discovery is best-effort** per pool; confirm the
  fallback in `.env` matches reality if a pool doesn't expose its fee
  in a query shape the bot recognizes.
- **CW20 balance/allowance**: the bot assumes your wallet directly holds
  whatever amount of TERRA a cycle needs mid-route (from the prior leg's
  output) — there's no separate CW20 allowance step needed since `Send`
  moves the tokens directly, but do confirm your wallet already holds
  TERRA if you ever want to *start* a cycle from TERRA rather than only
  passing through it.
- Start with low `MAX_TRADE_AMOUNT_*` values and `DRY_RUN=true` until
  you've watched several full cycles, including at least one 3-hop
  triangular opportunity, end to end.

## Extending

- **More pools**: add another `DexPool(...)` to the `pools` list in
  `arbitrage_bot.main()`. If it introduces a new asset, add that asset to
  `assets_to_check` too so cycles starting from it get considered.
- **CW20/CW20 pairs**: already supported by the same code path — a pool's
  two assets can be any mix of native/native, native/cw20, or cw20/cw20. -->



# Terra Classic Arbitrage Bot — LUNC / USTC / TERRA / LCW, 6 pools

Watches six pools across four assets and trades any profitable cycle it
finds, after fees, tax, gas, and a slippage buffer:

- **Terraswap** LUNC/USTC — `terra1l7vy20x940je7lskm6x9s839vjsmekz9k9mv7g`
- **Terraswap** LUNC/USTC — `terra1tndcaqxkpc5ce9qee5ggqf430mr2z3pefe5wj6`
- **Terraport** TERRA/LUNC — `terra1rlfns43umzqszm52txxmnseevffx2pe408c99m7cnvd828tdj67q9ftjs2`
- **Terraport** TERRA/USTC — `terra1p3xuq64hh47hdt0uf0ncy6pzfwplme62nu22vqmv0sk86zwy87uqpl0egn`
- **Terraport** LCW/LUNC — `terra13mg8pvsrhutgw06khvq43pcuhra8cwmxqftlfpxscfqlwxt6yxuqdtqp2f`
- **Terraport** LCW/USTC — `terra17lfxezdehx09g6pker4umyw46w3ssgptxauv8ek0zrd3arpswasq84uvxu`
- **Terraport** REV/LUNC — `terra1pjwkfssz5szyvvs73nwx5sznr4aaz0rgnpk87ayju9h2sw8d2wes9gx4x2`
- **Terraport** REV/USTC — `terra1cydw53epkst0slxyd8ax5rfqmzh9xn7d9wke4gt25vnev9y44w2qdvgt8v`

TERRA (`terra1ex0hjv3wurhj4wgup4jzlzaqj4av6xqd8le4etml7rg9rs207y4s8cdvrp`) and
LCW (`terra1wx48f5g06l9qxw8k4gn3200lmseu85vkgangwyn9xdu66mtfsfxq60hkdt`) are
CW20 tokens; LUNC and USTC are native coins. With 6 pools now checked each
loop across 4 assets, the graph search finds ~36 candidate cycles per loop
(2 to 4 hops each) — timing this comfortably fits inside
`POLL_INTERVAL_SECONDS` (the cycle search itself takes well under a
second; the pool RPC queries dominate loop time, not the math).

## How it's structured now

With 4 pools over 3 assets, the profitable move isn't always "buy here,
sell there" on one pair anymore — it can be triangular, e.g.
`USTC -[Terraswap]-> LUNC -[Terraport]-> TERRA -[Terraport]-> USTC`. So
instead of hardcoding directions, the bot builds a small graph and
searches it:

1. **`assets.py`** — an `Asset` is either a native coin or a CW20 token;
   everything downstream (pool queries, tax, swap messages) is keyed off
   this instead of raw denom strings.
2. **`pool_client.py`** — `DexPool` wraps one pair contract by address
   (no factory lookups) and reports reserves keyed by `Asset`.
3. **`graph.py`** — treats every pool as two directed edges (swap A→B and
   B→A) and does a bounded DFS to find every cycle (2 to 4 hops) that
   returns to its starting asset without reusing a pool. Also simulates
   a cycle's output given a starting amount, and estimates any asset's
   price in uusd terms (directly if a USTC pool exists, otherwise routed
   through LUNC) — used only for comparing profit across different
   starting assets, not for the swap math itself.
4. **`amm_math.py`** — the on-chain constant-product swap formula, plus
   ternary search for the profit-maximizing trade size (works the same
   whether the cycle is 2 hops or 4).
5. **`tax.py`** — Terra Classic's stability tax applies to native coin
   legs only; CW20 (TERRA) legs are correctly exempt.
6. **`executor.py`** — builds the right message shape per leg: native
   offers attach `funds` directly to the pair contract; CW20 offers call
   `Send` on the *token* contract with an embedded swap hook, since a
   pair contract can't pull CW20 funds on its own.

Each loop, the bot fetches all 4 pools' reserves once, enumerates every
cycle starting from LUNC, USTC, and TERRA, sizes and prices each one, and
executes the best if it clears `MIN_PROFIT_UUSD`.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

`.env` already has all four pool addresses and the TERRA token address
filled in from what you gave me. Still worth a quick sanity check on a
Terra Classic explorer that each is what you expect before running with
real funds. You'll need to fill in:

- `MNEMONIC` — the wallet that pays for and signs swaps.
- `DRY_RUN` — defaults to `true`. Leave it there until you're happy with
  what a few cycles of logged (but not broadcast) output look like.

Notable knobs:
- `MAX_TRADE_AMOUNT_ULUNA` / `MAX_TRADE_AMOUNT_UUSD` / `MAX_TRADE_AMOUNT_TERRA`
  — hard per-asset caps on a single leg's offer size, regardless of what
  the model thinks a larger trade would net.
- `MIN_PROFIT_UUSD` — minimum net profit (converted to uusd terms) to fire.
- `TERRAPORT_COMMISSION_RATE` / `TERRASWAP_COMMISSION_RATE` — fallbacks
  used only if the bot can't confirm the fee on-chain per pool at startup.

## Run

```bash
python arbitrage_bot.py
```

Each cycle logs the best opportunity found (even if not profitable
enough to fire), e.g.:

```
Best opportunity: USTC-[Terraport TERRA/USTC]->TERRA-[Terraport TERRA/LUNC]->LUNC-[Terraswap Pool 1]->USTC offer=... profit_uusd=...
```

## Things worth checking before going live with real funds

- **Gas estimate is a rough per-hop guess** (`GAS_UNITS_PER_HOP` in
  `arbitrage_bot.py`), scaled by the number of legs in a cycle — not a
  real simulation per opportunity. For tighter margins on triangular
  routes (which cost more gas than a 2-hop trade), consider replacing it
  with `terra.simulate_fee(msgs)` on the actual constructed messages.
- **No atomic/same-block execution.** Legs are broadcast sequentially.
  For a 3-hop cycle that's three separate transactions where the market
  (or another arbitrageur) can move between each one. `SLIPPAGE_BUFFER_BPS`
  and `max_spread` in `executor.py` are your safety margins — consider
  tightening them for longer cycles, since more hops means more exposure.
- **Commission rate discovery is best-effort** per pool; confirm the
  fallback in `.env` matches reality if a pool doesn't expose its fee
  in a query shape the bot recognizes.
- **CW20 balance/allowance**: the bot assumes your wallet directly holds
  whatever amount of TERRA a cycle needs mid-route (from the prior leg's
  output) — there's no separate CW20 allowance step needed since `Send`
  moves the tokens directly, but do confirm your wallet already holds
  TERRA if you ever want to *start* a cycle from TERRA rather than only
  passing through it.
- Start with low `MAX_TRADE_AMOUNT_*` values and `DRY_RUN=true` until
  you've watched several full cycles, including at least one 3-hop
  triangular opportunity, end to end.

## Extending

- **More pools**: add another `DexPool(...)` to the `pools` list in
  `arbitrage_bot.main()`. If it introduces a new asset, add that asset to
  `assets_to_check` too so cycles starting from it get considered.
- **CW20/CW20 pairs**: already supported by the same code path — a pool's
  two assets can be any mix of native/native, native/cw20, or cw20/cw20.