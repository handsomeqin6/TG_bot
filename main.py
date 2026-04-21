import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import db
import wallet
from config import (
    ADMIN_TG_ID, BOT_TOKEN, GROUP_ID, ORDER_TIMEOUT_HOURS,
    PAYMENT_AMOUNT, POLL_INTERVAL, TRONGRID_API_KEY, USDT_CONTRACT,
)
from monitor import check_payment, query_usdt_balance

_TG_HEADERS = {"Accept": "application/json", "TRON-PRO-API-KEY": TRONGRID_API_KEY}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = db.get_user(user_id)

    if user is None:
        idx = db.next_wallet_index()
        address = wallet.derive_tron_address(idx)
        db.create_user(user_id, idx, address)
        user = db.get_user(user_id)

    if user["invite_sent"]:
        await update.message.reply_text(
            "Your payment has been confirmed and the invite link has been sent.\n"
            "您已完成付款并收到邀请链接。如链接失效请联系管理员。"
        )
        return

    if user["paid"]:
        await update.message.reply_text(
            "Payment confirmed, generating invite link, please wait…\n"
            "付款已确认，邀请链接正在生成，请稍候……"
        )
        return

    addr = user["address"]
    pa = str(PAYMENT_AMOUNT).replace('.', '\\.')
    text = (
        f"Welcome\\! Please transfer *{pa} USDT* to the following exclusive TRC20 address:\n\n"
        f"`{addr}`\n\n"
        f"\\- TRC20 \\(TRON\\) network only\n"
        f"\\- Minimum amount: *{pa} USDT*\n"
        f"\\- Payment verified every 30 seconds, invite link sent after 20 block confirmations\n"
        f"\\- If you have any questions, please contact @GermanSparrow1\n\n"
        f"欢迎\\！请通过 *TRC20 网络* 向以下专属地址转入 *{pa} USDT*：\n\n"
        f"`{addr}`\n\n"
        f"\\- 仅支持 TRC20（波场）网络\n"
        f"\\- 到账金额须 ≥ *{pa} USDT*\n"
        f"\\- 每 30 秒自动核验，20 个区块确认后自动发送一次性入群链接\n"
        f"\\- 如有问题请联系 @GermanSparrow1\n\n"
        f"⚠️ 此地址仅限本账号专用，请勿分享给他人。"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_TG_ID:
        await update.message.reply_text("Unauthorized.")
        return

    total_paid = db.count_paid()
    total_revenue = total_paid * PAYMENT_AMOUNT
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    await update.message.reply_text(
        f"Stats / 统计\n\n"
        f"Total paid users / 总付款人数：{total_paid}\n\n"
        f"Estimated revenue / 预计总收款：{total_revenue:.2f} USDT\n\n"
        f"As of {now}"
    )


async def cmd_resetdb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_TG_ID:
        await update.message.reply_text("Unauthorized.")
        return
    deleted = db.reset_all_users()
    await update.message.reply_text(
        f"Done. Deleted {deleted} record(s) from the database.\n"
        f"已清空数据库，共删除 {deleted} 条订单记录。"
    )


async def cmd_checkorder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_TG_ID:
        await update.message.reply_text("Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /checkorder <user_tg_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    user = db.get_user(target_id)
    if user is None:
        await update.message.reply_text(f"No order found for user {target_id}.")
        return

    # Determine status
    paid         = bool(user["paid"])
    invite_sent  = bool(user["invite_sent"])
    reminded     = bool(user["expired_reminded"])
    created_at   = user["created_at"]          # e.g. "2024-01-15 10:23:45"

    if invite_sent:
        status = "completed (invite sent)"
    elif paid:
        status = "paid — waiting for invite"
    elif reminded:
        status = "expired (reminder sent)"
    else:
        # Check if past timeout window
        try:
            created_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(tz=timezone.utc) - created_dt).total_seconds() / 3600
            status = f"expired (age {age_hours:.1f}h)" if age_hours > ORDER_TIMEOUT_HOURS else "pending"
        except Exception:
            status = "pending"

    lines = [
        f"Order / 订单详情",
        f"",
        f"User ID     : {target_id}",
        f"Status      : {status}",
        f"Address     : {user['address']}",
        f"Wallet idx  : {user['wallet_idx']}",
        f"Created at  : {created_at} UTC",
        f"Paid        : {'Yes' if paid else 'No'}",
        f"Invite sent : {'Yes' if invite_sent else 'No'}",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_checkbalance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_TG_ID:
        await update.message.reply_text("Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /checkbalance <user_tg_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    user = db.get_user(target_id)
    if user is None:
        await update.message.reply_text(f"No order found for user {target_id}.")
        return

    address = user["address"]
    await update.message.reply_text(f"Querying on-chain balance for:\n{address}\nPlease wait…")

    try:
        async with aiohttp.ClientSession(headers=_TG_HEADERS) as session:
            async with session.get(
                f"https://api.trongrid.io/v1/accounts/{address}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = (await r.json(content_type=None)).get("data", [])
    except Exception as e:
        await update.message.reply_text(f"API query failed: {e}")
        return

    if not data:
        await update.message.reply_text(
            f"Address not activated on-chain yet (no TRX received).\n"
            f"Address: {address}"
        )
        return

    account  = data[0]
    trx_sun  = account.get("balance", 0)
    usdt_sun = 0
    for item in account.get("trc20", []):
        if USDT_CONTRACT in item:
            usdt_sun = int(item[USDT_CONTRACT])

    trx_display  = trx_sun  / 1_000_000
    usdt_display = usdt_sun / 1_000_000

    lines = [
        f"On-chain Balance / 链上余额",
        f"",
        f"User ID : {target_id}",
        f"Address : {address}",
        f"",
        f"TRX     : {trx_display:.2f} TRX",
        f"USDT    : {usdt_display:.6f} USDT",
        f"",
    ]
    if usdt_sun == 0:
        lines.append("No USDT received yet.")
    elif usdt_sun >= round(PAYMENT_AMOUNT * 1_000_000):
        lines.append("Payment amount reached — funds confirmed on-chain.")
    else:
        lines.append(f"Insufficient USDT (need {PAYMENT_AMOUNT} USDT).")

    await update.message.reply_text("\n".join(lines))


async def _poll_payments(context: ContextTypes.DEFAULT_TYPE) -> None:
    pending = db.get_uninvited_users()
    if not pending:
        return

    for user in pending:
        tg_id: int = user["tg_user_id"]
        address: str = user["address"]
        wallet_idx: int = user.get("wallet_idx", -1)

        # Step 1 — confirm payment if not yet done
        if not user["paid"]:
            result = await check_payment(address)
            if result is None:
                continue
            db.mark_paid(tg_id)
            tx_id     = result["tx_id"]
            amount_sun = result["amount_sun"]
            amount_usdt = amount_sun / 1_000_000
            logger.info("Payment confirmed: user=%s tx=%s amount=%.6f USDT",
                        tg_id, tx_id[:16], amount_usdt)

        else:
            tx_id = ""
            amount_usdt = PAYMENT_AMOUNT

        # Step 2 — generate one-time invite link and notify user
        try:
            expire = datetime.now(tz=timezone.utc) + timedelta(hours=1)
            link = await context.bot.create_chat_invite_link(
                chat_id=GROUP_ID,
                member_limit=1,
                expire_date=expire,
            )
            await context.bot.send_message(
                chat_id=tg_id,
                text=(
                    "Payment confirmed! Here is your one-time invite link (valid 1 hour):\n"
                    "付款已确认！入群链接（1 小时内有效，仅限使用一次）：\n\n"
                    f"{link.invite_link}"
                ),
            )
            db.mark_invite_sent(tg_id)
            logger.info("Invite sent: user=%s", tg_id)
        except Exception:
            logger.exception("Failed to send invite to user %s", tg_id)
            continue

        # Step 3 — notify admin
        if ADMIN_TG_ID:
            now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_TG_ID,
                    text=(
                        "New Payment / 新付款通知\n\n"
                        f"User ID / 用户ID: {tg_id}\n"
                        f"Amount / 金额: {amount_usdt:.6f} USDT\n"
                        f"TX / 交易: {tx_id or 'N/A'}\n"
                        f"Time / 时间: {now_str}"
                    ),
                )
            except Exception:
                logger.exception("Failed to send admin notification")


async def _rescue_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Every 10 minutes: scan all derived addresses (index 0..max_known+5) and check
    if any has sufficient USDT balance but NO corresponding DB record.
    If found, notify admin so the order can be handled manually.

    This catches payments made to addresses whose DB records were deleted (e.g. after /resetdb).
    Normal missed-tx cases (record exists, paid=0) are handled by the balance fallback
    inside check_payment() which runs every POLL_INTERVAL seconds.
    """
    known_addresses = db.get_all_addresses()
    max_idx = db.next_wallet_index()          # MAX(wallet_idx) + 1
    scan_up_to = max_idx + 5                  # a small buffer beyond known range

    orphans = []
    for idx in range(scan_up_to):
        addr = wallet.derive_tron_address(idx)
        if addr in known_addresses:
            continue                           # already tracked — skip
        balance_sun = await query_usdt_balance(addr)
        if balance_sun >= round(PAYMENT_AMOUNT * 1_000_000):
            orphans.append((idx, addr, balance_sun / 1_000_000))
            logger.warning(
                "Orphan address detected: idx=%d addr=%s balance=%.6f USDT",
                idx, addr, balance_sun / 1_000_000,
            )

    if orphans and ADMIN_TG_ID:
        lines = ["[RESCUE] Orphan address(es) with USDT balance detected:\n"]
        for idx, addr, bal in orphans:
            lines.append(f"  idx={idx}  bal={bal:.6f} USDT\n  {addr}")
        lines.append("\nPlease check manually and send invite if needed.")
        try:
            await context.bot.send_message(
                chat_id=ADMIN_TG_ID,
                text="\n".join(lines),
            )
        except Exception:
            logger.exception("Failed to send orphan alert to admin")


async def _check_expiry(context: ContextTypes.DEFAULT_TYPE) -> None:
    expiring = db.get_expiry_reminder_users(ORDER_TIMEOUT_HOURS)
    for user in expiring:
        tg_id = user["tg_user_id"]
        try:
            await context.bot.send_message(
                chat_id=tg_id,
                text=(
                    "Your order has expired. Please send /start to get a new address.\n"
                    "您的订单已过期，请重新发送 /start 获取新地址。"
                ),
            )
            db.mark_expired_reminded(tg_id)
            logger.info("Expiry reminder sent: user=%s", tg_id)
        except Exception:
            logger.exception("Failed to send expiry reminder to user %s", tg_id)


def main() -> None:
    db.init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("resetdb", cmd_resetdb))
    app.add_handler(CommandHandler("checkorder", cmd_checkorder))
    app.add_handler(CommandHandler("checkbalance", cmd_checkbalance))

    app.job_queue.run_repeating(_poll_payments, interval=POLL_INTERVAL, first=15)
    app.job_queue.run_repeating(_rescue_scan,   interval=600,          first=120)
    app.job_queue.run_repeating(_check_expiry,  interval=3600,         first=60)

    logger.info("Bot started. Polling every %ds.", POLL_INTERVAL)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
