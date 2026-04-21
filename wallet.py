from typing import Optional

from bip_utils import Bip39SeedGenerator, Bip44, Bip44Changes, Bip44Coins

from config import MNEMONIC

_seed_cache: Optional[bytes] = None


def _seed() -> bytes:
    global _seed_cache
    if _seed_cache is None:
        _seed_cache = Bip39SeedGenerator(MNEMONIC).Generate()
    return _seed_cache


def derive_tron_address(index: int) -> str:
    """Derive a unique TRC20-compatible TRON address at BIP44 path m/44'/195'/0'/0/<index>."""
    acc = (
        Bip44.FromSeed(_seed(), Bip44Coins.TRON)
        .Purpose()
        .Coin()
        .Account(0)
        .Change(Bip44Changes.CHAIN_EXT)
        .AddressIndex(index)
    )
    return acc.PublicKey().ToAddress()
