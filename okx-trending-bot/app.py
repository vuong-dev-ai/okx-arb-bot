"""
OKX Trend-Following Bot — Web dashboard + bot loop.

Vòng lặp:
  - Mỗi 60s: refresh giá hiện tại của open positions → update trailing stop, exit nếu hit
  - Mỗi khi có nến mới đóng (theo TIMEFRAME): scan watchlist, tính signal, mở vị thế mới nếu đủ slot
"""
import os, sys, time, threading, json, traceback, logging, hmac
from logging.handlers import RotatingFileHandler
from datetime import datetime

_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot.log')
_file_handler = RotatingFileHandler(_log_path, maxBytes=5_000_000, backupCount=3, encoding='utf-8')
_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
logging.getLogger().addHandler(_file_handler)
logging.getLogger().setLevel(logging.INFO)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('werkzeug').setLevel(logging.WARNING)
_botlog = logging.getLogger('bot')

# UTF-8 stdout để in tiếng Việt khi redirect
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

from flask import Flask, jsonify, render_template, Response, stream_with_context, request, abort

from strategy import (
    SCAN_COINS, TIMEFRAME, ADX_MIN, EMA_FAST, EMA_SLOW,
    RISK_PCT, MIN_USDT, LEVERAGE,
    PARTIAL_TP_ATR_MULT, ATR_PCT_MAX,
    MAX_LOSS_PCT, ALGO_AMEND_MIN_MOVE,
    get_candles, evaluate, get_available_usdt, get_last_price,
    calc_position_size, open_position, close_position, partial_close_position,
    update_pnl_and_stop, last_closed_candle_ts,
    get_okx_swap_positions, get_trend_1d, is_correlated,
    amend_stop_algo, cancel_stop_algo,
)
import analytics
import notifier
import backtest as bt
from config import SIMULATED
from ws_ticker import TickerWS

# WebSocket realtime ticker — subscribe tất cả SWAP của watchlist
_ws = TickerWS([f"{c}-USDT-SWAP" for c in SCAN_COINS], simulated=SIMULATED, max_age=15)


def _get_price(swap_id):
    """Ưu tiên giá từ WebSocket cache (sub-second), fallback REST."""
    p = _ws.get_price(swap_id)
    if p is not None:
        return p
    return get_last_price(swap_id)

analytics.init()
analytics.backfill_fees()  # cập nhật fee cho trade cũ trong DB

app = Flask(__name__)

# ── Bảo vệ endpoint đổi-trạng-thái (audit critical: /api/* POST không auth) ──
# Mặc định bind 127.0.0.1 → vào qua SSH tunnel (request đến như loopback, tin cậy).
# Nếu BIND_HOST=0.0.0.0 (public): mọi POST KHÔNG-loopback bắt buộc header X-Dash-Token == DASH_TOKEN.
_DASH_TOKEN = os.getenv('DASH_TOKEN', '').strip()

@app.before_request
def _guard_mutations():
    if request.method in ('POST', 'PUT', 'DELETE'):
        if request.remote_addr in ('127.0.0.1', '::1'):
            return
        sent = request.headers.get('X-Dash-Token', '')
        if not _DASH_TOKEN or not hmac.compare_digest(sent, _DASH_TOKEN):
            abort(401)

# ════════════════════ STATE ════════════════════
_lock  = threading.Lock()
_state = {
    'running':         False,
    'positions':       [],
    'watchlist':       [],     # snapshot mới nhất của từng coin
    'usdt':            0.0,
    'log':             [],
    'last_update':     '-',
    'last_candle_ts':  0,      # ts(ms) của nến đã evaluate gần nhất
    'closing':         set(),
    'io_busy':         set(),
}

MAX_POS       = 4               # 3→4: nới thêm 1 slot vào lệnh (cổng chặn chính sau khi hạ ADX_MIN)
MON_INT       = 2               # đọc giá WS cache + check trailing stop MỖI 2 GIÂY
BAL_INT       = 15              # đọc số dư REST mỗi 15s (tách khỏi MON 2s — đỡ đập API)

# ── Backtest state ────────────────────────────────────────────────
_bt_lock  = threading.Lock()
_bt_state = {'status': 'idle', 'progress': None, 'result': None, 'error': None}
TICK_LOG_INT  = 60              # ghi tick log vào DB mỗi 60s
SCAN_TICK     = 60              # check candle close mỗi 60s
RECON_INT     = 150             # reconcile với OKX mỗi 2.5 phút
TICK          = 1               # vòng lặp chính 1s (UI cảm nhận realtime)
MAX_LOG       = 300
SSE_TTL       = 1800
BAD_SCORE     = -2.0            # bỏ qua signal nếu coin có score lịch sử < ngưỡng này

STATE_FILE = os.path.join(os.path.dirname(__file__), 'state.json')
_bot_thread = None


def _log(msg: str):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    with _lock:
        _state['log'].append(line)
        if len(_state['log']) > MAX_LOG:
            _state['log'] = _state['log'][-MAX_LOG:]
    print(line, flush=True)
    try: _botlog.info(msg)
    except Exception: pass


# ════════════════════ PERSISTENCE ════════════════════
def _persist_state():
    try:
        with _lock:
            snap = [
                # Giữ _tp_fired để partial-TP KHÔNG kích lại sau restart (fix TC-04: đóng lố thêm 50%)
                {k: v for k, v in p.items() if not k.startswith('_') or k in ('_trade_id', '_tp_fired')}
                for p in _state['positions']
            ]
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'positions': snap, 'ts': time.time()}, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        try: _log(f"Lưu state.json lỗi: {e}")
        except Exception: pass


def _load_state():
    if not os.path.exists(STATE_FILE):
        return []
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return (json.load(f) or {}).get('positions') or []
    except Exception:
        return []


# ════════════════════ CONCURRENCY HELPERS ════════════════════
def _begin_close(coin):
    with _lock:
        if coin in _state['closing']:
            return False
        _state['closing'].add(coin)
    return True


def _end_close(coin):
    with _lock:
        _state['closing'].discard(coin)


def _arm_close_backoff(p):
    """Đóng thất bại → lùi lịch thử lại (exponential, tối đa 60s) thay vì đập API mỗi 2s."""
    n = p.get('_close_attempts', 0)
    p['_close_attempts']   = n + 1
    p['_close_fail_until'] = time.time() + min(60, 5 * (2 ** n))
    p['_exit'] = False  # sẽ tự bật lại ở chu kỳ sau nếu điều kiện thoát vẫn đúng


def _offload(key, fn, *args):
    with _lock:
        if key in _state['io_busy']:
            return
        _state['io_busy'].add(key)
    def runner():
        try: fn(*args)
        except Exception as e: _log(f"Lỗi IO[{key}]: {e}")
        finally:
            with _lock: _state['io_busy'].discard(key)
    threading.Thread(target=runner, daemon=True, name=f"io-{key}").start()


# ════════════════════ CLOSE WITH GUARD ════════════════════
def _close_one(p, reason, exit_price=None):
    coin = p['coin']
    if not _begin_close(coin):
        return False
    try:
        if exit_price is None:
            exit_price = get_last_price(p['swap_id']) or p['entry_price']
        try:
            close_position(p)
        except Exception as e:
            _log(f"[{coin}] Exception khi close_position: {e}")

        # VERIFY với OKX trước khi xóa local
        time.sleep(1.0)
        okx_pos = get_okx_swap_positions()
        if okx_pos is None:
            _log(f"[{coin}] ⚠ Không query được vị thế OKX khi đóng — backoff, thử lại sau")
            _arm_close_backoff(p)
            return False
        if coin in okx_pos:
            _log(f"[{coin}] ⚠ Sau khi đóng, OKX vẫn còn vị thế — backoff, thử lại sau (stop thật vẫn còn bảo vệ)")
            _arm_close_backoff(p)
            return False

        # Đã đóng xong → hủy nốt stop thật trên sàn (nếu còn treo)
        cancel_stop_algo(p['swap_id'], p.get('sl_algo_id'))
        p['_close_attempts'] = 0
        analytics.record_close(p, exit_price, reason=reason,
                               max_fav=p.get('_max_fav'), max_adv=p.get('_max_adv'))
        with _lock:
            _state['positions'] = [x for x in _state['positions'] if x['coin'] != coin]
        _persist_state()
        _log(f"[{coin}] Đóng ✓ ({reason}) @ ${exit_price:.4f}")

        # Animated notification theo win/loss
        pct = (p.get('_pnl') or {}).get('pct', 0)
        ev  = 'CLOSE_WIN' if pct >= 0 else 'CLOSE_LOSS'
        notifier.notify_event(
            ev,
            f"Đóng <b>{coin}</b> ({reason}) @ ${exit_price:.4f}  {pct:+.2f}%",
        )
        return True
    except Exception as e:
        _log(f"[{coin}] Lỗi đóng: {e}")
        _arm_close_backoff(p)
        return False
    finally:
        _end_close(coin)


# ════════════════════ BOT MAIN LOOP ════════════════════
def _reconcile_remove(p):
    """Xóa local position vì đã không còn trên OKX (đóng bên ngoài / liquidated / stop từ OKX).
    KHÔNG gọi close_position."""
    coin = p['coin']
    if not _begin_close(coin):
        return 0
    try:
        # Vị thế đã biến mất khỏi OKX (có thể stop algo đã fire). Hủy nốt algo treo nếu còn.
        cancel_stop_algo(p['swap_id'], p.get('sl_algo_id'))
        try:
            last = get_last_price(p['swap_id']) or p['entry_price']
        except Exception:
            last = p['entry_price']
        analytics.record_close(p, last, reason='external_close',
                               max_fav=p.get('_max_fav'), max_adv=p.get('_max_adv'))
        with _lock:
            _state['positions'] = [x for x in _state['positions'] if x['coin'] != coin]
        _persist_state()
        _log(f"[{coin}] ⚠ Reconciled — không còn trên OKX, đóng local @ ${last:.4f}")
        return 1
    except Exception as e:
        _log(f"[{coin}] Lỗi reconcile: {e}")
        return 0
    finally:
        _end_close(coin)


def _reconcile_with_okx():
    okx_pos = get_okx_swap_positions()
    if okx_pos is None:
        return 0
    with _lock:
        local = list(_state['positions'])
    removed = 0
    for p in local:
        # Trending bot: chỉ kiểm tra coin có còn pos trên OKX không
        if p['coin'] not in okx_pos:
            removed += _reconcile_remove(p)
    return removed


def _bot():
    last_mon = last_scan = last_recon = last_tick_log = 0
    last_bal = 0
    _my_thread = threading.current_thread()

    _log("━━━ Bot Trend-Following khởi động ━━━")
    _log(f"Timeframe={TIMEFRAME} · EMA={EMA_FAST}/{EMA_SLOW} · ADX≥{ADX_MIN} · Lev={LEVERAGE}x · Risk={RISK_PCT*100:.1f}%")
    if _ws.start():
        _log(f"WebSocket realtime ticker → đang kết nối ({len(_ws.instruments)} sym)")

    restored = _load_state()
    if restored:
        with _lock:
            _state['positions'] = restored
        _log(f"⚠ Restore {len(restored)} vị thế từ state.json — đang reconcile với OKX...")
        n = _reconcile_with_okx()
        if n:
            _log(f"Reconcile: xóa {n} vị thế phantom (không còn trên OKX)")

        # Watchdog khởi động lại: nếu giá đã VƯỢT stop của vị thế còn lại → đóng NGAY.
        # Phòng trường hợp bot từng tắt lâu (như sự cố BNB -525$ giữ 4 ngày không ai cắt).
        with _lock:
            restored_open = list(_state['positions'])
        for p in restored_open:
            try:
                px = _get_price(p['swap_id']) or get_last_price(p['swap_id'])
                stop_px = p.get('stop_price')
                if not px or not stop_px:
                    continue
                beyond = ((p['side'] == 'LONG'  and px <= stop_px) or
                          (p['side'] == 'SHORT' and px >= stop_px))
                if beyond:
                    _log(f"[{p['coin']}] ⚠ Khởi động lại: giá {px:.4f} đã vượt stop {stop_px:.4f} → đóng ngay")
                    _close_one(p, reason='stale_stop', exit_price=px)
            except Exception as e:
                _log(f"[{p['coin']}] watchdog lỗi: {e}")

    with _lock:
        _state['usdt'] = get_available_usdt()
    _log(f"Số dư: ${_state['usdt']:.2f} USDT")

    while True:
        with _lock:
            if not _state['running'] or _bot_thread is not _my_thread:
                break
        now = time.time()

        # ── Monitor open positions (every MON_INT — realtime từ WS) ──
        if now - last_mon >= MON_INT:
            with _lock:
                positions = list(_state['positions'])
            do_tick_log = (now - last_tick_log) >= TICK_LOG_INT
            for p in positions:
                try:
                    price = _get_price(p['swap_id'])
                    if not price:
                        _log(f"[{p['coin']}] ⚠ Không lấy được giá (WS+REST cùng fail) — bỏ qua tick này")
                        continue
                    pnl = update_pnl_and_stop(p, price)
                    p['_pnl']  = pnl
                    # Dời stop THẬT trên sàn theo trailing (chỉ khi đổi đủ lớn để không spam API)
                    new_stop = pnl.get('stop')
                    if p.get('sl_algo_id') and new_stop:
                        prev = p.get('sl_algo_px') or p['entry_price']
                        if prev and abs(new_stop - prev) / prev >= ALGO_AMEND_MIN_MOVE:
                            if amend_stop_algo(p['swap_id'], p['sl_algo_id'], new_stop=round(new_stop, 8)):
                                p['sl_algo_px'] = round(new_stop, 8)
                    if pnl.get('hit_stop', False):
                        p['_exit'] = True
                        p['_exit_reason'] = 'trail_stop'
                    elif pnl.get('pct', 0) <= -MAX_LOSS_PCT * 100:
                        # Kill-switch cứng: lỗ vượt ngưỡng → đóng NGAY, không chờ nến/EMA reverse (fix vụ ATOM bleed -19%)
                        p['_exit'] = True
                        p['_exit_reason'] = 'hard_stop'
                    p['_max_fav'] = max(p.get('_max_fav') or 0, pnl['pct'])
                    p['_max_adv'] = min(p.get('_max_adv') or 0, pnl['pct'])
                    if do_tick_log:
                        analytics.record_tick(p, price, pnl.get('price_pnl', 0), pnl.get('pct', 0))
                except Exception as e:
                    _log(f"[{p['coin']}] Lỗi monitor: {e}")
            if do_tick_log:
                last_tick_log = now

            # Partial TP — đóng 50% khi profit >= PARTIAL_TP_ATR_MULT × ATR
            for p in positions:
                if p.get('_tp_fired') or not p.get('_pnl'):
                    continue
                price = p['_pnl'].get('price')
                if not price:
                    continue
                target = PARTIAL_TP_ATR_MULT * p['entry_atr']
                hit = ((p['side'] == 'LONG'  and price >= p['entry_price'] + target) or
                       (p['side'] == 'SHORT' and price <= p['entry_price'] - target))
                if hit:
                    _log(f"[{p['coin']}] Partial TP triggered @ ${price:.4f}")
                    if partial_close_position(p):
                        p['_tp_fired'] = True
                        # Khóa BREAKEVEN cho phần còn lại: đã chốt 50% lãi tại 2×ATR nên cả lệnh
                        # đã dương — kéo stop về entry để runner không thể quay lại thành lỗ.
                        be = p['entry_price']
                        if p['side'] == 'LONG':
                            p['stop_price'] = max(p['stop_price'], be)
                        else:
                            p['stop_price'] = min(p['stop_price'], be)
                        # Đồng bộ stop THẬT trên sàn (sống cả khi bot offline)
                        if p.get('sl_algo_id') and amend_stop_algo(
                                p['swap_id'], p['sl_algo_id'], new_stop=round(p['stop_price'], 8)):
                            p['sl_algo_px'] = round(p['stop_price'], 8)
                        _persist_state()
                        notifier.notify_event('PARTIAL_TP',
                            f"Partial TP <b>{p['coin']}</b> @ ${price:.4f} · stop→BE ${p['stop_price']:.4f}")
                        _log(f"[{p['coin']}] Partial TP ✓ còn {p['contracts']} contracts · stop→breakeven ${p['stop_price']:.4f}")

            # Đóng những vị thế hit stop / EMA reverse (có backoff khi đóng fail → không storm API)
            now_close = time.time()
            for p in [x for x in positions if x.get('_exit') and now_close >= x.get('_close_fail_until', 0)]:
                pnl_snap = p.get('_pnl') or {}
                exit_px  = pnl_snap.get('price')
                pct      = pnl_snap.get('pct', 0)
                reason   = p.get('_exit_reason') or 'trail_stop'
                label    = {'ema_reverse': 'EMA reverse', 'hard_stop': 'Hard stop (-6%)'}.get(reason, 'Trailing stop')
                px_str   = f" @ ${exit_px:.4f}" if exit_px else ''
                _log(f"[{p['coin']}] {label} hit{px_str}")
                notifier.notify_event('STOP_HIT', f"{label} <b>{p['coin']}</b>{px_str}  {pct:+.2f}%")
                _close_one(p, reason=reason, exit_price=exit_px)

            # Số dư đọc thưa hơn (REST) — không cần realtime như giá
            if now - last_bal >= BAL_INT:
                bal = get_available_usdt()      # ngoài lock: tránh giữ lock khi gọi mạng
                with _lock:
                    _state['usdt'] = bal
                last_bal = now
            with _lock:
                _state['last_update'] = datetime.now().strftime('%H:%M:%S')
            last_mon = now

        # ── Reconcile với OKX ───────────────────────────────────
        if now - last_recon >= RECON_INT:
            with _lock:
                has_local = bool(_state['positions'])
            if has_local:
                try:
                    n = _reconcile_with_okx()
                    if n:
                        _log(f"Reconcile tự động: xóa {n} vị thế phantom")
                except Exception as e:
                    _log(f"Lỗi reconcile: {e}")
            last_recon = now

        # ── Scan signals khi có nến mới đóng ────────────────────
        if now - last_scan >= SCAN_TICK:
            cur_closed_ts = last_closed_candle_ts(TIMEFRAME, now)
            with _lock:
                processed_ts = _state['last_candle_ts']
            new_candle = cur_closed_ts > processed_ts

            # Chỉ fetch candles + evaluate khi có nến mới đóng
            if new_candle:
                _log(f"Nến mới đóng ({TIMEFRAME}) — scan {len(SCAN_COINS)} coin...")
                watch = []
                with _lock:
                    open_coins = {p['coin'] for p in _state['positions']}

                # Cache 1 lần / scan thay vì gọi mỗi coin
                score_map = analytics.coin_score_map()
                scan_usdt = get_available_usdt()
                stats = {'sig': 0, 'skip_score': 0, 'skip_vốn': 0, 'skip_1d': 0,
                         'skip_corr': 0, 'skip_full': 0, 'skip_atr': 0, 'opened': 0}

                for coin in SCAN_COINS:
                    with _lock:
                        if not _state['running']:
                            break
                    swap_id = f"{coin}-USDT-SWAP"
                    try:
                        df = get_candles(swap_id, bar=TIMEFRAME)
                        snap = evaluate(df)
                    except Exception as e:
                        _log(f"[{coin}] Lỗi scan: {e}")
                        continue
                    watch.append({'coin': coin, **snap})
                    analytics.record_signal(coin, snap)

                    # EMA reverse exit — đóng vị thế nếu EMA cross ngược chiều
                    with _lock:
                        open_pos = [p for p in _state['positions'] if p['coin'] == coin]
                    for p in open_pos:
                        if ((p['side'] == 'LONG'  and snap.get('cross_down')) or
                                (p['side'] == 'SHORT' and snap.get('cross_up'))):
                            p['_exit'] = True
                            p['_exit_reason'] = 'ema_reverse'
                            _log(f"[{coin}] EMA reverse ({p['side']}) → đóng vị thế")

                    sig = snap.get('signal')
                    if not sig:
                        continue
                    stats['sig'] += 1

                    if snap.get('atr_pct', 0) > ATR_PCT_MAX:
                        _log(f"[{coin}] Bỏ qua {sig} — ATR%={snap['atr_pct']:.2f}% > {ATR_PCT_MAX}% (volatility quá cao)")
                        stats['skip_atr'] += 1
                        continue

                    with _lock:
                        n_pos = len(_state['positions'])
                    if n_pos >= MAX_POS:
                        stats['skip_full'] += 1
                        continue
                    if coin in open_coins:
                        continue

                    hist_score = score_map.get(coin, 0)
                    if hist_score < BAD_SCORE:
                        _log(f"[{coin}] Bỏ qua {sig} — lịch sử kém (score={hist_score:.2f})")
                        stats['skip_score'] += 1
                        continue

                    notional, margin = calc_position_size(scan_usdt, snap['close'], snap['atr'])
                    if notional < MIN_USDT or margin < 1.0:
                        _log(f"[{coin}] Bỏ qua {sig} — vốn ${notional:.2f} thấp (cần ≥${MIN_USDT})")
                        stats['skip_vốn'] += 1
                        continue

                    trend_1d = get_trend_1d(coin)
                    if trend_1d and ((sig == 'LONG'  and trend_1d == 'DOWN') or
                                     (sig == 'SHORT' and trend_1d == 'UP')):
                        _log(f"[{coin}] Bỏ qua {sig} — 1D trend ngược ({trend_1d})")
                        stats['skip_1d'] += 1
                        continue

                    with _lock:
                        cur_positions = list(_state['positions'])
                    if is_correlated(coin, sig, cur_positions):
                        _log(f"[{coin}] Bỏ qua {sig} — đã có vị thế tương quan cùng chiều")
                        stats['skip_corr'] += 1
                        continue

                    hs_tag = f" · score {hist_score:+.2f}" if hist_score else ''
                    _log(f"[{coin}] {sig} · {snap.get('reason','')} · ADX={snap['adx']:.1f} · ATR%={snap['atr_pct']:.2f}%{hs_tag} · 1D={trend_1d or '?'} → ${notional:.2f}")
                    pos = open_position(coin, sig, notional, snap)
                    if pos:
                        analytics.record_open(pos)
                        with _lock:
                            _state['positions'].append(pos)
                        _persist_state()
                        open_coins.add(coin)
                        stats['opened'] += 1
                        # Refresh balance vì đã trừ margin
                        scan_usdt = get_available_usdt()
                        _log(f"[{coin}] {sig} ✓ entry=${pos['entry_price']:.4f} stop=${pos['stop_price']:.4f}")
                        ev = 'OPEN_LONG' if sig == 'LONG' else 'OPEN_SHORT'
                        notifier.notify_event(ev,
                            f"Mở <b>{coin}</b> {sig} ${notional:.0f}  entry={pos['entry_price']:.4f}  stop={pos['stop_price']:.4f}")

                # Tổng kết scan để user biết bot đang làm gì
                if stats['sig'] == 0:
                    _log(f"Scan xong — 0/{len(SCAN_COINS)} coin có signal (chờ cross hoặc trend đủ điều kiện)")
                else:
                    _log(f"Scan xong — {stats['sig']} signal · mở {stats['opened']} · "
                         f"skip [vốn={stats['skip_vốn']}, 1d={stats['skip_1d']}, "
                         f"atr={stats['skip_atr']}, score={stats['skip_score']}, corr={stats['skip_corr']}, full={stats['skip_full']}]")

                with _lock:
                    _state['watchlist'] = watch
                    _state['last_candle_ts'] = cur_closed_ts
            last_scan = now

        time.sleep(TICK)

    # ── Đóng tất cả khi dừng ───────────────────────────────────
    with _lock:
        ps = list(_state['positions'])
    if ps:
        _log(f"Đóng {len(ps)} vị thế...")
        for p in ps:
            _close_one(p, reason='bot_stop')
    _log("━━━ Bot đã dừng ━━━")


def _bot_safe():
    """Wrapper: auto-restart tối đa 3 lần nếu _bot() crash."""
    _my_thread = threading.current_thread()
    MAX_RETRIES = 3
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _bot()
            break
        except Exception:
            _log(f"━━━ BOT CRASH (lần {attempt}/{MAX_RETRIES}) ━━━")
            for line in traceback.format_exc().splitlines():
                _log(line)
            notifier.notify_event('BOT_CRASH', f"Bot crash lần {attempt}/{MAX_RETRIES}")
            if attempt < MAX_RETRIES:
                _log("Tự restart sau 30s...")
                time.sleep(30)
    with _lock:
        # Chỉ reset state nếu đây vẫn là bot thread hiện hành
        # (tránh ghi đè khi user stop → start lại nhanh)
        if _bot_thread is _my_thread:
            _state['running'] = False
        _state['closing'].clear()
    try: _ws.stop()
    except Exception: pass
    _log("━━━ Bot kết thúc — state đã reset ━━━")


# ════════════════════ API ════════════════════
def _build_status_payload():
    with _lock:
        ps      = list(_state['positions'])
        watch   = list(_state['watchlist'])
        usdt    = _state['usdt']
        running = _state['running']
        upd     = _state['last_update']
        logs    = list(_state['log'][-120:])

    out_ps = []
    for p in ps:
        pnl = p.get('_pnl') or {}
        notional = p.get('notional') or (p['contracts'] * p['ct_val'] * p['entry_price'])
        out_ps.append({
            'coin':        p['coin'],
            'side':        p['side'],
            'entry_price': p['entry_price'],
            'cur_price':   pnl.get('price'),
            'stop_price':  p.get('stop_price'),
            'notional':    round(notional, 2),
            'price_pnl':   round(pnl.get('price_pnl', 0), 4),
            'pct':         round(pnl.get('pct', 0), 3),
            'atr':         round(p.get('entry_atr') or 0, 6),
            'open_time':   datetime.fromtimestamp(p['open_time']).strftime('%d/%m %H:%M'),
            'max_fav':     round(p.get('_max_fav') or 0, 2),
            'max_adv':     round(p.get('_max_adv') or 0, 2),
        })

    out_watch = []
    for w in watch:
        if not w.get('signal') and not w.get('trend'):
            continue
        out_watch.append({
            'coin':     w['coin'],
            'signal':   w.get('signal'),
            'trend':    w.get('trend'),
            'close':    round(w.get('close') or 0, 4),
            'adx':      round(w.get('adx') or 0, 1),
            'atr_pct':  round(w.get('atr_pct') or 0, 2),
            'gap_pct':  round(w.get('gap_pct') or 0, 3),
            'strong':   bool(w.get('strong')),
            'vol_ok':   bool(w.get('vol_ok', True)),
        })
    # Sắp xếp: có signal trước, sau đó theo ADX desc
    out_watch.sort(key=lambda x: (x['signal'] is None, -(x['adx'] or 0)))

    return {
        'running':     running, 'usdt': usdt,
        'positions':   out_ps,
        'watchlist':   out_watch,
        'logs':        logs,
        'last_update': upd,
        'timeframe':   TIMEFRAME,
        'ws':          _ws.health(),
        'ts':          time.time(),
    }


@app.route('/api/status')
def api_status():
    return jsonify(_build_status_payload())


@app.route('/api/stream')
def api_stream():
    @stream_with_context
    def gen():
        last = None
        heartbeat = 0
        start = time.time()
        while True:
            if time.time() - start > SSE_TTL:
                return
            try:
                payload = _build_status_payload()
                data = json.dumps(payload, default=str, separators=(',', ':'))
                if data != last:
                    yield f"event: state\ndata: {data}\n\n"
                    last = data
                    heartbeat = 0
                else:
                    heartbeat += 1
                    if heartbeat >= 15:
                        yield ": keep-alive\n\n"
                        heartbeat = 0
                time.sleep(1.0)
            except GeneratorExit:
                return
            except Exception:
                time.sleep(2.0)
    return Response(gen(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'})


@app.route('/api/start', methods=['POST'])
def api_start():
    global _bot_thread
    with _lock:
        if _state['running']:
            return jsonify({'ok': False, 'msg': 'Bot đang chạy rồi'})
        _state['running'] = True
    _bot_thread = threading.Thread(target=_bot_safe, daemon=True, name='bot-main')
    _bot_thread.start()
    return jsonify({'ok': True})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    with _lock:
        if not _state['running']:
            return jsonify({'ok': False, 'msg': 'Bot chưa chạy'})
        _state['running'] = False
    return jsonify({'ok': True})


@app.route('/api/close/<coin>', methods=['POST'])
def api_close(coin):
    with _lock:
        pos = next((p for p in _state['positions'] if p['coin'] == coin), None)
        already = coin in _state['closing']
    if not pos:
        return jsonify({'ok': False, 'msg': f'Không tìm thấy {coin}'})
    if already:
        return jsonify({'ok': False, 'msg': f'{coin} đang được đóng'})
    _log(f"[{coin}] Đóng thủ công...")
    threading.Thread(target=_close_one, args=(pos, 'manual'),
                     daemon=True, name=f'close-{coin}').start()
    return jsonify({'ok': True})


@app.route('/api/close_all', methods=['POST'])
def api_close_all():
    """Đóng tất cả vị thế đang mở."""
    with _lock:
        ps = [p for p in _state['positions'] if p['coin'] not in _state['closing']]
    for p in ps:
        threading.Thread(target=_close_one, args=(p, 'manual_all'),
                         daemon=True, name=f"close-{p['coin']}").start()
    return jsonify({'ok': True, 'closed': len(ps)})


@app.route('/api/sync', methods=['POST'])
def api_sync():
    try:
        n = _reconcile_with_okx()
        return jsonify({'ok': True, 'removed': n,
                        'msg': f'Đã xóa {n} vị thế phantom' if n else 'Mọi vị thế khớp với OKX'})
    except Exception as e:
        return jsonify({'ok': False, 'msg': str(e)})


@app.route('/api/analytics')
def api_analytics():
    return jsonify({
        'global':  analytics.global_stats(),
        'coins':   analytics.coin_stats(),
        'recent':  analytics.recent_trades(20),
        'equity':  analytics.equity_curve(),
    })


@app.route('/api/backtest')
def api_backtest_status():
    with _bt_lock:
        return jsonify({
            'status':   _bt_state['status'],
            'progress': _bt_state['progress'],
            'result':   _bt_state['result'],
            'error':    _bt_state['error'],
        })


@app.route('/api/backtest/run', methods=['POST'])
def api_backtest_run():
    with _bt_lock:
        if _bt_state['status'] == 'running':
            return jsonify({'ok': False, 'msg': 'Backtest đang chạy'})
        _bt_state.update({'status': 'running', 'progress': None, 'result': None, 'error': None})

    data = request.get_json(silent=True) or {}
    candles = int(data.get('candles', 600))
    balance = float(data.get('balance', 10000))

    def _run():
        def _prog(stage, done, total):
            with _bt_lock:
                _bt_state['progress'] = {'stage': stage, 'done': done, 'total': total}
        try:
            result = bt.run(target_candles=candles, initial_balance=balance, progress_cb=_prog)
            with _bt_lock:
                if 'error' in result:
                    _bt_state.update({'status': 'error', 'error': result['error']})
                else:
                    _bt_state.update({'status': 'done', 'result': result})
        except Exception as e:
            with _bt_lock:
                _bt_state.update({'status': 'error', 'error': str(e)})

    threading.Thread(target=_run, daemon=True, name='backtest').start()
    return jsonify({'ok': True})


@app.route('/api/health')
def api_health():
    with _lock:
        running = _state['running']
        n_pos   = len(_state['positions'])
    return jsonify({
        'ok':        True,
        'bot':       'okx-trend',
        'running':   running,
        'positions': n_pos,
        'ts':        time.time(),
    })


@app.route('/')
def index():
    return render_template('index.html', timeframe=TIMEFRAME,
                           ema_fast=EMA_FAST, ema_slow=EMA_SLOW, adx_min=ADX_MIN,
                           leverage=LEVERAGE)


@app.route('/docs')
def docs():
    return render_template('docs.html', timeframe=TIMEFRAME,
                           ema_fast=EMA_FAST, ema_slow=EMA_SLOW, adx_min=ADX_MIN,
                           leverage=LEVERAGE)


@app.route('/deploy')
def deploy():
    return render_template('deploy.html')


if __name__ == '__main__':
    print("\n" + "="*52)
    print("  OKX Trend-Following Bot — Web Dashboard")
    print(f"  Timeframe: {TIMEFRAME} · EMA {EMA_FAST}/{EMA_SLOW} · ADX≥{ADX_MIN}")
    print("  Trình duyệt: http://localhost:5001")
    print("="*52 + "\n")
    app.run(host=os.getenv('BIND_HOST', '127.0.0.1'), port=5001, debug=False, use_reloader=False, threaded=True)
