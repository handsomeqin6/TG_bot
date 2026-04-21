"""NOWPayments API — create USDT TRC20 payment orders."""
import aiohttp

from config import NOWPAYMENTS_API_KEY, NOWPAYMENTS_IPN_URL, PAYMENT_AMOUNT

_API_BASE = "https://api.nowpayments.io/v1"


async def create_payment(user_id: int) -> dict:
    """
    Create a payment order on NOWPayments.

    Returns the full API response dict, which includes:
        pay_address  — TRC20 address the user should send to
        payment_id   — NOWPayments internal order ID
        pay_amount   — exact amount to send (may differ slightly from PAYMENT_AMOUNT)
        pay_currency — "usdttrc20"
    """
    headers = {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "price_amount": PAYMENT_AMOUNT,
        "price_currency": "usd",
        "pay_currency": "usdttrc20",
        "order_id": str(user_id),
        "ipn_callback_url": NOWPAYMENTS_IPN_URL,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{_API_BASE}/payment",
            headers=headers,
            json=payload,
        ) as r:
            r.raise_for_status()
            return await r.json()
