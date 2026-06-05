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
    "LTC", "BCH", "NEAR", "TON", "PEPE", "FLOKI",
]

# ── Ngưỡng vào/ra (đã chỉnh để KHÔNG churn lỗ phí) ──────────────────
# Phí round-trip = (0.1% spot + 0.05% swap) × 2 chân = 0.30% notional.
# Trước đây MIN_FUNDING_RATE=0.01%/8h: cần ~30 kỳ funding (~10 ngày) mới hoà phí,
# nhưng bot lại thoát sau vài giờ → lỗ phí 100%. Nâng ngưỡng để 1 lệnh kỳ vọng
# thu đủ funding bù phí.
MIN_FUNDING_RATE = 0.0015    # 0.15%/8h — sàn vào lệnh (≈3 kỳ funding > phí round-trip)
POSITION_PCT     = 0.15      # 15% số dư mỗi vị thế (giảm từ 30% → hạ phí tuyệt đối)
MIN_USDT         = 50.0      # bỏ qua lệnh quá nhỏ (phí cố định lấn át funding)
LEVERAGE         = "5"

SPOT_FEE_RATE    = 0.001    # 0.10% taker — spot
FUTURES_FEE_RATE = 0.0005   # 0.05% taker — futures
ROUND_TRIP_FEE   = (SPOT_FEE_RATE + FUTURES_FEE_RATE) * 2   # 0.30% — mở+đóng cả 2 chân
FEE_SAFETY       = 1.5      # biên an toàn: funding kỳ vọng phải vượt phí FEE_SAFETY lần
ENTRY_MIN_SETTLEMENTS = 3   # số kỳ funding kỳ vọng giữ — dùng cho cổng EV vào lệnh
EXIT_MIN_SETTLEMENTS  = 2   # KHÔNG thoát funding_flip trước khi thu đủ N kỳ funding
MIN_STAY_RATE    = 0.00005  # 0.005%/8h — rate thấp hơn mức này coi như "flip"
PRICE_STOP_PCT   = 0.05     # thoát khi price_pnl < -5% notional (nới: vị thế đã hedge nên 2% là nhiễu basis)


def adaptive_position_pct(funding_rate: float) -> float:
    """Scale vốn theo funding rate — rate cao vào nhiều hơn (đã hạ trần để giảm phí)."""
    if funding_rate >= 0.005:     # >= 0.5%/8h — cơ hội rất tốt
        return 0.20
    elif funding_rate >= 0.002:   # >= 0.2%/8h
        return POSITION_PCT       # 0.15
    else:
        return 0.10


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
    """Rate kỳ vọng THỰC THU ở (các) settlement sắp tới.

    Lý do: `funding_rate` của OKX là rate VỪA TRẢ ở kỳ trước (nhìn lại quá khứ),
    còn cái ta sẽ nhận là `nextFundingRate` (dự báo kỳ kế). Vào lệnh theo spike rate
    hiện tại đã gây loạt lệnh npay=0 lỗ phí trắng (NEAR 1.5%→-6$, OP 1.5%→-4$ trong 6 phút):
    rate hiện tại cao nhưng next≈0 nên thực thu = 0.

    Trả về rate dùng cho cổng EV vào lệnh:
      - Có cả next & current dương → lấy min (thận trọng, phải cùng xác nhận).
      - Chỉ current dương, next≈0 → coi như 0 (KHÔNG vào — chính là spike đảo chiều).
    """
    nr  = opp.get('next_rate') or 0.0
    cur = opp.get('funding_rate') or 0.0
    if nr > 0 and cur > 0:
        return min(nr, cur)
    return max(0.0, nr)


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
            total += float(b.get('balChg') or b.get('pnl') or 0)
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


def _set_leverage(swap_id):
    try:
        account_api.set_leverage(instId=swap_id, lever=LEVERAGE, mgnMode="isolated")
    except Exception as e:
        log.warning(f"set_leverage {swap_id}: {e}")


def _fmt_contracts(contracts, lot_sz):
    """Format contracts theo độ chính xác của lot_sz."""
    decimals = len(str(lot_sz).rstrip('0').split('.')[-1]) if '.' in str(lot_sz) else 0
    return str(round(contracts, decimals))


def _get_filled_qty(ord_id, inst_id, fallback=0.0):
    """Query order vừa khớp → trả về (accFillSz, avgPx). Retry nhẹ vì OKX có thể chưa cập nhật ngay."""
    for _ in range(3):
        time.sleep(0.25)
        try:
            r = trade_api.get_order(instId=inst_id, ordId=ord_id)
            if r and r.get('code') == '0' and r.get('data'):
                d = r['data'][0]
                acc = float(d.get('accFillSz') or 0)
                avg = float(d.get('avgPx') or 0)
                if acc > 0:
                    return acc, avg
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

    _set_leverage(swap_id)

    # Mua spot bằng USDT
    r_spot = trade_api.place_order(
        instId=spot_id, tdMode="cash",
        side="buy", ordType="market",
        sz=str(spot_usdt), tgtCcy="quote_ccy",
    )
    if r_spot.get('code') != '0':
        detail = (r_spot.get('data') or [{}])[0]
        log.error(f"  [{coin}] Spot buy lỗi [{detail.get('sCode')}]: {detail.get('sMsg') or r_spot.get('msg')}")
        return None

    # Lấy actual filled qty từ order — fix bug 51008 "insufficient spot balance"
    # Lý do: mua tgtCcy=quote_ccy với fee 0.1% sẽ nhận về ÍT hơn estimate (usdt/price).
    # Phải dùng accFillSz làm coin_amount để sell sau này không vượt balance.
    spot_ord_id = (r_spot.get('data') or [{}])[0].get('ordId', '')
    estimate_amt = round(contracts * ct_val, 8)
    actual_spot, actual_px = _get_filled_qty(spot_ord_id, spot_id, fallback=estimate_amt)
    # Buffer thêm 0.05% (fee biến động theo VIP tier + slippage) để tránh sell vượt
    coin_amount = round(actual_spot * 0.9995, 8) if actual_spot > 0 else estimate_amt

    time.sleep(0.3)

    # Short futures isolated 1x
    r_swap = trade_api.place_order(
        instId=swap_id, tdMode="isolated",
        side="sell", ordType="market", sz=sz_str,
    )
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
        'entry_price':        actual_px or price,
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
        log.error(f"  [{coin}] Đóng futures lỗi [{detail.get('sCode')}]: {detail.get('sMsg') or r_swap.get('msg')}")
        return False

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


def sell_spot(spot_id, amount):
    """Bán spot market. Tự động co lại số lượng nếu balance available < amount.

    Fix bug 51008: khi balance bị trừ bởi fee / share với strategy khác,
    nếu chênh nhỏ ta vẫn bán hết phần đang có thay vì fail nguyên đơn.
    """
    ccy = spot_id.split('-')[0]
    avail = _get_spot_available(ccy)
    if avail is not None and avail > 0:
        # Co lại số lượng nếu balance thực < amount yêu cầu (chênh do fee/dust)
        if avail < amount:
            log.info(f"  [{ccy}] Spot avail={avail:.8f} < cần {amount:.8f} → bán {avail:.8f}")
            amount = avail * 0.9995  # buffer 0.05% tránh float boundary
    elif avail == 0:
        log.warning(f"  [{ccy}] Spot balance = 0, bỏ qua sell {spot_id}")
        return True  # không có gì để bán, coi như đã đóng

    r = trade_api.place_order(
        instId=spot_id, tdMode="cash",
        side="sell", ordType="market", sz=str(round(amount, 8)),
    )
    if r.get('code') != '0':
        detail = (r.get('data') or [{}])[0]
        log.error(f"  Spot sell lỗi {spot_id} [{detail.get('sCode')}]: {detail.get('sMsg') or r.get('msg')}")
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
