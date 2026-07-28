"""Gateway config — load from `.env` or system env vars.

Required:
  TELEGRAM_TOKEN          — token bot Telegram (cùng token với 2 bot trading).
  TELEGRAM_ALLOWED_CHAT_IDS — comma-separated chat IDs được phép gọi command.

Optional:
  GATEWAY_ARB_URL         — mặc định http://localhost:5000
  GATEWAY_DAILY_HOUR      — giờ chạy daily summary (VN time), mặc định 22
  GATEWAY_ALERT_INTERVAL  — giây giữa các vòng poll alert, mặc định 300
  GATEWAY_DD_THRESHOLD    — % drawdown để alert, mặc định -5
  GATEWAY_PROFIT_THRESHOLD — % profit milestone, mặc định 10
"""
import os
import logging
import sys

from dotenv import load_dotenv

log = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.abspath(__file__))
# Cho phép share .env với arb bot
_ENV_CANDIDATES = [
    os.path.join(_BASE, '.env'),
    os.path.join(os.path.dirname(_BASE), 'okx-arb-bot', '.env'),
]
for p in _ENV_CANDIDATES:
    if os.path.exists(p):
        load_dotenv(p, override=False)


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

_raw_ids = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
if not _raw_ids:
    # Fallback: dùng TELEGRAM_CHAT_ID (chat dùng cho push) nếu chưa khai báo whitelist riêng
    _raw_ids = os.getenv("TELEGRAM_CHAT_ID", "").strip()

ALLOWED_CHAT_IDS = set()
for s in _raw_ids.split(','):
    s = s.strip()
    if s:
        try:
            ALLOWED_CHAT_IDS.add(int(s))
        except ValueError:
            log.warning(f"Bỏ qua chat_id không hợp lệ: {s!r}")

BOT_ENDPOINTS = {
    'arb':   os.getenv("GATEWAY_ARB_URL",   "http://localhost:5000").rstrip('/'),
}

# URL dashboard công khai (nginx basic-auth) — hiện nút 🌐 trong /menu nếu có
DASHBOARD_URL = os.getenv("GATEWAY_DASHBOARD_URL", "").strip()

DAILY_HOUR        = int(os.getenv("GATEWAY_DAILY_HOUR", "22"))
ALERT_INTERVAL    = int(os.getenv("GATEWAY_ALERT_INTERVAL", "300"))
DD_THRESHOLD      = float(os.getenv("GATEWAY_DD_THRESHOLD", "-5"))
PROFIT_THRESHOLD  = float(os.getenv("GATEWAY_PROFIT_THRESHOLD", "10"))


def validate():
    if not TELEGRAM_TOKEN:
        sys.stderr.write(
            "\n" + "="*60 +
            "\n  ❌ Thiếu TELEGRAM_TOKEN trong .env\n" +
            "="*60 + "\n"
        )
        sys.exit(1)
    if not ALLOWED_CHAT_IDS:
        sys.stderr.write(
            "\n" + "="*60 +
            "\n  ⚠ Chưa khai báo TELEGRAM_ALLOWED_CHAT_IDS (hoặc TELEGRAM_CHAT_ID).\n"
            "  Bất kỳ ai cũng có thể gọi command — gateway sẽ KHÔNG khởi động.\n"
            "  Lấy chat_id của bạn: gửi tin nhắn cho @userinfobot.\n" +
            "="*60 + "\n"
        )
        sys.exit(1)
