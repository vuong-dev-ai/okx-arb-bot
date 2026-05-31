"""Telegram notification — text + animated icons (sticker / GIF / dice).

Env vars:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  — bắt buộc để bật notification.
  BOT_NAME                          — prefix trong message (mặc định: TREND).

  ICON_<EVENT>_GIF                  — override URL GIF cho event. VD:
      ICON_OPEN_LONG_GIF=https://media.tenor.com/.../rocket.gif
  ICON_<EVENT>_STICKER              — override sticker file_id cho event.

Event types: OPEN_LONG, OPEN_SHORT, CLOSE_WIN, CLOSE_LOSS, STOP_HIT,
             PARTIAL_TP, BOT_START, BOT_CRASH.

Mỗi event có thể có (gif, sticker, dice). Ưu tiên: sticker > gif > dice > text.
Nếu sticker/gif gửi fail, fallback xuống loại kế tiếp; cuối cùng luôn gửi text.
"""
import os
import json
import logging
import threading
import urllib.request
import urllib.error

log = logging.getLogger(__name__)

_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "").strip()
_BOT   = os.getenv("BOT_NAME", "TREND").strip()

if not _TOKEN or not _CHAT:
    _missing = [n for n, v in (("TELEGRAM_TOKEN", _TOKEN), ("TELEGRAM_CHAT_ID", _CHAT)) if not v]
    log.warning(f"⚠ Notifier DISABLED — thiếu env: {', '.join(_missing)}. Mọi notify_event sẽ bị silently drop.")


# ════════════════════ ICONS ════════════════════
# Mặc định: dùng dice emoji (Telegram tự animate, không cần URL ngoài).
# User có thể override bằng env var ICON_<EVENT>_GIF / ICON_<EVENT>_STICKER.
_DEFAULT_ICONS = {
    'OPEN_LONG':  {'dice': '🎯'},
    'OPEN_SHORT': {'dice': '🎯'},
    'CLOSE_WIN':  {'dice': '🎰'},
    'CLOSE_LOSS': {'dice': '🎳'},
    'STOP_HIT':   {'dice': '🎲'},
    'PARTIAL_TP': {'dice': '🎯'},
    'BOT_START':  {'dice': '🎲'},
    'BOT_CRASH':  {'dice': '🎳'},
}


def _build_icons():
    """Merge env overrides vào _DEFAULT_ICONS."""
    icons = {ev: dict(cfg) for ev, cfg in _DEFAULT_ICONS.items()}
    for ev in icons:
        gif = os.getenv(f"ICON_{ev}_GIF", "").strip()
        if gif:
            icons[ev]['gif'] = gif
        stk = os.getenv(f"ICON_{ev}_STICKER", "").strip()
        if stk:
            icons[ev]['sticker'] = stk
    return icons


ICONS = _build_icons()


# ════════════════════ LOW-LEVEL ════════════════════
def _post(method: str, payload: dict, timeout: float = 10.0) -> bool:
    """POST tới Bot API. Trả True nếu HTTP 2xx và response code=ok."""
    if not _TOKEN or not _CHAT:
        return False
    url  = f"https://api.telegram.org/bot{_TOKEN}/{method}"
    data = json.dumps(payload).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode('utf-8', errors='replace') or '{}')
            return bool(body.get('ok'))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode('utf-8', errors='replace')
        except Exception:
            body = ''
        log.debug(f"Telegram {method} HTTP {e.code}: {body[:200]}")
        return False
    except Exception as e:
        log.debug(f"Telegram {method}: {e}")
        return False


def _send_text(text: str) -> bool:
    return _post("sendMessage", {
        "chat_id": _CHAT, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def _send_animation(gif_url: str, caption: str) -> bool:
    return _post("sendAnimation", {
        "chat_id": _CHAT, "animation": gif_url,
        "caption": caption, "parse_mode": "HTML",
    })


def _send_sticker(file_id: str, caption: str) -> bool:
    """sticker không hỗ trợ caption — gửi sticker rồi gửi text rời."""
    ok = _post("sendSticker", {"chat_id": _CHAT, "sticker": file_id})
    if caption:
        _send_text(caption)
    return ok


def _send_dice(emoji: str) -> bool:
    """sendDice: emoji ∈ {🎲, 🎯, 🏀, ⚽, 🎳, 🎰}."""
    return _post("sendDice", {"chat_id": _CHAT, "emoji": emoji})


def _spawn(fn, *args):
    """Fire-and-forget trên thread riêng (không block bot loop)."""
    if not _TOKEN or not _CHAT:
        return
    threading.Thread(target=fn, args=args, daemon=True,
                     name="telegram-notify").start()


# ════════════════════ PUBLIC API ════════════════════
def notify(text: str):
    """Gửi text thuần (backward-compat). Fire-and-forget, silent nếu chưa cấu hình."""
    full = f"[{_BOT}] {text}"
    _spawn(_send_text, full)


def notify_animation(gif_url: str, caption: str = ""):
    """Gửi GIF với caption HTML. Fallback text nếu gif fail."""
    cap = f"[{_BOT}] {caption}" if caption else f"[{_BOT}]"
    def runner():
        if not _send_animation(gif_url, cap):
            _send_text(cap)
    _spawn(runner)


def notify_sticker(file_id: str, caption: str = ""):
    """Gửi sticker (animated/static) + caption text rời."""
    cap = f"[{_BOT}] {caption}" if caption else f"[{_BOT}]"
    def runner():
        if not _send_sticker(file_id, cap):
            _send_text(cap)
    _spawn(runner)


def notify_dice(emoji: str = "🎯", caption: str = ""):
    """Gửi animated dice. Caption gửi rời (sendDice không có caption)."""
    cap = f"[{_BOT}] {caption}" if caption else None
    def runner():
        _send_dice(emoji)
        if cap:
            _send_text(cap)
    _spawn(runner)


def notify_event(event: str, text: str):
    """High-level: chọn icon theo event, kèm text làm caption.

    Ưu tiên: sticker > gif > dice > text.
    Mỗi loại fail thì fallback loại kế tiếp; cuối cùng text-only luôn được gửi.
    """
    cfg = ICONS.get(event) or {}
    cap = f"[{_BOT}] {text}"

    def runner():
        # 1. Sticker (animated nếu file_id là animated)
        stk = cfg.get('sticker')
        if stk and _send_sticker(stk, cap):
            return
        # 2. GIF / animation
        gif = cfg.get('gif')
        if gif and _send_animation(gif, cap):
            return
        # 3. Dice — Telegram tự render animation
        dice = cfg.get('dice')
        if dice and _send_dice(dice):
            # sendDice không có caption → gửi text rời
            _send_text(cap)
            return
        # 4. Fallback: text only
        _send_text(cap)

    _spawn(runner)
