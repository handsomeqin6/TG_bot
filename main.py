import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import db
import nowpayments
from config import (
    ADMIN_TG_ID, BOT_TOKEN, ORDER_TIMEOUT_HOURS, PAYMENT_AMOUNT,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = db.get_user(user_id)

    if user is not None and user["invite_sent"]:
        await update.message.reply_text(
            "Your payment has been confirmed and the invite link has been sent.\n"
            "您已完成付款并收到邀请链接。如链接失效请联系管理员。"
        )
        return

    if user is not None and user["paid"]:
        await update.message.reply_text(
            "Payment confirmed, generating invite link, please wait…\n"
            "付款已确认，邀请链接正在生成，请稍候……"
        )
        return

    # Create (or refresh) a NOWPayments order
    try:
        order = await nowpayments.create_payment(user_id)
    except Exception as e:
        logger.error("NOWPayments create_payment failed for user %s: %s", user_id, e)
        await update.message.reply_text(
            "Failed to create payment order. Please try again later.\n"
            "创建订单失败，请稍后再试。"
        )
        return

    pay_address = order.get("pay_address", "")
    pay_amount  = order.get("pay_amount", PAYMENT_AMOUNT)
    payment_id  = str(order.get("payment_id", ""))

    db.upsert_nowpayment(user_id, pay_address, payment_id)

    pa_display = f"{pay_amount}"
    pa_md = pa_display.replace('.', '\\.')

    text = (
        f"Welcome\\! Please transfer *{pa_md} USDT* to the following TRC20 address:\n\n"
        f"`{pay_address}`\n\n"
        f"\\- TRC20 \\(TRON\\) network only\n"
        f"\\- Exact amount: *{pa_md} USDT*\n"
        f"\\- Invite link will be sent automatically after payment is confirmed\n"
        f"\\- If you have any questions, please contact @GermanSparrow1\n\n"
        f"欢迎\\！请通过 *TRC20 网络* 向以下地址转入 *{pa_md} USDT*：\n\n"
        f"`{pay_address}`\n\n"
        f"\\- 仅支持 TRC20（波场）网络\n"
        f"\\- 请转入精确金额：*{pa_md} USDT*\n"
        f"\\- 付款确认后系统自动发送一次性入群链接\n"
        f"\\- 如有问题请联系 @GermanSparrow1"
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
        f"Total paid users: {total_paid}\n"
        f"Total paid users / 总付款人数：{total_paid}\n\n"
        f"Estimated revenue: {total_revenue:.2f} USDT\n"
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


async def _check_expiry(context: ContextTypes.DEFAULT_TYPE) -> None:
    expiring = db.get_expiry_reminder_users(ORDER_TIMEOUT_HOURS)
    for user in expiring:
        tg_id = user["tg_user_id"]
        try:
            await context.bot.send_message(
                chat_id=tg_id,
                text=(
                    "Your order has expired. Please send /start to create a new payment order.\n"
                    "您的订单已过期，请重新发送 /start 获取新的付款地址。"
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

    # Check for expired orders every hour
    app.job_queue.run_repeating(_check_expiry, interval=3600, first=60)

    logger.info("Bot started (NOWPayments mode).")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
