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
import json
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


def _parse_ids(raw):
    out = set()
    for s in (raw or '').split(','):
        s = s.strip()
        if not s:
            continue
        try:
            out.add(int(s))
        except ValueError:
            log.warning(f"Bỏ qua chat_id không hợp lệ: {s!r}")
    return out


_raw_ids = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
if not _raw_ids:
    # Fallback: dùng TELEGRAM_CHAT_ID (chat dùng cho push) nếu chưa khai báo whitelist riêng
    _raw_ids = os.getenv("TELEGRAM_CHAT_ID", "").strip()

OWNER_CHAT_IDS = _parse_ids(_raw_ids)

_VIEWERS_FILE = os.path.join(_BASE, 'viewers.json')


def _save_viewers(ids):
    try:
        tmp = _VIEWERS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'viewers': sorted(int(i) for i in ids)}, f)
        os.replace(tmp, _VIEWERS_FILE)
    except Exception as e:
        log.warning(f"Ghi viewers.json lỗi: {e}")


def _load_viewers(env_seed):
    try:
        with open(_VIEWERS_FILE, encoding='utf-8') as f:
            return {int(x) for x in json.load(f).get('viewers', [])}
    except FileNotFoundError:
        _save_viewers(env_seed)
        return set(env_seed)
    except Exception as e:
        log.warning(f"Đọc viewers.json lỗi: {e} — tạm dùng env")
        return set(env_seed)


VIEWER_CHAT_IDS = _load_viewers(_parse_ids(os.getenv("TELEGRAM_VIEWER_CHAT_IDS", ""))) - OWNER_CHAT_IDS
ALLOWED_CHAT_IDS = OWNER_CHAT_IDS | VIEWER_CHAT_IDS


def is_owner(cid):
    return cid in OWNER_CHAT_IDS


def is_viewer(cid):
    return cid in VIEWER_CHAT_IDS


def add_viewer(cid):
    cid = int(cid)
    if cid in OWNER_CHAT_IDS:
        return 'owner'
    if cid in VIEWER_CHAT_IDS:
        return 'da_co'
    VIEWER_CHAT_IDS.add(cid)
    ALLOWED_CHAT_IDS.add(cid)
    _save_viewers(VIEWER_CHAT_IDS)
    return 'ok'


def remove_viewer(cid):
    cid = int(cid)
    if cid not in VIEWER_CHAT_IDS:
        return 'khong_co'
    VIEWER_CHAT_IDS.discard(cid)
    ALLOWED_CHAT_IDS.discard(cid)
    _save_viewers(VIEWER_CHAT_IDS)
    return 'ok'

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
