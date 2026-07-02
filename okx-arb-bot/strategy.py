import os
import math
import time
import logging
import datetime as dt
from config import public_api, market_api, account_api, trade_api

log = logging.getLogger(__name__)


# ── Retry helper ──────────────────────────────────────────────────
def _retry(fn, *, attempts=3, base_delay=0.3, what=''):
    """Gọi fn() có retry với exponential backoff. Trả None nếu thất bại."""
    last_err = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    if what:
        log.warning(f"retry exhausted ({what}): {last_err}")
    return None


# ── UTC funding settlement count ──────────────────────────────────
# OKX trả funding tại 00:00, 08:00, 16:00 UTC mỗi ngày.
def funding_payments_since(open_ts, now_ts=None):
    """Số lần funding settlement đã rơi vào khoảng (open_ts, now_ts]."""
    if now_ts is None:
        now_ts = time.time()
    if now_ts <= open_ts:
        return 0
    # tz-aware UTC (utcfromtimestamp deprecated từ Python 3.12)
    open_dt = dt.datetime.fromtimestamp(open_ts, dt.timezone.utc)
    # boundary tiếp theo strictly > open_ts
    next_h = ((open_dt.hour // 8) + 1) * 8
    if next_h >= 24:
        nb = (open_dt + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        nb = open_dt.replace(hour=next_h, minute=0, second=0, microsecond=0)
    next_ts = nb.replace(tzinfo=dt.timezone.utc).timestamp()
    if next_ts > now_ts:
        return 0
    return int((now_ts - next_ts) // (8 * 3600)) + 1

SCAN_COINS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX",
    "DOT", "LINK", "ARB", "OP", "SUI", "TRX", "ATOM",
    "LTC", "BCH", "NEAR", "PEPE", "FLOKI",
    # Mở rộng watchlist (đều có CẢ spot lẫn swap trên OKX) — nhiều coin hơn ⇒ nhiều
    # cơ hội funding ≥ ngưỡng EV ⇒ lấp đủ MAX_POS=10 và tiến gần 20 lệnh/ngày khi funding rộng.
    # ⚠ DEMO: APT/TIA/SEI/WLD KHÔNG có SWAP trên paper-trading → đã bỏ (gây ws 60018 + scan phí REST). Thêm lại khi LIVE.
    # ⚠ TON (delist cả spot+swap) & LDO (mất swap) trên DEMO 07/2026 → đã bỏ. TON delist giữa lúc
    #   giữ vị thế chính là gốc sự cố unhedged 3.5 ngày (19-23/06). validate_scan_coins() lọc động khi khởi động.
    "INJ", "FIL", "AAVE",
]

# ── Ngưỡng vào/ra (đã chỉnh để KHÔNG churn lỗ phí) ──────────────────
# Phí round-trip = (0.1% spot + 0.05% swap) × 2 chân = 0.30% notional.
# Trước đây MIN_FUNDING_RATE=0.01%/8h: cần ~30 kỳ funding (~10 ngày) mới hoà phí,
# nhưng bot lại thoát sau vài giờ → lỗ phí 100%. Nâng ngưỡng để 1 lệnh kỳ vọng
# thu đủ funding bù phí.
MIN_FUNDING_RATE = 0.0012    # 0.12%/8h — sàn EV: 3 kỳ = 0.36% > phí 0.30% (GIỮ — đây là đáy EV, hạ nữa = bleed phí)
POSITION_PCT     = 0.07      # 7% số dư/vị thế (hạ 15→7): chứa được ~10 slot mà tổng vốn triển khai
                            # vẫn < số dư (mỗi vị thế tiêu ~1.2× amount = spot full + swap margin/5)
MIN_USDT         = 50.0      # bỏ qua lệnh quá nhỏ (phí cố định lấn át funding)
LEVERAGE         = "5"

# ── Vốn dành cho ARB ──
# get_available_usdt() trả số dư GỘP của cả tài khoản. CAPITAL_FRACTION giới hạn phần
# vốn arb được triển khai tối đa = CAPITAL_FRACTION × equity.
# LỊCH SỬ: hồi chung tài khoản với trend-bot đặt 0.5 (arb) + 0.5 (trend) ≤ 1.0. Trend đã
# xoá hẳn (15/06) → arb chạy MỘT MÌNH, default nâng 0.5→0.7. Server vẫn đọc env
# ARB_CAPITAL_FRACTION (env override code) — đổi giá trị live bằng cách sửa env đó.
CAPITAL_FRACTION = float(os.getenv('ARB_CAPITAL_FRACTION', '0.7'))
# Mỗi vị thế arb "tiêu" ≈ notional × (1 + 1/leverage): spot full + swap margin.
CAPITAL_PER_NOTIONAL = 1.0 + 1.0 / float(LEVERAGE)

SPOT_FEE_RATE    = 0.001    # 0.10% taker — spot
FUTURES_FEE_RATE = 0.0005   # 0.05% taker — futures
ROUND_TRIP_FEE   = (SPOT_FEE_RATE + FUTURES_FEE_RATE) * 2   # 0.30% — mở+đóng cả 2 chân
FEE_SAFETY       = 1.2      # biên an toàn (hạ 1.5→1.2: EV gate giờ cần coll ≥ 0.12%/8h, khớp MIN_FUNDING_RATE)
ENTRY_MIN_SETTLEMENTS = 3   # số kỳ funding kỳ vọng giữ — dùng cho cổng EV vào lệnh
EXIT_MIN_SETTLEMENTS  = 3   # HARD-CAP cho min-hold (1→3 — khớp ENTRY): exit funding_flip chỉ khi
                            # funding ĐÃ THU đủ bù phí round-trip; nếu rate sụp ngay mà chưa bù phí thì
                            # giữ tối đa 3 kỳ rồi thoát (chặn lỗ trên). Fix gốc bleed phí: trước đây vào
                            # kỳ vọng 3 kỳ nhưng cho thoát sau 1 kỳ → thu 1×rate < phí 0.30% = lỗ mỗi lệnh.
MIN_STAY_RATE    = 0.00005  # 0.005%/8h — rate thấp hơn mức này coi như "flip"
PRICE_STOP_PCT   = 0.05     # thoát khi price_pnl < -5% notional (nới: vị thế đã hedge nên 2% là nhiễu basis)


def adaptive_position_pct(funding_rate: float) -> float:
    """Scale vốn theo funding rate. Trần hạ để chứa ~10 slot đồng thời (MAX_POS=10) mà tổng
    vốn triển khai vẫn < số dư — mỗi vị thế tiêu ~1.2× amount (spot full + swap margin/5)."""
    if funding_rate >= 0.005:     # >= 0.5%/8h — cơ hội rất tốt
        return 0.10
    elif funding_rate >= 0.002:   # >= 0.2%/8h
        return 0.08
    else:
        return POSITION_PCT       # 0.07


def get_funding_rates():
    results = []
    for coin in SCAN_COINS:
        inst_id = f"{coin}-USDT-SWAP"
        resp = _retry(lambda iid=inst_id: public_api.get_funding_rate(instId=iid),
                      attempts=3, base_delay=0.3, what=f'funding_rate {inst_id}')
        try:
            if resp and resp.get('code') == '0' and resp.get('data'):
                d = resp['data'][0]
                results.append({
                    'coin':         coin,
                    'swap_id':      inst_id,
                    'spot_id':      f"{coin}-USDT",
                    'funding_rate': float(d['fundingRate']),
                    'next_rate':    float(d.get('nextFundingRate') or 0),
                    'annualized':   float(d['fundingRate']) * 3 * 365 * 100,
                })
        except Exception as e:
            log.debug(f"Parse funding {inst_id}: {e}")
        time.sleep(0.05)
    for r in results:
        r['entry_score'] = r['funding_rate'] * 0.4 + r['next_rate'] * 0.6
    results.sort(key=lambda x: x['entry_score'], reverse=True)
    return results


def collectible_rate(opp) -> float:
    """Rate kỳ vọng THỰC THU ở settlement sắp tới — dùng cho cổng EV vào lệnh.

    `nextFundingRate` (dự báo kỳ kế) là predictor tốt nhất KHI sàn có trả về.
    Nhưng OKX DEMO (và đôi lúc cả live ngay sau settlement) trả next=0 cho MỌI coin
    → nếu gate cứng theo next sẽ KHÔNG BAO GIỜ vào lệnh, kể cả cơ hội thật như
    ATOM 0.68%/8h (745% APY). Vì vậy:
      - next > 0 (sàn CÓ dự báo): tin next, cap ở current để thận trọng (lọc spike live).
      - next = 0 / không có: fallback CURRENT rate. Bảo vệ chống spike-revert chuyển sang
        MIN-HOLD (giữ tới khi thu ≥1 kỳ funding, chỉ price_stop mới đóng sớm) — xem app.py.
    """
    nr  = opp.get('next_rate') or 0.0
    cur = opp.get('funding_rate') or 0.0
    if nr > 0:
        return min(nr, cur) if cur > 0 else nr
    return max(0.0, cur)


def get_available_usdt():
    resp = _retry(lambda: account_api.get_account_balance(ccy="USDT"),
                  attempts=3, base_delay=0.3, what='account_balance')
    try:
        if resp and resp.get('code') == '0':
            for d in resp['data'][0].get('details', []):
                if d['ccy'] == 'USDT':
                    return float(d['availEq'])
    except Exception as e:
        log.error(f"Parse số dư: {e}")
    return 0.0


def get_funding_income(swap_id, since_ts):
    """Tổng funding THỰC NHẬN (USDT) cho swap_id kể từ since_ts, đọc từ OKX bills (type=8).

    Dương = nhận (short khi funding>0), âm = trả. Trả None nếu API fail (caller fallback
    sang ước lượng). Trả 0.0 nếu chưa qua settlement nào — đó là con số THẬT (npay=0).
    Bills chỉ lưu 7 ngày gần nhất; vị thế arb giữ < 1 ngày nên đủ.
    """
    resp = _retry(lambda: account_api.get_account_bills(instType="SWAP", type="8"),
                  attempts=2, base_delay=0.3, what=f'bills {swap_id}')
    if not resp or resp.get('code') != '0':
        return None
    since_ms = since_ts * 1000.0
    total = 0.0
    for b in (resp.get('data') or []):
        try:
            if b.get('instId') != swap_id:
                continue
            if float(b.get('ts') or 0) < since_ms:
                continue
            # Funding của bill type=8 nằm ở 'balChg' (live) NHƯNG OKX DEMO trả balChg='0.0000'
            # và để funding thật ở 'pnl'. Bug cũ `balChg or pnl`: '0.0000' là chuỗi truthy nên
            # KHÔNG bao giờ fallback sang pnl ⇒ funding LUÔN = 0 (gốc của ARB bleed phí 0.3%/lệnh).
            bc = float(b.get('balChg') or 0)
            pn = float(b.get('pnl') or 0)
            total += bc if bc != 0 else pn
        except (ValueError, TypeError):
            continue
    return total


def get_spot_price(inst_id):
    resp = _retry(lambda: market_api.get_ticker(instId=inst_id),
                  attempts=3, base_delay=0.2, what=f'ticker {inst_id}')
    try:
        if resp and resp.get('code') == '0' and resp.get('data'):
            return float(resp['data'][0]['last'])
    except Exception as e:
        log.error(f"Parse giá {inst_id}: {e}")
    return None


def get_okx_swap_positions():
    """Trả về dict {coin: {inst_id, pos_sz, avg_px}} với các vị thế SWAP đang mở trên OKX.
    Trả None nếu API fail (để caller biết KHÔNG kết luận gì)."""
    resp = _retry(lambda: account_api.get_positions(instType="SWAP"),
                  attempts=3, base_delay=0.3, what='get_positions')
    if not resp or resp.get('code') != '0':
        return None
    out = {}
    for r in (resp.get('data') or []):
        try:
            inst = r.get('instId', '')
            pos_sz = float(r.get('pos') or 0)
            if abs(pos_sz) < 1e-12:
                continue
            coin = inst.split('-')[0]
            out[coin] = {
                'inst_id': inst,
                'pos':     pos_sz,
                'avg_px':  float(r.get('avgPx') or 0),
            }
        except Exception:
            continue
    return out


def get_swap_info(inst_id):
    resp = _retry(lambda: public_api.get_instruments(instType="SWAP", instId=inst_id),
                  attempts=3, base_delay=0.3, what=f'instruments {inst_id}')
    try:
        if resp and resp.get('code') == '0' and resp.get('data'):
            d = resp['data'][0]
            return float(d['ctVal']), float(d['minSz']), float(d['lotSz'])
    except Exception as e:
        log.error(f"Parse instrument {inst_id}: {e}")
    return None, None, None


def get_spot_info(inst_id):
    """minSz, lotSz của một spot instrument. Trả (None, None) nếu API fail."""
    resp = _retry(lambda: public_api.get_instruments(instType="SPOT", instId=inst_id),
                  attempts=3, base_delay=0.3, what=f'spot instruments {inst_id}')
    try:
        if resp and resp.get('code') == '0' and resp.get('data'):
            d = resp['data'][0]
            return float(d['minSz']), float(d['lotSz'])
    except Exception as e:
        log.error(f"Parse spot instrument {inst_id}: {e}")
    return None, None


def get_instrument_state(inst_id):
    """Trạng thái giao dịch của instrument trên OKX:
      'live'      → giao dịch được (đặt lệnh market OK);
      'suspend' / 'preopen' / 'expired' / 'settlement' → KHÔNG đặt được lệnh (market đóng/tạm dừng).
    Trả None nếu API fail (caller KHÔNG được kết luận 'market đóng' khi không đọc được)."""
    resp = _retry(lambda: public_api.get_instruments(instType="SWAP", instId=inst_id),
                  attempts=3, base_delay=0.3, what=f'inst-state {inst_id}')
    try:
        if resp and resp.get('code') == '0' and resp.get('data'):
            return resp['data'][0].get('state')
    except Exception as e:
        log.debug(f"Parse state {inst_id}: {e}")
    return None


def _instrument_live(inst_type, inst_id):
    """True nếu instrument TỒN TẠI và state=='live'; False nếu KHÔNG tồn tại (đã delist)
    hoặc không 'live'; None nếu API fail (KHÔNG được kết luận 'chết' khi chỉ lỗi tạm thời)."""
    resp = _retry(lambda: public_api.get_instruments(instType=inst_type, instId=inst_id),
                  attempts=3, base_delay=0.3, what=f'inst-live {inst_id}')
    if resp is None:
        return None  # API fail → không kết luận
    try:
        data = resp.get('data') or []
        if not data:
            return False  # sàn trả rỗng (vd 51001) = instrument không còn
        return data[0].get('state') == 'live'
    except Exception as e:
        log.debug(f"Parse inst-live {inst_id}: {e}")
        return None


def validate_scan_coins():
    """Lọc SCAN_COINS lúc khởi động: chỉ GIỮ coin có CẢ spot lẫn swap còn 'live' trên sàn.

    Gốc sự cố (TON 19-23/06): OKX delist/settle instrument giữa lúc bot đang giữ vị thế →
    futures bị đóng 'bên ngoài' → bot mất hedge; đồng thời WS spam 51001/60018 mỗi reconnect
    và bot vẫn cố mở lệnh trên instrument chết. Loại sớm các coin này ngăn tái diễn.

    An toàn: coin API-fail (trả None) được GIỮ lại (không loại vì lỗi mạng tạm thời).
    Mutate SCAN_COINS IN-PLACE để mọi consumer (scan, orphan-check, WS) thấy CÙNG danh sách.
    Trả list coin đã loại."""
    live, dropped, unknown = [], [], []
    for c in SCAN_COINS:
        s = _instrument_live("SPOT", f"{c}-USDT")
        w = _instrument_live("SWAP", f"{c}-USDT-SWAP")
        if s is False or w is False:
            dropped.append(c)
        else:
            live.append(c)
            if s is None or w is None:
                unknown.append(c)
        time.sleep(0.05)
    if dropped:
        log.warning(f"⚠ Loại {len(dropped)} coin không còn 'live' (spot+swap) trên sàn: {dropped}")
    if unknown:
        log.warning(f"⚠ Không đọc được trạng thái (API fail) — TẠM GIỮ, kiểm tra lại sau: {unknown}")
    SCAN_COINS[:] = live  # in-place: giữ nguyên object mà app.py đã import
    return dropped


def _set_leverage(swap_id):
    try:
        account_api.set_leverage(instId=swap_id, lever=LEVERAGE, mgnMode="isolated")
    except Exception as e:
        log.warning(f"set_leverage {swap_id}: {e}")


def _fmt_contracts(contracts, lot_sz):
    """Format contracts theo độ chính xác của lot_sz."""
    decimals = len(str(lot_sz).rstrip('0').split('.')[-1]) if '.' in str(lot_sz) else 0
    return str(round(contracts, decimals))


def _get_filled_qty(ord_id, inst_id, fallback=0.0, attempts=8, delay=0.5):
    """Query order vừa khớp → trả về (accFillSz, avgPx).

    Retry KỸ (mặc định 8×0.5s ≈ 4s) vì OKX settle ~1s và đây là số liệu nền tảng:
    sai accFillSz khi MỞ → bán quá tay khi đóng → 51008 + lệch hedge. Thà chờ thêm
    vài giây còn hơn dùng số ước lượng.
    """
    for _ in range(attempts):
        time.sleep(delay)
        try:
            r = trade_api.get_order(instId=inst_id, ordId=ord_id)
            if r and r.get('code') == '0' and r.get('data'):
                d = r['data'][0]
                acc = float(d.get('accFillSz') or 0)
                avg = float(d.get('avgPx') or 0)
                state = d.get('state')
                if acc > 0:
                    return acc, avg
                if state in ('canceled', 'filled'):  # filled mà acc=0 thì khỏi đợi thêm
                    break
        except Exception as e:
            log.debug(f"get_order {ord_id}: {e}")
    return fallback, 0.0


def open_position(opportunity, usdt_amount):
    coin    = opportunity['coin']
    spot_id = opportunity['spot_id']
    swap_id = opportunity['swap_id']

    price = get_spot_price(spot_id)
    if not price:
        return None

    ct_val, min_sz, lot_sz = get_swap_info(swap_id)
    if ct_val is None:
        return None

    # Tính contracts, làm tròn xuống theo lot_sz dùng math.floor tránh float error
    raw_contracts = (usdt_amount / price) / ct_val
    steps         = math.floor(round(raw_contracts / lot_sz, 8))
    contracts     = steps * lot_sz

    if contracts < min_sz:
        min_cost = min_sz * ct_val * price
        log.warning(f"  [{coin}] Bỏ qua — vốn ${usdt_amount:.2f} < tối thiểu ${min_cost:.2f}")
        return None

    # Mua dư bù phí spot để coin nhận về ≈ contracts*ct_val (khớp chân short → giữ delta-neutral).
    # Trước đây mua đúng contracts*ct_val*price nên sau phí 0.1% giữ ÍT hơn short → lệch net-short, lỗ khi giá lên.
    spot_usdt  = round(contracts * ct_val * price / (1 - SPOT_FEE_RATE), 2)
    sz_str     = _fmt_contracts(contracts, lot_sz)

    # Re-check số dư NGAY trước khi đặt lệnh (chống race chia chung tài khoản với trend-bot:
    # số dư có thể đã bị bot kia tiêu trong lúc ta scan). Thiếu → bỏ lượt, KHÔNG để OKX reject giữa chừng.
    avail_now = get_available_usdt()
    if avail_now < spot_usdt * 1.02:
        log.warning(f"  [{coin}] Bỏ qua — số dư ${avail_now:.2f} < cần ${spot_usdt*1.02:.2f} (đã trừ buffer/đối thủ chung TK)")
        return None

    _set_leverage(swap_id)

    # Mua spot bằng USDT
    # FIX (audit 07/2026): place_order gọi thẳng SDK có thể NÉM exception (timeout/5xx/mạng rớt),
    # không chỉ trả code!=0. Chân spot exception TRƯỚC khi khớp → coi như chưa mua, abort an toàn.
    try:
        r_spot = trade_api.place_order(
            instId=spot_id, tdMode="cash",
            side="buy", ordType="market",
            sz=str(spot_usdt), tgtCcy="quote_ccy",
        )
    except Exception as e:
        log.error(f"  [{coin}] Spot buy NÉM EXCEPTION: {e} — abort, KHÔNG short.")
        return None
    if r_spot.get('code') != '0':
        detail = (r_spot.get('data') or [{}])[0]
        log.error(f"  [{coin}] Spot buy lỗi [{detail.get('sCode')}]: {detail.get('sMsg') or r_spot.get('msg')}")
        return None

    # XÁC NHẬN lượng coin THỰC GIỮ — fix bug 51008 + lệch hedge.
    # Nguồn sự thật theo thứ tự: (1) accFillSz từ order; (2) số dư available của coin sau settle.
    # KHÔNG dùng ước lượng nữa: nếu partial-fill mà dùng estimate → bán quá tay → 51008 + unhedged.
    spot_ord_id = (r_spot.get('data') or [{}])[0].get('ordId', '')
    actual_spot, _actual_px = _get_filled_qty(spot_ord_id, spot_id, fallback=0.0)
    time.sleep(0.4)  # cho spot settle vào balance trước khi đọc available
    held = _get_spot_available(coin)
    if held and held > 0:
        coin_amount = round(held, 8)        # số dư thực = hedge khít nhất (đã trừ phí), sell cap theo available
    elif actual_spot > 0:
        coin_amount = round(actual_spot, 8)
    else:
        # Không xác nhận được spot đã khớp → KHÔNG mở chân short (tránh lệch hedge).
        # Best-effort rollback bán lại phần có thể đã mua; nếu chưa khớp thì sell_spot tự bỏ qua.
        log.error(f"  [{coin}] ⚠ Không xác nhận spot khớp sau khi mua — abort, KHÔNG short. Thử rollback spot...")
        sell_spot(spot_id, round(contracts * ct_val, 8))
        return None

    # FIX (audit 07/2026): SIZE LẠI chân short theo lượng spot THỰC giữ, KHÔNG dùng full contracts
    # kế hoạch. Nếu spot khớp thiếu/phí ăn nhiều mà vẫn short đủ contracts → short > spot = NET-SHORT,
    # vỡ delta-neutral (lỗ khi giá lên). Làm tròn XUỐNG lotSz để |short| ≤ |spot|.
    hedge_contracts = math.floor(round((coin_amount / ct_val) / lot_sz, 8)) * lot_sz
    if hedge_contracts < min_sz:
        log.error(f"  [{coin}] ⚠ Spot thực giữ {coin_amount:.8f} chỉ đủ {hedge_contracts:g} contracts "
                  f"< minSz {min_sz:g} — abort, rollback spot.")
        sell_spot(spot_id, coin_amount)
        return None
    contracts = hedge_contracts
    sz_str    = _fmt_contracts(contracts, lot_sz)

    time.sleep(0.3)

    # Short futures isolated 1x
    # FIX (audit 07/2026): bọc try/except — exception SAU KHI spot đã mua mà không rollback = spot trần
    # (unhedged) + crash loop. Bắt exception → rollback bán spot y như nhánh code!=0.
    try:
        r_swap = trade_api.place_order(
            instId=swap_id, tdMode="isolated",
            side="sell", ordType="market", sz=sz_str,
        )
    except Exception as e:
        log.error(f"  [{coin}] Futures short NÉM EXCEPTION: {e} — hoàn spot để tránh unhedged...")
        time.sleep(1)
        if not sell_spot(spot_id, coin_amount):
            log.critical(f"  [{coin}] ROLLBACK THẤT BẠI sau exception short — kiểm tra thủ công spot {spot_id}!")
        return None
    if r_swap.get('code') != '0':
        detail = (r_swap.get('data') or [{}])[0]
        log.error(f"  [{coin}] Futures short lỗi [{detail.get('sCode')}]: {detail.get('sMsg') or r_swap.get('msg')} — hoàn spot...")
        time.sleep(1)
        r_rb = sell_spot(spot_id, coin_amount)
        if not r_rb:
            log.critical(f"  [{coin}] ROLLBACK THẤT BẠI — kiểm tra thủ công spot {spot_id}!")
        return None

    return {
        'coin':               coin,
        'spot_id':            spot_id,
        'swap_id':            swap_id,
        'coin_amount':        coin_amount,
        'contracts':          contracts,
        'lot_sz':             lot_sz,
        'ct_val':             ct_val,
        'entry_price':        _actual_px or price,
        'entry_funding_rate': opportunity['funding_rate'],
        'open_time':          time.time(),
    }


def close_position(position):
    """Đóng cả 2 chân (futures + spot). Trả True chỉ khi CẢ HAI thành công.

    Spot leg fail (51008 insufficient balance) thường do dust accumulation.
    Caller (_close_one) cần biết để retry / can thiệp thủ công.
    """
    coin   = position['coin']
    sz_str = _fmt_contracts(position['contracts'], position['lot_sz'])

    r_swap = trade_api.place_order(
        instId=position['swap_id'], tdMode="isolated",
        side="buy", ordType="market",
        sz=sz_str, reduceOnly="true",
    )
    if r_swap.get('code') != '0':
        detail = (r_swap.get('data') or [{}])[0]
        err = f"[{detail.get('sCode')}] {detail.get('sMsg') or r_swap.get('msg')}"
        position['_last_close_err'] = err
        log.error(f"  [{coin}] Đóng futures lỗi {err}")
        return False
    position['_last_close_err'] = None

    time.sleep(0.3)
    spot_ok = sell_spot(position['spot_id'], position['coin_amount'])
    if not spot_ok:
        log.warning(f"  [{coin}] Futures đóng OK nhưng spot sell fail — coin còn dangling, cần can thiệp thủ công")
        return False
    return True


def _get_spot_available(ccy):
    """Lấy số dư available của một coin (không phải USDT) từ trading account."""
    resp = _retry(lambda: account_api.get_account_balance(ccy=ccy),
                  attempts=3, base_delay=0.3, what=f'balance {ccy}')
    try:
        if resp and resp.get('code') == '0':
            for d in resp['data'][0].get('details', []):
                if d['ccy'] == ccy:
                    return float(d.get('availBal') or d.get('availEq') or 0)
    except Exception as e:
        log.debug(f"Parse balance {ccy}: {e}")
    return None


def get_all_spot_balances():
    """{ccy: availBal} cho MỌI coin != USDT có số dư available > 0. None nếu API fail.

    Dùng để phát hiện 'spot mồ côi' — coin còn nằm trong tài khoản nhưng KHÔNG có chân
    swap hedge (vd bot bị kill ngay giữa lúc đã mua spot nhưng chưa kịp short)."""
    resp = _retry(lambda: account_api.get_account_balance(),
                  attempts=3, base_delay=0.3, what='all_balances')
    if not resp or resp.get('code') != '0':
        return None
    out = {}
    try:
        for d in resp['data'][0].get('details', []):
            ccy = d.get('ccy')
            if not ccy or ccy == 'USDT':
                continue
            avail = float(d.get('availBal') or d.get('availEq') or 0)
            if avail > 0:
                out[ccy] = avail
    except Exception as e:
        log.debug(f"Parse all balances: {e}")
        return None
    return out


def sell_spot(spot_id, amount):
    """Bán spot market. Tự động co lại số lượng nếu balance available < amount.

    Fix bug 51008: khi balance bị trừ bởi fee / share với strategy khác,
    nếu chênh nhỏ ta vẫn bán hết phần đang có thay vì fail nguyên đơn.
    """
    ccy = spot_id.split('-')[0]
    avail = _get_spot_available(ccy)
    if avail is None:
        # API đọc số dư FAIL → KHÔNG bán mù (rủi ro bán quá tay → 51008, hoặc bán nhầm).
        # Trả False để caller backoff & thử lại; giữ vị thế ở trạng thái cần dọn spot.
        log.warning(f"  [{ccy}] Không đọc được số dư spot (API fail) — hoãn bán, thử lại sau")
        return False
    if avail <= 0:
        log.warning(f"  [{ccy}] Spot balance = 0, bỏ qua sell {spot_id}")
        return True  # không có gì để bán, coi như đã đóng

    min_sz, lot_sz = get_spot_info(spot_id)
    if min_sz is None or lot_sz is None:
        # Không xác định được minSz/lotSz sàn → KHÔNG đoán mù (có thể gửi sz sai bội số
        # lotSz và bị 51020 lặp vô hạn). Backoff, thử lại sau khi đọc được instrument info.
        log.warning(f"  [{ccy}] Không đọc được minSz/lotSz của {spot_id} — hoãn bán, thử lại sau")
        return False
    # Dust DƯỚI minSz của sàn → KHÔNG thể đặt lệnh (51020). Coi như đã đóng để
    # tránh kẹt vòng lặp retry vĩnh viễn (vd: còn 0.000517 TON trong khi minSz=1).
    if avail < min_sz:
        log.warning(f"  [{ccy}] Spot còn {avail:.8f} < minSz {min_sz:g} → dust không bán được, coi như đã đóng {spot_id}")
        return True
    # Co lại số lượng nếu balance thực < amount yêu cầu (chênh do fee/dust)
    if avail < amount:
        log.info(f"  [{ccy}] Spot avail={avail:.8f} < cần {amount:.8f} → bán {avail:.8f}")
        amount = avail * 0.9995  # buffer 0.05% tránh float boundary

    # Fix bug 51020 lặp vô hạn (incident TON 19-23/06, unhedged 3.5 ngày): sàn từ chối
    # sz KHÔNG phải bội số lotSz — avail đọc từ balance gần như không bao giờ tròn lotSz,
    # nên lệnh bị từ chối GIỐNG HỆT mỗi lần retry. Phải làm tròn XUỐNG theo lotSz trước khi gửi.
    if lot_sz > 0:
        amount = math.floor(amount / lot_sz) * lot_sz
    if amount < min_sz:
        log.warning(f"  [{ccy}] Sau khi làm tròn lotSz {lot_sz:g} còn {amount:.8f} < minSz {min_sz:g} "
                    f"→ dust không bán được, coi như đã đóng {spot_id}")
        return True

    sz_str = f"{amount:.8f}".rstrip('0').rstrip('.')
    r = trade_api.place_order(
        instId=spot_id, tdMode="cash",
        side="sell", ordType="market", sz=sz_str,
    )
    if r.get('code') != '0':
        detail = (r.get('data') or [{}])[0]
        log.error(f"  Spot sell lỗi {spot_id} [{detail.get('sCode')}]: {detail.get('sMsg') or r.get('msg')} "
                  f"(avail={avail:.8f} minSz={min_sz:g} lotSz={lot_sz:g} sz_gửi={sz_str})")
        return False
    return True


def check_exit_conditions(position):
    resp = _retry(lambda: public_api.get_funding_rate(instId=position['swap_id']),
                  attempts=3, base_delay=0.3, what=f"check_exit {position['coin']}")
    try:
        if resp and resp.get('code') == '0' and resp.get('data'):
            rate = float(resp['data'][0]['fundingRate'])
            return rate < MIN_STAY_RATE, rate
    except Exception as e:
        log.warning(f"Parse check_exit {position['coin']}: {e}")
    return False, None


def estimate_pnl(position, price=None, funding_override=None):
    """Tính PnL của vị thế arb.

    `price`: giá spot (vd từ WS cache); None → REST.
    `funding_override`: funding THỰC NHẬN từ OKX bills (xem get_funding_income).
        Nếu truyền vào → dùng số thật; nếu None → ước lượng = entry_rate × notional × n_kỳ
        (kém chính xác vì funding đổi mỗi 8h, dùng làm fallback khi bills fail).
    """
    if price is None:
        price = get_spot_price(position['spot_id'])
    if not price:
        return None

    n_payments  = funding_payments_since(position['open_time'])
    if funding_override is not None:
        funding_pnl = funding_override
    else:
        funding_pnl = (position['entry_funding_rate']
                       * position['contracts']
                       * position['ct_val']
                       * price * n_payments)
    price_change  = price - position['entry_price']
    net_price_pnl = (price_change * position['coin_amount']
                     - price_change * position['contracts'] * position['ct_val'])

    notional  = position['contracts'] * position['ct_val'] * price
    fee_est   = notional * (SPOT_FEE_RATE + FUTURES_FEE_RATE) * 2  # round-trip cả 2 legs

    return {
        'price':          price,
        'funding_pnl':    funding_pnl,
        'funding_actual': funding_override is not None,
        'price_pnl':      net_price_pnl,
        'total_pnl':      funding_pnl + net_price_pnl,
        'net_pnl':        funding_pnl + net_price_pnl - fee_est,
        'fee_est':        round(fee_est, 4),
        'n_payments':     n_payments,
    }
