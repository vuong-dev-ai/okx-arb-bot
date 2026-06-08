"""Telegram notification — text + animated icons (sticker / GIF / dice).

Env vars:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID  — bắt buộc để bật notification.
  BOT_NAME                          — prefix trong message (mặc định: ARB).

  ICON_<EVENT>_GIF                  — override URL GIF cho event.
  ICON_<EVENT>_STICKER              — override sticker file_id cho event.

Event types: OPEN_LONG, OPEN_SHORT, CLOSE_WIN, CLOSE_LOSS, STOP_HIT,
             PARTIAL_TP, BOT_START, BOT_CRASH.

Mỗi event có thể có (gif, sticker, dice). Ưu tiên: sticker > gif > dice > text.
"""
import os
import html
import json
import logging
import threading
import urllib.request
import urllib.error

log = logging.getLogger(__name__)

_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "").strip()
_BOT   = os.getenv("BOT_NAME", "ARB").strip()

if not _TOKEN or not _CHAT:
    _missing = [n for n, v in (("TELEGRAM_TOKEN", _TOKEN), ("TELEGRAM_CHAT_ID", _CHAT)) if not v]
    log.warning(f"⚠ Notifier DISABLED — thiếu env: {', '.join(_missing)}. Mọi notify/alert sẽ bị silently drop.")


# ════════════════════ ICONS ════════════════════
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
    ok = _post("sendSticker", {"chat_id": _CHAT, "sticker": file_id})
    if caption:
        _send_text(caption)
    return ok


def _send_dice(emoji: str) -> bool:
    return _post("sendDice", {"chat_id": _CHAT, "emoji": emoji})


def _spawn(fn, *args):
    if not _TOKEN or not _CHAT:
        return
    threading.Thread(target=fn, args=args, daemon=True,
                     name="telegram-notify").start()


# ════════════════════ PUBLIC API ════════════════════
def notify(text: str):
    full = f"[{_BOT}] {text}"
    _spawn(_send_text, full)


def notify_animation(gif_url: str, caption: str = ""):
    cap = f"[{_BOT}] {caption}" if caption else f"[{_BOT}]"
    def runner():
        if not _send_animation(gif_url, cap):
            _send_text(cap)
    _spawn(runner)


def notify_sticker(file_id: str, caption: str = ""):
    cap = f"[{_BOT}] {caption}" if caption else f"[{_BOT}]"
    def runner():
        if not _send_sticker(file_id, cap):
            _send_text(cap)
    _spawn(runner)


def notify_dice(emoji: str = "🎯", caption: str = ""):
    cap = f"[{_BOT}] {caption}" if caption else None
    def runner():
        _send_dice(emoji)
        if cap:
            _send_text(cap)
    _spawn(runner)


def notify_event(event: str, text: str):
    cfg = ICONS.get(event) or {}
    cap = f"[{_BOT}] {text}"

    def runner():
        stk = cfg.get('sticker')
        if stk and _send_sticker(stk, cap):
            return
        gif = cfg.get('gif')
        if gif and _send_animation(gif, cap):
            return
        dice = cfg.get('dice')
        if dice and _send_dice(dice):
            _send_text(cap)
            return
        _send_text(cap)

    _spawn(runner)


# ════════════════════ CRITICAL ALERTS ════════════════════
# Cảnh báo sự cố CẦN tay người (unhedged-spot, crash, WS chết, stop fail, phantom...).
# Có RETRY (3 lần) vì alert critical không được phép rớt, và DEDUP để không spam khi
# lỗi lặp mỗi tick.
import time as _time

_crit_lock = threading.Lock()
_crit_last = {}            # key -> last_sent_ts
CRIT_DEDUP_SEC = 600       # cùng 1 sự cố tối đa 1 alert / 10 phút


def notify_critical(text: str, key: str = None, dedup_sec: int = CRIT_DEDUP_SEC):
    """Gửi alert CRITICAL nổi bật (🚨) + retry + dedup theo `key`.

    key      : nhãn dedup (cùng key trong dedup_sec giây chỉ gửi 1 lần). Mặc định = text.
    dedup_sec: cửa sổ dedup. Đặt 0 để LUÔN gửi (vd alert tổng hợp định kỳ).
    """
    k = key or text
    now = _time.time()
    if dedup_sec:
        with _crit_lock:
            if now - _crit_last.get(k, 0) < dedup_sec:
                return
            _crit_last[k] = now
    log.error(f"CRITICAL: {text}")          # luôn vào bot.log dù telegram tắt/fail
    # parse_mode=HTML → PHẢI escape nội dung động (traceback exception hay chứa <, >, &
    # vd "TypeError: '<' not supported..."); nếu không Telegram trả 400 can't-parse-entities
    # và alert bị nuốt. Thẻ <b>…</b> bao ngoài là markup cố ý nên giữ nguyên.
    msg = f"🚨 <b>[{html.escape(_BOT)}] CRITICAL</b>\n{html.escape(text)}"
    if not _TOKEN or not _CHAT:
        return
    def runner():
        for i in range(3):
            if _send_text(msg):
                return
            _time.sleep(1.5 * (i + 1))
        log.error(f"CRITICAL telegram gửi FAIL sau 3 lần: {text}")
        # Gửi fail HOÀN TOÀN → nhả dedup để lần crash/sự cố kế tiếp còn được thử lại,
        # tránh trường hợp 1 lần fail (mạng chập lúc crash) khoá luôn alert trong dedup_sec.
        if dedup_sec:
            with _crit_lock:
                if _crit_last.get(k) == now:
                    _crit_last.pop(k, None)
    _spawn(runner)
