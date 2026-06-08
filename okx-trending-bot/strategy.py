"""
Trend-Following Strategy — OKX SWAP perpetuals, khung NGẮN HẠN (1H mặc định).

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
import os
import math
import time
import logging
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
    # Mở rộng để có đủ tín hiệu ≥5 lệnh/ngày trên khung 15m (đều là SWAP thanh khoản cao trên OKX)
    "PEPE", "FLOKI", "APT", "INJ", "TIA", "SEI", "WLD", "FIL", "AAVE", "LDO",
]

TIMEFRAME       = "1H"     # khung 1H: ~24 nến/ngày × 29 coin ⇒ ~5 lệnh/ngày, EV tốt hơn HẲN 15m.
                          # (Backtest majors: 1H ADX30 = −16%/50d vs 15m = −40~94% — 15m churn/whipsaw
                          # bleed nặng. 4H gốc của user = −45%/100d. Xem _bt_sweep2.py.)
CANDLE_LIMIT    = 200      # lấy 200 nến (≈8 ngày trên 1H) — đủ cho EMA55 + ADX14 ổn định

EMA_FAST        = 21
EMA_SLOW        = 55
ADX_PERIOD      = 14
ADX_MIN         = 30.0     # nâng 15→30: lọc chop mạnh, chỉ vào trend đã xác lập. Backtest: ADX30 cải thiện
                          # PF 0.38→0.77 vs ADX15. (ADX35 EV tốt nhất −6.7% nhưng chỉ 3.3 lệnh/ngày <5.)
ATR_PERIOD      = 14

CROSS_WINDOW    = 4        # nới 3→4: bắt cú cắt EMA trong 4 nến gần nhất (16h cho 4H) — vào lệnh dễ hơn
GAP_PCT_MAX     = 6.0      # continuation entry khi gap EMA/giá ≤ 6% (nới 4→6: vào dễ hơn trong
                          # trend kéo dài, vẫn loại move giãn quá xa >6% — rủi ro đảo chiều cao)
GAP_PCT_MIN     = 0.05     # và ≥ 0.05% (đủ tách bạch, tránh sideway)
ATR_PCT_MAX     = 6.0      # khung 1H: ATR%/nến vừa phải; >6%/nến là biến động cực đoan → bỏ (hạ 8→6 cho khớp TF)
VOL_FACTOR      = 0.5      # vol_ok khi vol > 0.5× trung bình (nới 1.0→0.5: code cũ ngầm dùng 1.0×; trend âm ỉ vol thấp vẫn vào được)
ADX_STRONG      = 28.0     # continuation: ADX ≥ mức này coi như trend đã vững (hạ 35→28 — mở rộng nhánh continuation)

STOP_ATR_MULT   = 3.0      # stop loss ban đầu (2.0→3.0): nới để noise 1H không stop sớm; ATR-sizing giữ $risk cố định
TRAIL_ATR_MULT  = 5.0      # trailing stop (2.5→5.0): ĐỂ WINNER CHẠY — đòn bẩy EV lớn nhất. Backtest: trail rộng + tắt
                          # partial-TP nâng PF 0.38→0.89 (trước đây winner bị cắt sớm còn loser chạy tới stop)

RISK_PCT        = 0.0075   # 0.75% balance rủi ro mỗi trade (giảm trong giai đoạn validate stop thật)
MAX_POS_PCT     = 0.15     # tối đa 15% balance vào 1 trade (cap)
MIN_USDT        = 15.0     # vốn tối thiểu để mở
LEVERAGE        = "5"      # isolated 5x
MAX_LOSS_PCT    = 0.06     # kill-switch: lỗ > 6% notional → đóng NGAY (không chờ nến/EMA reverse)
ALGO_AMEND_MIN_MOVE = 0.0015  # chỉ dời stop THẬT trên sàn khi trigger đổi ≥0.15% (tránh spam API)

ENABLE_PARTIAL_TP    = False # TẮT partial-TP: cắt winner sớm trong khi loser chạy tới stop ⇒ âm EV.
                            # Trailing stop lo việc bảo vệ lãi. (Thay magic PARTIAL_TP_ATR_MULT=99 cũ.)
PARTIAL_TP_ATR_MULT  = 2.0   # mức kích partial-TP khi BẬT (đóng 50% ở 2×ATR + khóa breakeven)
PARTIAL_TP_RATIO     = 0.5   # tỉ lệ đóng một phần

# ════════════════════ MULTI-TIMEFRAME REGIME (ĐẠI TU) ════════════════════
# Triết lý mới: KHÔNG chạy theo mọi cú cắt EMA 1H (đó là cách thua đã kiểm chứng).
# Chỉ vào lệnh khi 3 tầng ĐỒNG THUẬN:
#   1) Regime khung lớn 4H: EMA21/55 trên 4H phải cùng chiều VÀ 4H đang trending (ADX≥ngưỡng)
#      → loại sideway/chop, nơi crossover 1H bleed phí.
#   2) Regime thị trường (BTC 4H): không LONG alt khi BTC giảm, không SHORT alt khi BTC tăng
#      → cắt rủi ro tương quan (khi BTC dump, alt dump theo).
#   3) Trigger 1H: cú cắt/continuation EMA 1H (evaluate) — chỉ là TIMING trong trend 4H đã xác lập.
HTF_TIMEFRAME = "4H"        # khung regime
HTF_EMA_FAST  = 21
HTF_EMA_SLOW  = 55
HTF_ADX_MIN   = 22.0        # 4H ADX ≥ mức này mới coi là "đang trending" (regime gate)
BTC_REGIME_COIN = "BTC"     # coin đại diện thị trường

# ── Vốn dành cho TREND khi DÙNG CHUNG tài khoản với arb-bot ──
# Mỗi bot chỉ triển khai tối đa CAPITAL_FRACTION × equity (arb + trend ≤ 1.0).
CAPITAL_FRACTION = float(os.getenv('TREND_CAPITAL_FRACTION', '0.5'))

CORRELATED_GROUPS = [
    frozenset({"BTC", "ETH"}),
    frozenset({"SOL", "AVAX", "NEAR", "APT", "SUI", "SEI"}),
    frozenset({"ARB", "OP"}),
    frozenset({"DOGE", "PEPE", "FLOKI", "WLD"}),
    frozenset({"LINK", "AAVE", "LDO", "INJ"}),
    frozenset({"DOT", "ATOM", "TIA", "NEAR"}),
    frozenset({"LTC", "BCH"}),
    frozenset({"ADA", "XRP", "TRX"}),
    frozenset({"FIL"}),
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


# ════════════════════ MULTI-TIMEFRAME REGIME (ĐẠI TU) ════════════════════
def get_htf_regime(coin: str):
    """Regime khung 4H. Trả (direction, adx):
       - 'UP'/'DOWN' nếu 4H đang trending (ADX≥HTF_ADX_MIN) và EMA cùng chiều
       - 'SIDE' nếu 4H không trending (chop) → KHÔNG trade
       Trả None nếu fetch thất bại (caller coi như không đủ điều kiện)."""
    df = get_candles(f"{coin}-USDT-SWAP", bar=HTF_TIMEFRAME, limit=200)
    if df is None or len(df) < 60:
        return None
    df = add_indicators(df, ema_fast=HTF_EMA_FAST, ema_slow=HTF_EMA_SLOW)
    last = df.iloc[-1]
    adx = float(last['adx'])
    if adx < HTF_ADX_MIN:
        return ('SIDE', adx)
    if last['ema_fast'] > last['ema_slow']:
        return ('UP', adx)
    if last['ema_fast'] < last['ema_slow']:
        return ('DOWN', adx)
    return ('SIDE', adx)


def get_btc_regime():
    """Regime thị trường chung theo BTC 4H: 'UP'/'DOWN'/'SIDE'/None."""
    r = get_htf_regime(BTC_REGIME_COIN)
    return r[0] if r else None


def regime_allows(side: str, htf_dir, btc_dir) -> bool:
    """Cổng regime đa khung (loại chop + rủi ro tương quan BTC).

    LONG  : 4H phải UP   và BTC KHÔNG đang DOWN.
    SHORT : 4H phải DOWN và BTC KHÔNG đang UP.
    htf_dir None/SIDE → từ chối (4H không trending hoặc fetch fail)."""
    if not htf_dir or htf_dir == 'SIDE':
        return False
    if side == 'LONG':
        return htf_dir == 'UP' and btc_dir != 'DOWN'
    if side == 'SHORT':
        return htf_dir == 'DOWN' and btc_dir != 'UP'
    return False


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
    vol_ok  = bool(last['vol'] > VOL_FACTOR * avg_vol) if avg_vol > 0 else True

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
    # Continuation OK khi ADX đang tăng HOẶC đã rất mạnh (≥ADX_STRONG) — trend vững thì không
    # bắt buộc phải đang tăng (nhiều trend mạnh ADX cao đã plateau).
    adx_ok_cont = bool(adx_rising or float(last['adx']) >= ADX_STRONG)

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
    elif trend_up and strong and adx_ok_cont and gap_ok_long and vol_ok:
        snap['signal'] = 'LONG'
        snap['reason'] = f'continuation_up(gap={gap_pct:.2f}%,adx={float(last["adx"]):.0f})'
    elif trend_dn and strong and adx_ok_cont and gap_ok_short and vol_ok:
        snap['signal'] = 'SHORT'
        snap['reason'] = f'continuation_down(gap={gap_pct:.2f}%,adx={float(last["adx"]):.0f})'
    else:
        snap['signal'] = None
        reasons = [f"trend={snap['trend']}", f"adx={snap['adx']:.1f}"]
        if not strong:    reasons.append('adx_yếu')
        if not vol_ok:    reasons.append('vol_thấp')
        if not adx_ok_cont: reasons.append(f'adx_không_tăng&<{ADX_STRONG:.0f}')
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


# ════════════════════ NATIVE STOP (ALGO ORDER trên SÀN) ════════════════════
# Đặt stop-loss THẬT trên OKX để vị thế được bảo vệ NGAY CẢ KHI bot tắt/crash/mất mạng.
# Đây là fix cho sự cố BNB -525$ (bot tắt 4 ngày, stop chỉ chạy trong RAM nên vô dụng).
def _close_side(side: str) -> str:
    return 'sell' if side == 'LONG' else 'buy'


def place_stop_algo(swap_id: str, side: str, sz_str: str, stop_px: float) -> Optional[str]:
    """Đặt stop-loss conditional (market khi trigger, reduceOnly). Trả algoId hoặc None."""
    try:
        r = trade_api.place_algo_order(
            instId=swap_id, tdMode="isolated",
            side=_close_side(side), ordType="conditional",
            sz=sz_str, reduceOnly="true",
            slTriggerPx=str(stop_px), slOrdPx="-1",   # -1 = đóng market khi chạm trigger
            slTriggerPxType="last",
        )
        if r.get('code') == '0' and r.get('data'):
            return (r['data'][0] or {}).get('algoId') or None
        d = (r.get('data') or [{}])[0]
        log.warning(f"[{swap_id}] đặt stop algo lỗi [{d.get('sCode')}]: {d.get('sMsg') or r.get('msg')}")
    except Exception as e:
        log.warning(f"[{swap_id}] đặt stop algo exception: {e}")
    return None


def cancel_stop_algo(swap_id: str, algo_id: Optional[str]) -> None:
    """Hủy stop algo (best-effort, im lặng nếu đã không còn)."""
    if not algo_id:
        return
    try:
        trade_api.cancel_algo_order([{'algoId': algo_id, 'instId': swap_id}])
    except Exception as e:
        log.debug(f"[{swap_id}] hủy stop algo {algo_id}: {e}")


def amend_stop_algo(swap_id: str, algo_id: Optional[str],
                    new_stop: float = None, new_sz: str = None) -> bool:
    """Cập nhật trigger/size của stop algo (dùng cho trailing & partial-TP). Trả True nếu OK."""
    if not algo_id:
        return False
    kwargs = dict(instId=swap_id, algoId=algo_id)
    if new_stop is not None:
        kwargs['newSlTriggerPx'] = str(new_stop)
    if new_sz is not None:
        kwargs['newSz'] = str(new_sz)
    try:
        r = trade_api.amend_algo_order(**kwargs)
        return r.get('code') == '0'
    except Exception as e:
        log.debug(f"[{swap_id}] amend stop algo {algo_id}: {e}")
        return False


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

    # Đặt stop-loss THẬT trên sàn (sống sót khi bot offline). Không chặn lệnh nếu fail — chỉ cảnh báo.
    sl_algo_id = place_stop_algo(swap_id, side, sz_str, round(stop, 8)) if atr > 0 else None
    if sl_algo_id:
        log.info(f"[{coin}] Stop THẬT trên sàn ✓ algoId={sl_algo_id} @ ${stop:.4f}")
    else:
        log.warning(f"[{coin}] ⚠ KHÔNG đặt được stop thật trên sàn — chỉ còn stop phần mềm (rủi ro khi bot tắt)")

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
        'sl_algo_id':   sl_algo_id,         # id lệnh stop trên sàn (persist để hủy/sửa sau)
        'sl_algo_px':   round(stop, 8),     # trigger hiện tại của stop trên sàn
        'trail_anchor': price,           # giá cao nhất (long) / thấp nhất (short) đạt được
        'open_time':    time.time(),
        'notional':     contracts * ct_val * price,
    }


def close_position(position: dict) -> bool:
    # KHÔNG hủy stop algo ở đây: giữ nó tới khi _close_one xác nhận đã đóng (nếu đóng fail thì vị thế
    # vẫn còn stop bảo vệ). reduceOnly khiến algo + close không thể đóng lố nhau.
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


def _get_filled(ord_id, inst_id, attempts=6, delay=0.4):
    """Trả accFillSz THỰC của order (0 nếu chưa/không khớp). Retry vì OKX settle ~1s.
    Dùng để KHÔNG trừ contracts local khi lệnh chưa khớp thật (→ lệch size với sàn)."""
    if not ord_id:
        return 0.0
    for _ in range(attempts):
        time.sleep(delay)
        try:
            r = trade_api.get_order(instId=inst_id, ordId=ord_id)
            if r and r.get('code') == '0' and r.get('data'):
                d = r['data'][0]
                acc = float(d.get('accFillSz') or 0)
                if acc > 0:
                    return acc
                if d.get('state') in ('canceled', 'filled'):
                    break
        except Exception as e:
            log.debug(f"get_order {ord_id}: {e}")
    return 0.0


def partial_close_position(position: dict, ratio: float = PARTIAL_TP_RATIO) -> bool:
    """Đóng một phần vị thế (mặc định 50%). CHỈ trừ contracts theo lượng KHỚP THẬT
    (accFillSz) — tránh lệch size với sàn → 51169 khi đóng nốt."""
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

    # VERIFY khớp thật trước khi mutate local (fix: trừ contracts khi order reject/pending = lệch size)
    ord_id = (r.get('data') or [{}])[0].get('ordId', '')
    filled = _get_filled(ord_id, position['swap_id'])
    if filled <= 0:
        log.warning(f"[{position['coin']}] partial close KHÔNG xác nhận khớp (accFillSz=0) — giữ nguyên size, thử lại sau")
        return False
    # Trừ đúng lượng khớp (làm tròn theo lot để khớp với size sàn)
    filled = min(filled, position['contracts'])
    position['contracts'] -= filled
    position['notional']   = position['contracts'] * position['ct_val'] * position['entry_price']
    # Đồng bộ size stop THẬT trên sàn; nếu amend FAIL → stop còn size cũ (to hơn) → cảnh báo CRITICAL
    # vì khi đóng nốt sẽ lệch size (51169) và stop có thể đóng quá tay.
    algo_id = position.get('sl_algo_id')
    if algo_id and not amend_stop_algo(position['swap_id'], algo_id,
                                       new_sz=_fmt_sz(position['contracts'], position['lot_sz'])):
        try:
            import notifier
            notifier.notify_critical(
                f"{position['coin']}: partial-close OK nhưng KHÔNG sửa được size stop trên sàn → "
                f"stop lệch size (rủi ro 51169/đóng quá tay). Kiểm tra thủ công.",
                key=f"stop-mismatch-{position['coin']}")
        except Exception:
            log.error(f"[{position['coin']}] amend stop sau partial FAIL — stop lệch size, cần kiểm tra")
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
