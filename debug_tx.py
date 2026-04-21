import asyncio
import json
import aiohttp

ADDRESS = "TSp3Ns6YcQxSRaNEXPfnT9FuZMkxs5papg"
BASE_URL = "https://api.trongrid.io"

# Load API key from .env
from dotenv import load_dotenv
import os
load_dotenv()
API_KEY = os.getenv("TRONGRID_API_KEY", "")
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
HEADERS = {"Accept": "application/json", "TRON-PRO-API-KEY": API_KEY}

async def main():
    async with aiohttp.ClientSession(headers=HEADERS) as s:

        print("=" * 60)
        print("1) /v1/accounts/{address}/transactions/trc20")
        print("=" * 60)
        url1 = f"{BASE_URL}/v1/accounts/{ADDRESS}/transactions/trc20"
        async with s.get(url1, params={"contract_address": USDT_CONTRACT, "limit": 20}) as r:
            print(f"HTTP {r.status}")
            data = await r.json(content_type=None)
            print(json.dumps(data, indent=2, ensure_ascii=False))

        print()
        print("=" * 60)
        print("2) /v1/accounts/{address}/transactions/trc20 (no contract filter)")
        print("=" * 60)
        async with s.get(url1, params={"limit": 20}) as r:
            print(f"HTTP {r.status}")
            data = await r.json(content_type=None)
            print(json.dumps(data, indent=2, ensure_ascii=False))

        print()
        print("=" * 60)
        print("3) /v1/accounts/{address}/transactions (general, only_to=true)")
        print("=" * 60)
        url2 = f"{BASE_URL}/v1/accounts/{ADDRESS}/transactions"
        async with s.get(url2, params={"limit": 20, "only_to": "true"}) as r:
            print(f"HTTP {r.status}")
            data = await r.json(content_type=None)
            print(json.dumps(data, indent=2, ensure_ascii=False))

        print()
        print("=" * 60)
        print("4) /v1/transactions/{txid}  — block number check")
        print("=" * 60)
        TX_ID = "00fa0f03b58093d717aa5ab8b90e45b0a75801e004fa139b17ac0403e61904b1"
        async with s.get(f"{BASE_URL}/v1/transactions/{TX_ID}") as r:
            print(f"HTTP {r.status}")
            data = await r.json(content_type=None)
            print(json.dumps(data, indent=2, ensure_ascii=False))

        print()
        print("=" * 60)
        print("5) latest block number")
        print("=" * 60)
        async with s.post(f"{BASE_URL}/wallet/getnowblock") as r:
            data = await r.json(content_type=None)
            bn = data["block_header"]["raw_data"]["number"]
            print(f"latest block: {bn}")

        print()
        print("=" * 60)
        print("6) /v1/accounts/{address}/transactions  (ALL, no filter, incl. unconfirmed)")
        print("=" * 60)
        url3 = f"{BASE_URL}/v1/accounts/{ADDRESS}/transactions"
        async with s.get(url3, params={"limit": 50, "only_confirmed": "false"}) as r:
            print(f"HTTP {r.status}")
            data = await r.json(content_type=None)
            print(json.dumps(data, indent=2, ensure_ascii=False))

asyncio.run(main())
