"""
Trend-Following Strategy — OKX SWAP perpetuals, khung trung hạn (4H mặc định).

Signal:
  - BUY  (LONG)  khi EMA_FAST cắt LÊN EMA_SLOW + ADX ≥ ADX_MIN
  - SELL (SHORT) khi EMA_FAST cắt XUỐNG EMA_SLOW + ADX ≥ ADX_MIN

Risk management:
  - Stop loss   = entry − STOP_ATR_MULT × ATR  (long) / ngược lại (short)
  - Trailing    = ratchet theo TRAIL_ATR_MULT × ATR khi giá đi thuận
  - Exit thêm   = EMA crossover ngược chiều

Position sizing:
  - Risk per trade = balance × RISK_PCT
  - Notional       = risk_amount / stop_pct  (capped tại MAX_POS_PCT)
  - Margin         = notional / LEVERAGE
"""
import math
import time
import logging
import datetime as dt
from typing import Optional

import numpy as np
import pandas as pd

from config import public_api, market_api, account_api, trade_api

log = logging.getLogger(__name__)


# ════════════════════ CONFIG ════════════════════
SCAN_COINS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX",
    "DOT", "LINK", "ARB", "OP", "SUI", "TRX", "ATOM",
    "LTC", "BCH", "NEAR", "TON",
]

TIMEFRAME       = "4H"     # khung trung hạn: 4H / 6H / 12H / 1D
CANDLE_LIMIT    = 200      # lấy 200 nến để đủ cho EMA55 + ADX14

EMA_FAST        = 21
EMA_SLOW        = 55
ADX_PERIOD      = 14
ADX_MIN         = 20.0     # chỉ vào khi trend đủ mạnh
ATR_PERIOD      = 14

CROSS_WINDOW    = 3        # cho phép vào lệnh nếu EMA cross trong N nến gần nhất (12h cho 4H)
GAP_PCT_MAX     = 2.0      # continuation entry chỉ khi gap EMA/giá ≤ 2% (chưa quá xa cross)
GAP_PCT_MIN     = 0.05     # và ≥ 0.05% (đủ tách bạch, tránh sideway)
ATR_PCT_MAX     = 8.0      # bỏ qua coin có ATR% > 8% — volatility quá cao, stop loss xa, rủi ro lớn

STOP_ATR_MULT   = 2.0      # stop loss ban đầu
TRAIL_ATR_MULT  = 3.0      # trailing stop sau khi giá đi thuận

RISK_PCT        = 0.015    # 1.5% balance rủi ro mỗi trade
MAX_POS_PCT     = 0.25     # tối đa 25% balance vào 1 trade (cap)
MIN_USDT        = 15.0     # vốn tối thiểu để mở
LEVERAGE        = "5"      # isolated 5x

PARTIAL_TP_ATR_MULT  = 2.0   # đóng 50% khi profit >= 2×ATR
PARTIAL_TP_RATIO     = 0.5   # tỉ lệ đóng một phần

CORRELATED_GROUPS = [
    frozenset({"BTC", "ETH"}),
    frozenset({"SOL", "AVAX", "NEAR"}),
    frozenset({"ARB", "OP"}),
    frozenset({"DOGE", "PEPE", "FLOKI"}),
]


def is_correlated(coin: str, side: str, open_positions: list) -> bool:
    """True nếu đã có vị thế cùng chiều trên coin cùng nhóm tương quan."""
    for group in CORRELATED_GROUPS:
        if coin in group:
            for p in open_positions:
                if p['coin'] in group and p['side'] == side:
                    return True
    return False


# ════════════════════ RETRY HELPER ════════════════════
def _retry(fn, *, attempts=3, base_delay=0.3, what=''):
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


# ════════════════════ MARKET DATA ════════════════════
def get_candles(inst_id, bar=TIMEFRAME, limit=CANDLE_LIMIT) -> Optional[pd.DataFrame]:
    """Trả về DataFrame với cột [ts, open, high, low, close, vol] sắp xếp tăng dần theo thời gian.
    Chỉ giữ nến đã đóng (confirm='1')."""
    resp = _retry(lambda: market_api.get_candlesticks(instId=inst_id, bar=bar, limit=str(limit)),
                  attempts=3, base_delay=0.3, what=f'candles {inst_id}')
    if not resp or resp.get('code') != '0' or not resp.get('data'):
        return None
    rows = []
    for r in resp['data']:
        # OKX format: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        if len(r) < 9:
            continue
        if r[8] != '1':  # chỉ nến đã xác nhận
            continue
        rows.append({
            'ts':    int(r[0]),
            'open':  float(r[1]),
            'high':  float(r[2]),
            'low':   float(r[3]),
            'close': float(r[4]),
            'vol':   float(r[5]),
        })
    if not rows:
        return None
    df = pd.DataFrame(rows).sort_values('ts').reset_index(drop=True)
    return df


def get_available_usdt():
    resp = _retry(lambda: account_api.get_account_balance(ccy="USDT"),
                  attempts=3, base_delay=0.3, what='account_balance')
    try:
        if resp and resp.get('code') == '0':
            for d in resp['data'][0].get('details', []):
                if d['ccy'] == 'USDT':
                    return float(d['availEq'])
    except Exception as e:
        log.error(f"Parse balance: {e}")
    return 0.0


def get_last_price(inst_id):
    resp = _retry(lambda: market_api.get_ticker(instId=inst_id),
                  attempts=3, base_delay=0.2, what=f'ticker {inst_id}')
    try:
        if resp and resp.get('code') == '0' and resp.get('data'):
            return float(resp['data'][0]['last'])
    except Exception:
        pass
    return None


def get_okx_swap_positions():
    """Trả dict {coin: {inst_id, pos_sz, side, avg_px}} các vị thế SWAP còn mở trên OKX.
    Trả None nếu API fail."""
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
            # posSide ∈ {'long','short','net'} với one-way mode pos>0=long pos<0=short
            side = r.get('posSide') or ('LONG' if pos_sz > 0 else 'SHORT')
            out[coin] = {
                'inst_id': inst,
                'pos':     pos_sz,
                'side':    side.upper() if side else None,
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


def get_trend_1d(coin: str):
    """Trend 1D theo EMA21/55. Trả 'UP'/'DOWN'/'SIDE' hoặc None nếu fetch thất bại."""
    inst_id = f"{coin}-USDT-SWAP"
    df = get_candles(inst_id, bar='1D', limit=80)
    if df is None or len(df) < 60:
        return None
    df = add_indicators(df, ema_fast=21, ema_slow=55)
    last = df.iloc[-1]
    if last['ema_fast'] > last['ema_slow']:
        return 'UP'
    if last['ema_fast'] < last['ema_slow']:
        return 'DOWN'
    return 'SIDE'


# ════════════════════ INDICATORS ════════════════════
def add_indicators(df: pd.DataFrame,
                   ema_fast=EMA_FAST, ema_slow=EMA_SLOW,
                   atr_n=ATR_PERIOD, adx_n=ADX_PERIOD) -> pd.DataFrame:
    """Thêm cột: ema_fast, ema_slow, atr, adx, plus_di, minus_di."""
    df = df.copy()
    close = df['close']
    high  = df['high']
    low   = df['low']

    df['ema_fast'] = close.ewm(span=ema_fast, adjust=False).mean()
    df['ema_slow'] = close.ewm(span=ema_slow, adjust=False).mean()

    # ATR (Wilder smoothing via ewm alpha=1/n)
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df['atr'] = tr.ewm(alpha=1 / atr_n, adjust=False).mean()

    # ADX
    up_move   = high.diff()
    down_move = -low.diff()
    plus_dm  = ((up_move > down_move) & (up_move > 0)).astype(float) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)).astype(float) * down_move
    plus_dm  = plus_dm.fillna(0)
    minus_dm = minus_dm.fillna(0)

    atr_safe = df['atr'].replace(0, np.nan)
    plus_di  = 100 * plus_dm.ewm(alpha=1 / adx_n, adjust=False).mean()  / atr_safe
    minus_di = 100 * minus_dm.ewm(alpha=1 / adx_n, adjust=False).mean() / atr_safe
    di_sum   = (plus_di + minus_di).replace(0, np.nan)
    dx       = ((plus_di - minus_di).abs() / di_sum) * 100
    df['plus_di']  = plus_di.fillna(0).astype(float)
    df['minus_di'] = minus_di.fillna(0).astype(float)
    df['adx']      = dx.fillna(0).astype(float).ewm(alpha=1 / adx_n, adjust=False).mean()

    return df


# ════════════════════ SIGNAL ════════════════════
def evaluate(df: pd.DataFrame, adx_min=ADX_MIN, cross_window=CROSS_WINDOW) -> dict:
    """Trả về snapshot signal trên nến cuối cùng đã đóng.

    Entry conditions (đáp ứng 1 trong 2):
      1. FRESH CROSS — EMA21 cắt EMA55 trong `cross_window` nến gần nhất + ADX≥min + vol OK
      2. CONTINUATION — trend đã xác lập + ADX≥min + ADX đang tăng + gap_pct ∈ [GAP_MIN, GAP_MAX]
         (gap không quá xa cross point ⇒ chưa miss trend quá nhiều)
    """
    if df is None or len(df) < max(EMA_SLOW, ADX_PERIOD) + 5:
        return {'signal': None, 'reason': 'not_enough_data'}

    df = add_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]
    avg_vol = df['vol'].iloc[-21:-1].mean()
    vol_ok  = bool(last['vol'] > avg_vol) if avg_vol > 0 else True

    # ── Cross window: tìm cú cắt EMA trong N nến gần nhất ──
    cross_up = cross_down = False
    cross_age = None  # tuổi cross tính theo số nến (0 = vừa cross ở nến cuối)
    n_lookback = min(cross_window, len(df) - 1)
    for i in range(n_lookback):
        cur = df.iloc[-1 - i]
        prv = df.iloc[-2 - i]
        if prv['ema_fast'] <= prv['ema_slow'] and cur['ema_fast'] > cur['ema_slow']:
            cross_up = True
            cross_age = i
            break
        if prv['ema_fast'] >= prv['ema_slow'] and cur['ema_fast'] < cur['ema_slow']:
            cross_down = True
            cross_age = i
            break

    trend_up = last['ema_fast'] > last['ema_slow']
    trend_dn = last['ema_fast'] < last['ema_slow']
    strong   = float(last['adx']) >= adx_min
    # ADX rising = trend đang gia tăng (filter cho continuation)
    adx_rising = bool(last['adx'] > prev['adx'])

    gap_pct = (float(last['ema_fast']) - float(last['ema_slow'])) / float(last['close']) * 100
    gap_ok_long  = GAP_PCT_MIN <=  gap_pct <= GAP_PCT_MAX
    gap_ok_short = GAP_PCT_MIN <= -gap_pct <= GAP_PCT_MAX

    snap = {
        'ts':         int(last['ts']),
        'close':      float(last['close']),
        'ema_fast':   float(last['ema_fast']),
        'ema_slow':   float(last['ema_slow']),
        'gap_pct':    gap_pct,
        'atr':        float(last['atr']),
        'atr_pct':    float(last['atr']) / float(last['close']) * 100,
        'adx':        float(last['adx']),
        'adx_rising': adx_rising,
        'plus_di':    float(last['plus_di']),
        'minus_di':   float(last['minus_di']),
        'trend':      'UP' if trend_up else ('DOWN' if trend_dn else 'SIDE'),
        'strong':     strong,
        'vol_ok':     vol_ok,
        'cross_up':   bool(cross_up),
        'cross_down': bool(cross_down),
        'cross_age':  cross_age,
    }

    # ── Quyết định signal ──
    if cross_up and strong and vol_ok:
        snap['signal'] = 'LONG'
        snap['reason'] = f'fresh_cross_up(age={cross_age})+strong+vol'
    elif cross_down and strong and vol_ok:
        snap['signal'] = 'SHORT'
        snap['reason'] = f'fresh_cross_down(age={cross_age})+strong+vol'
    elif trend_up and strong and adx_rising and gap_ok_long and vol_ok:
        snap['signal'] = 'LONG'
        snap['reason'] = f'continuation_up(gap={gap_pct:.2f}%)+adx_rising'
    elif trend_dn and strong and adx_rising and gap_ok_short and vol_ok:
        snap['signal'] = 'SHORT'
        snap['reason'] = f'continuation_down(gap={gap_pct:.2f}%)+adx_rising'
    else:
        snap['signal'] = None
        reasons = [f"trend={snap['trend']}", f"adx={snap['adx']:.1f}"]
        if not strong:    reasons.append('adx_yếu')
        if not vol_ok:    reasons.append('vol_thấp')
        if not adx_rising: reasons.append('adx_giảm')
        if trend_up and not gap_ok_long:
            reasons.append(f'gap={gap_pct:.2f}%_ngoài[{GAP_PCT_MIN},{GAP_PCT_MAX}]')
        if trend_dn and not gap_ok_short:
            reasons.append(f'gap={gap_pct:.2f}%_ngoài[-{GAP_PCT_MAX},-{GAP_PCT_MIN}]')
        snap['reason'] = ', '.join(reasons)
    return snap


# ════════════════════ POSITION SIZING ════════════════════
def calc_position_size(balance, entry_price, atr, risk_pct=RISK_PCT,
                       max_pos_pct=MAX_POS_PCT, leverage=float(LEVERAGE)):
    """Trả về (notional_usdt, margin_usdt) theo ATR risk model."""
    if atr <= 0 or entry_price <= 0 or balance <= 0:
        return 0.0, 0.0
    risk_amount  = balance * risk_pct
    stop_dist    = STOP_ATR_MULT * atr
    stop_pct     = stop_dist / entry_price
    notional     = risk_amount / stop_pct if stop_pct > 0 else 0
    cap          = balance * max_pos_pct * leverage  # cap theo % balance × đòn bẩy
    notional     = min(notional, cap)
    margin       = notional / leverage
    return notional, margin


# ════════════════════ ORDER EXECUTION ════════════════════
def _fmt_sz(contracts, lot_sz):
    decimals = len(str(lot_sz).rstrip('0').split('.')[-1]) if '.' in str(lot_sz) else 0
    return str(round(contracts, decimals))


def _set_leverage(swap_id):
    try:
        account_api.set_leverage(instId=swap_id, lever=LEVERAGE, mgnMode="isolated")
    except Exception as e:
        log.warning(f"set_leverage {swap_id}: {e}")


def open_position(coin: str, side: str, notional_usdt: float, snap: dict) -> Optional[dict]:
    """Mở vị thế trên SWAP. side ∈ {'LONG','SHORT'}."""
    swap_id = f"{coin}-USDT-SWAP"
    price   = get_last_price(swap_id) or snap.get('close')
    if not price:
        log.warning(f"[{coin}] không lấy được giá")
        return None

    ct_val, min_sz, lot_sz = get_swap_info(swap_id)
    if ct_val is None:
        return None

    raw_contracts = (notional_usdt / price) / ct_val
    steps         = math.floor(round(raw_contracts / lot_sz, 8))
    contracts     = steps * lot_sz

    if contracts < min_sz:
        min_cost = min_sz * ct_val * price
        log.warning(f"[{coin}] vốn ${notional_usdt:.2f} < tối thiểu ${min_cost:.2f}")
        return None

    sz_str = _fmt_sz(contracts, lot_sz)
    _set_leverage(swap_id)

    okx_side = 'buy' if side == 'LONG' else 'sell'
    r = trade_api.place_order(
        instId=swap_id, tdMode="isolated",
        side=okx_side, ordType="market", sz=sz_str,
    )
    if r.get('code') != '0':
        d = (r.get('data') or [{}])[0]
        log.error(f"[{coin}] {side} mở lỗi [{d.get('sCode')}]: {d.get('sMsg') or r.get('msg')}")
        return None

    atr = float(snap.get('atr') or 0)
    stop = (price - STOP_ATR_MULT * atr) if side == 'LONG' else (price + STOP_ATR_MULT * atr)
    return {
        'coin':         coin,
        'swap_id':      swap_id,
        'side':         side,
        'contracts':    contracts,
        'lot_sz':       lot_sz,
        'ct_val':       ct_val,
        'entry_price':  price,
        'entry_atr':    atr,
        'stop_price':   stop,
        'trail_anchor': price,           # giá cao nhất (long) / thấp nhất (short) đạt được
        'open_time':    time.time(),
        'notional':     contracts * ct_val * price,
    }


def close_position(position: dict) -> bool:
    sz_str = _fmt_sz(position['contracts'], position['lot_sz'])
    okx_side = 'sell' if position['side'] == 'LONG' else 'buy'
    r = trade_api.place_order(
        instId=position['swap_id'], tdMode="isolated",
        side=okx_side, ordType="market",
        sz=sz_str, reduceOnly="true",
    )
    if r.get('code') != '0':
        d = (r.get('data') or [{}])[0]
        log.error(f"[{position['coin']}] đóng lỗi [{d.get('sCode')}]: {d.get('sMsg') or r.get('msg')}")
        return False
    return True


def partial_close_position(position: dict, ratio: float = PARTIAL_TP_RATIO) -> bool:
    """Đóng một phần vị thế (mặc định 50%). Cập nhật contracts trong position dict."""
    decimals = len(str(position['lot_sz']).rstrip('0').split('.')[-1]) if '.' in str(position['lot_sz']) else 0
    half_steps = round(position['contracts'] * ratio / position['lot_sz'])
    half       = round(half_steps * position['lot_sz'], max(decimals, 8))
    if half < position['lot_sz']:
        return False
    sz_str   = _fmt_sz(half, position['lot_sz'])
    okx_side = 'sell' if position['side'] == 'LONG' else 'buy'
    r = trade_api.place_order(
        instId=position['swap_id'], tdMode="isolated",
        side=okx_side, ordType="market",
        sz=sz_str, reduceOnly="true",
    )
    if r.get('code') != '0':
        d = (r.get('data') or [{}])[0]
        log.error(f"[{position['coin']}] partial close lỗi: {d.get('sMsg') or r.get('msg')}")
        return False
    position['contracts'] -= half
    position['notional']   = position['contracts'] * position['ct_val'] * position['entry_price']
    return True


# ════════════════════ PnL + STOP MANAGEMENT ════════════════════
def update_pnl_and_stop(position: dict, last_price: float, atr_now: float = None) -> dict:
    """Tính PnL hiện tại + cập nhật trailing stop (mutate position)."""
    side = position['side']
    entry = position['entry_price']
    qty   = position['contracts'] * position['ct_val']  # coin amount
    notional = position['notional']

    if side == 'LONG':
        price_pnl = (last_price - entry) * qty
        position['trail_anchor'] = max(position['trail_anchor'], last_price)
        atr = atr_now if atr_now is not None else position['entry_atr']
        new_stop = position['trail_anchor'] - TRAIL_ATR_MULT * atr
        position['stop_price'] = max(position['stop_price'], new_stop)
        hit_stop = last_price <= position['stop_price']
    else:  # SHORT
        price_pnl = (entry - last_price) * qty
        position['trail_anchor'] = min(position['trail_anchor'], last_price)
        atr = atr_now if atr_now is not None else position['entry_atr']
        new_stop = position['trail_anchor'] + TRAIL_ATR_MULT * atr
        position['stop_price'] = min(position['stop_price'], new_stop)
        hit_stop = last_price >= position['stop_price']

    pct = (price_pnl / notional * 100) if notional else 0

    return {
        'price':     last_price,
        'price_pnl': price_pnl,
        'pct':       pct,
        'hit_stop':  hit_stop,
        'stop':      position['stop_price'],
        'atr':       atr,
    }


# ════════════════════ CANDLE-CLOSE DETECTION ════════════════════
def timeframe_to_seconds(bar: str) -> int:
    bar = bar.upper().strip()
    if bar.endswith('H'):    return int(bar[:-1]) * 3600
    if bar.endswith('D'):    return int(bar[:-1]) * 86400
    if bar.endswith('M'):    return int(bar[:-1]) * 60
    return 4 * 3600


def last_closed_candle_ts(bar: str = TIMEFRAME, now: float = None) -> int:
    """Trả về timestamp (ms) của nến gần nhất ĐÃ đóng tính từ now (mặc định = thời gian hiện tại)."""
    if now is None:
        now = time.time()
    sec = timeframe_to_seconds(bar)
    # OKX bar boundaries align với UTC midnight cho 4H/12H/1D
    boundary = int(now // sec) * sec
    # Nến vừa đóng kết thúc tại boundary, nến đó MỞ tại boundary - sec
    return int((boundary - sec) * 1000)
