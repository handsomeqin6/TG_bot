"""
sweep.py — Sweep USDT from all paid derived addresses to the master wallet.

Usage:
    py sweep.py            # execute transfers
    py sweep.py --dry-run  # preview only, no transactions sent
"""
import argparse
import os
import sqlite3
import sys

from dotenv import load_dotenv
from tronpy import Tron
from tronpy.keys import PrivateKey
from tronpy.providers import HTTPProvider
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Changes, Bip44Coins

load_dotenv()

MNEMONIC          = os.environ["MNEMONIC"]
TRONGRID_API_KEY  = os.getenv("TRONGRID_API_KEY", "")
USDT_CONTRACT     = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
MASTER_ADDRESS    = "TCqzSozb1bF9ZzVWcvuH3VEcYfY5XcTo4v"
DB_PATH           = "bot.db"
FEE_LIMIT         = 30_000_000   # 30 TRX upper cap for energy fee
MIN_SWEEP_SUN     = 10_000       # skip balances below 0.01 USDT (dust)
LOW_TRX_WARN      = 15.0         # warn if TRX < 15 (may not cover gas)

# ── HD wallet ──────────────────────────────────────────────────────────────

_seed_cache: bytes | None = None

def _seed() -> bytes:
    global _seed_cache
    if _seed_cache is None:
        _seed_cache = Bip39SeedGenerator(MNEMONIC).Generate()
    return _seed_cache

def derive_private_key(index: int) -> str:
    acc = (
        Bip44.FromSeed(_seed(), Bip44Coins.TRON)
        .Purpose()
        .Coin()
        .Account(0)
        .Change(Bip44Changes.CHAIN_EXT)
        .AddressIndex(index)
    )
    return acc.PrivateKey().Raw().ToHex()

# ── Database ────────────────────────────────────────────────────────────────

def get_paid_rows() -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT tg_user_id, wallet_idx, address FROM users WHERE paid = 1"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]

# ── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep USDT to master wallet")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show balances and planned transfers without executing")
    args = parser.parse_args()

    # Build TronGrid client
    provider = HTTPProvider("https://api.trongrid.io",
                            api_key=TRONGRID_API_KEY or None)
    client = Tron(provider)
    usdt = client.get_contract(USDT_CONTRACT)

    rows = get_paid_rows()
    if not rows:
        print("No paid addresses found in database.")
        return

    print(f"Master address : {MASTER_ADDRESS}")
    print(f"Scanning       : {len(rows)} paid address(es)")
    print(f"Mode           : {'DRY RUN (no transactions)' if args.dry_run else 'LIVE'}")
    print("-" * 60)

    total_sun = 0

    for row in rows:
        uid  = row["tg_user_id"]
        idx  = row["wallet_idx"]
        addr = row["address"]

        # Check balances
        try:
            balance_sun = usdt.functions.balanceOf(addr)
        except Exception as e:
            print(f"[user {uid}] {addr}\n  ERROR reading USDT balance: {e}\n")
            continue

        try:
            trx_balance = client.get_account_balance(addr)
        except Exception:
            trx_balance = 0.0

        balance_usdt = balance_sun / 1_000_000
        print(f"[user {uid}] {addr}")
        print(f"  USDT: {balance_usdt:.6f}  |  TRX: {trx_balance:.6f}")

        if balance_sun < MIN_SWEEP_SUN:
            print(f"  Skip (balance below dust threshold {MIN_SWEEP_SUN / 1_000_000} USDT)\n")
            continue

        if trx_balance < LOW_TRX_WARN:
            print(f"  [WARN] TRX balance low ({trx_balance:.2f} TRX) - transfer may fail if energy is insufficient")

        if args.dry_run:
            print(f"  [DRY RUN] Would transfer {balance_usdt:.6f} USDT -> {MASTER_ADDRESS}\n")
            total_sun += balance_sun
            continue

        # Sign and broadcast
        priv_key = PrivateKey(bytes.fromhex(derive_private_key(idx)))
        try:
            txn = (
                usdt.functions.transfer(MASTER_ADDRESS, balance_sun)
                .with_owner(addr)
                .fee_limit(FEE_LIMIT)
                .build()
                .sign(priv_key)
            )
            receipt = txn.broadcast().wait()
            tx_id = receipt.get("txid") or receipt.get("id", "unknown")
            print(f"  OK Transferred {balance_usdt:.6f} USDT  |  txid: {tx_id}\n")
            total_sun += balance_sun
        except Exception as e:
            print(f"  FAIL Transfer failed: {e}\n")

    print("-" * 60)
    action = "Would sweep" if args.dry_run else "Swept"
    print(f"{action} total: {total_sun / 1_000_000:.6f} USDT")


if __name__ == "__main__":
    main()
