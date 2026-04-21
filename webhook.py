"""
NOWPayments IPN webhook server.

Run separately from the bot:
    py webhook.py

The URL https://your-domain.com/ipn must be publicly reachable.
For local testing use ngrok: ngrok http 8080
"""
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

import db
from config import ADMIN_TG_ID, BOT_TOKEN, GROUP_ID, NOWPAYMENTS_IPN_SECRET

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


def _verify_signature(body_dict: dict, signature: str) -> bool:
    """Verify NOWPayments HMAC-SHA512 IPN signature."""
    sorted_str = json.dumps(body_dict, sort_keys=True)
    expected = hmac.new(
        NOWPAYMENTS_IPN_SECRET.encode(),
        sorted_str.encode(),
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def _tg(method: str, **kwargs) -> dict:
    """POST to Telegram Bot API and return the JSON response."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=kwargs,
            timeout=10,
        )
        return r.json()
    except Exception as e:
        logger.error("Telegram API call failed (%s): %s", method, e)
        return {}


@app.route("/ipn", methods=["POST"])
def ipn():
    sig = request.headers.get("x-nowpayments-sig", "")
    body = request.get_json(force=True, silent=True) or {}

    if not _verify_signature(body, sig):
        logger.warning("IPN rejected: signature mismatch")
        return jsonify({"error": "invalid signature"}), 400

    status      = body.get("payment_status", "")
    order_id    = body.get("order_id", "")
    payment_id  = str(body.get("payment_id", ""))
    paid_amount = float(body.get("actually_paid", 0))
    tx_hash     = body.get("outcome_hash") or body.get("payin_hash") or "N/A"

    logger.info("IPN status=%s order_id=%s payment_id=%s", status, order_id, payment_id)

    if status != "finished":
        return jsonify({"status": "ignored", "payment_status": status}), 200

    # Parse user ID from order_id
    try:
        user_id = int(order_id)
    except (ValueError, TypeError):
        logger.error("Cannot parse user_id from order_id=%s", order_id)
        return jsonify({"error": "bad order_id"}), 400

    user = db.get_user(user_id)
    if user is None:
        logger.error("Unknown user_id=%s", user_id)
        return jsonify({"error": "user not found"}), 404

    if user["invite_sent"]:
        logger.info("Duplicate IPN for user %s — already processed", user_id)
        return jsonify({"status": "already processed"}), 200

    db.mark_paid(user_id)

    # Generate one-time invite link (1 hour, 1 use)
    expire_ts = int(time.time()) + 3600
    invite_resp = _tg(
        "createChatInviteLink",
        chat_id=GROUP_ID,
        member_limit=1,
        expire_date=expire_ts,
    )
    invite_link = invite_resp.get("result", {}).get("invite_link", "")

    if invite_link:
        _tg(
            "sendMessage",
            chat_id=user_id,
            text=(
                "Payment confirmed! Here is your one-time invite link (valid 1 hour):\n"
                "付款已确认！入群链接（1 小时内有效，仅限使用一次）：\n\n"
                f"{invite_link}"
            ),
        )
        db.mark_invite_sent(user_id)
        logger.info("Invite sent to user %s", user_id)
    else:
        logger.error("Failed to create invite link: %s", invite_resp)

    # Admin notification
    if ADMIN_TG_ID:
        now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        _tg(
            "sendMessage",
            chat_id=ADMIN_TG_ID,
            text=(
                "New Payment / 新付款通知\n\n"
                f"User ID / 用户ID: {user_id}\n"
                f"Amount / 金额: {paid_amount} USDT\n"
                f"TX Hash / 交易哈希: {tx_hash}\n"
                f"Time / 时间: {now_str}"
            ),
        )

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    db.init_db()
    logger.info("Webhook server starting on port 8080")
    app.run(host="0.0.0.0", port=8080, debug=False)
