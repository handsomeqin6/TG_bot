import asyncio
import logging
import time
from typing import Optional

import aiohttp

from config import (
    MASTER_ADDRESS, MASTER_PRIVATE_KEY,
    PAYMENT_AMOUNT, REQUIRED_CONFIRMATIONS,
    TRONGRID_API_KEY, USDT_CONTRACT,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.trongrid.io"
_HEADERS = {"Accept": "application/json", "TRON-PRO-API-KEY": TRONGRID_API_KEY}
# USDT has 6 decimal places; pre-compute the minimum amount in "sun" units
REQUIRED_UNITS: int = round(PAYMENT_AMOUNT * 1_000_000)
# TRON produces ~1 block every 3 seconds
_TRON_BLOCK_INTERVAL_MS = 3_000

# TRX to send for activation (in sun: 1 TRX = 1_000_000 sun)
_ACTIVATION_TRX_SUN = 5_000_000   # 5 TRX

# Base58 alphabet used by TRON (same as Bitcoin)
_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s.encode():
        n = n * 58 + _B58_ALPHABET.index(ch)
    result = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + result


def _tron_to_evm_hex(tron_addr: str) -> str:
    """Convert TRON base58check address to lowercase 20-byte EVM hex (no 0x prefix)."""
    raw = _b58decode(tron_addr)   # 25 bytes: 1 version(0x41) + 20 addr + 4 checksum
    return raw[1:21].hex()


async def _latest_block(session: aiohttp.ClientSession) -> int:
    async with session.post(f"{BASE_URL}/wallet/getnowblock") as r:
        data = await r.json(content_type=None)
        return int(data["block_header"]["raw_data"]["number"])


async def _trc20_transfers(session: aiohttp.ClientSession, address: str) -> list:
    """Standard TRC20 transfer records."""
    url = f"{BASE_URL}/v1/accounts/{address}/transactions/trc20"
    params = {"contract_address": USDT_CONTRACT, "limit": 50}
    async with session.get(url, params=params) as r:
        if r.status != 200:
            return []
        data = await r.json(content_type=None)
        return data.get("data", [])


async def _general_txs_for_usdt(session: aiohttp.ClientSession, address: str) -> list:
    """
    Fallback: query all transaction types and extract USDT transfers by parsing ABI data.
    Catches GasFree and other non-standard delivery paths missed by the TRC20 endpoint.
    Returns records normalised to the same shape as _trc20_transfers:
      {transaction_id, to, value (str), block_timestamp}
    """
    url = f"{BASE_URL}/v1/accounts/{address}/transactions"
    params = {"limit": 50, "only_confirmed": "false"}
    async with session.get(url, params=params) as r:
        if r.status != 200:
            return []
        data = await r.json(content_type=None)

    evm_target = _tron_to_evm_hex(address)
    results = []

    for tx in data.get("data", []):
        if tx.get("ret", [{}])[0].get("contractRet") != "SUCCESS":
            continue
        contracts = tx.get("raw_data", {}).get("contract", [])
        if not contracts:
            continue
        c = contracts[0]
        if c.get("type") != "TriggerSmartContract":
            continue
        val = c.get("parameter", {}).get("value", {})
        if val.get("contract_address") != USDT_CONTRACT:
            continue

        hex_data = val.get("data", "")
        if len(hex_data) < 136 or hex_data[:8] != "a9059cbb":
            continue

        recipient_evm = hex_data[32:72]
        if recipient_evm.lower() != evm_target.lower():
            continue

        amount_int = int(hex_data[72:136], 16)
        results.append({
            "transaction_id": tx.get("txID", ""),
            "to": address,
            "value": str(amount_int),
            "block_timestamp": tx.get("raw_data", {}).get("timestamp", 0),
        })

    return results


async def _tx_block_number(session: aiohttp.ClientSession, tx_id: str) -> int:
    async with session.post(
        f"{BASE_URL}/wallet/gettransactioninfobyid",
        json={"value": tx_id},
    ) as r:
        if r.status != 200:
            return 0
        data = await r.json(content_type=None)
        return int(data.get("blockNumber", 0))


def _confirmed_by_timestamp(block_timestamp_ms: int) -> bool:
    if not block_timestamp_ms:
        return False
    elapsed_ms = time.time() * 1000 - block_timestamp_ms
    estimated_blocks = elapsed_ms / _TRON_BLOCK_INTERVAL_MS
    return estimated_blocks >= REQUIRED_CONFIRMATIONS * 1.2


async def _check_tx_list(
    session: aiohttp.ClientSession, txs: list, latest: int
) -> Optional[dict]:
    for tx in txs:
        amount = int(tx.get("value", 0))
        if amount < REQUIRED_UNITS:
            continue

        tx_id = tx.get("transaction_id", "")
        block_ts = tx.get("block_timestamp", 0)

        if _confirmed_by_timestamp(block_ts):
            logger.info("Payment confirmed (timestamp) tx=%s amount=%d", tx_id[:16], amount)
            return {"tx_id": tx_id, "amount_sun": amount}

        if tx_id:
            block_num = await _tx_block_number(session, tx_id)
            if block_num and (latest - block_num) >= REQUIRED_CONFIRMATIONS:
                logger.info(
                    "Payment confirmed (block) tx=%s block=%d latest=%d",
                    tx_id[:16], block_num, latest,
                )
                return {"tx_id": tx_id, "amount_sun": amount}
    return None


async def check_payment(address: str) -> Optional[dict]:
    """
    Returns {"tx_id": str, "amount_sun": int} when a confirmed USDT payment is found,
    or None if no qualifying transaction exists yet.
    """
    try:
        async with aiohttp.ClientSession(headers=_HEADERS) as session:
            latest = await _latest_block(session)

            trc20_txs = await _trc20_transfers(session, address)
            logger.debug("Address %s: %d TRC20 record(s)", address, len(trc20_txs))
            trc20_txs = [t for t in trc20_txs if t.get("to") == address
                         and t.get("token_info", {}).get("address") == USDT_CONTRACT]
            result = await _check_tx_list(session, trc20_txs, latest)
            if result:
                return result

            gen_txs = await _general_txs_for_usdt(session, address)
            logger.debug("Address %s: %d general USDT record(s)", address, len(gen_txs))
            return await _check_tx_list(session, gen_txs, latest)

    except Exception:
        logger.exception("Payment check error for address %s", address)
    return None


# ---------------------------------------------------------------------------
# Auto-sweep: send TRX to activate child, then sweep USDT back to MASTER
# ---------------------------------------------------------------------------

def _do_sweep(child_address: str, child_privkey_hex: str, amount_sun: int) -> None:
    """
    Blocking helper (run in executor):
      1. Send 5 TRX from MASTER to child (to cover energy fees)
      2. Sleep 30 s (let the TRX land on-chain)
      3. Transfer USDT from child back to MASTER
    """
    from tronpy import Tron
    from tronpy.keys import PrivateKey

    client = Tron()  # mainnet
    master_key = PrivateKey(bytes.fromhex(MASTER_PRIVATE_KEY))
    child_key  = PrivateKey(bytes.fromhex(child_privkey_hex))

    # Step 1 — send 5 TRX from master to child
    logger.info("Sweep step 1: sending 5 TRX from %s to %s", MASTER_ADDRESS, child_address)
    txn = (
        client.trx.transfer(MASTER_ADDRESS, child_address, _ACTIVATION_TRX_SUN)
        .build()
        .sign(master_key)
    )
    ret = txn.broadcast()
    ret.wait()
    logger.info("TRX sent, txid=%s", ret.get("txid", "?")[:16])

    # Step 2 — wait for TRX to settle
    logger.info("Sweep step 2: waiting 30 s for TRX to settle…")
    time.sleep(30)

    # Step 3 — sweep USDT from child to MASTER
    logger.info(
        "Sweep step 3: transferring %d sun USDT from %s to %s",
        amount_sun, child_address, MASTER_ADDRESS,
    )
    contract = client.get_contract(USDT_CONTRACT)
    txn2 = (
        contract.functions.transfer(MASTER_ADDRESS, amount_sun)
        .with_owner(child_address)
        .fee_limit(15_000_000)   # 15 TRX max fee
        .build()
        .sign(child_key)
    )
    ret2 = txn2.broadcast()
    ret2.wait()
    logger.info("USDT swept, txid=%s", ret2.get("txid", "?")[:16])


async def auto_sweep(child_address: str, child_privkey_hex: str, amount_sun: int) -> None:
    """
    Async wrapper: runs the blocking sweep in a thread so it doesn't block the bot.
    Logs but never raises — a sweep failure must not prevent the invite link from being sent.
    """
    if not MASTER_ADDRESS or not MASTER_PRIVATE_KEY:
        logger.warning("auto_sweep skipped: MASTER_ADDRESS or MASTER_PRIVATE_KEY not configured")
        return
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, _do_sweep, child_address, child_privkey_hex, amount_sun
        )
        logger.info("auto_sweep completed for %s", child_address)
    except Exception:
        logger.exception("auto_sweep failed for %s — sweep skipped", child_address)
