import logging
import time
from typing import Optional

import aiohttp

from config import (
    PAYMENT_AMOUNT, REQUIRED_CONFIRMATIONS,
    TRONGRID_API_KEY, USDT_CONTRACT,
)

logger = logging.getLogger(__name__)

# Addresses permanently excluded from payment polling.
# Add any address that should never trigger payment confirmation or invite sending.
BLACKLISTED_ADDRESSES: set = {
    "TSp3Ns6YcQxSRaNEXPfnT9FuZMkxs5papg",  # residual 1.5 USDT, no valid order
}

BASE_URL = "https://api.trongrid.io"
_HEADERS = {"Accept": "application/json", "TRON-PRO-API-KEY": TRONGRID_API_KEY}
# USDT has 6 decimal places; pre-compute the minimum amount in "sun" units
REQUIRED_UNITS: int = round(PAYMENT_AMOUNT * 1_000_000)
# TRON produces ~1 block every 3 seconds
_TRON_BLOCK_INTERVAL_MS = 3_000

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


async def _usdt_balance(session: aiohttp.ClientSession, address: str) -> int:
    """
    Query current on-chain USDT balance for address via account endpoint.
    Returns amount in sun; 0 if address not activated or holds no USDT.
    """
    try:
        async with session.get(
            f"{BASE_URL}/v1/accounts/{address}",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                return 0
            data = (await r.json(content_type=None)).get("data", [])
    except Exception:
        return 0
    if not data:
        return 0
    for item in data[0].get("trc20", []):
        if USDT_CONTRACT in item:
            return int(item[USDT_CONTRACT])
    return 0


async def query_usdt_balance(address: str) -> int:
    """Public helper: return current USDT balance (sun) for any address."""
    async with aiohttp.ClientSession(headers=_HEADERS) as session:
        return await _usdt_balance(session, address)


async def _check_tx_list(
    session: aiohttp.ClientSession, txs: list, latest: int
) -> Optional[dict]:
    for tx in txs:
        amount = int(tx.get("value", 0))
        if amount < REQUIRED_UNITS:
            continue

        tx_id    = tx.get("transaction_id", "")
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

    Query order:
      1. /transactions/trc20  -- standard TRC20 Transfer events
      2. /transactions        -- all tx types, ABI-parsed (GasFree fallback)

    Addresses in BLACKLISTED_ADDRESSES are always skipped and return None immediately.
    """
    if address in BLACKLISTED_ADDRESSES:
        logger.debug("Address %s is blacklisted — skipping", address)
        return None
    try:
        async with aiohttp.ClientSession(headers=_HEADERS) as session:
            latest = await _latest_block(session)

            # Path 1 — standard TRC20 transfer events
            trc20_txs = await _trc20_transfers(session, address)
            logger.debug("Address %s: %d TRC20 record(s)", address, len(trc20_txs))
            trc20_txs = [t for t in trc20_txs if t.get("to") == address
                         and t.get("token_info", {}).get("address") == USDT_CONTRACT]
            result = await _check_tx_list(session, trc20_txs, latest)
            if result:
                return result

            # Path 2 — general transactions, ABI-parsed (GasFree / non-standard)
            gen_txs = await _general_txs_for_usdt(session, address)
            logger.debug("Address %s: %d general USDT record(s)", address, len(gen_txs))
            result = await _check_tx_list(session, gen_txs, latest)
            if result:
                return result

    except Exception:
        logger.exception("Payment check error for address %s", address)
    return None
