"""
sweep.py -- Standalone USDT collection script.

Usage:
    py sweep.py           # scan balances + interactive confirmation
    py sweep.py --dry-run # scan only, no transactions
"""
import argparse
import sys
import time

import requests
from tronpy import Tron
from tronpy.keys import PrivateKey

import db
import wallet
from config import (
    MASTER_ADDRESS, MASTER_PRIVATE_KEY,
    TRONGRID_API_KEY, USDT_CONTRACT,
)

# -----------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------
BASE_URL            = "https://api.trongrid.io"
_HEADERS            = {"Accept": "application/json", "TRON-PRO-API-KEY": TRONGRID_API_KEY}
_ACTIVATION_TRX_SUN = 5_000_000    # 5 TRX per unactivated address
_SWEEP_FEE_LIMIT    = 15_000_000   # 15 TRX max fee for TRC20 transfer
_MIN_USDT_SUN       = 100_000      # skip balances < 0.1 USDT (dust)


# -----------------------------------------------------------------------
# Balance helpers (synchronous REST)
# -----------------------------------------------------------------------

def get_account_balances(address: str) -> tuple:
    """
    Returns (trx_sun: int, usdt_sun: int).
    trx_sun == 0 means address is not yet activated on-chain.
    """
    try:
        r = requests.get(
            f"{BASE_URL}/v1/accounts/{address}",
            headers=_HEADERS,
            timeout=10,
        )
        data = r.json().get("data", [])
    except Exception as e:
        print(f"  [WARN] balance query failed for {address}: {e}")
        return 0, 0

    if not data:
        return 0, 0   # address not activated

    account  = data[0]
    trx_sun  = account.get("balance", 0)
    usdt_sun = 0
    for item in account.get("trc20", []):
        if USDT_CONTRACT in item:
            usdt_sun = int(item[USDT_CONTRACT])
    return trx_sun, usdt_sun


def get_master_trx_sun() -> int:
    trx, _ = get_account_balances(MASTER_ADDRESS)
    return trx


# -----------------------------------------------------------------------
# Blockchain operations (tronpy, blocking)
# -----------------------------------------------------------------------

def send_trx(client: Tron, master_key: PrivateKey,
             to_address: str, amount_sun: int) -> str:
    """Send TRX from master to to_address. Returns txid."""
    txn = (
        client.trx.transfer(MASTER_ADDRESS, to_address, amount_sun)
        .build()
        .sign(master_key)
    )
    ret = txn.broadcast()
    ret.wait()
    return ret.get("txid", "?")


def sweep_usdt(client: Tron, child_key: PrivateKey,
               child_address: str, usdt_sun: int) -> str:
    """Transfer usdt_sun from child_address to MASTER_ADDRESS. Returns txid."""
    contract = client.get_contract(USDT_CONTRACT)
    txn = (
        contract.functions.transfer(MASTER_ADDRESS, usdt_sun)
        .with_owner(child_address)
        .fee_limit(_SWEEP_FEE_LIMIT)
        .build()
        .sign(child_key)
    )
    ret = txn.broadcast()
    ret.wait()
    return ret.get("txid", "?")


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def main(dry_run: bool) -> None:
    db.init_db()

    # ---- 1. Load all paid users ----------------------------------------
    with db._conn() as con:
        rows = con.execute(
            "SELECT tg_user_id, wallet_idx, address FROM users WHERE paid = 1"
        ).fetchall()
    users = [dict(r) for r in rows]

    if not users:
        print("No paid users found in database.")
        return

    print(f"\nScanning {len(users)} paid address(es)...\n")

    # ---- 2. Query balances --------------------------------------------
    targets         = []
    total_usdt_sun  = 0
    need_activation = 0

    for u in users:
        addr    = u["address"]
        idx     = u["wallet_idx"]
        trx_sun, usdt_sun = get_account_balances(addr)
        usdt      = usdt_sun / 1_000_000
        trx       = trx_sun  / 1_000_000
        activated = trx_sun > 0

        status = "activated" if activated else "NOT activated"
        print(f"  [{idx:>3}] {addr}  TRX={trx:.2f}  USDT={usdt:.6f}  ({status})")

        if usdt_sun >= _MIN_USDT_SUN:
            targets.append({
                "address":    addr,
                "wallet_idx": idx,
                "trx_sun":    trx_sun,
                "usdt_sun":   usdt_sun,
                "activated":  activated,
            })
            total_usdt_sun += usdt_sun
            if not activated:
                need_activation += 1

    # ---- 3. Summary report --------------------------------------------
    trx_needed = need_activation * 5
    print()
    print("=" * 62)
    print("SWEEP SUMMARY")
    print("=" * 62)
    print(f"  Addresses with USDT balance  : {len(targets)}")
    print(f"  Unactivated (need 5 TRX each): {need_activation}")
    print(f"  TRX required from master     : {trx_needed} TRX")
    print(f"  Total USDT to collect        : {total_usdt_sun / 1_000_000:.6f} USDT")
    print(f"  Destination (master)         : {MASTER_ADDRESS}")
    print("=" * 62)

    if not targets:
        print("\nNothing to sweep.")
        return

    if dry_run:
        print("\n[DRY-RUN] No transactions sent.")
        return

    # ---- 4. Confirm ---------------------------------------------------
    print()
    confirm = input("Type 'yes' to proceed with sweep: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        return

    # ---- 5. Check master TRX balance ----------------------------------
    master_trx_sun = get_master_trx_sun()
    master_trx     = master_trx_sun / 1_000_000
    trx_needed_sun = need_activation * _ACTIVATION_TRX_SUN

    print(f"\nMaster TRX balance: {master_trx:.2f} TRX")
    if master_trx_sun < trx_needed_sun:
        shortfall = (trx_needed_sun - master_trx_sun) / 1_000_000
        print(f"[ERROR] Insufficient TRX. Need {trx_needed} TRX, have {master_trx:.2f} TRX.")
        print(f"        Please top up at least {shortfall:.2f} more TRX to:")
        print(f"        {MASTER_ADDRESS}")
        sys.exit(1)

    print("TRX balance sufficient. Starting sweep...\n")

    # ---- 6. Execute sweeps --------------------------------------------
    client     = Tron()
    master_key = PrivateKey(bytes.fromhex(MASTER_PRIVATE_KEY))

    swept_count = 0
    swept_usdt  = 0
    failed      = 0

    for t in targets:
        addr      = t["address"]
        idx       = t["wallet_idx"]
        usdt_sun  = t["usdt_sun"]
        activated = t["activated"]
        usdt      = usdt_sun / 1_000_000

        print(f"[{idx:>3}] {addr}")
        print(f"      USDT: {usdt:.6f}")

        child_key = PrivateKey(bytes.fromhex(wallet.derive_tron_privkey(idx)))

        try:
            # Step A -- activate address with 5 TRX if needed
            if not activated:
                print("      -> Sending 5 TRX for activation...")
                txid_trx = send_trx(client, master_key, addr, _ACTIVATION_TRX_SUN)
                print(f"      -> TRX tx: {txid_trx[:24]}...")
                print("      -> Waiting 30 s for TRX to settle...")
                time.sleep(30)

            # Step B -- sweep USDT to master
            print(f"      -> Sweeping {usdt:.6f} USDT to master...")
            txid_usdt = sweep_usdt(client, child_key, addr, usdt_sun)
            print(f"      -> USDT tx: {txid_usdt[:24]}...")
            print("      -> OK\n")

            swept_count += 1
            swept_usdt  += usdt_sun

        except Exception as e:
            print(f"      -> FAILED: {e}\n")
            failed += 1

    # ---- 7. Final report ----------------------------------------------
    print("=" * 62)
    print("SWEEP COMPLETE")
    print("=" * 62)
    print(f"  Successfully swept : {swept_count} address(es)")
    print(f"  Total USDT swept   : {swept_usdt / 1_000_000:.6f} USDT")
    if failed:
        print(f"  Failed             : {failed} address(es)  <-- check logs above")
    print("=" * 62)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sweep USDT from paid child addresses to master wallet"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scan and report only; do not send any transactions"
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
