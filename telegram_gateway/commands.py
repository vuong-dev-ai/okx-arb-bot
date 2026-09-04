"""Async command handlers cho gateway."""
import asyncio
import json
import logging
import os
from datetime import datetime
from functools import wraps

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import clients
import config
from config import (
    ALLOWED_CHAT_IDS, OWNER_CHAT_IDS, VIEWER_CHAT_IDS,
    is_owner, DD_THRESHOLD, PROFIT_THRESHOLD,
)

log = logging.getLogger(__name__)


# ════════════════════ AUTH ════════════════════
def auth(handler):
    """Decorator: chỉ chat_id trong whitelist mới được gọi."""
    @wraps(handler)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if not chat or chat.id not in ALLOWED_CHAT_IDS:
            log.warning(f"Từ chối chat_id={chat.id if chat else '?'}")
            if update.effective_message:
                await update.effective_message.reply_text(
                    "⛔ Bạn không có quyền dùng bot này.\n"
                    "Liên hệ owner để được thêm vào whitelist."
                )
            return
        try:
            return await handler(update, ctx)
        except Exception as e:
            log.exception(f"Handler {handler.__name__} crash: {e}")
            if update.effective_message:
                await update.effective_message.reply_text(f"⚠ Lỗi xử lý lệnh: {e}")
    return wrapper


def owner_only(handler):
    @wraps(handler)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or not is_owner(user.id):
            log.warning(f"Chặn thao tác từ người không phải owner user_id={user.id if user else '?'}")
            if update.effective_message:
                await update.effective_message.reply_text(
                    "Bạn chỉ có quyền xem, không thể thao tác trên bot."
                )
            elif update.callback_query:
                await update.callback_query.answer(
                    "Bạn chỉ có quyền xem, không thao tác được.", show_alert=True)
            return
        return await handler(update, ctx)
    return wrapper


# ════════════════════ HELPERS ════════════════════
_BOTS = ('arb',)


def _esc(s) -> str:
    s = '' if s is None else str(s)
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


# ════════════════════ COMMANDS ════════════════════
@auth
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>Lệnh khả dụng</b>\n\n"
        "/menu\n"
        "/web\n"
        "/help\n"
        "/dsquyen\n"
        "Cấp: <code>/capquyen &lt;id&gt;</code> · Thu: <code>/thuquyen &lt;id&gt;</code>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@auth
async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Bảng lệnh</b>\n\n"
        "/menu\n"
        "/web\n"
        "/help\n"
        "/dsquyen\n"
        "Cấp: <code>/capquyen &lt;id&gt;</code> · Thu: <code>/thuquyen &lt;id&gt;</code>",
        parse_mode=ParseMode.HTML,
    )


ISSUE_AUTH = os.path.expanduser('~/event-lab/issue_auth.py')
PY_BIN = os.path.expanduser('~/venv/bin/python')


@auth
async def cmd_web(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cấp tài khoản + mật khẩu web MỚI, hiệu lực 5 phút.

    Chủ bot nhận quyền đầy đủ. Người chỉ xem nhận tài khoản chỉ đọc nếu issue_auth.py hỗ trợ.
    """
    user = update.effective_user
    chat = update.effective_chat
    role = 'owner' if (user and is_owner(user.id)) else 'view'
    log.warning(f'Yêu cầu cấp mật khẩu web ({role}) từ user_id={user.id if user else "?"} '
                f'chat_id={chat.id if chat else "?"}')
    msg = update.effective_message
    if not os.path.exists(ISSUE_AUTH):
        await msg.reply_text('⚠ Không tìm thấy issue_auth.py trên server')
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            PY_BIN, ISSUE_AUTH, '--role', role,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
        d = json.loads(out.decode().strip() or '{}')
    except Exception as e:
        await msg.reply_text(f'⚠ Cấp mật khẩu lỗi: {e}')
        return
    if d.get('error') or not d.get('user'):
        await msg.reply_text(f'⚠ {d.get("error", "không cấp được")}')
        return
    mins = int(d.get('ttl', 300)) // 60
    quyen = ('👀 <b>CHỈ XEM</b> — không thao tác được' if role == 'view'
             else '🔓 Toàn quyền')
    await msg.reply_text(
        f'🔐 <b>Tài khoản web mới</b> ({quyen})\n'
        f'Tên đăng nhập: <code>{_esc(d["user"])}</code>\n'
        f'Mật khẩu: <code>{_esc(d["pass"])}</code>\n\n'
        f'http://134.185.80.111:8080/\n'
        f'⏱ Hết hiệu lực sau <b>{mins} phút</b>\n'
        f'<i>Tài khoản cùng loại cũ đã bị xoá và phiên cùng loại đã bị đăng xuất.</i>',
        parse_mode=ParseMode.HTML)


def _parse_id_args(args):
    ids = []
    for a in (args or []):
        a = a.strip().lstrip('@')
        try:
            ids.append(int(a))
        except ValueError:
            pass
    return ids


@owner_only
async def cmd_capquyen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ids = _parse_id_args(ctx.args)
    if not ids:
        await update.effective_message.reply_text(
            "Dùng: <code>/capquyen &lt;id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    out = []
    for cid in ids:
        r = config.add_viewer(cid)
        out.append({
            'ok':    f'✅ Đã cấp quyền XEM cho <code>{cid}</code>',
            'da_co': f'ℹ️ <code>{cid}</code> đã là người xem rồi',
            'owner': f'⚠️ <code>{cid}</code> là CHỦ bot — khỏi cần cấp',
        }.get(r, f'<code>{cid}</code>: {r}'))
    await update.effective_message.reply_text('\n'.join(out), parse_mode=ParseMode.HTML)


@owner_only
async def cmd_thuquyen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ids = _parse_id_args(ctx.args)
    if not ids:
        await update.effective_message.reply_text(
            "Dùng: <code>/thuquyen &lt;id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    out = []
    for cid in ids:
        r = config.remove_viewer(cid)
        out.append({
            'ok':       f'🚫 Đã THU quyền của <code>{cid}</code>',
            'khong_co': f'ℹ️ <code>{cid}</code> vốn không phải người xem',
        }.get(r, f'<code>{cid}</code>: {r}'))
    await update.effective_message.reply_text('\n'.join(out), parse_mode=ParseMode.HTML)


@owner_only
async def cmd_dsquyen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    o = ', '.join(f'<code>{i}</code>' for i in sorted(OWNER_CHAT_IDS)) or '—'
    v = ', '.join(f'<code>{i}</code>' for i in sorted(VIEWER_CHAT_IDS)) or '—'
    await update.effective_message.reply_text(
        f'👑 <b>Chủ:</b> {o}\n'
        f'👀 <b>Chỉ xem:</b> {v}\n\n'
        f'Cấp: <code>/capquyen &lt;id&gt;</code> · Thu: <code>/thuquyen &lt;id&gt;</code>',
        parse_mode=ParseMode.HTML)


# ════════════════════ JOBS ════════════════════
# Track alert state để không spam (key: (bot, coin, type) → bool)
_alert_state: dict = {}
# Bot offline tracking
_bot_fail_count: dict  = {b: 0 for b in _BOTS}
_bot_offline_alerted: dict = {b: False for b in _BOTS}
# Loss streak tracking
_streak_alerted: dict  = {b: False for b in _BOTS}
LOSS_STREAK_THRESHOLD  = 3   # cảnh báo khi thua liên tiếp ≥ N lệnh


async def _send_all(ctx, text: str):
    """Gửi message đến tất cả chat trong whitelist."""
    for cid in ALLOWED_CHAT_IDS:
        try:
            await ctx.bot.send_message(cid, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.debug(f"send_all → {cid}: {e}")


async def job_alerts(ctx: ContextTypes.DEFAULT_TYPE):
    """Poll status cả 2 bot — alert drawdown, profit, bot offline."""
    for b in _BOTS:
        data = await clients.status(b)

        # ── Bug fix: Bot offline escalation ──────────────────────
        if not data:
            _bot_fail_count[b] = _bot_fail_count.get(b, 0) + 1
            if _bot_fail_count[b] == 3 and not _bot_offline_alerted.get(b):
                _bot_offline_alerted[b] = True
                await _send_all(ctx,
                    f"🔴 <b>{b.upper()} OFFLINE</b> — không phản hồi "
                    f"({_bot_fail_count[b]} lần liên tiếp). Kiểm tra ngay!"
                )
            continue

        # Bot phục hồi sau khi offline
        if _bot_offline_alerted.get(b):
            _bot_offline_alerted[b] = False
            await _send_all(ctx, f"🟢 <b>{b.upper()}</b> đã trở lại online.")
        _bot_fail_count[b] = 0

        positions = data.get('positions') or []
        live_keys = set()
        for p in positions:
            coin = p.get('coin')
            pct  = p.get('pct') or 0
            cur  = p.get('cur_price') or 0
            if pct <= DD_THRESHOLD:
                key = (b, coin, 'dd')
                live_keys.add(key)
                if not _alert_state.get(key):
                    _alert_state[key] = True
                    for cid in ALLOWED_CHAT_IDS:
                        try:
                            await ctx.bot.send_message(
                                cid,
                                f"🔻 <b>{b.upper()} · {_esc(coin)}</b> drawdown "
                                f"<b>{pct:.2f}%</b> @ {cur:.4f}",
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception as e:
                            log.debug(f"alert dd → {cid}: {e}")
            if pct >= PROFIT_THRESHOLD:
                key = (b, coin, 'profit')
                live_keys.add(key)
                if not _alert_state.get(key):
                    _alert_state[key] = True
                    for cid in ALLOWED_CHAT_IDS:
                        try:
                            await ctx.bot.send_dice(cid, emoji='🎰')
                            await ctx.bot.send_message(
                                cid,
                                f"🎯 <b>{b.upper()} · {_esc(coin)}</b> profit "
                                f"<b>{pct:+.2f}%</b> @ {cur:.4f}",
                                parse_mode=ParseMode.HTML,
                            )
                        except Exception as e:
                            log.debug(f"alert profit → {cid}: {e}")
        for key in list(_alert_state.keys()):
            if key[0] == b and key not in live_keys:
                _alert_state.pop(key, None)


async def job_streak_check(ctx: ContextTypes.DEFAULT_TYPE):
    """Kiểm tra loss streak mỗi 5 phút — cảnh báo nếu thua liên tiếp ≥ N lệnh."""
    for b in _BOTS:
        d = await clients.analytics(b)
        if not d:
            continue
        recent = (d.get('recent') or [])[:LOSS_STREAK_THRESHOLD + 2]
        if len(recent) < LOSS_STREAK_THRESHOLD:
            continue
        streak = 0
        for t in recent:
            net = t.get('net_pnl') or t.get('pnl') or 0
            if net < 0:
                streak += 1
            else:
                break
        if streak >= LOSS_STREAK_THRESHOLD and not _streak_alerted.get(b):
            _streak_alerted[b] = True
            streak_pnl = sum(
                (t.get('net_pnl') or t.get('pnl') or 0)
                for t in recent[:streak]
            )
            await _send_all(ctx,
                f"⚠ <b>{b.upper()}</b> — {streak} lệnh thua liên tiếp\n"
                f"Net loss chuỗi: <b>{streak_pnl:+.2f}$</b>\n"
                f"Cân nhắc kiểm tra chiến lược hoặc dừng bot tạm thời."
            )
        elif streak < LOSS_STREAK_THRESHOLD:
            _streak_alerted[b] = False


async def job_daily(ctx: ContextTypes.DEFAULT_TYPE):
    """22:00 VN hàng ngày — gửi báo cáo PnL (net, sau fee) cho mọi chat."""
    from datetime import timezone, timedelta, time as _time
    vn_tz    = timezone(timedelta(hours=7))
    today_vn = datetime.now(vn_tz).date()
    start_ts = datetime.combine(today_vn, _time.min, tzinfo=vn_tz).timestamp()
    out = ["📅 <b>Báo cáo cuối ngày</b> (VN time)"]
    for b in _BOTS:
        d = await clients.analytics(b)
        recent = (d or {}).get('recent') or []
        today_trades = [t for t in recent if (t.get('close_ts') or 0) >= start_ts]
        n     = len(today_trades)
        wins  = sum(1 for t in today_trades if (t.get('net_pnl') or t.get('pnl') or 0) > 0)
        gross = sum((t.get('total_pnl') or t.get('pnl') or 0) for t in today_trades)
        fee   = sum((t.get('fee') or 0) for t in today_trades)
        net   = sum((t.get('net_pnl') or t.get('pnl') or 0) for t in today_trades)
        out.append(
            f"\n<b>{b.upper()}</b> · {n}T · {wins}W/{n - wins}L\n"
            f"Gross {gross:+.2f}$ · Fee -{abs(fee):.2f}$ · Net <b>{net:+.2f}$</b>"
        )
    await _send_all(ctx, '\n'.join(out))
