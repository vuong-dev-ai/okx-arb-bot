"""Async command handlers cho gateway."""
import json
import logging
import urllib.parse
from datetime import datetime
from functools import wraps
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import clients
from config import (
    ALLOWED_CHAT_IDS,
    DD_THRESHOLD, PROFIT_THRESHOLD,
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
_BOTS = ('trend', 'arb')


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


def _format_status(bot: str, data: dict) -> str:
    if not data:
        return f"<b>{bot.upper()}</b>: ❌ không phản hồi"
    running   = '▶ chạy' if data.get('running') else '⏸ dừng'
    usdt      = data.get('usdt') or 0
    positions = data.get('positions') or []
    upd       = _esc(data.get('last_update', '-'))
    lines = [
        f"<b>{bot.upper()}</b> · {running} · 💵 ${usdt:.2f} · ⏱ {upd}",
        f"📦 {len(positions)} vị thế",
    ]
    for p in positions[:10]:
        coin = _esc(p.get('coin'))
        side = _esc(p.get('side', ''))
        pct  = p.get('pct') or 0
        cur  = p.get('cur_price') or 0
        emoji = '🟢' if pct >= 0 else '🔴'
        lines.append(
            f"  {emoji} <code>{coin}</code> {side} {pct:+.2f}% @ {cur:.4f}"
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
        "Quản lý cả 2 bot (arb + trend) trong 1 nơi.\n\n"
        "Gõ /help để xem danh sách lệnh, hoặc /menu để mở bảng nút.\n"
        f"Whitelist: <code>{len(ALLOWED_CHAT_IDS)}</code> chat IDs được cấp quyền."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


@auth
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 <b>Lệnh khả dụng</b>\n\n"
        "<b>📊 Xem trạng thái</b>\n"
        "/status [trend|arb|all] — bot + vị thế\n"
        "/positions [trend|arb] — chi tiết vị thế\n"
        "/balance — USDT khả dụng\n"
        "/stats [trend|arb] — win rate, PF, net PnL, fee\n"
        "/equity [trend|arb] — biểu đồ equity (net)\n"
        "/daily — báo cáo PnL hôm nay (net, sau fee)\n"
        "/summary — báo cáo 7 ngày + fee breakdown\n\n"
        "<b>⚙ Điều khiển</b>\n"
        "/start_trend · /stop_trend\n"
        "/start_arb · /stop_arb\n"
        "/close &lt;bot&gt; &lt;coin&gt; — VD <code>/close trend BTC</code>\n"
        "/close_all &lt;bot&gt;\n"
        "/sync &lt;bot&gt; — reconcile với OKX\n\n"
        "<b>🔬 Backtest (trend bot)</b>\n"
        "/backtest [candles] [balance] — chạy backtest mới\n"
        "/backtest status — xem kết quả\n\n"
        "<b>🔧 Khác</b>\n"
        "/menu — bảng nút inline\n"
        "/ping — check gateway còn sống"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@auth
async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("📈 Trend status",  callback_data="status:trend"),
         InlineKeyboardButton("📉 Arb status",    callback_data="status:arb")],
        [InlineKeyboardButton("📊 Trend stats",   callback_data="stats:trend"),
         InlineKeyboardButton("📊 Arb stats",     callback_data="stats:arb")],
        [InlineKeyboardButton("💹 Trend equity",  callback_data="equity:trend"),
         InlineKeyboardButton("💹 Arb equity",    callback_data="equity:arb")],
        [InlineKeyboardButton("▶ Start trend",    callback_data="start:trend"),
         InlineKeyboardButton("⏸ Stop trend",     callback_data="stop:trend")],
        [InlineKeyboardButton("▶ Start arb",      callback_data="start:arb"),
         InlineKeyboardButton("⏸ Stop arb",       callback_data="stop:arb")],
        [InlineKeyboardButton("🔄 Sync trend",    callback_data="sync:trend"),
         InlineKeyboardButton("🔄 Sync arb",      callback_data="sync:arb")],
    ]
    await update.message.reply_text(
        "<b>Bảng điều khiển</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb),
    )


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


@auth
async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot = _bot_arg(ctx.args, default='all')
    targets = _BOTS if bot == 'all' else (bot,)
    out = []
    for b in targets:
        data = await clients.status(b)
        ps = (data or {}).get('positions') or []
        out.append(f"<b>{b.upper()}</b> · {len(ps)} vị thế")
        if not ps:
            out.append("  (trống)")
        for p in ps:
            coin = _esc(p.get('coin'))
            side = _esc(p.get('side', ''))
            pct  = p.get('pct') or 0
            entry = p.get('entry_price') or 0
            cur   = p.get('cur_price') or 0
            stop  = p.get('stop_price') or 0
            emoji = '🟢' if pct >= 0 else '🔴'
            out.append(
                f"  {emoji} <code>{coin}</code> {side} {pct:+.2f}%\n"
                f"     entry={entry:.4f} · cur={cur:.4f} · stop={stop:.4f}"
            )
    await update.message.reply_text('\n'.join(out), parse_mode=ParseMode.HTML)


@auth
async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    out = []
    for b in _BOTS:
        d = await clients.status(b)
        usdt = (d or {}).get('usdt') or 0
        out.append(f"<b>{b.upper()}</b>: ${usdt:.2f}")
    await update.message.reply_text(' · '.join(out), parse_mode=ParseMode.HTML)


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
    bot = _bot_arg(ctx.args, default='trend') or 'trend'
    if bot == 'all':
        bot = 'trend'
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


@auth
async def cmd_start_trend(update, ctx): await _ctrl(update, 'start', 'trend')
@auth
async def cmd_stop_trend(update, ctx):  await _ctrl(update, 'stop',  'trend')
@auth
async def cmd_start_arb(update, ctx):   await _ctrl(update, 'start', 'arb')
@auth
async def cmd_stop_arb(update, ctx):    await _ctrl(update, 'stop',  'arb')


@auth
async def cmd_close(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Cú pháp: <code>/close &lt;trend|arb&gt; &lt;COIN&gt;</code>\n"
            "VD: <code>/close trend BTC</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    bot = ctx.args[0].lower().strip()
    if bot not in _BOTS:
        await update.message.reply_text(f"Bot không hợp lệ: {bot} (chỉ: trend, arb)")
        return
    coin = ctx.args[1].upper().strip()
    r = await clients.close_coin(bot, coin)
    if not r:
        await update.message.reply_text(f"⚠ {bot.upper()} không phản hồi")
        return
    icon = '✅' if r.get('ok') else '⚠'
    msg = r.get('msg') or ('Đang đóng' if r.get('ok') else 'fail')
    await update.message.reply_text(
        f"{icon} <b>{bot.upper()}</b> close <code>{_esc(coin)}</code>: {_esc(msg)}",
        parse_mode=ParseMode.HTML,
    )


@auth
async def cmd_close_all(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Cú pháp: <code>/close_all &lt;trend|arb&gt;</code>",
                                        parse_mode=ParseMode.HTML)
        return
    bot = ctx.args[0].lower().strip()
    if bot not in _BOTS:
        await update.message.reply_text(f"Bot không hợp lệ: {bot}")
        return
    r = await clients.close_all(bot)
    if not r:
        await update.message.reply_text(f"⚠ {bot.upper()} không phản hồi")
        return
    n = r.get('closed', 0)
    await update.message.reply_text(
        f"✅ <b>{bot.upper()}</b> đang đóng {n} vị thế",
        parse_mode=ParseMode.HTML,
    )


@auth
async def cmd_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bot = _bot_arg(ctx.args, default='all')
    targets = _BOTS if bot == 'all' else (bot,)
    for b in targets:
        await _ctrl(update, 'sync', b)


@auth
async def cmd_backtest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /backtest               — chạy backtest mặc định (600 nến, $10,000)
    /backtest status        — xem trạng thái / kết quả backtest đang chạy
    /backtest 300 5000      — 300 nến, vốn $5,000
    """
    args = ctx.args or []

    # /backtest status
    if args and args[0].lower() == 'status':
        d = await clients.backtest_status()
        if not d:
            await update.message.reply_text("⚠ Trend bot không phản hồi")
            return
        status = d.get('status', 'unknown')
        prog   = d.get('progress')
        result = d.get('result')
        if status == 'running':
            pct = 0
            label = 'Đang chạy...'
            if prog and prog.get('total', 0) > 0:
                pct = int(prog['done'] / prog['total'] * 100)
                stage = {'fetch': 'Tải data', 'simulate': 'Simulation'}.get(prog.get('stage', ''), prog.get('stage', ''))
                label = f"{stage}: {prog['done']}/{prog['total']} ({pct}%)"
            await update.message.reply_text(
                f"⏳ <b>Backtest đang chạy</b>\n{label}",
                parse_mode=ParseMode.HTML
            )
        elif status == 'done' and result:
            r = result
            p = r.get('params', {})
            days = round((p.get('total_bars', 0) * 4) / 24)
            net_sign = '+' if r.get('roi_pct', 0) >= 0 else ''
            text = (
                f"✅ <b>Backtest xong</b> · {days} ngày · ${p.get('initial_balance', 10000):,.0f} vốn\n\n"
                f"Trades: {r.get('n_trades', 0)} · Win: {r.get('win_rate', 0)}%\n"
                f"ROI: <b>{net_sign}{r.get('roi_pct', 0)}%</b> · PF: {r.get('profit_factor', 0)}\n"
                f"Max DD: {r.get('max_drawdown', 0)}% · Sharpe: {r.get('sharpe', 0)}\n"
                f"Expectancy: ${r.get('expectancy', 0):.2f}/trade"
            )
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        elif status == 'error':
            await update.message.reply_text(
                f"❌ Backtest lỗi: {_esc(d.get('error', '?'))}",
                parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text(f"Status: {_esc(status)} (chưa có kết quả)")
        return

    # /backtest [candles] [balance]
    candles = 600
    balance = 10000.0
    try:
        if len(args) >= 1: candles = int(args[0])
        if len(args) >= 2: balance = float(args[1])
    except ValueError:
        await update.message.reply_text(
            "Cú pháp: <code>/backtest [candles] [balance]</code>\n"
            "VD: <code>/backtest 300 5000</code> hoặc <code>/backtest status</code>",
            parse_mode=ParseMode.HTML
        )
        return

    days = round(candles * 4 / 24)
    r = await clients.run_backtest(candles=candles, balance=balance)
    if not r:
        await update.message.reply_text("⚠ Trend bot không phản hồi")
        return
    if r.get('ok'):
        await update.message.reply_text(
            f"⏳ <b>Backtest bắt đầu</b> · {days} ngày · ${balance:,.0f} vốn\n"
            f"Dùng <code>/backtest status</code> để xem kết quả.",
            parse_mode=ParseMode.HTML
        )
    else:
        await update.message.reply_text(f"⚠ {_esc(r.get('msg', 'fail'))}")


@auth
async def cmd_daily(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        gross = sum((t.get('pnl') or 0) for t in today_trades)
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
    await update.message.reply_text('\n'.join(out), parse_mode=ParseMode.HTML)


@auth
async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import time as _t
    cutoff = _t.time() - 7 * 86400
    out = ["📆 <b>Báo cáo 7 ngày</b>"]
    for b in _BOTS:
        d = await clients.analytics(b)
        recent = (d or {}).get('recent') or []
        week  = [t for t in recent if (t.get('close_ts') or 0) >= cutoff]
        n     = len(week)
        wins  = sum(1 for t in week if (t.get('net_pnl') or t.get('pnl') or 0) > 0)
        gross = sum((t.get('pnl') or 0) for t in week)
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
    await update.message.reply_text('\n'.join(out), parse_mode=ParseMode.HTML)


# ════════════════════ CALLBACK (inline keyboard) ════════════════════
@auth
async def cb_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        action, bot = q.data.split(':', 1)
    except ValueError:
        return
    if action == 'status':
        data = await clients.status(bot)
        await q.message.reply_text(_format_status(bot, data), parse_mode=ParseMode.HTML)
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
    elif action in ('start', 'stop', 'sync'):
        await _ctrl(update, action, bot)


# ════════════════════ JOBS ════════════════════
# Track alert state để không spam (key: (bot, coin, type) → bool)
_alert_state: dict = {}
# Bot offline tracking
_bot_fail_count: dict  = {b: 0 for b in ('trend', 'arb')}
_bot_offline_alerted: dict = {b: False for b in ('trend', 'arb')}
# Loss streak tracking
_streak_alerted: dict  = {b: False for b in ('trend', 'arb')}
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
        gross = sum((t.get('pnl') or 0) for t in today_trades)
        fee   = sum((t.get('fee') or 0) for t in today_trades)
        net   = sum((t.get('net_pnl') or t.get('pnl') or 0) for t in today_trades)
        out.append(
            f"\n<b>{b.upper()}</b> · {n}T · {wins}W/{n - wins}L\n"
            f"Gross {gross:+.2f}$ · Fee -{abs(fee):.2f}$ · Net <b>{net:+.2f}$</b>"
        )
    await _send_all(ctx, '\n'.join(out))
