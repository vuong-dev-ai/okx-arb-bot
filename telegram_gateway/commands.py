"""Async command handlers cho gateway."""
import asyncio
import json
import logging
import sys
import urllib.parse
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import clients
from config import (
    ALLOWED_CHAT_IDS,
    DD_THRESHOLD, PROFIT_THRESHOLD,
    DASHBOARD_URL,
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


# ════════════════════ HELPERS ════════════════════
_BOTS = ('arb',)


def _bot_arg(args, default: Optional[str] = None) -> Optional[str]:
    """Lấy bot từ args[0]. Trả None nếu không hợp lệ và không có default."""
    if args:
        v = args[0].lower().strip()
        if v in _BOTS:
            return v
        if v == 'all':
            return 'all'
    return default


def _esc(s) -> str:
    s = '' if s is None else str(s)
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _plain_chunks(text: str, max_len: int = 3900):
    remaining = text
    while len(remaining) > max_len:
        cut = remaining.rfind("\n", 0, max_len)
        if cut < 1000:
            cut = max_len
        yield remaining[:cut].strip()
        remaining = remaining[cut:].strip()
    if remaining:
        yield remaining


def _signal_bot_module():
    bot_dir = Path(__file__).resolve().parents[1]
    bot_dir_s = str(bot_dir)
    if bot_dir_s not in sys.path:
        sys.path.insert(0, bot_dir_s)
    import okx_coin_analysis_bot

    return okx_coin_analysis_bot


async def _build_signal_report() -> str:
    signal_bot = _signal_bot_module()
    return await asyncio.to_thread(signal_bot.generate_report)


async def _reply_signal_report(message):
    waiting = await message.reply_text("🔎 Đang soi chart OKX... chờ mình bắt 3 tín hiệu sáng nhất.")
    try:
        report = await _build_signal_report()
    except Exception as e:
        log.exception(f"Build signal report fail: {e}")
        await waiting.edit_text(f"⚠ Không tạo được báo cáo tín hiệu: {e}")
        return

    chunks = list(_plain_chunks(report))
    if not chunks:
        await waiting.edit_text("Chưa có tín hiệu đủ dữ liệu.")
        return
    await waiting.edit_text(chunks[0])
    for chunk in chunks[1:]:
        await message.reply_text(chunk)


async def _build_accuracy_report() -> str:
    signal_bot = _signal_bot_module()
    # accuracy_report() có fetch mạng (chấm lại lệnh cũ) → chạy trong thread riêng.
    return await asyncio.to_thread(signal_bot.accuracy_report)


async def _reply_accuracy_report(message):
    waiting = await message.reply_text("📊 Đang chấm lại các dự đoán cũ và tính tỉ lệ đúng/sai...")
    try:
        report = await _build_accuracy_report()
    except Exception as e:
        log.exception(f"Build accuracy report fail: {e}")
        await waiting.edit_text(f"⚠ Không tạo được báo cáo tỉ lệ: {e}")
        return

    chunks = list(_plain_chunks(report))
    if not chunks:
        await waiting.edit_text("Chưa có dữ liệu tỉ lệ.")
        return
    await waiting.edit_text(chunks[0])
    for chunk in chunks[1:]:
        await message.reply_text(chunk)


def _format_status(bot: str, data: dict) -> str:
    if not data:
        return f"<b>{bot.upper()}</b>: ❌ không phản hồi"
    running   = '▶ chạy' if data.get('running') else '⏸ dừng'
    usdt      = data.get('usdt') or 0
    teq       = data.get('total_eq') or 0
    positions = data.get('positions') or []
    upd       = _esc(data.get('last_update', '-'))
    teq_s = f" · 💰 ${teq:,.2f}" if teq else ''
    lines = [
        f"<b>{bot.upper()}</b> · {running} · ⏱ {upd}",
        f"💵 Khả dụng ${usdt:,.2f}{teq_s}",
        f"📦 {len(positions)} vị thế",
    ]
    for p in positions[:10]:
        coin = _esc(p.get('coin'))
        pct  = p.get('pct') or 0
        net  = p.get('net_pnl') or 0
        npay = p.get('n_pay') or 0
        emoji = '🟢' if net >= 0 else '🔴'
        lines.append(
            f"  {emoji} <code>{coin}</code> net {net:+.2f}$ ({pct:+.2f}%) · {npay}x funding"
        )
    return '\n'.join(lines)


def _format_analytics(bot: str, data: dict) -> str:
    if not data:
        return f"<b>{bot.upper()}</b>: ❌ không có dữ liệu"
    g = data.get('global') or {}
    n       = g.get('n') or 0
    wr      = g.get('win_rate') or 0
    pf      = g.get('profit_factor') or 0
    avg_w   = g.get('avg_win') or 0
    avg_l   = g.get('avg_loss') or 0
    gross   = g.get('total_pnl') or 0
    fee     = g.get('total_fee') or 0
    net     = g.get('total_net_pnl') or gross
    lines = [
        f"📊 <b>{bot.upper()} stats</b>",
        f"Trades: {n} · Win rate: {wr:.1f}% · PF: {pf:.2f}",
        f"Avg win: {avg_w:+.2f}% · Avg loss: {avg_l:+.2f}%",
        f"Gross PnL: {gross:+.2f}$ · Fee: -{abs(fee):.2f}$",
        f"Net PnL: <b>{net:+.2f}$</b>",
    ]
    coins = (data.get('coins') or [])[:5]
    if coins:
        lines.append('\n<b>Top coins:</b>')
        for c in coins:
            net_c = c.get('total_net_pnl') or c.get('total_pnl') or 0
            lines.append(
                f"  <code>{_esc(c.get('coin'))}</code> · "
                f"{(c.get('n') or 0)}T · "
                f"WR {(c.get('win_rate') or 0):.0f}% · "
                f"net {net_c:+.2f}$"
            )
    return '\n'.join(lines)


def _equity_chart_url(points: list, title: str) -> Optional[str]:
    """Build quickchart.io URL cho equity curve.
    points = list of dicts: {'ts': float, 'cum_net'|'cum_net_pnl'|'cum_pnl': float}
    """
    if not points:
        return None
    labels = [datetime.fromtimestamp(p['ts']).strftime('%m-%d') for p in points]
    values = [round(
        p.get('cum_net') or p.get('cum_net_pnl') or p.get('cum_pnl') or 0, 2
    ) for p in points]
    spec = {
        "type": "line",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": title,
                "data": values,
                "borderColor": "rgb(40,167,69)",
                "backgroundColor": "rgba(40,167,69,0.15)",
                "fill": True,
                "tension": 0.25,
            }],
        },
        "options": {
            "plugins": {"title": {"display": True, "text": title}},
            "scales": {"y": {"title": {"display": True, "text": "Cum PnL ($)"}}},
        },
    }
    payload = urllib.parse.quote(json.dumps(spec, separators=(',', ':')))
    return f"https://quickchart.io/chart?c={payload}&w=720&h=360&bkg=white"


# ════════════════════ COMMANDS ════════════════════
@auth
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 <b>OKX Bot Gateway</b>\n"
        "Quản lý arb bot.\n\n"
        "Gõ /help để xem danh sách lệnh, hoặc /menu để mở bảng nút.\n"
        f"Whitelist: <code>{len(ALLOWED_CHAT_IDS)}</code> chat IDs được cấp quyền."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


@auth
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>Lệnh khả dụng</b>\n\n"
        "<b>📊 Xem trạng thái</b>\n"
        "/status — bot + vị thế\n"
        "/positions — chi tiết vị thế\n"
        "/balance — USDT khả dụng + tổng tài sản\n"
        "/opps — cơ hội funding đang scan\n"
        "/profit — tổng đã lời (chốt + đang mở)\n"
        "/stats — win rate, PF, net PnL, fee\n"
        "/equity — biểu đồ equity (net)\n"
        "/daily — báo cáo PnL hôm nay (net, sau fee)\n"
        "/summary — báo cáo 7 ngày + fee breakdown\n\n"
        "<b>📌 Tín hiệu</b>\n"
        "/signals — top 3 coin Long/Short tiềm năng\n"
        "/accuracy — tỉ lệ đúng/sai các dự đoán đã đưa\n\n"
        "<b>⚙ Điều khiển</b>\n"
        "/start_arb · /stop_arb\n"
        "/close &lt;coin&gt; — VD <code>/close BTC</code>\n"
        "/close_all\n"
        "/sync — reconcile với OKX\n\n"
        "<b>🔧 Khác</b>\n"
        "/menu — bảng nút inline\n"
        "/dashboard — link web dashboard\n"
        "/ping — check gateway còn sống"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


def _menu_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("📉 Status",         callback_data="status:arb"),
         InlineKeyboardButton("📦 Vị thế",         callback_data="positions:arb")],
        [InlineKeyboardButton("💰 Số dư",          callback_data="balance:arb"),
         InlineKeyboardButton("🧲 Cơ hội funding", callback_data="opps:arb")],
        [InlineKeyboardButton("📈 Tổng đã lời",    callback_data="profit:arb"),
         InlineKeyboardButton("💹 Equity chart",   callback_data="equity:arb")],
        [InlineKeyboardButton("📅 Hôm nay",        callback_data="daily:arb"),
         InlineKeyboardButton("📆 7 ngày",         callback_data="summary:arb")],
        [InlineKeyboardButton("🎯 Top 3 signals",  callback_data="signals:okx"),
         InlineKeyboardButton("📊 Tỉ lệ đúng/sai", callback_data="accuracy:okx")],
        [InlineKeyboardButton("▶ Start bot",       callback_data="start:arb"),
         InlineKeyboardButton("⏸ Stop bot",        callback_data="stop:arb")],
        [InlineKeyboardButton("🔄 Sync OKX",       callback_data="sync:arb"),
         InlineKeyboardButton("🧹 Đóng tất cả",    callback_data="closeall:arb")],
    ]
    if DASHBOARD_URL:
        kb.append([InlineKeyboardButton("🌐 Mở Dashboard", url=DASHBOARD_URL)])
    return InlineKeyboardMarkup(kb)


@auth
async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Bảng điều khiển</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=_menu_keyboard(),
    )


@auth
async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _reply_signal_report(update.effective_message)


@auth
async def cmd_accuracy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _reply_accuracy_report(update.effective_message)


@auth
async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = ["🏓 pong"]
    for b in _BOTS:
        h = await clients.health(b)
        ok = '🟢' if h and h.get('ok') else '🔴'
        parts.append(f"{ok} {b}")
    await update.message.reply_text(' · '.join(parts))


@auth
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot = _bot_arg(ctx.args, default='all')
    targets = _BOTS if bot == 'all' else (bot,)
    blocks = []
    for b in targets:
        data = await clients.status(b)
        blocks.append(_format_status(b, data))
    await update.message.reply_text('\n\n'.join(blocks), parse_mode=ParseMode.HTML)


async def _positions_text(targets=_BOTS) -> str:
    out = []
    for b in targets:
        data = await clients.status(b)
        ps = (data or {}).get('positions') or []
        out.append(f"<b>{b.upper()}</b> · {len(ps)} vị thế")
        if not ps:
            out.append("  (trống)")
        for p in ps:
            coin  = _esc(p.get('coin'))
            pct   = p.get('pct') or 0
            entry = p.get('entry_price') or 0
            cur   = p.get('cur_price') or 0
            net   = p.get('net_pnl') or 0
            fund  = p.get('funding_pnl') or 0
            npay  = p.get('n_pay') or 0
            emoji = '🟢' if net >= 0 else '🔴'
            out.append(
                f"  {emoji} <code>{coin}</code> net {net:+.2f}$ ({pct:+.2f}%)\n"
                f"     entry={entry:.4f} · cur={cur:.4f} · funding {fund:+.2f}$ ({npay}x)"
            )
    return '\n'.join(out)


@auth
async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot = _bot_arg(ctx.args, default='all')
    targets = _BOTS if bot == 'all' else (bot,)
    await update.message.reply_text(await _positions_text(targets), parse_mode=ParseMode.HTML)


async def _balance_text() -> str:
    out = []
    for b in _BOTS:
        d = await clients.status(b)
        usdt = (d or {}).get('usdt') or 0
        teq  = (d or {}).get('total_eq') or 0
        out.append(f"<b>{b.upper()}</b>\n💵 USDT khả dụng: ${usdt:,.2f}")
        if teq:
            out.append(f"💰 Tổng tài sản: <b>${teq:,.2f}</b>\n"
                       f"<i>(gồm spot chân long + margin swap)</i>")
    return '\n'.join(out)


@auth
async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await _balance_text(), parse_mode=ParseMode.HTML)


async def _opps_text() -> str:
    d = await clients.status('arb')
    if not d:
        return "<b>ARB</b>: ❌ không phản hồi"
    opps = d.get('opps') or []
    min_rate = d.get('min_rate') or 0.0012
    held = {p.get('coin') for p in (d.get('positions') or [])}
    lines = [f"🧲 <b>Cơ hội funding</b> (ngưỡng vào ≥ {min_rate*100:.2f}%/8h)"]
    if not opps:
        lines.append("(chưa có dữ liệu scan)")
    for o in opps[:10]:
        coin = o.get('coin')
        rate = o.get('rate') or 0
        apy  = o.get('apy') or 0
        if coin in held:
            mark = '📦'   # đã giữ vị thế coin này
        elif rate >= min_rate:
            mark = '🟢'   # đủ ngưỡng, có thể vào
        else:
            mark = '⚪'
        lines.append(f"  {mark} <code>{_esc(coin)}</code> {rate*100:.4f}%/8h · APY {apy:.1f}%")
    lines.append("\n📦 đang giữ · 🟢 đủ ngưỡng · ⚪ dưới ngưỡng")
    return '\n'.join(lines)


@auth
async def cmd_opps(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await _opps_text(), parse_mode=ParseMode.HTML)


async def _profit_text() -> str:
    d  = await clients.analytics('arb')
    st = await clients.status('arb')
    g  = (d or {}).get('global') or {}
    n_closed   = g.get('n') or 0
    net_closed = g.get('total_net_pnl') or g.get('total_pnl') or 0
    ps = (st or {}).get('positions') or []
    net_open = sum((p.get('net_pnl') or 0) for p in ps)
    teq = (st or {}).get('total_eq') or 0
    lines = [
        "💰 <b>Tổng đã lời (ARB)</b>",
        f"Đã chốt ({n_closed} lệnh): <b>{net_closed:+,.2f}$</b>",
        f"Đang mở ({len(ps)} vị thế): {net_open:+,.2f}$",
        f"━━━━━━━━━━━━",
        f"Tổng: <b>{net_closed + net_open:+,.2f}$</b>",
    ]
    if teq:
        lines.append(f"Tổng tài sản: ${teq:,.2f}")
    return '\n'.join(lines)


@auth
async def cmd_profit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await _profit_text(), parse_mode=ParseMode.HTML)


@auth
async def cmd_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if DASHBOARD_URL:
        await update.message.reply_text(
            f"🌐 Dashboard: {DASHBOARD_URL}\n<i>(đăng nhập basic-auth)</i>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("Chưa cấu hình GATEWAY_DASHBOARD_URL.")


@auth
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot = _bot_arg(ctx.args, default='all')
    targets = _BOTS if bot == 'all' else (bot,)
    blocks = []
    for b in targets:
        data = await clients.analytics(b)
        blocks.append(_format_analytics(b, data))
    await update.message.reply_text('\n\n'.join(blocks), parse_mode=ParseMode.HTML)


@auth
async def cmd_equity(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot = 'arb'
    data = await clients.analytics(bot)
    points = (data or {}).get('equity') or []
    if not points:
        await update.message.reply_text(
            f"<b>{bot.upper()}</b>: chưa có equity curve",
            parse_mode=ParseMode.HTML,
        )
        return
    url = _equity_chart_url(points, f"{bot.upper()} Equity Curve")
    if not url:
        await update.message.reply_text("⚠ Không build được chart URL")
        return
    await update.message.reply_photo(
        photo=url,
        caption=f"💹 <b>{bot.upper()}</b> · {len(points)} closed trades",
        parse_mode=ParseMode.HTML,
    )


async def _ctrl(update: Update, action: str, bot: str):
    fn = {'start': clients.start_bot, 'stop': clients.stop_bot,
          'sync':  clients.sync}.get(action)
    if not fn:
        return
    r = await fn(bot)
    if not r:
        await update.effective_message.reply_text(
            f"⚠ <b>{bot.upper()}</b> không phản hồi",
            parse_mode=ParseMode.HTML,
        )
        return
    ok = r.get('ok')
    msg = r.get('msg') or ('OK' if ok else 'fail')
    icon = '✅' if ok else '⚠'
    await update.effective_message.reply_text(
        f"{icon} <b>{bot.upper()}</b> {action}: {_esc(msg)}",
        parse_mode=ParseMode.HTML,
    )


def _confirm_kb(action: str, bot: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Xác nhận", callback_data=f"{action}ok:{bot}"),
        InlineKeyboardButton("❌ Huỷ",      callback_data="cancel:_"),
    ]])


_CONFIRM_TEXT = {
    'stop':     "⚠ <b>Dừng bot?</b>\nBot sẽ tự đóng TẤT CẢ vị thế đang mở.",
    'closeall': "⚠ <b>Đóng tất cả vị thế?</b>\nMọi vị thế delta-neutral sẽ được đóng (cả spot lẫn swap).",
}


async def _ask_confirm(message, action: str, bot: str = 'arb'):
    await message.reply_text(
        _CONFIRM_TEXT[action],
        parse_mode=ParseMode.HTML,
        reply_markup=_confirm_kb(action, bot),
    )


@auth
async def cmd_start_arb(update, ctx):   await _ctrl(update, 'start', 'arb')
@auth
async def cmd_stop_arb(update, ctx):    await _ask_confirm(update.effective_message, 'stop')


@auth
async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text(
            "Cú pháp: <code>/close &lt;COIN&gt;</code>\n"
            "VD: <code>/close BTC</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    bot = 'arb'
    coin = ctx.args[0].upper().strip()
    r = await clients.close_coin(bot, coin)
    if not r:
        await update.message.reply_text(f"⚠ {bot.upper()} không phản hồi")
        return
    if r.get('ok'):
        await update.message.reply_text(
            f"⏳ <b>{bot.upper()}</b> đã NHẬN lệnh đóng <code>{_esc(coin)}</code> — đang xử lý nền.\n"
            f"Sẽ báo lại khi đóng XONG (✅) hoặc khi KHÔNG đóng được (🚨, vd market đóng cửa).\n"
            f"<i>Tin này KHÔNG có nghĩa đã đóng.</i>",
            parse_mode=ParseMode.HTML,
        )
    else:
        msg = r.get('msg') or 'fail'
        await update.message.reply_text(
            f"⚠ <b>{bot.upper()}</b> close <code>{_esc(coin)}</code>: {_esc(msg)}",
            parse_mode=ParseMode.HTML,
        )


async def _do_close_all(message, bot: str = 'arb'):
    r = await clients.close_all(bot)
    if not r:
        await message.reply_text(f"⚠ {bot.upper()} không phản hồi")
        return
    n = r.get('closed', 0)
    await message.reply_text(
        f"⏳ <b>{bot.upper()}</b> đã NHẬN lệnh đóng {n} vị thế — đang xử lý nền.\n"
        f"Sẽ báo riêng từng coin khi đóng XONG (✅) hoặc khi KHÔNG đóng được (🚨, vd market đóng cửa).\n"
        f"<i>Tin này KHÔNG có nghĩa đã đóng — kiểm tra /status để xác nhận.</i>",
        parse_mode=ParseMode.HTML,
    )


@auth
async def cmd_close_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _ask_confirm(update.effective_message, 'closeall')


@auth
async def cmd_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot = _bot_arg(ctx.args, default='all')
    targets = _BOTS if bot == 'all' else (bot,)
    for b in targets:
        await _ctrl(update, 'sync', b)


async def _daily_text() -> str:
    from datetime import timezone, timedelta, time as _time
    vn_tz    = timezone(timedelta(hours=7))
    today_vn = datetime.now(vn_tz).date()
    start_ts = datetime.combine(today_vn, _time.min, tzinfo=vn_tz).timestamp()
    out = ["📅 <b>Báo cáo hôm nay</b> (VN time)"]
    for b in _BOTS:
        d = await clients.analytics(b)
        recent = (d or {}).get('recent') or []
        today_trades = [t for t in recent if (t.get('close_ts') or 0) >= start_ts]
        n    = len(today_trades)
        wins = sum(1 for t in today_trades if (t.get('net_pnl') or t.get('pnl') or 0) > 0)
        gross = sum((t.get('total_pnl') or t.get('pnl') or 0) for t in today_trades)
        fee   = sum((t.get('fee') or 0) for t in today_trades)
        net   = sum((t.get('net_pnl') or t.get('pnl') or 0) for t in today_trades)
        out.append(f"\n<b>{b.upper()}</b> · {n} trades · {wins}W/{n - wins}L")
        out.append(f"Gross {gross:+.2f}$ · Fee -{abs(fee):.2f}$ · Net <b>{net:+.2f}$</b>")
        for t in today_trades[:5]:
            coin = _esc(t.get('coin'))
            net_t = t.get('net_pnl') or t.get('pnl') or 0
            roi   = t.get('net_roi_pct') or t.get('roi_pct') or 0
            emo   = '🟢' if net_t >= 0 else '🔴'
            out.append(f"  {emo} <code>{coin}</code> net {net_t:+.2f}$ ({roi:+.2f}%)")
    return '\n'.join(out)


@auth
async def cmd_daily(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await _daily_text(), parse_mode=ParseMode.HTML)


async def _summary_text() -> str:
    import time as _t
    cutoff = _t.time() - 7 * 86400
    out = ["📆 <b>Báo cáo 7 ngày</b>"]
    for b in _BOTS:
        d = await clients.analytics(b)
        recent = (d or {}).get('recent') or []
        week  = [t for t in recent if (t.get('close_ts') or 0) >= cutoff]
        n     = len(week)
        wins  = sum(1 for t in week if (t.get('net_pnl') or t.get('pnl') or 0) > 0)
        gross = sum((t.get('total_pnl') or t.get('pnl') or 0) for t in week)
        fee   = sum((t.get('fee') or 0) for t in week)
        net   = sum((t.get('net_pnl') or t.get('pnl') or 0) for t in week)
        wr    = (wins / n * 100) if n else 0
        # Loss streak
        streak = 0
        for t in week:
            if (t.get('net_pnl') or t.get('pnl') or 0) < 0:
                streak += 1
            else:
                break
        streak_s = f" · streak -{streak}❌" if streak >= 2 else ""
        out.append(
            f"\n<b>{b.upper()}</b> · {n}T · WR {wr:.0f}%{streak_s}\n"
            f"Gross {gross:+.2f}$ · Fee -{abs(fee):.2f}$ · Net <b>{net:+.2f}$</b>"
        )
    return '\n'.join(out)


@auth
async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await _summary_text(), parse_mode=ParseMode.HTML)


# ════════════════════ CALLBACK (inline keyboard) ════════════════════
@auth
async def cb_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        action, bot = q.data.split(':', 1)
    except ValueError:
        return
    if action == 'signals':
        await _reply_signal_report(q.message)
    elif action == 'accuracy':
        await _reply_accuracy_report(q.message)
    elif action == 'status':
        data = await clients.status(bot)
        await q.message.reply_text(_format_status(bot, data), parse_mode=ParseMode.HTML)
    elif action == 'positions':
        await q.message.reply_text(await _positions_text(), parse_mode=ParseMode.HTML)
    elif action == 'balance':
        await q.message.reply_text(await _balance_text(), parse_mode=ParseMode.HTML)
    elif action == 'opps':
        await q.message.reply_text(await _opps_text(), parse_mode=ParseMode.HTML)
    elif action == 'profit':
        await q.message.reply_text(await _profit_text(), parse_mode=ParseMode.HTML)
    elif action == 'daily':
        await q.message.reply_text(await _daily_text(), parse_mode=ParseMode.HTML)
    elif action == 'summary':
        await q.message.reply_text(await _summary_text(), parse_mode=ParseMode.HTML)
    elif action == 'stats':
        data = await clients.analytics(bot)
        await q.message.reply_text(_format_analytics(bot, data), parse_mode=ParseMode.HTML)
    elif action == 'equity':
        data = await clients.analytics(bot)
        points = (data or {}).get('equity') or []
        url = _equity_chart_url(points, f"{bot.upper()} Equity Curve") if points else None
        if url:
            await q.message.reply_photo(photo=url,
                caption=f"💹 <b>{bot.upper()}</b>", parse_mode=ParseMode.HTML)
        else:
            await q.message.reply_text(f"{bot.upper()}: chưa có equity")
    # ── Hành động nguy hiểm: hỏi xác nhận trước ──
    elif action in ('stop', 'closeall'):
        await _ask_confirm(q.message, action, bot)
    elif action == 'cancel':
        await q.edit_message_text("❌ Đã huỷ.")
    elif action == 'stopok':
        await q.edit_message_text("⏸ Đang dừng bot...")
        await _ctrl(update, 'stop', bot)
    elif action == 'closeallok':
        await q.edit_message_text("🧹 Đang gửi lệnh đóng tất cả...")
        await _do_close_all(q.message, bot)
    elif action in ('start', 'sync'):
        await _ctrl(update, action, bot)


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
