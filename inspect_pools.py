"""
Prints raw reserves and implied prices for every configured pool, with no
profit math involved — just what's actually sitting on-chain right now.
Use this to sanity-check a suspicious "opportunity" against real-world
prices before trusting the bot's math.

Run: python inspect_pools.py
"""
import config
from assets import Asset
from pool_client import DexPool
from terra_client import TerraClient


def main():
    config.validate()
    terra = TerraClient()

    lunc = Asset(kind="native", id=config.DENOM_LUNC, decimals=6, display="LUNC")
    ustc = Asset(kind="native", id=config.DENOM_USTC, decimals=6, display="USTC")
    terra_token = Asset(kind="cw20", id=config.TERRA_CW20_ADDRESS,
                         decimals=config.TERRA_DECIMALS, display="TERRA")
    lcw_token = Asset(kind="cw20", id=config.LCW_CW20_ADDRESS,
                       decimals=config.LCW_DECIMALS, display="LCW")
    rev_token = Asset(kind="cw20", id=config.REV_CW20_ADDRESS,
                       decimals=config.REV_DECIMALS, display="REV")

    pools = [
        DexPool(config.TERRASWAP_POOL_1_NAME, terra, config.TERRASWAP_POOL_1,
                 lunc, ustc, config.TERRASWAP_COMMISSION_RATE),
        DexPool(config.TERRASWAP_POOL_2_NAME, terra, config.TERRASWAP_POOL_2,
                 lunc, ustc, config.TERRASWAP_COMMISSION_RATE),
        DexPool("Terraport TERRA/LUNC", terra, config.TERRAPORT_POOL_TERRA_LUNC,
                 terra_token, lunc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport TERRA/USTC", terra, config.TERRAPORT_POOL_TERRA_USTC,
                 terra_token, ustc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport LCW/LUNC", terra, config.TERRAPORT_POOL_LCW_LUNC,
                 lcw_token, lunc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport LCW/USTC", terra, config.TERRAPORT_POOL_LCW_USTC,
                 lcw_token, ustc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport REV/LUNC", terra, config.TERRAPORT_POOL_REV_LUNC,
                 rev_token, lunc, config.TERRAPORT_COMMISSION_RATE),
        DexPool("Terraport REV/USTC", terra, config.TERRAPORT_POOL_REV_USTC,
                 rev_token, ustc, config.TERRAPORT_COMMISSION_RATE),
    ]

    print(f"{'Pool':<25} {'Asset A':<8} {'Reserve A':>18} {'Asset B':<8} {'Reserve B':>18} {'Price (B per A)':>18}")
    print("-" * 100)
    for p in pools:
        state = p.get_state()
        a, b = p.asset_x, p.asset_y
        ra = state.reserves[a.key()]
        rb = state.reserves[b.key()]
        price = rb / ra if ra else 0
        print(f"{p.name:<25} {str(a):<8} {ra:>18,} {str(b):<8} {rb:>18,} {price:>18.8f}")


if __name__ == "__main__":
    main()