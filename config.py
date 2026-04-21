import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
GROUP_ID: str = os.environ["GROUP_ID"]
PAYMENT_AMOUNT: float = float(os.getenv("PAYMENT_AMOUNT", "15.0"))
MNEMONIC: str = os.environ["MNEMONIC"]
TRONGRID_API_KEY: str = os.getenv("TRONGRID_API_KEY", "")
ADMIN_TG_ID: int = int(os.getenv("ADMIN_TG_ID", "0"))

# NOWPayments
NOWPAYMENTS_API_KEY: str = os.getenv("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_IPN_SECRET: str = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
NOWPAYMENTS_IPN_URL: str = os.getenv("NOWPAYMENTS_IPN_URL", "")

# Official TRC20-USDT contract on TRON mainnet
USDT_CONTRACT: str = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

REQUIRED_CONFIRMATIONS: int = 20
POLL_INTERVAL: int = 30       # seconds between payment checks
ORDER_TIMEOUT_HOURS: int = int(os.getenv("ORDER_TIMEOUT_HOURS", "24"))
DB_PATH: str = "bot.db"
