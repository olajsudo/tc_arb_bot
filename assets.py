"""
Represents one fungible asset — either a native coin (like uluna/uusd) or a
CW20 token (like TERRA). Pools, swap messages, and tax calculations all key
off this instead of raw denom strings, so adding a CW20 asset doesn't need
special-casing scattered through the codebase.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Asset:
    kind: str          # "native" or "cw20"
    id: str             # denom for native, contract address for cw20
    decimals: int = 6
    display: str = ""

    def info(self) -> dict:
        """The {"native_token": ...} / {"token": ...} shape used in
        Terraswap/Astroport/Terraport query and message payloads."""
        if self.kind == "native":
            return {"native_token": {"denom": self.id}}
        return {"token": {"contract_addr": self.id}}

    def key(self) -> str:
        """Stable dict key, since two Assets with the same id but
        different kind should never collide (not a real scenario here,
        but keeps things unambiguous)."""
        return f"{self.kind}:{self.id}"

    def __str__(self) -> str:
        return self.display or self.id

    @staticmethod
    def from_chain_info(info: dict) -> "Asset":
        """Builds an Asset from a pool's on-chain asset info response."""
        if "native_token" in info:
            return Asset(kind="native", id=info["native_token"]["denom"])
        return Asset(kind="cw20", id=info["token"]["contract_addr"])
