"""
Central configuration. Everything sensitive or environment-specific lives in
.env (copy .env.example -> .env and fill it in). Nothing here should contain
real secrets.
"""
import os
from decimal import Decimal
from dotenv import load_dotenv

load_dotenv()


def _dec(key: str, default: str) -> Decimal:
    return Decimal(os.getenv(key, default))


def _int(key: str, default: str) -> int:
    return int(os.getenv(key, default))


def _bool(key: str, default: str) -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "on")


# Root logger level. Defaults to INFO (Best opportunity / Skipping / Timing /
# errors, WITHOUT the per-cycle "Checked ..." and "refusing to compute safe
# execution params" DEBUG lines, or urllib3's connection-level noise).
# Set LOG_LEVEL=DEBUG in .env for a session when you actually need to see
# every cycle's sizing math again (e.g. to check the required-max-spread
# distribution the way arb_20260830.log let us do).
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()


# --- Wallet / network ---
MNEMONIC = os.getenv("MNEMONIC", "")
LCD_URL = os.getenv("LCD_URL", "https://terra-classic-lcd.publicnode.com")
CHAIN_ID = os.getenv("CHAIN_ID", "columbus-5")

# LCD FAILOVER — ADDED 2026-08-07 after a real log (arb_20260807.log) showed
# terra-classic-lcd.publicnode.com stalling ~12 simultaneous pool queries at
# once (10s read timeout each, before urllib3's own same-endpoint retry even
# kicked in) — turning what's normally a ~10s loop into 26s and 42s ones
# twice in under 5 minutes. That's a bigger latency risk than anything in
# this bot's own CPU/sizing logic.
#
# PRUNED 2026-08-24: all three of the original defaults here —
# lcd.terra.dev, terra-lcd.easy2stake.com, terra.stakesystems.io — were
# confirmed via arb_20260824.log to fail on EVERY attempt with a DNS
# resolution error ("Name or service not known"), not a timeout or a
# rejection — 39/39/39 failures across that log, zero successes, ever.
# That means the "failover" this list was meant to provide has been
# providing NONE: during a real primary-endpoint stall in that same log
# (~23s, 26+ pools' state fetches failing at once), every one of those
# pools fell through all 3 dead fallbacks before giving up and using
# stale state — the fallback chain cost real time on every failure
# without ever actually catching one.
#
# IMPORTANT — this may be a DNS/firewall property of wherever THIS bot
# is actually deployed, not proof these hosts are globally dead; I have
# no live network access to verify that distinction myself. Defaulting
# to EMPTY rather than guessing at other unverified replacement URLs —
# a fabricated "working" fallback that's actually also dead is worse
# than an honest empty list, since it would look like protection that
# isn't there. Verify real alternatives from your deployment host
# directly (`curl` or `nslookup` each candidate) and set
# LCD_FALLBACK_URLS yourself once you have ones that actually resolve.
_LCD_FALLBACK_DEFAULT = ""
LCD_FALLBACK_URLS = [u.strip().rstrip("/") for u in
                      os.getenv("LCD_FALLBACK_URLS", _LCD_FALLBACK_DEFAULT).split(",") if u.strip()]
# Primary first, then fallbacks (deduped, primary never repeated).
LCD_URLS = [LCD_URL.rstrip("/")] + [u for u in LCD_FALLBACK_URLS if u != LCD_URL.rstrip("/")]

# PARKED 2026-08-24: pools whose live commission-simulation query is
# confirmed to NEVER succeed for a structural (not transient-network)
# reason, so DexPool.refresh_commission_if_due skips the query entirely
# instead of retrying it forever. Terraport GDEX/GRDX confirmed via
# arb_20260824.log: it reports commission as fee-on-input for this pair
# every single time (see pool_client._resolve_commission_via_simulation's
# docstring), tripping the anomaly-rejection check with 100% consistency
# — and because a pool that's NEVER resolved successfully never sets its
# TTL timestamp, this was being retried on every commission_refresh_loop
# tick (every 30s, forever) instead of respecting the 600s TTL like a
# working pool does. Add a pool's exact `name` here only once its live
# query has been confirmed permanently broken, not just occasionally
# flaky — a pool that merely fails sometimes (e.g. WESO JURIS/cwLUNC,
# whose failures in that same log look like the same dead-DNS issue
# above, not a logic-level one) should keep retrying, since fixing
# LCD_FALLBACK_URLS may resolve it on its own.
COMMISSION_SIMULATION_PARKED = {"Terraport GDEX/GRDX"}
# Per-endpoint timeout for the read-only pool/balance queries above — was a
# flat 10s before this fix. 10s was fine for a single reliable endpoint but
# means one bad endpoint can eat 10 real seconds before even trying a
# fallback; failing over to a fresh endpoint after ~4s (healthy responses in
# the log were consistently under 100ms) loses almost nothing on the happy
# path and caps the worst case at len(LCD_URLS) * this value instead of a
# single stuck connection stalling the whole loop.
LCD_QUERY_TIMEOUT = float(os.getenv("LCD_QUERY_TIMEOUT", "4"))

# ADDED 2026-08-26: terra_client.py's simulate_fee() and _wait_for_tx()
# go through terra_classic_sdk's LCDClient, NOT pool_client's raw REST
# session — LCD_QUERY_TIMEOUT above never applied to them. That SDK's
# LCDClient constructor takes no timeout argument, and shares its
# architecture with the terra_sdk it's forked from, whose synchronous
# LCDClient wraps an aiohttp session under the hood — aiohttp's own
# default timeout, if unset, is 5 MINUTES. Confirmed via arb_20260826.log
# that a single simulate_fee call cost 7.04s of a 15.49s real-check stall
# on a slow (not dead) public LCD — nowhere near 5 minutes, but nowhere
# near LCD_QUERY_TIMEOUT's 4s either, because nothing was enforcing that
# 4s on this call. terra_client._call_with_deadline wraps these SDK calls
# in a worker thread and enforces this from the outside instead of trying
# to configure timeout internals the SDK doesn't expose. Kept as its own
# constant (not reusing LCD_QUERY_TIMEOUT) since building+signing a tx
# locally before the network round trip is real work a plain query
# doesn't do — a little more headroom than 4s avoids false-positive
# timeouts on an endpoint that's simply not instant.
#
# RESTORED 2026-08-27: this constant was accidentally DROPPED on
# 2026-08-27 when config.py got rebuilt from an outdated re-upload that
# predated this addition — the CYCLE_FAIL_COOLDOWN_SECONDS change that
# day was layered onto that stale file without checking it still had
# everything from this one. The result: terra_client.py (correctly)
# called config.SIMULATE_FEE_TIMEOUT_SECONDS, it didn't exist, EVERY
# simulate_fee call raised AttributeError, and _prepare_execution
# (correctly, by its own design) refused to trade on an unverified gas
# number — 141/141 real-check attempts failed this way in
# arb_20260827.log, for zero trades that entire session. The lesson: keep
# config.py and terra_client.py deployed as a matched pair going forward.
SIMULATE_FEE_TIMEOUT_SECONDS = float(os.getenv("SIMULATE_FEE_TIMEOUT_SECONDS", "6"))

# --- Denoms ---
DENOM_LUNC = os.getenv("DENOM_LUNC", "uluna")
DENOM_USTC = os.getenv("DENOM_USTC", "uusd")
# Axelar-bridged USDC (from Ethereum) — a native IBC asset, NOT a CW20
# token. Unlike every other token added today (TERRA, LCW, MIR, ASTRO,
# TRIT, JURIS), this has no contract address and no CW20 transfer tax to
# discover — it moves as `funds` on the pair contract exactly like LUNC/
# USTC, and any stability-tax exposure is already handled generically by
# tax.py's get_tax_rate()/get_tax_cap() (which queries by denom, not by a
# hardcoded LUNC/USTC list). Decimals assumed 6 (USDC's decimal count is
# 6 on every chain we're aware of, including its Axelar-wrapped forms) —
# not independently confirmed on this specific IBC denom.
DENOM_USDC_AXL = os.getenv("DENOM_USDC_AXL", "ibc/E1E3674A0E4E1EF9C69646F9AF8D9497173821826074622D831BAB73CCB99A2D")
# A SEPARATE, DIFFERENT ibc/ denom from DENOM_USDC_AXL above — not the same
# asset, different channel/hash, do not assume it shares USDC.eth.axl's
# confirmed-zero-tax or liquidity profile. Native (kind="native"), no
# contract address, same generic tax.py handling as every other native
# asset in this file (get_tax_rate()/get_tax_cap() query by denom).
DENOM_USDC = os.getenv("DENOM_USDC", "ibc/0BB9D8513E8E8E9AE6A9D211D9136E6DA42288DDE6CFAA453A150A4566054DC5")

# --- cwLUNC / cwUSTC CW20 tokens — CW20 WRAPPERS around native LUNC/USTC.
# ADDED 2026-08-04: reportedly the route people are now using to dodge the
# 2026-08-02 stability tax hike — a cycle that stays entirely in cwLUNC/
# cwUSTC/other-CW20 terms pays NO chain stability tax at all (tax.
# calculate_tax only fires stability tax when asset.kind=="native"; see
# graph.simulate_cycle_costs_uusd's docstring for the full native-vs-cw20
# breakdown). The wrap/unwrap leg itself (cwLUNC<->LUNC, cwUSTC<->USTC)
# still has a native side and is still taxed normally — the saving is on
# every leg AFTER that, while value stays wrapped. Decimals ASSUMED 6, not
# independently confirmed. Whether the wrapper contract itself charges its
# own transfer tax (like LCW does) is UNVERIFIED — assumed 0 until a real
# round-trip proves otherwise, same as every other freshly-added CW20 here.
CWLUNC_CW20_ADDRESS = os.getenv("CWLUNC_CW20_ADDRESS", "terra10fusc7487y4ju2v5uavkauf3jdpxx9h8sc7wsqdqg4rne8t4qyrq8385q6")
CWLUNC_DECIMALS = _int("CWLUNC_DECIMALS", "6")
CWUSTC_CW20_ADDRESS = os.getenv("CWUSTC_CW20_ADDRESS", "terra1uncwzdhxdktqpx4rj6mkuhl0ekv0raua0058rr7zgnapm9najyyqgtpf6h")
CWUSTC_DECIMALS = _int("CWUSTC_DECIMALS", "6")

# --- cwLUNC/LUNC (Terraswap — already-trusted venue, no interface risk).
# LOW LIQUIDITY per the person who supplied this address — same caution as
# ASTROPORT_POOL_ASTRO_LUNC/TERRAPORT_POOL_USDCAXL_TERRA above; scanned on
# PERIODIC_POOL_SCAN_INTERVAL rather than every loop, same as REV.
TERRASWAP_POOL_CWLUNC_LUNC = os.getenv("TERRASWAP_POOL_CWLUNC_LUNC", "terra155epe8zy4npgwsaum7989eqnnk84yn3zy6r9t3zjgvkyzx55x3fs082zz9")

# --- Terraswap pools (LUNC/USTC) ---
TERRASWAP_POOL_1 = os.getenv("TERRASWAP_POOL_1", "")
TERRASWAP_POOL_1_NAME = os.getenv("TERRASWAP_POOL_1_NAME", "Terraswap Pool 1")
TERRASWAP_POOL_2 = os.getenv("TERRASWAP_POOL_2", "")
TERRASWAP_POOL_2_NAME = os.getenv("TERRASWAP_POOL_2_NAME", "Terraswap Pool 2")
TERRASWAP_COMMISSION_RATE = _dec("TERRASWAP_COMMISSION_RATE", "0.003")

# --- TERRA CW20 token --- CONFIRMED 2026-08-30 by the user against the
# CW20 address returned by probe_new_usdc_pools_20260830.py for both new
# TERRA/USDC pools (LUNCSWAP_POOL_TERRA_USDC and TERRA_USDC_POOL_UNKNOWN)
# — that address is this token, resolving the "ASSUMED" caveat on both
# pool entries in arbitrage_bot.py.
TERRA_CW20_ADDRESS = os.getenv("TERRA_CW20_ADDRESS", "terra1ex0hjv3wurhj4wgup4jzlzaqj4av6xqd8le4etml7rg9rs207y4s8cdvrp")
TERRA_DECIMALS = _int("TERRA_DECIMALS", "6")

# --- Terraport pools (TERRA/LUNC, TERRA/USTC) ---
TERRAPORT_POOL_TERRA_LUNC = os.getenv("TERRAPORT_POOL_TERRA_LUNC", "")
TERRAPORT_POOL_TERRA_USTC = os.getenv("TERRAPORT_POOL_TERRA_USTC", "")
TERRAPORT_COMMISSION_RATE = _dec("TERRAPORT_COMMISSION_RATE", "0.02")
# Terraport's own docs (terraport.gitbook.io/terraport-docs/how-to/swap, checked
# 2026-07-13): "transaction fees are set above the standard trading fees to
# include a temporary commission rate increase to 2% for each trade... intended
# to be reduced to 0.3% (0.08% treasury, 0.22% liquidity providers) after the
# completion of the recovery plan and subject to governance approval."
#
# We had this defaulted to 0.003 (their eventual target rate, not their
# CURRENT one) since pool_client.py has never been able to confirm a real
# commission_rate on-chain for any Terraport pool ("using default commission
# rate 0.003 (not found on-chain)" every single loop). That 1.7% gap between
# assumed (0.3%) and real (2%) fee is almost exactly the size of the
# reproducible simulate_fee overflow seen on 2026-07-13 (Cannot Sub ratio
# ~1.68%) — this was very likely the actual root cause, not live market
# drift or a code bug in leg-amount calculation.
#
# IMPORTANT: Terraport describes this as TEMPORARY and tied to governance —
# it may change back to 0.3% at some unknown future point. If Terraport-
# routed cycles start looking unprofitable that previously looked fine (or
# simulate_fee overflows return with a DIFFERENT consistent ratio), re-check
# their docs before assuming something else broke.

# --- LCW CW20 token ---
LCW_CW20_ADDRESS = os.getenv("LCW_CW20_ADDRESS", "")
LCW_DECIMALS = _int("LCW_DECIMALS", "6")

# --- Terraport pools (LCW/LUNC, LCW/USTC) ---
TERRAPORT_POOL_LCW_LUNC = os.getenv("TERRAPORT_POOL_LCW_LUNC", "")
TERRAPORT_POOL_LCW_USTC = os.getenv("TERRAPORT_POOL_LCW_USTC", "")

# --- MIR CW20 token ---
MIR_CW20_ADDRESS = os.getenv("MIR_CW20_ADDRESS", "terra15gwkyepfc6xgca5t5zefzwy42uts8l2m4g40k6")
MIR_DECIMALS = _int("MIR_DECIMALS", "6")

# --- MIR/USTC pools (Terraswap + Astroport — same pair, two venues) ---
TERRASWAP_POOL_MIR_USTC = os.getenv("TERRASWAP_POOL_MIR_USTC", "terra1amv303y8kzxuegvurh0gug2xe9wkgj65enq2ux")
ASTROPORT_POOL_MIR_USTC = os.getenv("ASTROPORT_POOL_MIR_USTC", "terra143xxfw5xf62d5m32k3t4eu9s82ccw80lcprzl9")

# --- ASTRO CW20 token ---
ASTRO_CW20_ADDRESS = os.getenv("ASTRO_CW20_ADDRESS", "terra1xj49zyqrwpv5k928jwfpfy2ha668nwdgkwlrg3")
ASTRO_DECIMALS = _int("ASTRO_DECIMALS", "6")

# --- ASTRO pools (Astroport ASTRO/LUNC, ASTRO/USTC) ---
# NOTE: ASTROPORT_POOL_ASTRO_LUNC has low liquidity per the person who supplied
# this address — expect edge readings on it to be noisier/less trustworthy
# than the other pools, and consider whether it's worth a tighter sizing cap.
ASTROPORT_POOL_ASTRO_LUNC = os.getenv("ASTROPORT_POOL_ASTRO_LUNC", "terra1nujm9zqa4hpaz9s8wrhrp86h3m9xwprjt9kmf9")
ASTROPORT_POOL_ASTRO_USTC = os.getenv("ASTROPORT_POOL_ASTRO_USTC", "terra1l7xu2rl3c7qmtx3r5sd2tz25glf6jh8ul7aag7")

ASTROPORT_COMMISSION_RATE = _dec("ASTROPORT_COMMISSION_RATE", "0.003")


# --- TRIT CW20 token ---
TRIT_CW20_ADDRESS = os.getenv("TRIT_CW20_ADDRESS", "terra1g6fm3yu79gv0rc8067n2nnfpf0vks6n0wpzaf4u7w48tdrmj98zsy7uu00")
TRIT_DECIMALS = _int("TRIT_DECIMALS", "6")

# --- TRIT/LUNC pools (Terraswap + Terraport — same pair, two venues) ---
TERRASWAP_POOL_TRIT_LUNC = os.getenv("TERRASWAP_POOL_TRIT_LUNC", "terra1rdm4u439w8lery29m5dv5z6w7raea0gvt29qefqhr37n5n4yhphsxlqpwc")
TERRAPORT_POOL_TRIT_LUNC = os.getenv("TERRAPORT_POOL_TRIT_LUNC", "terra1suu8wggkc6utp3zkya58v6chfjp2ppx37ufzz0p8ynl7v9zzrpsswucxmp")

# --- TRIT/USTC pools (Terraswap + Terraport — same pair, two venues) ---
TERRASWAP_POOL_TRIT_USTC = os.getenv("TERRASWAP_POOL_TRIT_USTC", "terra1ma72dw9wlkj23nar0qxjhecs24ygzywrqu3rphtjzk2czh0jgyrq2n86lz")
TERRAPORT_POOL_TRIT_USTC = os.getenv("TERRAPORT_POOL_TRIT_USTC", "terra1dvcrax42rtcn5j0kmn5vw2r63rch2x2gztunfkeyrg4yfs9tsksqkg0yyt")


# --- JURIS CW20 token ---
JURIS_CW20_ADDRESS = os.getenv("JURIS_CW20_ADDRESS", "terra1vhgq25vwuhdhn9xjll0rhl2s67jzw78a4g2t78y5kz89q9lsdskq2pxcj2")
JURIS_DECIMALS = _int("JURIS_DECIMALS", "6")

# --- JURIS/LUNC pools (Terraport + Garuda DeFi — same pair, two venues) ---
TERRAPORT_POOL_JURIS_LUNC = os.getenv("TERRAPORT_POOL_JURIS_LUNC", "terra13w6cruzc0xkdhpmrnspr5j282x3wyx0m4f6kjnr65tpq63usn8eqp5emy4")
GARUDA_POOL_JURIS_LUNC = os.getenv("GARUDA_POOL_JURIS_LUNC", "terra1f6pxjjtemwhrypv8y52yv2j98d8j0jlwkp455q0fu8qenlctpy8qh6h74l")

# --- JURIS/TERRA pool (Terraport) — a CW20/CW20 pair (both JURIS and
# TERRA are tokens, no native asset on either side). pool_client.py and
# executor.py are written generically off Asset objects, so this SHOULD
# work with no code changes — but it's the first pool of this shape this
# bot has ever traded, so treat the first few loops touching it as a
# trust-but-verify case, same as everything else added today. This also
# links JURIS into the existing TERRA-rooted cycles (TERRA/LUNC,
# TERRA/USTC), not just JURIS/LUNC directly.
TERRAPORT_POOL_JURIS_TERRA = os.getenv("TERRAPORT_POOL_JURIS_TERRA", "terra18lzl6drsnyr6m9mplfw2fkr8kw6gsvar7wr8wzqgcx7xva58dhpsq7jgdy")

# --- REV CW20 token ---
REV_CW20_ADDRESS = os.getenv("REV_CW20_ADDRESS", "terra1wd7rtrc4tn3xunftxe0kl494cx2368x99w0k5z73rpqrywvy72hqrm726c")
REV_DECIMALS = _int("REV_DECIMALS", "6")

# --- REV/LUNC, REV/USTC pools (Terraport). Per pool_client.py's
# _resolve_commission_via_simulation docstring, asset_x is set to the CW20
# side (rev_token) here, same as every other Terraport CW20/native pool in
# this file — putting the native asset in asset_x instead would silently
# resolve a bogus ~2-orders-of-magnitude-too-low commission rate. Not yet
# independently smoke-tested the way TRIT/JURIS were; treat the first few
# loops touching these two pools as trust-but-verify, same as any new pool.
TERRAPORT_POOL_REV_LUNC = os.getenv("TERRAPORT_POOL_REV_LUNC", "terra1pjwkfssz5szyvvs73nwx5sznr4aaz0rgnpk87ayju9h2sw8d2wes9gx4x2")
TERRAPORT_POOL_REV_USTC = os.getenv("TERRAPORT_POOL_REV_USTC", "terra1cydw53epkst0slxyd8ax5rfqmzh9xn7d9wke4gt25vnev9y44w2qdvgt8v")

# --- BON CW20 token — ADDED for testing, at the user's explicit request.
# Not yet independently smoke-tested; treat the first few loops touching
# these two pools as trust-but-verify, same as REV/TRIT/JURIS were when
# first added. Decimals ASSUMED 6 (this codebase's default for every
# other CW20 here), NOT independently confirmed on-chain — override via
# BON_DECIMALS if that turns out to be wrong. Transfer tax also UNVERIFIED
# — assumed 0 until a real round-trip smoke test proves otherwise, same
# pattern as MIR/TRIT/JURIS when they were added (see REV_TRANSFER_TAX_BPS's
# comment for what that verification looked like when a real tax WAS found).
BON_CW20_ADDRESS = os.getenv("BON_CW20_ADDRESS", "terra15a3wr4ecpye8xhs0xmc2un2t8f87rxhfmfkc88x2s9r2klt4ymnsalq8hn")
BON_DECIMALS = _int("BON_DECIMALS", "6")

# --- MOON — ADDED 2026-08-29 at the user's request, two pools on
# already-trusted venues (Terraport, Garuda). Following the same pattern
# as GDEX/GRDX (Terraport) and Garuda GRDX/LUNC: no per-pool schema probe
# required since the venue itself is already confirmed compatible, but
# treat the first several loops as trust-but-verify per pool, same as
# every freshly-enabled pair. Decimals UNVERIFIED — assumed 6 like every
# other token here unless proven otherwise; revisit if a real trade's
# realized amount doesn't match predicted by a clean power-of-10 ratio.
MOON_CW20_ADDRESS = os.getenv("MOON_CW20_ADDRESS", "terra1apl7g8hcfnlfhgp6xtqmszauvfpsqvmx9sq0f85xcs39uxtw6psq7d65jg")
MOON_DECIMALS = _int("MOON_DECIMALS", "6")
TERRAPORT_POOL_MOON_LUNC = os.getenv("TERRAPORT_POOL_MOON_LUNC", "terra1e93unpcrrq6mpfj6s87ns6cpuj6vjqfnvtfdwdgh3e4quxdhcl0sv2g8wn")
GARUDA_POOL_MOON_TERRA = os.getenv("GARUDA_POOL_MOON_TERRA", "terra1fx2mdd2yv7ff9e8nnazpsh5kt0hd9crnv0gg9ltsyqtrfxu0682s65dtqq")

# --- JEFF, DFC, and a new JURIS/GRDX pair — ADDED 2026-08-29 at the user's
# request, NOT YET ATTACHED to the live pool list. Per the user's explicit
# instruction, all of these get probed before anything is wired in as a
# DexPool/GarudaPool — see probe_new_pools_20260829.py. Two of the
# addresses below (JEFF/LUNC and JEFF/USDC) were supplied as "Unknown
# pool" — venue unconfirmed, so neither Terraswap-standard nor
# Garuda-style shape should be assumed; the probe checks both. One
# LUNC/DFC address is similarly unknown-venue (described by the user as
# "where most traders exit... $12k LP" — real depth if it checks out, but
# still unverified). Decimals UNVERIFIED — assumed 6 like every other
# token here unless proven otherwise.
JEFF_CW20_ADDRESS = os.getenv("JEFF_CW20_ADDRESS", "terra1sjs7rvuj0h7uk40vv3wlhdn0we5mhx0957z4g5900shn7865vwcsh7medu")
JEFF_DECIMALS = _int("JEFF_DECIMALS", "6")
DFC_CW20_ADDRESS = os.getenv("DFC_CW20_ADDRESS", "terra1r9laq5788d36gxmf8jkayln3g5szg4ql0nmccs")
DFC_DECIMALS = _int("DFC_DECIMALS", "6")

# --- JEFF/DFC transfer tax: CONFIRMED clean via TWO smoke_test_jeff_dfc.py
# round trips (2026-08-29, both runs: Terraport JEFF/LUNC -> Garuda
# JEFF/LUNC and Garuda LUNC/DFC -> Terraswap LUNC/DFC), same bar GRDX was
# held to (2 clean passes, 2026-08-05 and 2026-08-06). Both runs landed
# within noise of each other (reserves barely moved between them). Both
# tokens' LUNC-inbound leg (JEFF->LUNC, DFC->LUNC) showed a consistent
# ~150.0bps event-vs-actual gap both times — but that's the SAME native
# Terra Classic stability tax already confirmed for cwLUNC/BENANCE/GRDX
# (see CW20_DIRECTIONAL_TAX_BPS's comment: a native-LUNC-side tax, already
# priced generically via tax.calculate_tax's native branch, NOT a
# CW20-specific tax on JEFF/DFC themselves). The token-inbound leg
# (LUNC->JEFF, LUNC->DFC) showed NO gap on either token, either run — same
# "0bps confirmed" shape as GDEX/GRDX/FUN, not REV's "real tax found"
# shape. No JEFF_TRANSFER_TAX_BPS/DFC_TRANSFER_TAX_BPS constant added
# (both correctly fall back to 0 via cw20_transfer_tax_rate's default).

GARUDA_POOL_JURIS_GRDX = os.getenv("GARUDA_POOL_JURIS_GRDX", "terra14mwaykq2k2z77xlqwn78x782mp5x37f556ywr4ydvm8urspgazaqqcpyh8")

JEFF_POOL_LUNC_UNKNOWN = os.getenv("JEFF_POOL_LUNC_UNKNOWN", "terra106ssxefutyz8hghsxdx5zsunk203r9q3nludja6m9ztq7mwdp8hqkswluc")
JEFF_POOL_USDC_UNKNOWN = os.getenv("JEFF_POOL_USDC_UNKNOWN", "terra1jpcvjxhqmluhqylujmwxcqdvyv8rs5ner65c3wr0z5qghc5tasvssgap38")
TERRAPORT_POOL_JEFF_LUNC = os.getenv("TERRAPORT_POOL_JEFF_LUNC", "terra1txv5p838hpcpahf4dmnshyv6959s0qkhlf3w8zymdxley6zhyhlszkthr4")
TERRAPORT_POOL_JEFF_USTC = os.getenv("TERRAPORT_POOL_JEFF_USTC", "terra1tmtrdatrars9s6nms50kp83v7mn92qsk9p6kayp4mhsu7w6tk0eqswlpa3")

GARUDA_POOL_LUNC_DFC = os.getenv("GARUDA_POOL_LUNC_DFC", "terra1vnd9rje8rrc0pe9x3xdswy6mmteu0xkje85fl5r9srm72gyqvlvqpyul8x")
TERRASWAP_POOL_LUNC_DFC = os.getenv("TERRASWAP_POOL_LUNC_DFC", "terra196hnsfjaghqj3tgg2hq99e24txw575yukzmmy2z87u5qfrlkw5ks55zepj")
LUNC_DFC_POOL_UNKNOWN = os.getenv("LUNC_DFC_POOL_UNKNOWN", "terra14p6vgwa6pt9wmxp7t54ly4ujk8cc4kehvc4dttztutpd0hmtjkns5dl0ad")

# --- Three more USDC pools (config.DENOM_USDC, the native IBC denom, NOT
# DENOM_USDC_AXL) — ADDED 2026-08-30 at the user's request. CONFIRMED and
# WIRED IN (arbitrage_bot.py) 2026-08-30 via probe_new_usdc_pools_
# 20260830.py's real query output — raw responses kept in that run's log,
# summary here:
#
#   - GARUDA_POOL_USDC_LUNC: {"pair":{}} -> 500 (EXPECTED for a genuine
#     Garuda pair_base contract — see GarudaPool's docstring, no such
#     query exists on this family at all). {"pool":{}} -> real Garuda
#     shape (asset1/asset2/reserve1/reserve2), asset1=DENOM_USDC,
#     asset2=uluna. Matches its "Garuda Defi" label. Wired as GarudaPool
#     (usdc, lunc, GARUDA_COMMISSION_RATE) — native/native, no CW20 side,
#     so commission is pinned to that fixed rate forever, same as every
#     other Garuda pool.
#   - LUNCSWAP_POOL_TERRA_USDC: Terraswap-family shape confirmed
#     (asset_infos + assets[]), no pair_type field (expected for this
#     venue, not a red flag — same as LUNCSWAP_POOL_JURIS_USDC and the
#     ampLUNC/Terraswap pools). CW20 side CONFIRMED 2026-08-30 by the user
#     to be TERRA_CW20_ADDRESS (see that constant's own comment). Wired
#     as DexPool(terra_token, usdc, LUNCSWAP_COMMISSION_RATE); terra_token
#     is asset_x so commission self-corrects via live simulation, same
#     reasoning as LUNCSWAP_POOL_JURIS_USDC.
#   - TERRA_USDC_POOL_UNKNOWN: venue NAME still not identified, but schema
#     IS confirmed Terraswap/Astroport-family, and unlike the LuncSwap
#     pool above it explicitly reports pair_type={"xyk":{}} — a different
#     fingerprint, so treat as a genuinely separate unnamed venue, not
#     LuncSwap. Same CW20 (confirmed TERRA)/native USDC pair. Wired as
#     DexPool with a placeholder ASTROPORT_COMMISSION_RATE default (same
#     self-correction logic applies). Left NOT validated as required
#     since nothing depends on it being set until a venue is identified.
GARUDA_POOL_USDC_LUNC = os.getenv("GARUDA_POOL_USDC_LUNC", "terra1vnt3tjg0v98hgp0vx8nynvklnjqzkzsqvtpzv9v56r800gdhmxwstv5y64")
LUNCSWAP_POOL_TERRA_USDC = os.getenv("LUNCSWAP_POOL_TERRA_USDC", "terra1gll3xekkgn0kxu2l6gx6gvlv97ddrs42xzksgyls23gmuhclckys7hthr2")
TERRA_USDC_POOL_UNKNOWN = os.getenv("TERRA_USDC_POOL_UNKNOWN", "terra10ahm3q2zeftde4fmpurwz4te3m0q2nnw5le5pp5ru5w65862whjspk2yd4")

# --- BON/USTC, BON/LUNC pools (Terraport). Per pool_client.py's
# _resolve_commission_via_simulation docstring, asset_x is set to the CW20
# side (bon_token) here, same as every other Terraport CW20/native pool in
# this file — putting the native asset in asset_x instead would silently
# resolve a bogus ~2-orders-of-magnitude-too-low commission rate.
TERRAPORT_POOL_BON_USTC = os.getenv("TERRAPORT_POOL_BON_USTC", "terra1u7ng3yaez0qqthujs792xfsu9pgus6a4hvets49uz8euuduzv0lq895p3t")
TERRAPORT_POOL_BON_LUNC = os.getenv("TERRAPORT_POOL_BON_LUNC", "terra1fn2st04srafy4wl9uj5hdm26xvju55ha4w20aareme7s02fgmesq8fs097")

# --- WESO DeFi — a NEW venue for this bot. UPDATED 2026-08-04 with real
# on-chain findings from check_new_venues_interface.py's first run:
#
#   - WESO_ROUTER_ADDRESS (originally supplied as "cwLUNC/LUNC"): this is
#     NOT a pair contract at all — it's the WESO swap ROUTER
#     (wesoswap_router), confirmed via its real QueryMsg variant list
#     (simulate_swap_operations, execute_swap_operations, route, etc).
#     pool_client.py/executor.py are built around direct pair contracts;
#     a router needs a genuinely different integration (build a swap
#     route through it, not a single pair swap) — not attempted here. If
#     a real WESO cwLUNC/LUNC PAIR contract exists, its address is still
#     unknown; this one can't be used as a substitute.
#   - WESO_POOL_CWLUNC_CWUSTC (originally supplied as "cwUSTC/USTC"): the
#     real {"pair":{}} response shows this is actually a cwLUNC/cwUSTC
#     pool (both CW20, no native USTC anywhere in it) — the same kind of
#     pasted-label mixup the ampLUNC/Astroport pools had (see
#     ASTROPORT_POOL_LUNC_USTC's history above). Renamed to match its
#     real contents. Its query schema IS Terraswap-standard — {"pair":{}}
#     and {"pool":{}} both returned proper shapes — the original "Asset
#     mismatch" failure was just from probing with native USTC, which
#     this pool doesn't contain at all. The genuine native cwUSTC/USTC
#     pairing (if it exists on WESO) still needs its own real address.
#   - WESO_POOL_JURIS_CWLUNC: schema CONFIRMED Terraswap-compatible —
#     {"pair":{}}/{"pool":{}} both returned correct shapes with exactly
#     the expected assets, and a real gas-simulated swap message was
#     PARSED correctly, rejected only for offering below this pool's own
#     minimum order size (1,000,000 base units) — a business-logic
#     rejection, not a schema one. This is the strongest positive signal
#     any new-venue pool has gotten so far. Still needs one clean PASS at
#     a proper probe size (check_new_venues_interface.py's PROBE_AMOUNT
#     was bumped to 2,000,000 to clear this) before being enabled, and a
#     real smoke-test round trip for cwLUNC/JURIS's own transfer tax
#     regardless of that pass.
#
# amm_math.simulate_swap ONLY implements xyk — cwLUNC/cwUSTC is a
# correlated/pegged pair (a wrapped asset against its own underlying),
# the same shape that turned out to be a StableSwap curve for ampLUNC/
# LUNC on Astroport. CONFIRM curve type (check_pool_curve_type.py or the
# pair query's own pair_type field) before enabling WESO_POOL_CWLUNC_CWUSTC
# specifically — JURIS/cwLUNC is not a pegged pair, so this concern
# doesn't apply there. All pools remain DISABLED in arbitrage_bot.py
# pending a final PASS + curve-type check where relevant.
WESO_COMMISSION_RATE = _dec("WESO_COMMISSION_RATE", "0.003")  # UNVERIFIED GUESS
# 2026-08-28: the module-default 1,000,000-unit commission probe sits right
# at WESO_POOL_JURIS_CWLUNC's own documented minimum order size (see history
# above) and is intermittently getting rejected with a 500 rather than a
# clean business-logic error. check_new_venues_interface.py already found
# 2,000,000 clears this cleanly on 2026-08-04 — reusing that same confirmed
# value here instead of guessing a new one.
WESO_COMMISSION_PROBE_AMOUNT = _int("WESO_COMMISSION_PROBE_AMOUNT", "2000000")
WESO_ROUTER_ADDRESS = os.getenv("WESO_ROUTER_ADDRESS", "terra1nynrxdccq0r9ghrz0sq7tjkkh8wug0ggg4lkzsags8r9dyhf7ypqx5gsr8")  # NOT a pair — see above
WESO_POOL_CWLUNC_CWUSTC = os.getenv("WESO_POOL_CWLUNC_CWUSTC", "terra1l7yt60npu2vj67v7qhdam3l7h0pejtsgysfjll2srqaxppt635pqqjh8hc")
WESO_POOL_JURIS_CWLUNC = os.getenv("WESO_POOL_JURIS_CWLUNC", "terra14jedagazgdawpjfn37yhec5lfxs5fh22r6cl3uspa4x9yt8hnhlsp322v7")
# WESO_POOL_CWLUNC_LUNC — INVESTIGATED 2026-08-29, NOT ADDED. The address
# supplied (terra10fusc7487y4ju2v5uavkauf3jdpxx9h8sc7wsqdqg4rne8t4qyrq8385q6)
# is WESO's cwLUNC CW20 token contract, not a pair contract — its {"pair":{}}
# / {"pool":{}} responses were self-referential nonsense (contract_addr,
# liquidity_token, and the "cwLUNC" asset's own contract_addr all identical;
# both reserve amounts identical to each other at ~1 quadrillion raw units,
# roughly 145x LUNC's entire circulating supply — not real reserve data).
# The address WESO's UI actually routes trade transactions to for this pair
# is terra1nynrxdccq0r9ghrz0sq7tjkkh8wug0ggg4lkzsags8r9dyhf7ypqx5gsr8 — which
# is WESO_ROUTER_ADDRESS above, the same router already confirmed NOT a pair
# contract back on 2026-08-04. WESO does not appear to expose a distinct pair
# contract per pool at all; every pair (including JURIS/cwLUNC, which IS
# enabled below) likely settles through this router under the hood in
# WESO's own UI, but WESO_POOL_JURIS_CWLUNC's address was separately
# confirmed to itself be a real, independent, Terraswap-compatible pair
# contract (not the router, not a token address) — so that pool is fine as
# wired. This one just doesn't have an equivalent standalone pair contract
# to point at. Trading cwLUNC/LUNC on WESO specifically would require
# building genuine router integration (simulate_swap_operations /
# execute_swap_operations against WESO_ROUTER_ADDRESS) — not attempted here.
# No config constant kept for this address; if a real WESO cwLUNC/LUNC pair
# contract is found later, add it fresh rather than reusing this name.

# --- USDC/LUNC (Terraswap) and LUNC/USDC (Terraport) — both already-
# trusted venues, uses the NEW config.DENOM_USDC (not DENOM_USDC_AXL —
# see that constant's comment). Terraport is native/native here, so its
# commission resolution always falls back to TERRAPORT_COMMISSION_RATE
# per DexPool.__init__'s docstring (that rate is already the confirmed
# real 2% figure, not a guess, so the fallback is safe).
TERRASWAP_POOL_USDC_LUNC = os.getenv("TERRASWAP_POOL_USDC_LUNC", "terra19h62lw77rluxf6yg4szcclcgk9tsalx72cv7dlzvzs8gy20g70js7c9jkc")
TERRAPORT_POOL_LUNC_USDC = os.getenv("TERRAPORT_POOL_LUNC_USDC", "terra1a29fltd5h5y8se0xanw48wkmqg7nfpmv5jsl472uun0274h8xatqd3yzfh")

# --- LuncSwap.fun — a NEW venue, never queried by this bot before (added
# 2026-08-28 at the user's request). Unlike WESO/Garuda, which each got a
# real check_new_venues_interface.py / probe_garuda_schema.py PASS before
# their pool objects were even written, this address hasn't been queried at
# all yet — no confirmation of {"pair":{}}/{"pool":{}} response shape, no
# pair_type, no confirmation this is even an xyk (constant-product) pool
# (amm_math.simulate_swap only implements xyk — see its module docstring).
# Garuda's pair_base contract looked address-compatible at a glance and
# turned out to use a completely different query/response/ExecuteMsg schema
# (see pool_client.GarudaPool's docstring) — assuming Terraswap-standard
# here without checking first is exactly the mistake that pattern warns
# against. LUNCSWAP_COMMISSION_RATE is an UNVERIFIED GUESS (same 0.3%
# placeholder used for WESO before its rate was ever confirmed). JURIS/USDC
# pairs a CW20 (JURIS) against the native config.DENOM_USDC IBC denom — same
# asset objects already built for the enabled USDC/LUNC pools above.
LUNCSWAP_COMMISSION_RATE = _dec("LUNCSWAP_COMMISSION_RATE", "0.003")  # UNVERIFIED GUESS
LUNCSWAP_POOL_JURIS_USDC = os.getenv("LUNCSWAP_POOL_JURIS_USDC", "terra1lk7fsgfs4phnh6npa89q2uweu4yytmg9cpacgpmuvwr8rmc9gz5s5vak60")
# ADDED 2026-08-28: the module-default 1,000,000-raw-unit commission probe
# (pool_client._COMMISSION_PROBE_AMOUNT) is 1 JURIS token — worth a small
# fraction of a cent against this pool's real reserves (10.3T JURIS / 25.3k
# USDC per arb_20260828.log), so the simulated return/commission rounded to
# ~1 raw unit each, every single refresh, permanently rejected by the sane-
# bounds check (see pool_client.DexPool's commission_probe_amount docstring).
# This is 5,000 JURIS instead — still a negligible sliver of the pool's real
# reserves (~0.00005%), but large enough that the simulated raw output lands
# in the tens-of-thousands-of-raw-units range instead of low single digits,
# which is what the rate math actually needs to resolve something meaningful.
LUNCSWAP_COMMISSION_PROBE_AMOUNT = _int("LUNCSWAP_COMMISSION_PROBE_AMOUNT", "5000000000")

# --- BENANCE, GDEX, GRDX, FUN CW20 tokens — decimals ASSUMED 6 (the
# convention for every other CW20 in this file), not independently
# confirmed for any of these four specific tokens.
BENANCE_CW20_ADDRESS = os.getenv("BENANCE_CW20_ADDRESS", "terra1ctvrh09s3q2tgxm88vt6zexle8wcf22qwhxe5qa2wchc9e2ynw3qhvksyl")
BENANCE_DECIMALS = _int("BENANCE_DECIMALS", "6")
GDEX_CW20_ADDRESS = os.getenv("GDEX_CW20_ADDRESS", "terra16hzl7lge3jjnlazvut2ypt7upk68z5dycc9rwy608fah8q0y3fuslkenzc")
GDEX_DECIMALS = _int("GDEX_DECIMALS", "6")
GRDX_CW20_ADDRESS = os.getenv("GRDX_CW20_ADDRESS", "terra12f3f5fzfzxckc0qlv3rmwwkjfhzevpwmx77345n0zuu2678vxf0sm6vvcw")
GRDX_DECIMALS = _int("GRDX_DECIMALS", "6")
FUN_CW20_ADDRESS = os.getenv("FUN_CW20_ADDRESS", "terra1le9l8gpwl2f48xluphnwkqrnhwxqt27xhducfzqvncmsh9swavlqjpy7c9")
FUN_DECIMALS = _int("FUN_DECIMALS", "6")

# --- Garuda DeFi: BENANCE/LUNC, BENANCE/JURIS, GDEX/LUNC, GDEX/GRDX,
# FUN/GDEX — ENABLED since 2026-08-04 once probe_garuda_schema.py confirmed
# the pair_base ExecuteMsg schema (executor.build_swap_msg_garuda) and
# check_new_venues_interface.py showed PASS on all 5. (This comment used to
# say these were disabled for the same reason GARUDA_POOL_JURIS_LUNC below
# is — that was true before the schema was reverse-engineered; it's stale
# now and left only so the JURIS/LUNC comment below still makes sense.)
GARUDA_POOL_BENANCE_LUNC = os.getenv("GARUDA_POOL_BENANCE_LUNC", "terra174euun47r0xhwkslza67avnm5r4gtxnckfeeuud39c0ahtu5hryq57vel3")
GARUDA_POOL_BENANCE_JURIS = os.getenv("GARUDA_POOL_BENANCE_JURIS", "terra1pkj2r0m75sn6swdq0r4y4dfdk39xdhpylaeq324tfm38ecfjszwqy24rfy")
GARUDA_POOL_GDEX_LUNC = os.getenv("GARUDA_POOL_GDEX_LUNC", "terra13nk3znj8s74ugzycaqsaxtdex54fnxmeazsv8nnk9rycjg7peykq7s3hud")
GARUDA_POOL_GDEX_GRDX = os.getenv("GARUDA_POOL_GDEX_GRDX", "terra1gu2cm3f2wuh8t7kzj9v7y2j89lxfj6zw6ds7vt9hmp9scj3sc6eqapel3c")
GARUDA_POOL_FUN_GDEX = os.getenv("GARUDA_POOL_FUN_GDEX", "terra13x7nt6eg8fm5uz4975vr48plgdq2aa9pcqc4gddm58qd5p77t38q80jkmr")

# --- FUN — three more pools ADDED 2026-08-29 at the user's request, same
# already-trusted venues as everything else FUN-related above (Garuda
# pair_base schema confirmed via check_new_venues_interface.py/
# probe_garuda_schema.py; Terraport confirmed since this bot's earliest
# pools) — no new-venue schema probe needed, same bar as MOON/GDEX-GRDX.
# Treat the first several loops touching each as trust-but-verify, same
# as any freshly-wired pool. FUN_TRANSFER_TAX_BPS above is still the
# UNVERIFIED-assumed-0 reading from the single FUN/GDEX round trip
# tested 2026-08-05 — these three give more real trade evidence to
# confirm (or correct) that against, but don't change it themselves.
GARUDA_POOL_FUN_LUNC = os.getenv("GARUDA_POOL_FUN_LUNC", "terra17w4q8g2jgf3rm5n2eanp2xsyzvgn96sxqner4urtdrkd7cv5u2yqqwg9nf")
GARUDA_POOL_FUN_JURIS = os.getenv("GARUDA_POOL_FUN_JURIS", "terra1kax667emxal5lgrex0hepeazltcrzrrhv5t9ek2dz6nsdu2yhnhq6lygjd")
GARUDA_POOL_GRDX_FUN = os.getenv("GARUDA_POOL_GRDX_FUN", "terra1umc5ucy5mfxaaz6e3eg03f9gqjpsryfc8w8z3783d2n95g5n9zssur9hca")

# --- Garuda GRDX/LUNC — ADDED 2026-08-05 (user-supplied address). NOT YET
# interface-checked (check_new_venues_interface.py) or smoke-tested for its
# own real transfer tax the way the other 5 Garuda pools above were before
# being trusted. Deliberately NOT added to arbitrage_bot.py's live pool
# list yet — follow the same process every other Garuda pool went through:
# check_new_venues_interface.py first (zero-cost schema/reserve check),
# then a real smoke-test round trip, before uncommenting/enabling this.
GARUDA_POOL_GRDX_LUNC = os.getenv("GARUDA_POOL_GRDX_LUNC", "terra1lhpvlec34lda370cwnf78fyapud7d4f8hjwuzjgljf4dt5vt2m5q4qzw7t")

# --- GDEX/GRDX (Terraport — already-trusted venue) — enabled, a CW20/
# CW20 pair like JURIS/TERRA, so no asset_x-ordering concern (see
# DexPool.__init__'s docstring — that rule is specifically about native
# vs. CW20, not two CW20s). Not yet independently smoke-tested against
# these specific tokens; treat first few loops as trust-but-verify.
TERRAPORT_POOL_GDEX_GRDX = os.getenv("TERRAPORT_POOL_GDEX_GRDX", "terra1d880u4454dscex5wy5ckcf3skqkk6dwjf373p74daadgrggeksvq3axfk7")

# --- FUN/LUNC (Terraport — already-trusted venue) — ADDED 2026-08-29.
# CW20/native pair; fun_token is asset_x (the CW20 side), matching
# DexPool.__init__'s ordering rule so live commission resolution can
# self-correct instead of being pinned to TERRAPORT_COMMISSION_RATE's
# default forever. Not yet independently smoke-tested against FUN;
# treat first several loops as trust-but-verify, same as MOON/LUNC was.
TERRAPORT_POOL_FUN_LUNC = os.getenv("TERRAPORT_POOL_FUN_LUNC", "terra1vsqxkcx7jv0q5newczm9x4lpna7rmcph8h5ay0kww0dvymvqfm9q3439gm")

# --- Garuda DeFi is a confirmed-incompatible venue. Originally flagged
# 2026-07-14 from JURIS/LUNC alone (smoke_test_juris.py); CONFIRMED
# 2026-08-04 as a VENUE-LEVEL issue, not a one-pool fluke —
# check_new_venues_interface.py's real query against all 5 Garuda pools
# added that day (BENANCE/LUNC, BENANCE/JURIS, GDEX/LUNC, GDEX/GRDX,
# FUN/GDEX) shows every single one reports contract type `pair_base` on
# {"pool":{}}, and the 3 pools with a probeable balance (BENANCE/LUNC,
# BENANCE/JURIS, GDEX/LUNC) all hit the exact same schema errors as
# JURIS/LUNC: `unknown variant 'info', expected 'native' or 'cw20'` for
# native offers, `missing field 'min_receive'` for the CW20 Send hook.
# The other 2 (GDEX/GRDX, FUN/GDEX) couldn't be probed the same way
# (wallet held zero of either token at test time — that failure was a
# balance issue, not a schema one) but there is no reason to expect them
# to differ from their 3 confirmed siblings on the same venue. Do NOT
# enable any Garuda pool piecemeal based on an unprobed pass — fix
# executor.py's message-building for pair_base's tagged-enum ExecuteMsg
# (`{"native":{...}}`/`{"cw20":{...}}` instead of `{"swap":{"offer_asset":
# {info, amount}}}`, plus a `min_receive` field on the Cw20HookMsg) once,
# then reconsider the whole venue together.
GARUDA_COMMISSION_RATE = _dec("GARUDA_COMMISSION_RATE", "0.005")  # 0.5% per docs.garuda-defi.org (0.2% LP + 0.3% GDEX shares) — confirmed 2026-07-14, venue still disabled pending schema fix

# --- FUTURE (Futureflare) CW20 token ---
# Decimals ASSUMED 6 (the convention for every other CW20 in this file) —
# not independently confirmed for this specific token. Verify against the
# token contract's own token_info query before trusting sizing math on it.
FUTURE_CW20_ADDRESS = os.getenv("FUTURE_CW20_ADDRESS", "terra1rk57qhszgdxt7vp6f7xhuqq5k7kdrqz5cevee4jfvyw7rgga6snqv4tj6m")
FUTURE_DECIMALS = _int("FUTURE_DECIMALS", "6")

# --- Futureflare pools (FUTURE/LUNC, FUTURE/TERRA, FUTURE/TRIT) ---
# FUTURE is a token (project name "Futureflare"), and its pools are on
# TERRAPORT — an already-trusted venue (confirmed working elsewhere via
# TRIT/JURIS/REV). No new-venue interface risk here, unlike Garuda. Not yet
# independently smoke-tested against THIS specific token, though — treat
# the first few loops touching these as trust-but-verify, same as REV was
# when it was first added (see TERRAPORT_POOL_REV_LUNC/USTC's docstring
# above). Uses TERRAPORT_COMMISSION_RATE, not a separate rate — there's no
# separate "Futureflare venue" to have its own commission behavior.
FUTUREFLARE_POOL_FUTURE_LUNC = os.getenv("FUTUREFLARE_POOL_FUTURE_LUNC", "terra1amdjatqkgxga3mmgsz8jq75y62jt0eqmwc06g5k03uzjq6vnrglq3wv3g7")
FUTUREFLARE_POOL_FUTURE_TERRA = os.getenv("FUTUREFLARE_POOL_FUTURE_TERRA", "terra16m5q99uununuy0xkjl2jvd6h3nkthez42pfhm0tcday2h2h7f4fslj6c0t")
FUTUREFLARE_POOL_FUTURE_TRIT = os.getenv("FUTUREFLARE_POOL_FUTURE_TRIT", "terra16lph92gx3vj55u70vlwnafagqv6rmhqttvasehep9ta8j8kgkvpq452m3w")

# --- ampLUNC (Terra Classic liquid-staking derivative) CW20 token ---
# Decimals ASSUMED 6 — not independently confirmed.
AMPLUNC_CW20_ADDRESS = os.getenv("AMPLUNC_CW20_ADDRESS", "terra1wvk6r3pmj0835udwns4r5e0twsclvcyuq9ucgm")
AMPLUNC_DECIMALS = _int("AMPLUNC_DECIMALS", "6")

# --- White Whale LUNC/USTC pool ---
# White Whale is ALSO a new venue for this bot (same interface caveat as
# Futureflare above — unverified). On top of that, White Whale is known
# for offering StableSwap-curve pools specifically for correlated/pegged
# pairs, and LUNC/USTC is exactly the kind of pair that gets that
# treatment rather than a plain constant-product pool. amm_math.
# simulate_swap ONLY implements the xyk (constant-product) formula — if
# this specific pool is actually a StableSwap pool, every return_amount/
# spread/commission this bot computes for it will be systematically wrong
# WITHOUT erroring, which is worse than the interface-mismatch failure
# mode (that one at least fails loud, at the gas-simulation step, before
# funds move). CONFIRM this pool's curve type (query the contract's own
# config/pool-info, or check White Whale's UI/docs for this specific pool)
# before enabling it — don't assume it's xyk just because every other pool
# in this file is.
WHITEWHALE_COMMISSION_RATE = _dec("WHITEWHALE_COMMISSION_RATE", "0.003")
WHITEWHALE_POOL_LUNC_USTC = os.getenv("WHITEWHALE_POOL_LUNC_USTC", "terra1wm3jtcq0fuftvmfq0skmqyxnl3x3j42x8ae56q7eg7v6jsf5eg4qmz36ts")

# --- ampLUNC pools on Astroport/Terraswap (venues already trusted for
# interface — TRIT/MIR/ASTRO already prove these two work) ---
# The interface risk from Futureflare/White Whale above does NOT apply
# here. The StableSwap-curve risk DOES: Astroport specifically supports
# both xyk AND stable pool types on the same factory, selected per-pair,
# and ampLUNC/LUNC (a liquid-staking derivative against its own underlying
# asset) is a textbook stable-pool candidate — that's the whole point of a
# stable curve, keeping a pegged pair's slippage low. Same caveat as
# White Whale above: CONFIRM each pool's curve type before enabling; do
# not assume xyk. Disabled in arbitrage_bot.py's pool list pending that.
# CORRECTED 2026-08-02: check_pool_curve_type.py's actual on-chain query
# revealed the pasted labels for these three addresses didn't match their
# real assets — this is the corrected mapping, verified via each pool's own
# {"pair":{}} query response, not the supplied list:
#   terra1m6ywlgn6wrjuagcmmezzz2a029gtldhey5k552  -> uusd/uluna (NOT ampLUNC
#     at all — a plain third LUNC/USTC pool on Astroport, see
#     ASTROPORT_POOL_LUNC_USTC below instead)
#   terra132qwlqxffksjfg6ntzp4m5786lrlmgrufzx5c6  -> ampLUNC + uluna (this
#     is ampLUNC/LUNC, not ampLUNC/USTC as the supplied list said)
#   terra1daxuedmyeu8cak0ds43u6emj7srkgjeka0twe4  -> ampLUNC + uusd (this
#     one WAS correctly ampLUNC/USTC)
# All three confirmed pair_type={"xyk":{}} — curve type was never the
# problem here, the asset pairing was.
ASTROPORT_POOL_LUNC_USTC = os.getenv("ASTROPORT_POOL_LUNC_USTC", "terra1m6ywlgn6wrjuagcmmezzz2a029gtldhey5k552")
AMPLUNC_ASTROPORT_POOL_LUNC = os.getenv("AMPLUNC_ASTROPORT_POOL_LUNC", "terra132qwlqxffksjfg6ntzp4m5786lrlmgrufzx5c6")
AMPLUNC_ASTROPORT_POOL_USTC = os.getenv("AMPLUNC_ASTROPORT_POOL_USTC", "terra1daxuedmyeu8cak0ds43u6emj7srkgjeka0twe4")
# CORRECTED 2026-08-02: check_pool_curve_type.py's asset check caught the
# SAME kind of pasted-label mixup here that hit the Astroport pools, just
# shuffled differently — 2 of these 3 addresses had their pair labels
# wrong. Verified mapping (no pair_type field on any of the three, which
# is expected — Terraswap predates that Astroport convention and has no
# stable-pool code path to report regardless):
#   terra12gkp648mkgj6esr5y6dwc2q4mftnfzk2pudka8   -> ampLUNC + uluna
#     (labeled "ampLUNC/USTC 1" — actually ampLUNC/LUNC)
#   terra1q3pem26pyxq7qggmxkh5egd4km0a7l0gxe84wk   -> ampLUNC + uusd
#     (labeled "ampLUNC/USTC 2" — correct as labeled)
#   terra1305xtjl5qtlpdlp8gg0k8u4yl05hj7qvyffvd4   -> ampLUNC + uusd
#     (labeled "ampLUNC/LUNC, very low liquidity" — actually ampLUNC/USTC)
AMPLUNC_TERRASWAP_POOL_LUNC = os.getenv("AMPLUNC_TERRASWAP_POOL_LUNC", "terra12gkp648mkgj6esr5y6dwc2q4mftnfzk2pudka8")
AMPLUNC_TERRASWAP_POOL_USTC_1 = os.getenv("AMPLUNC_TERRASWAP_POOL_USTC_1", "terra1q3pem26pyxq7qggmxkh5egd4km0a7l0gxe84wk")
AMPLUNC_TERRASWAP_POOL_USTC_2 = os.getenv("AMPLUNC_TERRASWAP_POOL_USTC_2", "terra1305xtjl5qtlpdlp8gg0k8u4yl05hj7qvyffvd4")  # "(very low liquidity)" per the pool list supplied


# --- USDC.eth.axl pools (Terraport LUNC/USTC/TERRA legs + a Terraswap
# USTC leg — USDC.eth.axl/USTC exists on BOTH venues, same pair-two-venue
# shape as MIR/USTC and TRIT). Every pool here is native-or-cw20 vs. a
# native asset, so no new code path is needed anywhere — executor.py
# already handles native offers via `funds` and cw20 offers via the Send
# hook, and pool_client.py's Asset-keyed reserves don't care which side
# is native. ---
TERRAPORT_POOL_USDCAXL_LUNC = os.getenv("TERRAPORT_POOL_USDCAXL_LUNC", "terra17tk9v62lgvum67rs2mx98h6kz86m6m7lg5k0gnv407xhgc957z5qsp404w")
TERRAPORT_POOL_USDCAXL_USTC = os.getenv("TERRAPORT_POOL_USDCAXL_USTC", "terra1j3gpzmpgr3vns06sexf4d2pj2ydqj6gwhnxz0fy44elad06ace8qyhnf3v")
# LOW LIQUIDITY per whoever supplied this address — same caution as the
# ASTROPORT_POOL_ASTRO_LUNC note earlier: expect noisier/less trustworthy
# edge readings here than the other pools. MAX_POOL_RESERVE_FRACTION
# already protects against oversizing into it, but worth knowing why a
# cycle through this specific leg might look erratic.
TERRAPORT_POOL_USDCAXL_TERRA = os.getenv("TERRAPORT_POOL_USDCAXL_TERRA", "terra1sdwdgj80t295t7vfd89dj0pgm08pequmecpacmy8ur2x48l46xcstep3n6")
TERRASWAP_POOL_USTC_USDCAXL = os.getenv("TERRASWAP_POOL_USTC_USDCAXL", "terra1vh6h792xrerpf9c2965jxvj6zdxkdjssmsfz2n")

# --- CW20 transfer tax (per-token, empirically discovered — NOT something
# the chain or pool exposes). TERRA confirmed 0% via a live smoke test on
# 2026-07-11. LCW confirmed ~5% the same way: a real swap's reported
# return_amount (49,254,235) didn't match the actual wallet balance
# received (46,791,524) — a ~5.00% gap consistent with a transfer tax
# baked into the LCW contract itself. Treat any newly added CW20 the same
# way: assume 0 until proven otherwise with a small real round-trip.
TERRA_TRANSFER_TAX_BPS = _int("TERRA_TRANSFER_TAX_BPS", "0")
LCW_TRANSFER_TAX_BPS = _int("LCW_TRANSFER_TAX_BPS", "500")  # 5.00%, empirical
# Confirmed via smoke_test_astro.py on 2026-07-13: a real 20,000 uusd -> ASTRO
# -> USTC round-trip returned 19,782 uusd vs. the pool's own reported
# return_amount of 19,881 uusd — a 99 uusd shortfall the AMM's own commission
# doesn't account for (that's already netted out of return_amount). 99/19881
# = ~49.8 bps, close enough to a clean 50 bps (0.50%) to treat as the real
# design value rather than rounding noise, the same way LCW's 5% turned out
# to be a deliberate figure. This also explains (in direction and rough
# magnitude — different route/reserves, so not expected to match exactly)
# the ~0.35% simulate_fee mismatch seen on 2026-07-13 in a live 3-leg cycle
# routed through Astroport ASTRO/LUNC -> ASTRO/USTC.
ASTRO_TRANSFER_TAX_BPS = _int("ASTRO_TRANSFER_TAX_BPS", "50")  # 0.50%, empirical
# MIR and TRIT have never been verified empirically the way TERRA/LCW/ASTRO
# were. Treat as unconfirmed until a small real round-trip swap proves it,
# the same way LCW's 5% and ASTRO's 0.50% were discovered (see
# smoke_test_lcw.py / smoke_test_astro.py for the pattern to follow).
MIR_TRANSFER_TAX_BPS = _int("MIR_TRANSFER_TAX_BPS", "0")     # UNVERIFIED — assumed 0
TRIT_TRANSFER_TAX_BPS = _int("TRIT_TRANSFER_TAX_BPS", "0")   # UNVERIFIED — assumed 0
JURIS_TRANSFER_TAX_BPS = _int("JURIS_TRANSFER_TAX_BPS", "0")  # UNVERIFIED — assumed 0
# Confirmed via smoke_test_rev_transfer_tax.py on 2026-07-30: a real 2 LUNC ->
# REV swap through Terraport REV/LUNC reported return_amount=596009 REV via
# the swap event, but the actual wallet balance only increased by 590049 REV —
# a 5960 REV shortfall that comes out to EXACTLY 100.00 bps (1.00%), not just
# close to round the way ASTRO's 49.8bps was. This is what caused the
# 2026-07-30 atomic smoke test to fail with an on-chain "Cannot Sub" overflow
# on the REV->USTC leg: with this tax unconfigured (assumed 0%), leg_amounts
# for that leg was computed too large by exactly this shortfall.
REV_TRANSFER_TAX_BPS = _int("REV_TRANSFER_TAX_BPS", "100")  # 1.00%, empirical
CWLUNC_TRANSFER_TAX_BPS = _int("CWLUNC_TRANSFER_TAX_BPS", "150")  # 1.50%, empirical —
# confirmed 2026-08-05 via smoke_test_new_tokens.py: cwLUNC->LUNC leg (Terraswap
# cwLUNC/LUNC) showed event return_amount=1988016 vs actual balance delta=1958196,
# a 29820 base-unit gap = exactly 150.00 bps. The LUNC->cwLUNC direction showed 0
# bps (rounding noise only), so this tax appears to be one-directional (out of
# cwLUNC) — same asymmetric pattern worth re-confirming on the next re-run before
# fully trusting it live.
CWUSTC_TRANSFER_TAX_BPS = _int("CWUSTC_TRANSFER_TAX_BPS", "0")   # UNVERIFIED — assumed 0
BENANCE_TRANSFER_TAX_BPS = _int("BENANCE_TRANSFER_TAX_BPS", "500")  # legacy flat rate —
# CONFIRMED ASYMMETRIC 2026-08-05 (round 3): this flat constant is now only a
# fallback for callers that don't ask for a direction. The real, direction-
# specific rates are BENANCE_TRANSFER_TAX_IN_BPS / _OUT_BPS below — see those
# for the actual confirmed numbers and how they were measured.
GDEX_TRANSFER_TAX_BPS = _int("GDEX_TRANSFER_TAX_BPS", "0")      # UNVERIFIED — assumed 0
GRDX_TRANSFER_TAX_BPS = _int("GRDX_TRANSFER_TAX_BPS", "0")      # UNVERIFIED — assumed 0
FUN_TRANSFER_TAX_BPS = _int("FUN_TRANSFER_TAX_BPS", "0")       # UNVERIFIED — assumed 0
BON_TRANSFER_TAX_BPS = _int("BON_TRANSFER_TAX_BPS", "0")       # UNVERIFIED — assumed 0
ELPACO_TRANSFER_TAX_BPS = _int("ELPACO_TRANSFER_TAX_BPS", "0")  # CONFIRMED 0bps 2026-09-02 via
# smoke_test_lix_ltk_elpaco_rotti.py — two independent buy legs (LUNC->ELPACO via Garuda,
# LTK->ELPACO via Garuda) both showed exactly 0.00 bps gap. Sell legs (ELPACO->LUNC,
# ELPACO->LTK) showed the usual ~150bps / ~20bps gaps fully explained by native LUNC
# stability tax and LTK's own IN tax respectively (see LTK_TRANSFER_TAX_IN_BPS below) — no
# ELPACO-side tax on either direction.
ROTTI_TRANSFER_TAX_BPS = _int("ROTTI_TRANSFER_TAX_BPS", "0")   # CONFIRMED 0bps 2026-09-02 via
# smoke_test_lix_ltk_elpaco_rotti.py — FIVE independent buy legs into ROTTI (LUNC->ROTTI via
# Garuda, LUNC->ROTTI via Terraport, FUN->ROTTI, JURIS->ROTTI, GRDX->ROTTI) all showed exactly
# 0.00 bps gap. Sell legs (ROTTI->LUNC via both venues) showed the usual ~150bps gap fully
# explained by native LUNC stability tax — no ROTTI-side tax either direction.

# DIRECTIONAL transfer tax rates — ADDED 2026-08-05 (round 3), CORRECTED
# 2026-08-05 (round 5), EXTENDED 2026-09-02 with LIX and LTK. Every
# X->LUNC leg tested so far (cwLUNC, BENANCE, JURIS, GRDX, LIX, LTK,
# ELPACO, ROTTI — 8 unrelated tokens, 4 different venues: Terraswap,
# Terraport, Garuda, LuncSwap) showed the SAME ~150.00 bps gap whenever
# LUNC/USTC was the asset actually returned. A CW20 contract has
# no mechanism to tax a NATIVE coin, so this can't be a coincidentally-
# identical per-token sell tax — it's Terra Classic's own native
# stability tax (tax.get_tax_rate(), currently 1.5% — see tax.py's
# docstring) landing on the LUNC the pool sends back, which graph.py
# ALREADY models generically via the native branch of tax.calculate_tax()
# firing on edge.asset_out whenever it's LUNC, independent of the CW20 on
# the other side. The _OUT_BPS constants below were originally set to 150
# under the wrong assumption this was a distinct CW20-side tax — that
# double-counted the same 1.5% twice per cycle (once here, once via the
# pre-existing native-asset path) and would have made the bot too
# conservative on every cwLUNC/BENANCE sell leg, potentially skipping real
# profitable trades. Reset to 0 — the real ~1.5% cost on these legs is
# still fully priced, just via the correct (native, not CW20) mechanism,
# so JURIS, GRDX, ELPACO, and ROTTI need NO new tax constants for their
# "out" gaps either.
#
# The IN side is unaffected: a CW20 buy leg's swap event already reflects
# whatever the contract actually received (native tax on the LUNC going
# IN would already be baked into that number before the event fires), so
# a gap there can ONLY come from the CW20 contract's own transfer logic —
# BENANCE's confirmed 500bps buy-tax is genuine and stays as-is, and so
# are LIX's 200bps and LTK's ~20bps below.
CWLUNC_TRANSFER_TAX_IN_BPS = _int("CWLUNC_TRANSFER_TAX_IN_BPS", "0")
CWLUNC_TRANSFER_TAX_OUT_BPS = _int("CWLUNC_TRANSFER_TAX_OUT_BPS", "0")

BENANCE_TRANSFER_TAX_IN_BPS = _int("BENANCE_TRANSFER_TAX_IN_BPS", "500")
BENANCE_TRANSFER_TAX_OUT_BPS = _int("BENANCE_TRANSFER_TAX_OUT_BPS", "0")

# CONFIRMED 2026-09-02 via smoke_test_lix_ltk_elpaco_rotti.py. Two
# independent buy legs into LIX (LUNC->LIX via Garuda, and LTK->LIX via
# Garuda) both showed exactly 200.00 bps — a clean, consistent 2.00% buy
# tax baked into the LIX contract itself (confirmed venue-independent:
# same number on two different pools). Sell legs (LIX->LUNC, LIX->LTK)
# showed gaps fully explained by the OTHER side's own tax (native LUNC
# stability tax, and LTK's own IN tax respectively) — no separate
# LIX-side sell tax detected.
LIX_TRANSFER_TAX_IN_BPS = _int("LIX_TRANSFER_TAX_IN_BPS", "200")
LIX_TRANSFER_TAX_OUT_BPS = _int("LIX_TRANSFER_TAX_OUT_BPS", "0")
# Flat legacy fallback for any direction=None caller — mirrors the
# confirmed IN rate (conservative: treating an untaxed sell leg as taxed
# is a missed trade, treating a taxed buy leg as untaxed is a real loss).
LIX_TRANSFER_TAX_BPS = _int("LIX_TRANSFER_TAX_BPS", "200")

# CONFIRMED 2026-09-02 via smoke_test_lix_ltk_elpaco_rotti.py. FOUR
# independent buy legs into LTK (LUNC->LTK via Terraswap: 19.74bps,
# LUNC->LTK via Garuda: 19.71bps, LIX->LTK via Garuda: 18.24bps,
# USTC->LTK via Garuda: 17.61bps) all landed in a tight ~17.6-19.96bps
# band (ELPACO->LTK also showed 19.96bps) — consistent with a genuine
# ~0.20% buy tax on the LTK contract, rounded UP to 20bps here rather
# than averaged down, since underestimating a tax risks the bot sizing a
# trade as profitable when it isn't (overestimating just costs a few
# skipped marginal trades). Re-run smoke_test_lix_ltk_elpaco_rotti.py a
# few more times if you want to narrow this further before trusting it
# at high size. Sell legs (LTK->LUNC, LTK->USTC) showed gaps fully
# explained by native LUNC/USTC stability tax — no separate LTK-side
# sell tax detected.
LTK_TRANSFER_TAX_IN_BPS = _int("LTK_TRANSFER_TAX_IN_BPS", "20")
LTK_TRANSFER_TAX_OUT_BPS = _int("LTK_TRANSFER_TAX_OUT_BPS", "0")
LTK_TRANSFER_TAX_BPS = _int("LTK_TRANSFER_TAX_BPS", "20")  # flat legacy fallback, see LIX above

# Maps a CW20 contract address to a {"in": bps, "out": bps} dict for tokens
# with a CONFIRMED directional split. Any token NOT in this dict falls back
# to the flat cw20_transfer_tax_rate table below (symmetric assumption) —
# that's still correct for tokens where no directional gap has ever been
# observed (GDEX/GRDX/FUN/ELPACO/ROTTI all showed 0bps on every direction
# tested) or that simply haven't been re-tested for asymmetry yet.
CW20_DIRECTIONAL_TAX_BPS = {
    CWLUNC_CW20_ADDRESS: {"in": CWLUNC_TRANSFER_TAX_IN_BPS, "out": CWLUNC_TRANSFER_TAX_OUT_BPS},
    BENANCE_CW20_ADDRESS: {"in": BENANCE_TRANSFER_TAX_IN_BPS, "out": BENANCE_TRANSFER_TAX_OUT_BPS},
    # LIX_CW20_ADDRESS and LTK_CW20_ADDRESS aren't defined until the
    # LIX/LTK/ELPACO/ROTTI block further down this file (added
    # 2026-09-02, after the rest of this section already existed) — their
    # entries are added to this same dict right after that block instead
    # of here, via CW20_DIRECTIONAL_TAX_BPS[...] = {...}. Still governs
    # both tokens the same way; just physically located lower in the file.
}


def cw20_transfer_tax_rate(contract_addr: str, direction: str = None) -> Decimal:
    """
    direction is "in" (token moving into the wallet, e.g. a swap's return
    leg), "out" (token moving out of the wallet to a pool, e.g. a swap's
    offer leg), or None (caller doesn't know/care about direction — uses
    the old flat symmetric rate, kept for backward compatibility with any
    caller that predates the directional split).

    A token with a CONFIRMED directional entry in CW20_DIRECTIONAL_TAX_BPS
    always uses that (ignoring the flat rate below) whenever a direction is
    given — passing direction=None for cwLUNC or BENANCE is almost always a
    mistake now and will silently fall back to each token's flat legacy
    constant instead of its real, confirmed-asymmetric rates.
    """
    if direction in ("in", "out") and contract_addr in CW20_DIRECTIONAL_TAX_BPS:
        bps = CW20_DIRECTIONAL_TAX_BPS[contract_addr][direction]
        return Decimal(bps) / Decimal(10000)

    rates = {
        TERRA_CW20_ADDRESS: TERRA_TRANSFER_TAX_BPS,
        LCW_CW20_ADDRESS: LCW_TRANSFER_TAX_BPS,
        MIR_CW20_ADDRESS: MIR_TRANSFER_TAX_BPS,
        ASTRO_CW20_ADDRESS: ASTRO_TRANSFER_TAX_BPS,
        TRIT_CW20_ADDRESS: TRIT_TRANSFER_TAX_BPS,
        JURIS_CW20_ADDRESS: JURIS_TRANSFER_TAX_BPS,
        REV_CW20_ADDRESS: REV_TRANSFER_TAX_BPS,
        CWLUNC_CW20_ADDRESS: CWLUNC_TRANSFER_TAX_BPS,
        CWUSTC_CW20_ADDRESS: CWUSTC_TRANSFER_TAX_BPS,
        BENANCE_CW20_ADDRESS: BENANCE_TRANSFER_TAX_BPS,
        GDEX_CW20_ADDRESS: GDEX_TRANSFER_TAX_BPS,
        GRDX_CW20_ADDRESS: GRDX_TRANSFER_TAX_BPS,
        FUN_CW20_ADDRESS: FUN_TRANSFER_TAX_BPS,
        BON_CW20_ADDRESS: BON_TRANSFER_TAX_BPS,
        ELPACO_CW20_ADDRESS: ELPACO_TRANSFER_TAX_BPS,
        ROTTI_CW20_ADDRESS: ROTTI_TRANSFER_TAX_BPS,
        LIX_CW20_ADDRESS: LIX_TRANSFER_TAX_BPS,
        LTK_CW20_ADDRESS: LTK_TRANSFER_TAX_BPS,
    }
    bps = rates.get(contract_addr, 0)
    return Decimal(bps) / Decimal(10000)


# --- Risk / sizing ---
# No fixed trade-amount caps. Instead, each opportunity's intrinsic edge
# (profit rate, measured via a small probe trade before slippage bites)
# decides what fraction of your LIVE wallet balance the bot is willing to
# risk: a weak edge gets a small fraction, a strong edge gets up to
# BALANCE_FRACTION_MAX. The actual trade size within that ceiling is still
# found by the profit-maximizing search in amm_math — this fraction is a
# risk ceiling, not a target.
# CHANGED 2026-08-25 at the user's request: was 0.02 (2%). At 2%, a
# weak-but-real edge could size a trade down to a few hundred uluna, where
# a single flat per-hop gas cost (paid regardless of trade size) ate the
# entire edge and then some — the fee-to-trade-size ratio only makes sense
# above a meaningfully larger floor. 0.15 (15%) sits in the middle of the
# requested 10-20% range: even the weakest tradeable edge now risks enough
# of the live balance that one gas payment buys what used to take 2-3
# separate small trades (and 2-3x the gas) to capture the same total size.
BALANCE_FRACTION_MIN = float(os.getenv("BALANCE_FRACTION_MIN", "0.15"))   # weakest tradeable edge
BALANCE_FRACTION_MAX = float(os.getenv("BALANCE_FRACTION_MAX", "0.60"))  # strongest edge
EDGE_LOW_BPS = _int("EDGE_LOW_BPS", "20")    # 0.20% edge -> BALANCE_FRACTION_MIN
EDGE_HIGH_BPS = _int("EDGE_HIGH_BPS", "150")  # 1.50% edge -> BALANCE_FRACTION_MAX
PROBE_FRACTION = float(os.getenv("PROBE_FRACTION", "0.001"))  # 0.1% of balance, for edge measurement
MIN_PROBE_AMOUNT = _int("MIN_PROBE_AMOUNT", "10000")  # floor so tiny balances don't probe with ~0

# ADDED 2026-09-01: probe_amount_for/PROBE_FRACTION above size the probe
# ONLY off wallet balance — fine on a route of deep pools, but a route
# that touches a thin pool (a freshly-listed cw20 with a few hundred
# dollars of reserve) can get a probe that's a large fraction of THAT
# pool's own depth even at 0.1% of a healthy balance. When that happens
# the probe stops measuring the "intrinsic edge... before slippage has a
# chance to distort it" (sizing.py's own module docstring) — it IS the
# slippage, and probe_edge_bps comes back as an artifact of probe size
# (thousands of bps) rather than a real opportunity. sizing.
# probe_amount_for_cycle uses this fraction, walked leg-by-leg the same
# way liquidity_cap_for_cycle does, to shrink the probe to what the
# shallowest pool on the SPECIFIC route can absorb before the probe swap
# is simulated at all.
#
# Deliberately its own separate constant, not a reuse of
# MAX_POOL_RESERVE_FRACTION: that one (with TRADE_SIZE_MULTIPLIER) is
# tuned for how much of a pool a REAL trade may safely consume; a probe
# is supposed to barely move the pool, so this should stay well below
# MAX_POOL_RESERVE_FRACTION's own value. This only changes the probe
# measurement and the edge_bps derived from it — the real trade size is
# still governed by max_offer_for_cycle -> liquidity_cap_for_cycle /
# spread_cap_for_cycle exactly as before.
PROBE_MAX_POOL_RESERVE_FRACTION = float(os.getenv("PROBE_MAX_POOL_RESERVE_FRACTION", "0.01"))

# Never offer more than this fraction of a single pool's relevant reserve
# on ANY leg of a cycle. Unlike a fixed absolute cap (which would need a
# different number per asset and per pool depth), this scales automatically:
# a thin pool gets a small real-uluna ceiling, a deep pool gets a large one,
# from the same one setting. This directly targets the failure mode seen on
# 2026-07-12: a real trade through a flagged low-liquidity pool only
# realized ~24% of predicted profit, almost certainly because the offer was
# large relative to that specific pool's depth even though it was a
# perfectly reasonable fraction of wallet balance.
#
# RAISED 2026-08-02 from 0.05 -> 0.15 at the user's request: small trades in
# thin pools were taking multiple loop iterations to fully capture an
# opportunity, leaving a window for other traders to arb it away before the
# bot finished. This directly REOPENS the 2026-07-12 exposure, just to a
# lesser degree — this ceiling is a safety cap on top of amm_math.
# find_optimal_trade_size's own profit-maximizing search, not a target size
# itself, so the actual chosen offer only grows toward this new, higher
# ceiling when the math says net profit is still climbing there (the
# ternary search already self-limits via slippage) — but the 2026-07-12
# incident proves that theoretical math and realized on-chain results can
# diverge sharply at large-relative-to-reserve sizes, for reasons beyond
# what a static snapshot captures (execution latency, on-chain state
# drift). Watch realized-vs-predicted profit closely after this change,
# especially on thin pools — if that ratio degrades the way it did in the
# original incident, this is the first number to bring back down.
MAX_POOL_RESERVE_FRACTION = float(os.getenv("MAX_POOL_RESERVE_FRACTION", "0.15"))

# ADDED 2026-08-24 at the user's explicit request: multiplies
# MAX_POOL_RESERVE_FRACTION for every pool's per-trade liquidity cap
# (see sizing.liquidity_cap_for_cycle) — aimed at capturing in one or two
# larger trades what was otherwise taking several separate loops' worth
# of smaller ones on the same persisting opportunity, each paying its own
# gas. Only actually changes sizing on pools where MAX_POOL_RESERVE_
# FRACTION was already the binding constraint (thin pools); a deep pool's
# cap rarely binds regardless of this multiplier. This is the same lever
# tied to the 2026-07-12 loss (see MAX_POOL_RESERVE_FRACTION's own
# comment) — 2.0 here means twice that same per-trade pool-depth
# exposure, on every pool. Watch realized-vs-predicted profit after this
# change, same as any MAX_POOL_RESERVE_FRACTION-adjacent one.
TRADE_SIZE_MULTIPLIER = float(os.getenv("TRADE_SIZE_MULTIPLIER", "2.0"))

# ADDED 2026-08-26 at the user's explicit request, and meant to REPLACE
# trips/hops as the way the bot captures more of a persisting opportunity.
# TRADE_SIZE_MULTIPLIER above only widens the POOL-DEPTH ceiling
# (liquidity_cap_for_cycle) — it does nothing for the WALLET-FRACTION
# ceiling computed earlier in sizing.max_offer_for_cycle (edge_to_fraction
# * balance), which is usually the tighter constraint and is what actually
# decides how big a single trip gets proposed at. This multiplies THAT
# ceiling directly: the user's own framing was "I want it to spend 2x more
# — not through many msg or trips or hops, just increase the size to get
# more profit" — so this is applied once, to the wallet-fraction ceiling,
# BEFORE liquidity_cap_for_cycle/spread_cap_for_cycle run. Those two still
# apply afterward exactly as before and remain a hard safety limit — this
# multiplier can propose a bigger single trip, but can never push it past
# what a pool can actually safely absorb. Net effect: more of an
# opportunity gets captured by ONE bigger message instead of several small
# ones (see arbitrage_bot.plan_repeated_cycle_execution's max_trips=1,
# which now leans on this instead of repeated trips for the same goal).
SINGLE_TRIP_SIZE_MULTIPLIER = float(os.getenv("SINGLE_TRIP_SIZE_MULTIPLIER", "2.0"))

# Always keep this much LUNC untouched for gas, regardless of how
# attractive an opportunity looks — the bot will refuse to trade if this
# would be breached.
#
# RAISED 2026-08-31 from 20000000 (~20 LUNC): that flat floor was sized for
# roughly one leg's worth of gas and never accounted for SINGLE_TRIP_SIZE_
# MULTIPLIER / repeated-trip bundling inflating a single broadcast into many
# messages. Real gas for a 4-leg atomic cycle runs ~77-90 LUNC — more than
# 3x the old floor — and multi-trip/leg-split bundles on the biggest
# candidates can run up to ~150 LUNC. This value is ONLY a coarse, cheap
# early-exit check (see the flat check at the top of _execute_winning_cycle
# in arbitrage_bot.py); it is NOT what actually gates a broadcast anymore.
# The real gate is the hard pre-broadcast check in _execute_winning_cycle,
# which re-fetches live LUNC balance and checks the SPECIFIC amount-to-
# broadcast + that broadcast's own real simulate_fee-derived gas, for every
# LUNC-start cycle, right before it's sent. Set above the realistic
# biggest-bundle ceiling (~150 LUNC) rather than tuned tight, since the
# precise check downstream is what actually has to be right.
GAS_RESERVE_ULUNA = _int("GAS_RESERVE_ULUNA", "150000000")  # ~150 LUNC

# --- Tax fail-safe defaults ---
# Used ONLY by tax.py when the live tax_rate/tax_cap fetch has NEVER once
# succeeded (a genuine cold-start failure) — otherwise tax.py keeps the
# last successfully fetched value instead of falling back to these. See
# tax.py's module docstring for the full reasoning: the community passed a
# vote 2026-08-02 raising the stability tax from 0.5% to 1.5% (3x), which
# turned "fail safe by assuming 0% tax" from a cheap mistake into an
# expensive one — a fetch outage could otherwise make the bot think native
# transfers are free when they cost 1.5% per leg. TAX_RATE_FAILSAFE_DEFAULT
# should be updated if governance changes this again — it's a deliberately
# conservative floor, not a substitute for the live fetch, which is always
# preferred when reachable.
TAX_RATE_FAILSAFE_DEFAULT = _dec("TAX_RATE_FAILSAFE_DEFAULT", "0.015")  # 1.5% as of the 2026-08-02 vote
# Effectively UNBOUNDED on purpose, not "a reasonable-sounding cap" — a
# tax_cap that's too LOW is exactly as dangerous as a tax_rate of 0: it
# silently understates cost on any trade above it (min(rate*amount, cap)
# picks the cap, discarding the real uncapped cost). Verified this against
# a 100,000 LUNC trade at 1.5%: a cap of "1000 whole units" (an earlier,
# wrong first draft of this default) understated tax by 500M uluna versus
# the true uncapped cost — the exact failure mode this whole change exists
# to eliminate. Setting this near-infinite means calculate_tax's min(tax,
# cap) always resolves to the full uncapped rate*amount in the fail-safe
# case — the conservative assumption, same direction as the rate default.
TAX_CAP_FAILSAFE_DEFAULT = _int("TAX_CAP_FAILSAFE_DEFAULT", "999999999999999999")

# Denoms where a live-fetched tax_cap of EXACTLY 0 is a CONFIRMED genuine
# exemption, not a fetch/parsing problem — see tax.py's calculate_tax
# docstring: confirmed 2026-07-15 via real round-trip swaps
# (smoke_test_usdcaxl_and_juris.py) that USDC.eth.axl carries 0% shortfall
# vs. a consistent 50bps (later 150bps, post the 2026-08-02 hike) shortfall
# on every LUNC/USTC-output leg in the same run — i.e. USDC.eth.axl's cap=0
# was cross-checked against real chain behavior at the time, not just
# trusted blindly. cap=0 for any OTHER denom is treated as suspicious by
# tax.calculate_tax (logged loudly, cap NOT applied) until it's been
# independently confirmed here the same way — see the 2026-08-08 finding
# below for why blind trust was wrong for DENOM_LUNC/DENOM_USTC specifically.
#
# CONFIRMED WRONG for DENOM_LUNC and DENOM_USTC as of 2026-08-08:
# smoke_test_tax_per_hop.py ran 5 real swaps across 2 separate cycles and
# found tax_cap for BOTH "uluna" and "uusd" was being live-fetched as 0
# (making calculate_tax return 0 for EVERY native-asset leg, in EITHER
# direction) — while the REAL on-chain balance delta on every single
# native-asset RECEIVE leg (5/5) showed an exact, uncapped 1.50% deduction,
# matching tax.get_tax_rate() precisely with no capping effect at all. That
# means the model was silently treating LUNC/USTC transfers as tax-FREE
# while the chain was still charging the full rate — the bot could size and
# approve cycles believing native legs cost nothing when they actually cost
# 1.5% each, understating true cost on almost every cycle (nearly all of
# them touch LUNC or USTC). Root cause (a genuine on-chain cap=0 that the
# chain doesn't actually enforce, vs. a units/response-shape bug in
# tax.get_tax_cap) is not yet confirmed — until it is, DENOM_LUNC/
# DENOM_USTC are deliberately left OUT of this allowlist so calculate_tax
# does not zero their tax out.
TAX_CAP_ZERO_CONFIRMED_DENOMS = {DENOM_USDC_AXL}

# RESTORED 2026-08-27 — dropped in the same stale-re-upload incident as
# SIMULATE_FEE_TIMEOUT_SECONDS above (see that constant's comment). Mirrors
# TAX_CAP_ZERO_CONFIRMED_DENOMS's reasoning but for tax_rate itself: a live
# tax_rate fetch of exactly 0 (HTTP 200, not a failure) is the same
# structurally-broken-treasury-module symptom already confirmed for
# tax_cap on this LCD, not a real 0% rate. False until independently
# cross-checked (smoke_test_tax_per_hop.py) the way USDC.eth.axl's cap=0
# was — until then, tax.get_tax_rate() treats a live 0 as suspicious and
# falls back to TAX_RATE_FAILSAFE_DEFAULT instead of trusting it.
TAX_RATE_ZERO_CONFIRMED = _bool("TAX_RATE_ZERO_CONFIRMED", "false")

MIN_PROFIT_UUSD = _int("MIN_PROFIT_UUSD", "50000")
# Was 1000 uusd (~$0.001) — effectively no real margin. A live trade on
# 2026-07-12 through a 3-leg cycle paid ~67.4M uluna in real gas alone
# (tens of thousands of uusd) and the ACTUAL net profit came in at only
# ~24% of what was predicted (real gas + real slippage on a low-liquidity
# leg ate the rest) — 1000 was nowhere near enough cushion to survive that
# kind of prediction error. This check runs on real_profit_uusd, which is
# already net of AMM commission, both-side tax (native stability tax +
# any CW20 transfer tax), and real chain-simulated gas — so this really is
# "profit after fees and tax", not gross profit. Raise further if you want
# more headroom against slippage/estimate error; the entire safety margin
# against a bad real-world outcome comes from how much bigger this number
# is than your typical real gas cost.

MIN_PROFIT_ULUNA = _int("MIN_PROFIT_ULUNA", "50000000")  # ~50 LUNC, after tax and gas
# Cycles that START in LUNC are judged directly against this uluna floor
# instead of being converted to uusd and compared against MIN_PROFIT_UUSD.
# LUNC's low per-unit uusd price means that conversion silently demanded
# an unrealistic multi-thousand-LUNC profit before a LUNC-rooted cycle
# would ever pass — confirmed 2026-08-01 as the main reason MIR/ASTRO/
# TRIT/JURIS/TERRA opportunities weren't executing, since most real
# cycles route through LUNC somewhere. Every other start asset (USTC,
# TERRA, MIR, etc.) still uses MIN_PROFIT_UUSD as before.

# ADDED 2026-08-25 at the user's explicit request, after real trade
# history (a Terraport REV/USTC pool) showed the bot chaining SIX
# separate ~2-3 USTC buys in one loop, each one its own leg of its own
# trip, for a fee bill north of 600 LUNC on that single bundled tx. The
# MIN_PROFIT_* floors above only ask "is the NEXT increment still
# marginally profitable?" — on a thin/liquidity-capped pool that stays
# technically true down to a couple dollars per trip, so
# plan_repeated_cycle_execution kept greedily bundling more and more tiny
# trips instead of doing one properly-sized trade (or just skipping a
# pool too shallow to be worth trading at all). This is a SEPARATE gate
# from MIN_PROFIT_*: it checks the trade's SIZE, not its profit, and
# applies inside evaluate_cycle to every trip (the initial one AND every
# extra trip plan_repeated_cycle_execution considers), so a trip that
# decays below this floor stops the greedy extension instead of adding
# yet another tiny message. Same two-currency split as MIN_PROFIT_ULUNA/
# MIN_PROFIT_UUSD above and for the same reason (LUNC's low per-unit uusd
# price would otherwise make one uusd-only floor unusable for either
# side). Tune directly to taste — the user's own guidance was "10-30 USTC
# or 2-3k LUNC", so these default to the low end of that range; raise
# either one if trips are still coming in smaller than you want.
# CHANGED 2026-08-26: replaced with a SCALING floor below
# (MIN_TRIP_GAS_MULTIPLE) after arb_20260826.log showed this flat number
# throwing out a real trade — a Terraport REV cycle whose safe,
# spread-capped size came out somewhere under 2,000 LUNC (thin pool, so
# spread_cap_for_cycle rightly shrank it down from a 6.7-BILLION-uluna
# raw liquidity ceiling) despite a genuine, large edge. A flat number
# can't work across pools with wildly different depth — it was either
# too high for a thin pool's honest safe size (this case) or too low to
# matter on a deep one. See MIN_TRIP_GAS_MULTIPLE below.
# ADDED 2026-08-27 — see arbitrage_bot._cycle_in_cooldown's comment for
# the full story. How long a cycle that just failed its REAL check
# (spread ceiling or real gas simulation, not just the cheap flat-guess
# floor) is excluded from being treated as "passing" again. Long enough
# to give other real candidates several loops' worth of a fair shot
# (loops in the traced session ran roughly every 12-16s, so 60s is
# ~4-5 loops); short enough that a genuinely transient failure (a real
# price move, a momentary LCD hiccup) isn't locked out for long. A cycle
# that's structurally broken (e.g. a wrong assumed commission rate) will
# just keep re-failing and re-entering cooldown every time it's retried —
# this doesn't fix that, it only stops it from crowding out everything
# else in the meantime.
CYCLE_FAIL_COOLDOWN_SECONDS = float(os.getenv("CYCLE_FAIL_COOLDOWN_SECONDS", "60"))

MIN_TRIP_GAS_MULTIPLE = _dec("MIN_TRIP_GAS_MULTIPLE", "10")
# A trip must be worth at least this many multiples of ITS OWN gas cost
# (gas_cost_in_start_asset, already computed per-cycle in evaluate_cycle
# — same units as the trip amount, so no LUNC/uusd split is even needed
# this time). This scales exactly where the flat floor couldn't: a
# 2-hop cycle's gas cost is half a 4-hop cycle's, so its minimum trip
# size is automatically half as much too; a cheap-gas LUNC-rooted trip
# gets a smaller floor than an expensive multi-hop one without either
# needing its own constant. Read as "gas must be at most 1/N of the
# trade" — 10 means gas capped at ~10% of trip size. Raise this to be
# more conservative (demand trades further above their own gas cost),
# lower it to allow thinner-but-real opportunities through.

# ADDED 2026-08-26 at the user's explicit request, with their own numbers
# as the defaults — see evaluate_cycle's comment on how these are used.
# Different job from MIN_TRIP_GAS_MULTIPLE above: that one asks "is this
# trade big enough to be worth its own gas" (a FLOOR, scales with gas
# cost). This is a TARGET the bot actively pushes a trip's size UP toward
# — past find_optimal_trade_size's pure profit-maximizing point — real
# trade history (a Terraport REV pool) showed that peak landing at only
# ~5 USTC per trip, repeated across three separate loops instead of one
# ~15 USTC trip. Only takes effect if profit still holds at the bigger
# size (see evaluate_cycle) — never forces a trade that would turn
# unprofitable, and is still hard-bounded by the same liquidity/spread
# safety ceiling every other trip size already respects.
TARGET_TRIP_AMOUNT_ULUNA = _int("TARGET_TRIP_AMOUNT_ULUNA", "1500000000")  # 1,500 LUNC
TARGET_TRIP_UUSD_EQUIV = _int("TARGET_TRIP_UUSD_EQUIV", "12000000")  # ~12 USTC-equivalent

MIN_PROFIT_MARGIN_MULTIPLE = _dec("MIN_PROFIT_MARGIN_MULTIPLE", "0.15")
# LOWERED 2026-08-26 from 1.0 to 0.15 after arb_20260826.log showed this
# was the actual cause of nearly every missed trade that session — 216
# real-check FAILs against just 2 PASSes across the whole run, and every
# single repeating candidate (TRIT/LUNC, both MIR routes, REV via Pool 2)
# cleared MIN_PROFIT_ULUNA/MIN_PROFIT_UUSD comfortably (the TRIT/LUNC
# cycle netted 69.7 LUNC against a 50 LUNC floor) and STILL failed here,
# every loop, because this check alone demanded net profit be worth at
# least 100% of the trade's total real cost (commission+tax+gas) — i.e.
# revenue had to be double the cost, not just genuinely profitable. The
# four real candidates measured in that log actually achieved
# profit/cost ratios of 0.18-0.60; 0.15 sits just under the tightest of
# those so they'd have passed, while still requiring a real, non-trivial
# margin over cost (not "breakeven" — see the note below on what the
# previous value's own comment got wrong about that word).
#
# The comment on the prior value (1.0, kept below for history) described
# it as "BREAKEVEN" — that characterization doesn't hold up: at
# multiple=1.0, required_margin_uusd = 1.0 * total_cost_uusd, and the
# check demands real_profit_uusd (already net of commission, tax, AND
# gas) be >= that — i.e. profit itself must equal the full cost, meaning
# total revenue was actually required to be 2x cost, not break even on
# it. That's likely why it was rejecting genuinely profitable trades
# outright rather than just filtering thin ones. Worth watching realized
# vs predicted profit closely after this change, the same way the
# 2026-08-22 change asked to — 0.15 still asks for a real cushion, but a
# meaningfully smaller one than before.
#
# LOWERED 2026-08-22 from 1.3 to 1.0 at the user's explicit request, after
# being told directly what 1.0 means: real_net_in_start_asset only has to
# equal real_fees_and_tax_uusd + real_gas_uusd — this is BREAKEVEN on the
# bot's own cost estimate, not a profit requirement. There is no longer
# any cushion in this check against the bot's own estimate being wrong.
#
# WORTH KNOWING: this is the exact value this module's own comment used
# to call out as "a floor, not a sensible value" (see the 2.0->1.3 entry
# below, kept for history). It was set that way because of the
# 2026-07-12 incident, where a real trade's realized profit came in at
# ~24% of what this bot's own math predicted beforehand — at 1.0x, that
# same kind of estimate error no longer costs you a smaller-than-hoped
# profit, it costs you money outright, on every single trade where it
# recurs, since "breakeven" realized at 24% is a real loss of principal
# net of gas and tax, not a missed opportunity. The flat floor
# (MIN_PROFIT_UUSD/MIN_PROFIT_ULUNA) and spread-ceiling/real-gas checks
# elsewhere in this pipeline still apply and still catch some bad cycles
# outright — this multiple is no longer doing any of the cushioning work
# on top of those. Watch real_profit_uusd vs actually-realized profit
# closely after this change, more than any prior threshold move in this
# file's history.
#
# 2.0 -> 1.3 (still true, kept for history): 1.3 means net profit must
# exceed real cost by 30%. 2.0 was calibrated directly after 2026-07-12;
# 1.3 was already a materially thinner cushion against that same failure
# mode, before this file was lowered further to 1.0.
# ADDED 2026-08-03: a flat MIN_PROFIT_UUSD/MIN_PROFIT_ULUNA floor alone lets
# a cycle through the moment it clears zero-plus-a-little, even when the
# real cost it just paid was a large fraction of gross profit — no cushion
# left for the next bit of slippage/estimate error. This is a SEPARATE,
# multiplicative requirement checked alongside the flat floor (see
# arbitrage_bot._prepare_execution): real_net_in_start_asset must be at
# least MIN_PROFIT_MARGIN_MULTIPLE times the REAL total cost paid on that
# specific cycle — AMM commission AND tax on BOTH sides of every leg
# (graph_module.simulate_cycle_costs_uusd, fees are charged on every leg,
# not just once), plus real chain-simulated gas — not just above the flat
# floor. Example: total real cost=450 with this at 2.0 requires net profit
# >= 900, i.e. total proceeds well above break-even rather than "barely
# positive." CORRECTED 2026-08-04: an earlier version of this check only
# summed outgoing-side native tax and missed commission plus incoming-side
# CW20 tax — see graph.simulate_cycle_costs_uusd's docstring. Whichever of
# the flat floor or this margin requirement is stricter for a given cycle
# wins — this does not relax MIN_PROFIT_UUSD/MIN_PROFIT_ULUNA, it adds on
# top of them. Tune higher if real trades keep coming in near the edge of
# profitable; 1.0 would mean "profit must merely exceed costs," which is
# closer to the no-cushion behavior this was added to fix, so treat 1.0 as
# a floor on this setting, not a sensible operating value.

SLIPPAGE_BUFFER_BPS = _int("SLIPPAGE_BUFFER_BPS", "50")

# --- Slippage protection (belief_price / max_spread) ---
# Previously every leg used a flat max_spread=0.02 (2%) with no belief_price
# at all — 4x looser than SLIPPAGE_BUFFER_BPS (0.5%) assumed in simulation,
# and no reference price, so the contract had nothing to check the fill
# against. Fix: derive belief_price from the same snapshot reserves used
# for sizing, and set max_spread to (this leg's own simulated price impact,
# already priced into the approved profit) + SPREAD_TOLERANCE_BPS of real
# extra room for drift between the snapshot and the leg actually landing.
SPREAD_TOLERANCE_BPS = _int("SPREAD_TOLERANCE_BPS", "50")       # 0.5% room for genuine drift, beyond our own trade's expected impact
MAX_SPREAD_CEILING_BPS = _int("MAX_SPREAD_CEILING_BPS", "1000")  # 10% hard sanity cap — exceed this and the cycle is aborted, not traded blind

# Diagnostic-only — NEVER used for real sizing or execution, only to log
# (at INFO, so it survives LOG_LEVEL=INFO) what a cycle that MAX_SPREAD_
# CEILING_BPS just rejected would have sized to and profited at a looser
# ceiling. arb_20260830.log and arb_20260831.log both showed ~42% of all
# ceiling refusals landing within 1000-1050 bps of the 1000.0 cutoff —
# this exists to find out, from real pool state, whether that cluster is
# genuinely profitable or just noise, before ever touching the real
# ceiling. Raise/lower freely; it has zero effect on trading.
SHADOW_SPREAD_CEILING_BPS = _int("SHADOW_SPREAD_CEILING_BPS", "1500")

# --- Gas ---
GAS_DENOM = os.getenv("GAS_DENOM", "uluna")
GAS_PRICE = _dec("GAS_PRICE", "28.325")
GAS_ADJUSTMENT = float(os.getenv("GAS_ADJUSTMENT", "1.6"))

# Empty by default on purpose — "arb"/"arb-atomic" (the old hardcoded
# memos) made every transaction from this wallet trivially greppable on
# any block explorer or mempool watcher as "this is an arbitrage bot,
# here's exactly which txs to copy or front-run." An empty memo blends in
# with the overwhelming majority of ordinary transactions instead of
# advertising the wallet's activity. Configurable rather than fixed to
# something else, in case a specific value is ever wanted (e.g. your own
# bookkeeping) without another code change.
TX_MEMO = os.getenv("TX_MEMO", "")

# --- Loop ---
# --- Loop ---
# LOWERED 2026-08-27 from 8 to 2 at the user's explicit request for more
# trade throughput. This sleep runs after EVERY loop iteration regardless
# of whether anything happened that loop — measured from arb_20260827.log
# that the median loop-to-loop gap was ~16.4s while the actual scan/size/
# real-check work only took ~8s, meaning roughly half of every loop was
# pure idle time contributed by this one constant. Terra Classic blocks
# land roughly every ~6s (see terra_client.get_latest_block_height's
# comment), so 2s still avoids spinning many times between blocks with
# nothing new to see, while cutting out the bulk of the dead time. This
# doesn't touch sizing, margin checks, or spread caps in any way — it's
# purely how often the bot looks, not what it's willing to do once it
# looks. If LCD rate limits or CPU load become a problem at this
# cadence, raise it back up; nothing else needs to change to do so.
POLL_INTERVAL_SECONDS = _int("POLL_INTERVAL_SECONDS", "2")

# How many loop iterations between times a pool marked with a non-default
# DexPool(scan_interval=...) gets fed into cycle-scanning. Currently used
# for Terraswap cwLUNC/LUNC (see arbitrage_bot.py's pool list) — genuinely
# thin/low-liquidity, so it's scanned periodically rather than every loop.
# REV/LUNC and REV/USTC previously used this too but were changed
# (2026-08-07, at the operator's request) to scan every loop like every
# other pool — no more special-cased treatment for REV. This setting does
# NOT affect reserve fetching, commission refresh, or a pool's
# tradeability — only how often it's included in the cycle search.
PERIODIC_POOL_SCAN_INTERVAL = _int("PERIODIC_POOL_SCAN_INTERVAL", "5")

# --- Smoke test (see smoke_test.py) ---
SMOKE_TEST_AMOUNT_ULUNA = _int("SMOKE_TEST_AMOUNT_ULUNA", "2000000")  # 2 LUNC
SMOKE_TEST_COUNT = _int("SMOKE_TEST_COUNT", "3")
SMOKE_TEST_INTERVAL_SECONDS = _int("SMOKE_TEST_INTERVAL_SECONDS", "15")

# --- Safety ---
DRY_RUN = _bool("DRY_RUN", "true")

# --- Manual forced test trade (keyword trigger) ---
# Typing this word + Enter into the bot's stdin while it's running arms a
# ONE-SHOT flag: on the NEXT loop, the best cycle found so far (regardless
# of whether it clears MIN_PROFIT_UUSD) gets executed for real at a small,
# probe-sized offer (sizing.probe_amount_for — the same tiny amount
# already used just to measure a cycle's intrinsic edge), instead of the
# usual full profit-based sizing. Every OTHER safety check still applies
# unchanged: spread ceiling, real chain gas simulation, the LUNC gas
# reserve floor, and DRY_RUN. This exists to answer "is the bot actually
# capable of executing live/atomic right now" without waiting for a real
# profitable opportunity to show up on its own.
FORCE_TRADE_KEYWORD = os.getenv("FORCE_TRADE_KEYWORD", "FORCE")

# Hard ceiling (in uusd-equivalent terms) on a forced test trade's
# starting value, regardless of which asset/cycle it ends up using or how
# large your live balance is. A probe-sized offer is already small, but
# this is a second, independent cap so a forced trade can never
# accidentally be large just because the wallet balance for that
# particular asset happens to be big.
FORCE_TRADE_MAX_UUSD_EQUIV = _int("FORCE_TRADE_MAX_UUSD_EQUIV", "500000")  # ~$0.50

# --- Execution mode ---
# ATOMIC=true bundles all legs of a cycle into ONE transaction (true
# all-or-nothing execution, no window between legs for the pool to move).
# ATOMIC=false uses the original sequential per-leg broadcasts with
# balance-delta re-measurement between legs. Default false until atomic
# mode has been observed working correctly in DRY_RUN across a range of
# cycle shapes (2, 3, 4 hops) and asset kinds (native start vs CW20 start).
ATOMIC_EXECUTION = _bool("ATOMIC_EXECUTION", "false")

# --- Candidate fallback ---
# Each loop used to try only the single #1 candidate by flat-guess
# profit_uusd, and give up entirely for that iteration if it failed the
# real spread/gas checks — even when a smaller-but-real edge further down
# the ranked list (e.g. through a lower-fee-percentage route) would have
# cleared every check. Now the loop tries candidates in descending
# profit_uusd order until one survives. This caps how many of them get a
# real simulate_fee() call (each one is a network round trip, and this
# whole fallback exists to make the loop find MORE real trades, not to
# make it slower) — raise if you want deeper fallback and can tolerate a
# few more seconds per loop when the top candidates keep failing.
#
# RAISED 2026-08-22 from 5 to 20: confirmed via arb_20260822.log that this
# was routinely exhausted — every sampled loop logged "Tried 5 candidate(s)
# ... of 25 sizeable and passing their own floor" with all 5 failing the
# real margin check, meaning the other 20 candidates (which may well have
# included cycles through the deep, high-volume LUNC/USTC pools) never got
# a real check at all that loop. The candidates burning the first 5 slots
# were consistently REV-anchored cycles — REV/LUNC and REV/USTC are
# genuinely thin pools whose probe-based edge_bps looks artificially large
# at the flat-guess ranking stage but collapses once real gas/commission/
# tax are priced in. Raising this doesn't loosen any profitability check —
# it just gives the loop more chances to reach past the noisy, low-
# liquidity candidates sitting at the top of the flat-guess ranking to a
# real opportunity further down. Each extra try costs one simulate_fee()
# network round trip; with the tax.py logging-flood fix (see tax.py's
# _FAIL_LOG_THROTTLE) also landed, this shouldn't meaningfully change loop
# time. Consider also excluding/deprioritizing known-thin pools (REV) from
# the flat-guess ranking itself as a further, complementary fix.
MAX_CANDIDATES_PER_LOOP = _int("MAX_CANDIDATES_PER_LOOP", "20")

# --- Leg splitting ---
# When on, a leg whose pool is thin enough that splitting its
# already-approved total offer into several sequential same-direction
# trades (bundled into the SAME atomic transaction) nets more real
# output than one trade of that total will do so — see
# graph.simulate_leg_split's docstring for why this is a real, on-chain
# effect (the Terraswap/Astroport-family formula deducts the spread
# term from output in addition to commission, and that term grows faster
# than linearly with offer size, so several smaller passes keep more of
# it). This does NOT change how much total value is allowed through a
# pool — MAX_POOL_RESERVE_FRACTION still caps that upstream, exactly as
# before; splitting only changes how the already-capped total is
# executed. A deep pool's optimal split naturally converges to 1 trip
# (no benefit, so no extra gas spent) — this never forces splitting.
# Only applies when ATOMIC_EXECUTION is also on (splitting relies on
# bundling multiple messages into one broadcast).
ENABLE_LEG_SPLITTING = _bool("ENABLE_LEG_SPLITTING", "true")

# ADDED 2026-08-25, built from real evidence (arb_20260825.log): the same
# cycle executed 4 separate times across ~85 real seconds, each paying
# its own gas — 19.2% of the total profit captured across those 4 trades
# went to redundant gas alone, on top of the multi-second window between
# separate loop iterations where a competing trader could take the rest.
# When on, a winning cycle first tries a greedy multi-TRIP extension
# (arbitrage_bot.plan_repeated_cycle_execution) — repeated full passes
# through the SAME cycle, each re-sized against a local, progressively-
# updated copy of reserves, bundled into ONE atomic transaction — before
# falling back to per-LEG splitting (ENABLE_LEG_SPLITTING). Purely local
# math until the final bundle is built, so this doesn't add meaningfully
# to loop time; gets its own real simulate_fee + full margin re-check
# before use, same safety pattern as the leg-splitting feature, and falls
# back to the plain single-trip plan if that re-check fails. Only applies
# when ATOMIC_EXECUTION is also on.
ENABLE_REPEATED_CYCLE_EXECUTION = _bool("ENABLE_REPEATED_CYCLE_EXECUTION", "true")


# --- LIX, LTK, ELPACO, ROTTI — ADDED 2026-09-02, all four brand-new CW20s
# supplied by the user in one batch along with 14 pool addresses across
# LuncSwap.fun, Garuda DeFi, Terraswap, and Terraport. Decimals assumed 6
# (the standard for every CW20 on this chain so far — none has ever been
# anything else) but NOT independently confirmed on-chain the way a few
# early tokens were (e.g. TERRA_DECIMALS).
#
# UPDATE 2026-09-02: real transfer-tax smoke test run via
# smoke_test_lix_ltk_elpaco_rotti.py — see LIX_TRANSFER_TAX_IN_BPS,
# LTK_TRANSFER_TAX_IN_BPS, ELPACO_TRANSFER_TAX_BPS, and
# ROTTI_TRANSFER_TAX_BPS above for the confirmed numbers and how they
# were measured. LIX (200bps in) and LTK (~20bps in) are now in
# CW20_DIRECTIONAL_TAX_BPS; ELPACO and ROTTI confirmed 0bps and are in
# the flat rates dict. 13 of the 14 pools got at least one real,
# confirmed-clean transaction in that run:
#   - LuncSwap LUNC/LIX: initially untested (2026-09-02 run hit a
#     transient DNS resolution error before broadcasting anything).
#     CONFIRMED CLEAN 2026-09-03 via smoke_test_luncswap_lunc_lix.py — real
#     round trip completed: LUNC->LIX showed exactly 200.00 bps (THIRD
#     independent confirmation of LIX's 200bps buy tax, now across all 3
#     venues it trades on: Garuda LUNC/LIX, Garuda LTK/LIX, and LuncSwap
#     LUNC/LIX all agree exactly), LIX->LUNC showed the usual ~150bps
#     fully explained by native stability tax. This pool's venue
#     mechanics are now confirmed, not just LIX's own tax.
#   - Garuda USDC/LTK failed with a real on-chain error ("Invalid fee
#     amount" at gas estimation, status 500) — not a tax finding, some
#     other unresolved issue with this specific pool/pair. PARKED (see
#     arbitrage_bot.py) until that's understood; USTC/LTK confirmed clean
#     as an alternative route to price LTK against a stablecoin.
# The other 12 pools all completed a full real round trip cleanly.
LIX_DECIMALS = _int("LIX_DECIMALS", "6")
LIX_CW20_ADDRESS = os.getenv("LIX_CW20_ADDRESS", "terra16hl9mwp67l8xjy8hlhasmzldlmdpw963gmpj6e2em0g7xy4el2ys6kwtlt")


LTK_DECIMALS = _int("LTK_DECIMALS", "6")
LTK_CW20_ADDRESS = os.getenv("LTK_CW20_ADDRESS", "terra1mm8tdp40r2slzwqxk8jsz66ayc4zp69muxeateq37x2xquttzsaqy7275a")

# Deferred from the CW20_DIRECTIONAL_TAX_BPS dict literal near the top of
# this file — LIX_CW20_ADDRESS/LTK_CW20_ADDRESS weren't defined yet there.
# See LIX_TRANSFER_TAX_IN_BPS / LTK_TRANSFER_TAX_IN_BPS above that dict
# for how these numbers were confirmed (2026-09-02).
CW20_DIRECTIONAL_TAX_BPS[LIX_CW20_ADDRESS] = {"in": LIX_TRANSFER_TAX_IN_BPS, "out": LIX_TRANSFER_TAX_OUT_BPS}
CW20_DIRECTIONAL_TAX_BPS[LTK_CW20_ADDRESS] = {"in": LTK_TRANSFER_TAX_IN_BPS, "out": LTK_TRANSFER_TAX_OUT_BPS}

ELPACO_DECIMALS = _int("ELPACO_DECIMALS", "6")
ELPACO_CW20_ADDRESS = os.getenv("ELPACO_CW20_ADDRESS", "terra1ljyvgw50u67r3ep7pp7qexgnsgy96fl57q0suut325ehed7eal8qwdtdq4")

ROTTI_DECIMALS = _int("ROTTI_DECIMALS", "6")
ROTTI_CW20_ADDRESS = os.getenv("ROTTI_CW20_ADDRESS", "terra12j3xuxx52cg045qk37ee4k4u4fsgvyuf8d89dh7c9mr706jvxdascahqej")

# LuncSwap.fun is already a trusted venue (see LUNCSWAP_POOL_JURIS_USDC /
# LUNCSWAP_POOL_TERRA_USDC above) — Terraswap-family shape, uses
# LUNCSWAP_COMMISSION_RATE.
LUNCSWAP_POOL_LUNC_LIX = os.getenv("LUNCSWAP_POOL_LUNC_LIX", "terra1g6627t8f04yr97hjdg65pganrntag2633ll630gqlc8d0svyz5qs8y4rew")

# Garuda DeFi is already a trusted venue (pair_base contracts, see the
# GARUDA_POOL_* block above) — uses GARUDA_COMMISSION_RATE, no live
# commission resolution.
GARUDA_POOL_LUNC_LIX = os.getenv("GARUDA_POOL_LUNC_LIX", "terra1q4fndxdyz7tkuqkc2u2yy8urfp5c395d5fknxu4pgzwte3zef7cqn3yvdt")
GARUDA_POOL_LTK_LIX = os.getenv("GARUDA_POOL_LTK_LIX", "terra1cynajzu7c5ulqddakpc2tlsfakhqutx9362f3m407pvw8dzfaqxq7sgmkz")
GARUDA_POOL_LUNC_LTK = os.getenv("GARUDA_POOL_LUNC_LTK", "terra1a7vjhp0nf3nnspu6m92asr64zmtjyagwx7anv76yephzuncs980s4gvfsm")
GARUDA_POOL_USDC_LTK = os.getenv("GARUDA_POOL_USDC_LTK", "terra19pqtmnemesg7asvngpky0rxs25k4jdl9ayr9w5tzqmkd9kmndveqsdr95s")
GARUDA_POOL_USTC_LTK = os.getenv("GARUDA_POOL_USTC_LTK", "terra1ml2ktnvj4m5dev09339rlx2y2pvw8cx2dfym2tw0n3ucps2jnfaquun0w0")
GARUDA_POOL_LTK_ELPACO = os.getenv("GARUDA_POOL_LTK_ELPACO", "terra1fl0325wjhkecddfhzr0g9dutvvrcuhsz73f5d8ukusld6p60hxvsv0rq8t")
GARUDA_POOL_LUNC_ELPACO = os.getenv("GARUDA_POOL_LUNC_ELPACO", "terra1mml68fp36vcap75d4dat9fglsh9ars4r88fy98lgk37zsqlt5skqa2rkkh")
GARUDA_POOL_ROTTI_LUNC = os.getenv("GARUDA_POOL_ROTTI_LUNC", "terra1yp777w3wqdq85k734krhlyx2mhexgnvxe082few942t8w7ad08hqmqxymr")
GARUDA_POOL_FUN_ROTTI = os.getenv("GARUDA_POOL_FUN_ROTTI", "terra172dat5l5zz26j0p4c8uaqprxpmptutt77f43pzs4uel5azdwfnasnz8m05")
GARUDA_POOL_ROTTI_JURIS = os.getenv("GARUDA_POOL_ROTTI_JURIS", "terra1w277a4dq4rhemm4z0nrqd4fvh8reycrunhwdj24ttd7lekuuf0cqyvfmmn")
GARUDA_POOL_ROTTI_GRDX = os.getenv("GARUDA_POOL_ROTTI_GRDX", "terra1kle4fxnmy84vkz0wg25qs9j4t9jegsdf9vpvgyde32c0z2uwwkyq0vy57q")

# Terraswap and Terraport are already-trusted venues.
TERRASWAP_POOL_LUNC_LTK = os.getenv("TERRASWAP_POOL_LUNC_LTK", "terra1h4punhthc5dg3gxzmxlu3ejjjl2p335r203qh2gm4gt6qrjnkndshug7xc")
TERRAPORT_POOL_ROTTI_LUNC = os.getenv("TERRAPORT_POOL_ROTTI_LUNC", "terra1u5tl9hf3q5uhtch5xan5rkf0w9awzn0klwffsphupykafm52n6jqr3ns8y")


def validate():
    missing = []
    if not MNEMONIC:
        missing.append("MNEMONIC")
    if not TERRASWAP_POOL_1:
        missing.append("TERRASWAP_POOL_1")
    if not TERRASWAP_POOL_2:
        missing.append("TERRASWAP_POOL_2")
    if TERRASWAP_POOL_1 and TERRASWAP_POOL_1 == TERRASWAP_POOL_2:
        missing.append("TERRASWAP_POOL_1 and TERRASWAP_POOL_2 must be different pools")
    if not TERRA_CW20_ADDRESS:
        missing.append("TERRA_CW20_ADDRESS")
    if not TERRAPORT_POOL_TERRA_LUNC:
        missing.append("TERRAPORT_POOL_TERRA_LUNC")
    if not TERRAPORT_POOL_TERRA_USTC:
        missing.append("TERRAPORT_POOL_TERRA_USTC")
    if not LCW_CW20_ADDRESS:
        missing.append("LCW_CW20_ADDRESS")
    if not TERRAPORT_POOL_LCW_LUNC:
        missing.append("TERRAPORT_POOL_LCW_LUNC")
    if not TERRAPORT_POOL_LCW_USTC:
        missing.append("TERRAPORT_POOL_LCW_USTC")
    if not MIR_CW20_ADDRESS:
        missing.append("MIR_CW20_ADDRESS")
    if not TERRASWAP_POOL_MIR_USTC:
        missing.append("TERRASWAP_POOL_MIR_USTC")
    if not ASTROPORT_POOL_MIR_USTC:
        missing.append("ASTROPORT_POOL_MIR_USTC")
    if not ASTRO_CW20_ADDRESS:
        missing.append("ASTRO_CW20_ADDRESS")
    if not ASTROPORT_POOL_ASTRO_LUNC:
        missing.append("ASTROPORT_POOL_ASTRO_LUNC")
    if not ASTROPORT_POOL_ASTRO_USTC:
        missing.append("ASTROPORT_POOL_ASTRO_USTC")
    if not TRIT_CW20_ADDRESS:
        missing.append("TRIT_CW20_ADDRESS")
    if not TERRASWAP_POOL_TRIT_LUNC:
        missing.append("TERRASWAP_POOL_TRIT_LUNC")
    if not TERRAPORT_POOL_TRIT_LUNC:
        missing.append("TERRAPORT_POOL_TRIT_LUNC")
    if not TERRASWAP_POOL_TRIT_USTC:
        missing.append("TERRASWAP_POOL_TRIT_USTC")
    if not TERRAPORT_POOL_TRIT_USTC:
        missing.append("TERRAPORT_POOL_TRIT_USTC")
    if not JURIS_CW20_ADDRESS:
        missing.append("JURIS_CW20_ADDRESS")
    if not TERRAPORT_POOL_JURIS_LUNC:
        missing.append("TERRAPORT_POOL_JURIS_LUNC")
    if not GARUDA_POOL_JURIS_LUNC:
        missing.append("GARUDA_POOL_JURIS_LUNC")
    if not TERRAPORT_POOL_JURIS_TERRA:
        missing.append("TERRAPORT_POOL_JURIS_TERRA")
    if not REV_CW20_ADDRESS:
        missing.append("REV_CW20_ADDRESS")
    if not TERRAPORT_POOL_REV_LUNC:
        missing.append("TERRAPORT_POOL_REV_LUNC")
    if not TERRAPORT_POOL_REV_USTC:
        missing.append("TERRAPORT_POOL_REV_USTC")
    if not BON_CW20_ADDRESS:
        missing.append("BON_CW20_ADDRESS")
    if not MOON_CW20_ADDRESS:
        missing.append("MOON_CW20_ADDRESS")
    if not TERRAPORT_POOL_MOON_LUNC:
        missing.append("TERRAPORT_POOL_MOON_LUNC")
    if not GARUDA_POOL_MOON_TERRA:
        missing.append("GARUDA_POOL_MOON_TERRA")
    if not JEFF_CW20_ADDRESS:
        missing.append("JEFF_CW20_ADDRESS")
    if not DFC_CW20_ADDRESS:
        missing.append("DFC_CW20_ADDRESS")
    if not GARUDA_POOL_JURIS_GRDX:
        missing.append("GARUDA_POOL_JURIS_GRDX")
    if not JEFF_POOL_LUNC_UNKNOWN:
        missing.append("JEFF_POOL_LUNC_UNKNOWN")
    if not JEFF_POOL_USDC_UNKNOWN:
        missing.append("JEFF_POOL_USDC_UNKNOWN")
    if not TERRAPORT_POOL_JEFF_LUNC:
        missing.append("TERRAPORT_POOL_JEFF_LUNC")
    if not TERRAPORT_POOL_JEFF_USTC:
        missing.append("TERRAPORT_POOL_JEFF_USTC")
    if not GARUDA_POOL_LUNC_DFC:
        missing.append("GARUDA_POOL_LUNC_DFC")
    if not TERRASWAP_POOL_LUNC_DFC:
        missing.append("TERRASWAP_POOL_LUNC_DFC")
    # LUNC_DFC_POOL_UNKNOWN intentionally NOT validated as required — its
    # pool isn't attached to the live list (see arbitrage_bot.py comment),
    # so there's nothing that depends on it being set.
    if not GARUDA_POOL_USDC_LUNC:
        missing.append("GARUDA_POOL_USDC_LUNC")
    if not LUNCSWAP_POOL_TERRA_USDC:
        missing.append("LUNCSWAP_POOL_TERRA_USDC")
    # TERRA_USDC_POOL_UNKNOWN intentionally NOT validated as required —
    # venue unidentified and not attached to the live list; see comment
    # above GARUDA_POOL_USDC_LUNC.
    if not TERRAPORT_POOL_BON_LUNC:
        missing.append("TERRAPORT_POOL_BON_LUNC")
    if not TERRAPORT_POOL_BON_USTC:
        missing.append("TERRAPORT_POOL_BON_USTC")
    if not TERRAPORT_POOL_USDCAXL_LUNC:
        missing.append("TERRAPORT_POOL_USDCAXL_LUNC")
    if not TERRAPORT_POOL_USDCAXL_USTC:
        missing.append("TERRAPORT_POOL_USDCAXL_USTC")
    if not TERRAPORT_POOL_USDCAXL_TERRA:
        missing.append("TERRAPORT_POOL_USDCAXL_TERRA")
    if not TERRASWAP_POOL_USTC_USDCAXL:
        missing.append("TERRASWAP_POOL_USTC_USDCAXL")
    if not FUTURE_CW20_ADDRESS:
        missing.append("FUTURE_CW20_ADDRESS")
    if not FUTUREFLARE_POOL_FUTURE_LUNC:
        missing.append("FUTUREFLARE_POOL_FUTURE_LUNC")
    if not FUTUREFLARE_POOL_FUTURE_TERRA:
        missing.append("FUTUREFLARE_POOL_FUTURE_TERRA")
    if not FUTUREFLARE_POOL_FUTURE_TRIT:
        missing.append("FUTUREFLARE_POOL_FUTURE_TRIT")
    if not AMPLUNC_CW20_ADDRESS:
        missing.append("AMPLUNC_CW20_ADDRESS")
    if not WHITEWHALE_POOL_LUNC_USTC:
        missing.append("WHITEWHALE_POOL_LUNC_USTC")
    if not AMPLUNC_ASTROPORT_POOL_LUNC:
        missing.append("AMPLUNC_ASTROPORT_POOL_LUNC")
    if not AMPLUNC_ASTROPORT_POOL_USTC:
        missing.append("AMPLUNC_ASTROPORT_POOL_USTC")
    if not ASTROPORT_POOL_LUNC_USTC:
        missing.append("ASTROPORT_POOL_LUNC_USTC")
    if not AMPLUNC_TERRASWAP_POOL_USTC_1:
        missing.append("AMPLUNC_TERRASWAP_POOL_USTC_1")
    if not AMPLUNC_TERRASWAP_POOL_USTC_2:
        missing.append("AMPLUNC_TERRASWAP_POOL_USTC_2")
    if not AMPLUNC_TERRASWAP_POOL_LUNC:
        missing.append("AMPLUNC_TERRASWAP_POOL_LUNC")
    if not CWLUNC_CW20_ADDRESS:
        missing.append("CWLUNC_CW20_ADDRESS")
    if not CWUSTC_CW20_ADDRESS:
        missing.append("CWUSTC_CW20_ADDRESS")
    if not TERRASWAP_POOL_CWLUNC_LUNC:
        missing.append("TERRASWAP_POOL_CWLUNC_LUNC")
    if not WESO_ROUTER_ADDRESS:
        missing.append("WESO_ROUTER_ADDRESS")
    if not WESO_POOL_CWLUNC_CWUSTC:
        missing.append("WESO_POOL_CWLUNC_CWUSTC")
    if not WESO_POOL_JURIS_CWLUNC:
        missing.append("WESO_POOL_JURIS_CWLUNC")
    if not TERRASWAP_POOL_USDC_LUNC:
        missing.append("TERRASWAP_POOL_USDC_LUNC")
    if not TERRAPORT_POOL_LUNC_USDC:
        missing.append("TERRAPORT_POOL_LUNC_USDC")
    if not BENANCE_CW20_ADDRESS:
        missing.append("BENANCE_CW20_ADDRESS")
    if not GDEX_CW20_ADDRESS:
        missing.append("GDEX_CW20_ADDRESS")
    if not GRDX_CW20_ADDRESS:
        missing.append("GRDX_CW20_ADDRESS")
    if not FUN_CW20_ADDRESS:
        missing.append("FUN_CW20_ADDRESS")
    if not GARUDA_POOL_BENANCE_LUNC:
        missing.append("GARUDA_POOL_BENANCE_LUNC")
    if not GARUDA_POOL_BENANCE_JURIS:
        missing.append("GARUDA_POOL_BENANCE_JURIS")
    if not GARUDA_POOL_GDEX_LUNC:
        missing.append("GARUDA_POOL_GDEX_LUNC")
    if not GARUDA_POOL_GDEX_GRDX:
        missing.append("GARUDA_POOL_GDEX_GRDX")
    if not GARUDA_POOL_FUN_GDEX:
        missing.append("GARUDA_POOL_FUN_GDEX")
    if not GARUDA_POOL_FUN_LUNC:
        missing.append("GARUDA_POOL_FUN_LUNC")
    if not GARUDA_POOL_FUN_JURIS:
        missing.append("GARUDA_POOL_FUN_JURIS")
    if not GARUDA_POOL_GRDX_FUN:
        missing.append("GARUDA_POOL_GRDX_FUN")
    if not TERRAPORT_POOL_FUN_LUNC:
        missing.append("TERRAPORT_POOL_FUN_LUNC")
    if not GARUDA_POOL_GRDX_LUNC:
        missing.append("GARUDA_POOL_GRDX_LUNC")
    if not TERRAPORT_POOL_GDEX_GRDX:
        missing.append("TERRAPORT_POOL_GDEX_GRDX")
    if not LUNCSWAP_POOL_JURIS_USDC:
        missing.append("LUNCSWAP_POOL_JURIS_USDC")
    if not LIX_CW20_ADDRESS:
        missing.append("LIX_CW20_ADDRESS")
    if not LTK_CW20_ADDRESS:
        missing.append("LTK_CW20_ADDRESS")
    if not ELPACO_CW20_ADDRESS:
        missing.append("ELPACO_CW20_ADDRESS")
    if not ROTTI_CW20_ADDRESS:
        missing.append("ROTTI_CW20_ADDRESS")
    if not LUNCSWAP_POOL_LUNC_LIX:
        missing.append("LUNCSWAP_POOL_LUNC_LIX")
    if not GARUDA_POOL_LUNC_LIX:
        missing.append("GARUDA_POOL_LUNC_LIX")
    if not GARUDA_POOL_LTK_LIX:
        missing.append("GARUDA_POOL_LTK_LIX")
    if not GARUDA_POOL_LUNC_LTK:
        missing.append("GARUDA_POOL_LUNC_LTK")
    if not GARUDA_POOL_USDC_LTK:
        missing.append("GARUDA_POOL_USDC_LTK")
    if not GARUDA_POOL_USTC_LTK:
        missing.append("GARUDA_POOL_USTC_LTK")
    if not GARUDA_POOL_LTK_ELPACO:
        missing.append("GARUDA_POOL_LTK_ELPACO")
    if not GARUDA_POOL_LUNC_ELPACO:
        missing.append("GARUDA_POOL_LUNC_ELPACO")
    if not GARUDA_POOL_ROTTI_LUNC:
        missing.append("GARUDA_POOL_ROTTI_LUNC")
    if not GARUDA_POOL_FUN_ROTTI:
        missing.append("GARUDA_POOL_FUN_ROTTI")
    if not GARUDA_POOL_ROTTI_JURIS:
        missing.append("GARUDA_POOL_ROTTI_JURIS")
    if not GARUDA_POOL_ROTTI_GRDX:
        missing.append("GARUDA_POOL_ROTTI_GRDX")
    if not TERRASWAP_POOL_LUNC_LTK:
        missing.append("TERRASWAP_POOL_LUNC_LTK")
    if not TERRAPORT_POOL_ROTTI_LUNC:
        missing.append("TERRAPORT_POOL_ROTTI_LUNC")
    if missing:
        raise SystemExit(
            "Missing/invalid required config: " + ", ".join(missing) +
            "\nFill these in your .env file before running."
        )