import os, sys, time, threading, json, traceback, logging
from logging.handlers import RotatingFileHandler
from datetime import datetime

# ── Persistent file logging (5MB × 3 backups) ───────────────
_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot.log')
_file_handler = RotatingFileHandler(_log_path, maxBytes=5_000_000, backupCount=3, encoding='utf-8')
_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
logging.getLogger().addHandler(_file_handler)
logging.getLogger().setLevel(logging.INFO)
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('werkzeug').setLevel(logging.WARNING)
_botlog = logging.getLogger('bot')

# Windows: ép UTF-8 cho stdout/stderr để print tiếng Việt không crash khi redirect
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import openpyxl
from flask import Flask, jsonify, render_template, Response, stream_with_context

from strategy import (
    get_funding_rates, get_available_usdt,
    open_position, close_position, sell_spot,
    check_exit_conditions, estimate_pnl, get_spot_price,
    get_okx_swap_positions,
    MIN_FUNDING_RATE, POSITION_PCT, MIN_USDT, PRICE_STOP_PCT,
    adaptive_position_pct, SCAN_COINS,
)
from excel_logger import log_pnl_snapshot, export_json, push_to_github, EXCEL_FILE
import analytics
import notifier
from config import SIMULATED
from ws_ticker import TickerWS

analytics.init()
analytics.backfill_fees()  # cập nhật fee cho trade cũ trong DB

# WebSocket realtime — subscribe SPOT tickers cho mọi coin trong watchlist
_ws = TickerWS([f"{c}-USDT" for c in SCAN_COINS], simulated=SIMULATED, max_age=15)


def _get_spot_price(spot_id):
    """Ưu tiên giá từ WS cache (sub-second), fallback REST."""
    p = _ws.get_price(spot_id)
    if p is not None:
        return p
    return get_spot_price(spot_id)

app = Flask(__name__)

_lock  = threading.Lock()
_state = {
    'running':       False,
    'positions':     [],
    'opportunities': [],
    'usdt':          0.0,
    'log':           [],
    'last_update':   '-',
    'closing':       set(),   # coin đang trong tiến trình đóng (chống double-close)
    'io_busy':       set(),   # job IO đang chạy (excel, push) — chống stacking
}

MAX_POS         = 3
SCAN_INT        = 300       # scan funding rate mỗi 5 phút
MON_INT         = 2         # đọc giá WS + cập nhật PnL MỖI 2 GIÂY (realtime)
FUNDING_CHK_INT = 30        # check funding rate exit-condition mỗi 30s (rate đổi 8h/lần)
TICK_LOG_INT    = 60        # ghi tick log vào DB mỗi 60s
RECON_INT       = 150       # reconcile với OKX mỗi 2.5 phút
EXCL_INT        = 8*3600    # ghi Excel mỗi 8 giờ
PUSH_INT        = 300       # push GitHub Pages mỗi 5 phút
TICK            = 1         # vòng lặp chính 1s (UI cảm nhận realtime)
MAX_LOG         = 300
SSE_TTL         = 1800
_bot_thread = None

STATE_FILE = os.path.join(os.path.dirname(__file__), 'state.json')


# ── Persistence ──────────────────────────────────────────────────
def _persist_state():
    """Lưu vị thế đang mở ra disk (atomic write)."""
    try:
        with _lock:
            snap = [
                {k: v for k, v in p.items() if not k.startswith('_') or k == '_trade_id'}
                for p in _state['positions']
            ]
        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'positions': snap, 'ts': time.time()}, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        # Không cho lỗi persist phá vỡ bot — chỉ log
        try: _log(f"Lưu state.json lỗi: {e}")
        except Exception: pass


def _load_state():
    if not os.path.exists(STATE_FILE):
        return []
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('positions', []) or []
    except Exception:
        return []


# ── Race-condition guard ─────────────────────────────────────────
def _begin_close(coin):
    """Trả True nếu đoạt được quyền đóng coin này; False nếu đã có thread khác."""
    with _lock:
        if coin in _state['closing']:
            return False
        _state['closing'].add(coin)
    return True


def _end_close(coin):
    with _lock:
        _state['closing'].discard(coin)


# ── IO offload (fire-and-forget worker để bot không bị treo bởi git/Excel) ─
def _offload(key, fn, *args):
    with _lock:
        if key in _state['io_busy']:
            return  # job cùng key đang chạy, bỏ qua chu kỳ này
        _state['io_busy'].add(key)
    def runner():
        try:
            fn(*args)
        except Exception as e:
            _log(f"Lỗi IO[{key}]: {e}")
        finally:
            with _lock:
                _state['io_busy'].discard(key)
    threading.Thread(target=runner, daemon=True, name=f"io-{key}").start()


def _log(msg: str):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    with _lock:
        _state['log'].append(line)
        if len(_state['log']) > MAX_LOG:
            _state['log'] = _state['log'][-MAX_LOG:]
    try: _botlog.info(msg)
    except Exception: pass


def _close_one(p, reason):
    """Đóng 1 vị thế + verify với OKX trước khi xóa local. Trả True nếu confirmed closed."""
    coin = p['coin']
    if not _begin_close(coin):
        return False
    try:
        # PnL tươi để ghi analytics — fallback về cache nếu REST fail
        try:
            final_pnl = estimate_pnl(p)
        except Exception:
            final_pnl = None
        final_pnl = final_pnl or p.get('_pnl')

        # Thử đóng — lưu return value để xử lý edge case bên dưới
        try:
            close_ok = close_position(p)
        except Exception as e:
            _log(f"[{coin}] Exception khi close_position: {e}")
            close_ok = False

        # VERIFY futures với OKX trước khi xóa local
        time.sleep(1.0)  # cho exchange settle
        okx_pos = get_okx_swap_positions()
        if okx_pos is not None and coin in okx_pos:
            _log(f"[{coin}] ⚠ Sau khi đóng, OKX vẫn còn vị thế futures — GIỮ local, cần can thiệp")
            return False

        # Futures đã đóng nhưng spot leg fail → thử bán nốt spot (sell_spot tự co theo balance)
        if not close_ok:
            _log(f"[{coin}] ⚠ Futures OK nhưng spot leg lỗi — retry bán spot...")
            if not sell_spot(p['spot_id'], p['coin_amount']):
                _log(f"[{coin}] ⚠ Spot vẫn fail — GIỮ local position, vào sàn kiểm tra coin {p['spot_id']}")
                return False

        # Confirmed closed
        analytics.record_close(p, final_pnl, reason=reason)
        with _lock:
            _state['positions'] = [x for x in _state['positions'] if x['coin'] != coin]
        _persist_state()
        pnl_val = (final_pnl or {}).get('net_pnl') or (final_pnl or {}).get('total_pnl') or 0
        _log(f"[{coin}] Đóng ✓ ({reason})  net {pnl_val:+.2f}$")
        ev = 'CLOSE_WIN' if pnl_val >= 0 else 'CLOSE_LOSS'
        notifier.notify_event(ev, f"Đóng <b>{coin}</b> ({reason})  net {pnl_val:+.2f}$")
        return True
    except Exception as e:
        _log(f"[{coin}] Lỗi đóng: {e}")
        return False
    finally:
        _end_close(coin)


def _reconcile_remove(p):
    """Xóa vị thế khỏi local state vì futures đã không còn trên OKX (liquidated / đóng tay).
    Bán spot để tránh để lại unhedged position trên sàn."""
    coin = p['coin']
    if not _begin_close(coin):
        return 0
    try:
        try:
            final_pnl = estimate_pnl(p) or {}
        except Exception:
            final_pnl = {}
        # Futures đã đóng bên ngoài — bán nốt spot để giải phóng hedge
        if not sell_spot(p['spot_id'], p['coin_amount']):
            _log(f"[{coin}] ⚠ Bán spot thất bại sau reconcile — kiểm tra thủ công {p['spot_id']}")
        analytics.record_close(p, final_pnl, reason='external_close')
        with _lock:
            _state['positions'] = [x for x in _state['positions'] if x['coin'] != coin]
        _persist_state()
        _log(f"[{coin}] ⚠ Reconciled — không còn trên OKX, đóng local (external_close)")
        return 1
    except Exception as e:
        _log(f"[{coin}] Lỗi reconcile: {e}")
        return 0
    finally:
        _end_close(coin)


def _reconcile_with_okx():
    """Query OKX, drop những local position không còn tồn tại. Trả số đã reconcile."""
    okx_pos = get_okx_swap_positions()
    if okx_pos is None:
        return 0  # API fail — không kết luận
    with _lock:
        local = list(_state['positions'])
    removed = 0
    for p in local:
        if p['coin'] not in okx_pos:
            removed += _reconcile_remove(p)
    return removed


def _bot():
    last_scan = last_mon = last_excel = last_push = last_recon = last_tick_log = last_fund_chk = 0
    last_opps: list = []
    _my_thread = threading.current_thread()

    _log("━━━ Bot OKX Funding Arb khởi động ━━━")
    if _ws.start():
        _log(f"WebSocket realtime ticker → đang kết nối ({len(_ws.instruments)} sym)")

    # Restore vị thế đã lưu (nếu app từng dừng đột ngột)
    restored = _load_state()
    if restored:
        with _lock:
            _state['positions'] = restored
        _log(f"⚠ Restore {len(restored)} vị thế từ state.json — đang reconcile với OKX...")
        n = _reconcile_with_okx()
        if n:
            _log(f"Reconcile: xóa {n} vị thế phantom (không còn trên OKX)")

    with _lock:
        _state['usdt'] = get_available_usdt()
    _log(f"Số dư: ${_state['usdt']:.2f} USDT")

    while True:
        with _lock:
            if not _state['running'] or _bot_thread is not _my_thread:
                break
        now = time.time()

        # ── Cập nhật PnL & kiểm tra thoát ───────────────────────
        if now - last_mon >= MON_INT:
            with _lock:
                positions = list(_state['positions'])

            do_tick_log  = (now - last_tick_log) >= TICK_LOG_INT
            do_fund_chk  = (now - last_fund_chk) >= FUNDING_CHK_INT
            for p in positions:
                try:
                    # Ưu tiên giá WS (sub-second), fallback REST
                    spot_price = _get_spot_price(p['spot_id'])
                    p['_pnl']  = estimate_pnl(p, price=spot_price)
                    # Funding rate chỉ check thưa (rate đổi mỗi 8h)
                    if do_fund_chk:
                        ok, rate       = check_exit_conditions(p)
                        p['_cur_rate'] = rate
                        if ok:
                            p['_exit']        = True
                            p['_exit_reason'] = 'funding_flip'
                    # Price stop — thoát nếu giá diverge quá PRICE_STOP_PCT
                    if not p.get('_exit') and p.get('_pnl'):
                        notional = p['contracts'] * p['ct_val'] * p['entry_price']
                        if p['_pnl'].get('price_pnl', 0) < -(notional * PRICE_STOP_PCT):
                            p['_exit']        = True
                            p['_exit_reason'] = 'price_stop'
                            _log(f"[{p['coin']}] Price stop (-{PRICE_STOP_PCT*100:.0f}%) → đóng")
                    if do_tick_log and p.get('_pnl'):
                        analytics.record_tick(p, p['_pnl'], funding_rate=p.get('_cur_rate'))
                except Exception as e:
                    _log(f"[{p['coin']}] Lỗi cập nhật: {e}")
            if do_tick_log: last_tick_log = now
            if do_fund_chk: last_fund_chk = now

            for p in [x for x in positions if x.get('_exit')]:
                reason = p.get('_exit_reason') or 'funding_flip'
                label  = 'Price stop' if reason == 'price_stop' else 'Funding rate thấp'
                _log(f"[{p['coin']}] {label} → đóng vị thế...")
                _close_one(p, reason=reason)

            with _lock:
                _state['usdt']        = get_available_usdt()
                _state['last_update'] = datetime.now().strftime('%H:%M:%S')
            last_mon = now

        # ── Reconcile với OKX (xóa phantom positions) ────────────
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

        # ── Scan cơ hội ───────────────────────────────────────────
        if now - last_scan >= SCAN_INT:
            with _lock:
                n_pos = len(_state['positions'])

            if n_pos < MAX_POS:
                _log(f"SCAN — ${_state['usdt']:.2f} USDT  |  {datetime.now().strftime('%H:%M')}")
                opps = get_funding_rates()
                if opps:
                    analytics.record_scan(opps)
                    opps = analytics.rank_opps(opps)
                last_opps = opps or []
                with _lock:
                    _state['opportunities'] = last_opps[:10]

                if opps:
                    with _lock:
                        open_coins = {p['coin'] for p in _state['positions']}
                    for opp in opps:
                        with _lock:
                            n_pos = len(_state['positions'])
                            go    = _state['running']
                        if not go or n_pos >= MAX_POS:
                            break
                        if opp['coin'] in open_coins:
                            continue
                        if opp['funding_rate'] < MIN_FUNDING_RATE:
                            break
                        # Bỏ qua coin có lịch sử rất xấu (score < -1.5)
                        if opp.get('hist_score', 0) < -1.5:
                            _log(f"[{opp['coin']}] Bỏ qua — lịch sử kém (score={opp['hist_score']:.2f})")
                            continue
                        usdt   = get_available_usdt()
                        amount = usdt * adaptive_position_pct(opp['funding_rate'])
                        if amount < MIN_USDT:
                            _log(f"Số dư thấp (${usdt:.2f}) — dừng scan")
                            break
                        hs = opp.get('hist_score', 0)
                        hs_tag = f" · score {hs:+.2f}" if hs else ''
                        _log(f"[{opp['coin']}] Vào lệnh ${amount:.2f} @ {opp['funding_rate']*100:.4f}%/8h{hs_tag}")
                        pos = open_position(opp, amount)
                        if pos:
                            analytics.record_open(pos)
                            with _lock:
                                _state['positions'].append(pos)
                            _persist_state()
                            open_coins.add(opp['coin'])
                            _log(f"[{opp['coin']}] Mở ✓ giá=${pos['entry_price']:.2f}")
                            notifier.notify_event('OPEN_LONG',
                                f"Mở <b>{opp['coin']}</b> ${amount:.0f} @ {opp['funding_rate']*100:.4f}%/8h")
                else:
                    _log("Không lấy được funding rate")
            last_scan = now

        # ── Ghi Excel mỗi 8 giờ (offload, không chặn bot) ───────
        if now - last_excel >= EXCL_INT:
            with _lock:
                ps = [dict(p) for p in _state['positions']]
                u  = _state['usdt']
            if ps:
                def _do_excel(positions=ps, usdt=u):
                    pl = [estimate_pnl(p) for p in positions]
                    log_pnl_snapshot(positions, pl, usdt)
                    _log("Ghi Excel ✓ → pnl_log.xlsx")
                _offload('excel', _do_excel)
            last_excel = now

        # ── Push GitHub Pages mỗi 5 phút (offload) ──────────────
        if now - last_push >= PUSH_INT:
            with _lock:
                ps = [dict(p) for p in _state['positions']]
                u  = _state['usdt']
            opps_copy = list(last_opps)
            def _do_push(positions=ps, usdt=u, opps=opps_copy):
                pl = [p.get('_pnl') or estimate_pnl(p) for p in positions]
                export_json({'positions': positions, 'pnl_list': pl,
                             'opportunities': opps, 'usdt': usdt})
                if push_to_github():
                    _log("GitHub Pages ✓ (data.json)")
            _offload('push', _do_push)
            last_push = now

        time.sleep(TICK)

    # ── Đóng tất cả khi dừng (dùng race-guard) ──────────────────
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
        if _bot_thread is _my_thread:
            _state['running'] = False
        _state['closing'].clear()
    try: _ws.stop()
    except Exception: pass
    _log("━━━ Bot kết thúc — state đã reset ━━━")


# ── API ───────────────────────────────────────────────────────────

def _build_status_payload():
    with _lock:
        ps      = list(_state['positions'])
        opps    = list(_state['opportunities'])
        usdt    = _state['usdt']
        running = _state['running']
        upd     = _state['last_update']
        logs    = list(_state['log'][-120:])

    out_ps = []
    for p in ps:
        pnl = p.get('_pnl') or {}
        vin = p['contracts'] * p['ct_val'] * p['entry_price']
        pct = (pnl.get('total_pnl', 0) / vin * 100) if vin else 0
        out_ps.append({
            'coin':        p['coin'],
            'entry_price': p['entry_price'],
            'cur_price':   pnl.get('price'),
            'usdt_in':     round(vin, 2),
            'funding_pnl': round(pnl.get('funding_pnl', 0), 4),
            'price_pnl':   round(pnl.get('price_pnl', 0), 4),
            'total_pnl':   round(pnl.get('total_pnl', 0), 4),
            'net_pnl':     round(pnl.get('net_pnl', 0), 4),
            'fee_est':     round(pnl.get('fee_est', 0), 4),
            'pct':         round(pct, 3),
            'n_pay':       pnl.get('n_payments', 0),
            'open_time':   datetime.fromtimestamp(p['open_time']).strftime('%d/%m %H:%M'),
            'exit':        p.get('_exit', False),
        })

    return {
        'running': running, 'usdt': usdt, 'positions': out_ps,
        'opps':    [{'coin': o['coin'], 'rate': o['funding_rate'],
                     'apy': o['annualized'], 'next': o['next_rate']} for o in opps],
        'logs': logs, 'last_update': upd,
        'ws':   _ws.health(),
        'ts':   time.time(),
    }


@app.route('/api/status')
def api_status():
    return jsonify(_build_status_payload())


@app.route('/api/stream')
def api_stream():
    """Server-Sent Events: push state diffs ~1s for true real-time UI."""
    @stream_with_context
    def gen():
        last = None
        heartbeat = 0
        start = time.time()
        while True:
            # Tự kết thúc sau SSE_TTL (client EventSource sẽ tự reconnect)
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
    return Response(
        gen(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache, no-transform',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


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
        already_closing = coin in _state['closing']
    if not pos:
        return jsonify({'ok': False, 'msg': f'Không tìm thấy {coin}'})
    if already_closing:
        return jsonify({'ok': False, 'msg': f'{coin} đang được đóng — chờ hoàn tất'})
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
    """Ép sync ngay với OKX, xóa phantom positions."""
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
        'recent':  analytics.recent_trades(15),
        'equity':  analytics.equity_curve(),
    })


@app.route('/api/pnl-history')
def api_pnl():
    excel_path = os.path.join(os.path.dirname(__file__), EXCEL_FILE)
    if not os.path.exists(excel_path):
        return jsonify({'headers': [], 'rows': []})
    try:
        wb   = openpyxl.load_workbook(excel_path, data_only=True)
        ws   = wb.active
        hdrs = [c.value for c in ws[1] if c.value]
        rows = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if any(v is not None and v != '' for v in row):
                rows.append([str(v) if v is not None else '' for v in row[:len(hdrs)]])
        wb.close()
        return jsonify({'headers': hdrs, 'rows': rows})
    except Exception as e:
        return jsonify({'error': str(e), 'headers': [], 'rows': []})


@app.route('/api/health')
def api_health():
    with _lock:
        running = _state['running']
        n_pos   = len(_state['positions'])
    return jsonify({
        'ok':        True,
        'bot':       'okx-arb',
        'running':   running,
        'positions': n_pos,
        'ts':        time.time(),
    })


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/docs')
def docs():
    return render_template('docs.html')


@app.route('/deploy')
def deploy():
    return render_template('deploy.html')


if __name__ == '__main__':
    print("\n" + "="*50)
    print("  OKX Arb Bot — Web Dashboard")
    print("  Mở trình duyệt: http://localhost:5000")
    print("="*50 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)
