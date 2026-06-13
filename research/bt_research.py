"""
Research backtest harness cho TREND bot — KHÔNG deploy (nằm ngoài 3 thư mục bot).

Mục tiêu: kiểm chứng các đề xuất từ chẩn đoán lỗ 12/06 BẰNG SỐ trước khi sửa prod:
  - trailing ATR quá rộng (TRAIL_ATR_MULT=5.0) → winner trả lại lãi
  - regime gate EMA-4H trễ → short alt ngay đáy

Khác với backtest.py prod, harness này:
  1) MÔ PHỎNG ĐÚNG LIVE: trail dùng entry_atr CỐ ĐỊNH (live gọi update_pnl_and_stop(p,price)
     với atr_now=None) + hard-stop −MAX_LOSS_PCT notional. (backtest.py prod dùng atr động & bỏ hard-stop.)
  2) THÊM breakeven + time-stop để test.
  3) Parametrize toàn bộ → grid sweep + OOS split, không sửa hằng số module.
  4) Cache kline xuống đĩa (fetch 1 lần) → sweep nhanh, không spam API.

Dùng:
  python bt_research.py cache            # fetch + cache (1 lần, hoặc --refetch)
  python bt_research.py baseline         # chạy config hiện tại (sanity check)
  python bt_research.py grid             # sweep, ghi grid_results.json
"""
import os, sys, time, json, pickle, math, argparse, datetime as dt
from typing import Optional

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# import strategy/config của trending bot
_HERE = os.path.dirname(os.path.abspath(__file__))
_BOT  = os.path.join(os.path.dirname(_HERE), 'okx-trending-bot')
sys.path.insert(0, _BOT)
os.chdir(_BOT)  # để config.py tìm .env tương đối

import config            # noqa: E402
import strategy as S     # noqa: E402

CACHE_DIR = os.path.join(_HERE, '_cache')
os.makedirs(CACHE_DIR, exist_ok=True)

TAKER_FEE   = 0.0005     # 0.05% taker mỗi chiều (khớp backtest.py)
LEVERAGE_F  = float(S.LEVERAGE)
BTC_COIN    = S.BTC_REGIME_COIN

# ════════════════════ FETCH + CACHE ════════════════════
def _fetch_deep(inst_id: str, bar: str, target: int) -> Optional[pd.DataFrame]:
    """Paginate lùi qua get_history_candlesticks để lấy ~target nến đã đóng."""
    m = config.market_api
    rows, after = [], None
    seen = set()
    for _ in range(60):
        if len(rows) >= target:
            break
        kw = dict(instId=inst_id, bar=bar, limit='100')
        if after:
            kw['after'] = str(after)
        try:
            r = m.get_history_candlesticks(**kw)
        except Exception:
            r = None
        if not r or r.get('code') != '0' or not r.get('data'):
            break
        batch = r['data']
        new = 0
        for c in batch:
            if len(c) < 9 or c[8] != '1':
                continue
            ts = int(c[0])
            if ts in seen:
                continue
            seen.add(ts)
            rows.append({'ts': ts, 'open': float(c[1]), 'high': float(c[2]),
                         'low': float(c[3]), 'close': float(c[4]), 'vol': float(c[5])})
            new += 1
        if new == 0:
            break
        after = min(int(c[0]) for c in batch)
        time.sleep(0.10)
    if not rows:
        return None
    return (pd.DataFrame(rows).sort_values('ts').drop_duplicates('ts').reset_index(drop=True))


def build_cache(refetch=False, tf_target=6000, htf_target=2000):
    path = os.path.join(CACHE_DIR, 'klines.pkl')
    if os.path.exists(path) and not refetch:
        with open(path, 'rb') as f:
            return pickle.load(f)
    coins = S.SCAN_COINS
    data = {'tf': {}, 'htf': {}}
    htf_coins = set(coins) | {BTC_COIN}
    print(f"[cache] fetch 1H ×{len(coins)} (target {tf_target}) + {S.HTF_TIMEFRAME} ×{len(htf_coins)} (target {htf_target})")
    for i, coin in enumerate(coins):
        df = _fetch_deep(f"{coin}-USDT-SWAP", S.TIMEFRAME, tf_target)
        if df is not None and len(df) >= 120:
            data['tf'][coin] = df
        n = 0 if df is None else len(df)
        print(f"  1H {coin:6s} {n:5d} bars")
    for coin in sorted(htf_coins):
        hdf = _fetch_deep(f"{coin}-USDT-SWAP", S.HTF_TIMEFRAME, htf_target)
        if hdf is not None and len(hdf) >= 60:
            data['htf'][coin] = hdf
        n = 0 if hdf is None else len(hdf)
        print(f"  {S.HTF_TIMEFRAME} {coin:6s} {n:5d} bars")
    with open(path, 'wb') as f:
        pickle.dump(data, f)
    print(f"[cache] saved → {path}")
    return data


# ════════════════════ PREP (indicators + regime arrays) ════════════════════
def _htf_regime_arrays(hdf):
    sec = {'4H': 4*3600, '6H': 6*3600, '12H': 12*3600, '1D': 86400, '1H': 3600}.get(S.HTF_TIMEFRAME, 4*3600)
    bar_ms = sec * 1000
    keys, dirs, dipos = [], [], []
    for i in range(len(hdf)):
        adx = float(hdf['adx'].iloc[i])
        ef, es = float(hdf['ema_fast'].iloc[i]), float(hdf['ema_slow'].iloc[i])
        cl = float(hdf['close'].iloc[i])
        if adx < S.HTF_ADX_MIN:
            d = 'SIDE'
        elif ef > es:
            d = 'UP'
        elif ef < es:
            d = 'DOWN'
        else:
            d = 'SIDE'
        keys.append(int(hdf['ts'].iloc[i]) + bar_ms)   # close time (no lookahead)
        dirs.append(d)
        # price-vs-EMA: giá đóng so với ema_slow (xác nhận xu hướng thực, ít trễ hơn EMA-cross)
        dipos.append(1 if cl > es else (-1 if cl < es else 0))
    return keys, dirs, dipos


def _dir_at(keys, dirs, ts_ms):
    if not keys:
        return None, 0
    import bisect
    idx = bisect.bisect_right(keys, ts_ms) - 1
    return (dirs[idx] if idx >= 0 else None), idx


def prep(data):
    coins = [c for c in S.SCAN_COINS if c in data['tf']]
    DON_WINDOWS = [10, 20, 30, 55]
    ROC_WINDOWS = [12, 24, 48]
    dfs, idx_map, arrs = {}, {}, {}
    for c in coins:
        df = S.add_indicators(data['tf'][c]).reset_index(drop=True)
        dfs[c] = df
        idx_map[c] = {int(ts): i for i, ts in enumerate(df['ts'])}
        hi, lo, cl = df['high'], df['low'], df['close']
        a = dict(
            ts=df['ts'].to_numpy(), o=df['open'].to_numpy(), h=hi.to_numpy(),
            l=lo.to_numpy(), c=cl.to_numpy(), v=df['vol'].to_numpy(),
            ef=df['ema_fast'].to_numpy(), es=df['ema_slow'].to_numpy(),
            atr=df['atr'].to_numpy(), adx=df['adx'].to_numpy(),
            pdi=df['plus_di'].to_numpy(), mdi=df['minus_di'].to_numpy(),
        )
        # Donchian: high/low của N nến TRƯỚC (shift(1) → loại nến hiện tại, KHÔNG lookahead)
        a['don_hi'] = {n: hi.rolling(n).max().shift(1).to_numpy() for n in DON_WINDOWS}
        a['don_lo'] = {n: lo.rolling(n).min().shift(1).to_numpy() for n in DON_WINDOWS}
        # ROC% = (close/close[-n] - 1)*100 (momentum)
        a['roc'] = {n: (cl / cl.shift(n) - 1).mul(100).to_numpy() for n in ROC_WINDOWS}
        # RSI(14) (Wilder)
        d = cl.diff()
        up = d.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        dn = (-d.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        a['rsi'] = (100 - 100/(1 + up/dn.replace(0, np.nan))).fillna(50).to_numpy()
        arrs[c] = a
    htf = {}
    for c, hdf in data['htf'].items():
        h2 = S.add_indicators(hdf, ema_fast=S.HTF_EMA_FAST, ema_slow=S.HTF_EMA_SLOW)
        htf[c] = _htf_regime_arrays(h2)
    timeline = sorted(set().union(*[set(a['ts']) for a in arrs.values()]))
    return dict(coins=coins, dfs=dfs, idx_map=idx_map, arrs=arrs, htf=htf, timeline=timeline)


# ════════════════════ ENTRY MODELS (cắm-rút qua p['entry_model']) ════════════════════
def _entry(a, idx, p):
    """Dispatcher: gọi mô hình entry theo p['entry_model']. Mỗi model trả (sig, entry_px, atr) hoặc None."""
    return _ENTRY_MODELS.get(p.get('entry_model', 'emacross'), _entry_emacross)(a, idx, p)


# ── Model: EMA-cross/continuation (BASELINE — khớp strategy._eval_bar / evaluate hiện tại) ──
def _entry_emacross(a, idx, p):
    if idx < 70:
        return None
    c = a['c'][idx]; atr = a['atr'][idx]
    if c <= 0 or atr <= 0:
        return None
    atr_pct = atr / c * 100
    if atr_pct > S.ATR_PCT_MAX:
        return None
    avg_vol = a['v'][max(0, idx-21):idx].mean()
    vol_ok = bool(a['v'][idx] > S.VOL_FACTOR * avg_vol) if avg_vol > 0 else True
    cw = p['cross_window']
    cross_up = cross_down = False
    for i in range(min(cw, idx)):
        if a['ef'][idx-i-1] <= a['es'][idx-i-1] and a['ef'][idx-i] > a['es'][idx-i]:
            cross_up = True; break
        if a['ef'][idx-i-1] >= a['es'][idx-i-1] and a['ef'][idx-i] < a['es'][idx-i]:
            cross_down = True; break
    ef, es = a['ef'][idx], a['es'][idx]
    trend_up, trend_dn = ef > es, ef < es
    adx = a['adx'][idx]
    strong = adx >= p['adx_min']
    adx_rising = adx > a['adx'][idx-1]
    adx_ok_cont = bool(adx_rising or adx >= S.ADX_STRONG)
    gap_pct = (ef - es) / c * 100
    gap_ok_long  = S.GAP_PCT_MIN <=  gap_pct <= S.GAP_PCT_MAX
    gap_ok_short = S.GAP_PCT_MIN <= -gap_pct <= S.GAP_PCT_MAX
    sig = None
    if cross_up and strong and vol_ok:
        sig = 'LONG'
    elif cross_down and strong and vol_ok:
        sig = 'SHORT'
    elif trend_up and strong and adx_ok_cont and gap_ok_long and vol_ok:
        sig = 'LONG'
    elif trend_dn and strong and adx_ok_cont and gap_ok_short and vol_ok:
        sig = 'SHORT'
    return None if sig is None else (sig, c, atr)


def _common_ok(a, idx, p):
    """Guard chung: đủ data, atr%/giá hợp lệ. Trả (c, atr) hoặc None."""
    if idx < 70:
        return None
    c, atr = a['c'][idx], a['atr'][idx]
    if c <= 0 or atr <= 0 or atr / c * 100 > S.ATR_PCT_MAX:
        return None
    if a['adx'][idx] < p['adx_min']:
        return None
    return c, atr


# ── Model: BREAKOUT Donchian (close vượt high/low N nến TRƯỚC) ──
def _entry_breakout(a, idx, p):
    g = _common_ok(a, idx, p)
    if not g:
        return None
    c, atr = g
    n = p.get('don_n', 20)
    hi, lo = a['don_hi'][n][idx], a['don_lo'][n][idx]
    if hi != hi or lo != lo:        # nan (chưa đủ N nến)
        return None
    if p.get('bo_vol'):
        av = a['v'][max(0, idx-21):idx].mean()
        if not (av > 0 and a['v'][idx] > S.VOL_FACTOR * av):
            return None
    if c > hi:
        return ('LONG', c, atr)
    if c < lo:
        return ('SHORT', c, atr)
    return None


# ── Model: PULLBACK trong xu hướng (hồi về ema_fast rồi bật lại) ──
def _entry_pullback(a, idx, p):
    g = _common_ok(a, idx, p)
    if not g:
        return None
    c, atr = g
    ef, es = a['ef'][idx], a['es'][idx]
    k = p.get('pb_k', 0.5)
    if ef > es:                     # uptrend
        pulled = a['l'][idx-1] <= ef + k*atr
        resume = c > ef and c > a['c'][idx-1]
        if pulled and resume:
            return ('LONG', c, atr)
    elif ef < es:                   # downtrend
        pulled = a['h'][idx-1] >= ef - k*atr
        resume = c < ef and c < a['c'][idx-1]
        if pulled and resume:
            return ('SHORT', c, atr)
    return None


# ── Model: MOMENTUM (ROC vượt ngưỡng, cùng chiều EMA) ──
def _entry_momentum(a, idx, p):
    g = _common_ok(a, idx, p)
    if not g:
        return None
    c, atr = g
    n, thr = p.get('roc_n', 24), p.get('roc_thr', 3.0)
    r = a['roc'][n][idx]
    if r != r:
        return None
    ef, es = a['ef'][idx], a['es'][idx]
    if r >= thr and ef > es:
        return ('LONG', c, atr)
    if r <= -thr and ef < es:
        return ('SHORT', c, atr)
    return None


# ── Model: EMA-cross + bộ lọc (ROC cùng chiều / RSI không cực đoan) ──
def _entry_emacross_filtered(a, idx, p):
    base = _entry_emacross(a, idx, p)
    if not base:
        return None
    sig = base[0]
    if p.get('f_roc'):
        r = a['roc'][p.get('roc_n', 24)][idx]
        if r == r:
            m = p.get('f_roc_min', 0.0)
            if sig == 'LONG' and r < m:
                return None
            if sig == 'SHORT' and r > -m:
                return None
    if p.get('f_rsi'):
        rsi = a['rsi'][idx]
        if sig == 'LONG' and rsi > p.get('f_rsi_hi', 78):
            return None
        if sig == 'SHORT' and rsi < p.get('f_rsi_lo', 22):
            return None
    return base


# ── Model: PULLBACK MEAN-REVERT trong trend (mua dip về EMA + RSI; bán rally) — thiết kế Agent 5 ──
# Tape mean-reverting → "mua khi hồi về vùng giá trị trong trend" có forward-edge dương.
def _entry_pullback_mr(a, idx, p):
    g = _common_ok(a, idx, p)            # idx>=70, c/atr hợp lệ, atr%<6, adx>=adx_min
    if not g:
        return None
    c, atr = g
    ef, es = a['ef'][idx], a['es'][idx]
    rsi = a['rsi'][idx]
    dip_ema = es if p.get('pb_deep') else ef    # hồi về EMA55 (sâu) hay EMA21 (nông)
    if ef > es:                          # uptrend → mua dip
        if a['l'][idx] <= dip_ema and rsi < p.get('rsi_long_max', 45):
            return ('LONG', c, atr)
    elif ef < es:                        # downtrend → bán rally
        if a['h'][idx] >= dip_ema and rsi > p.get('rsi_short_min', 55):
            return ('SHORT', c, atr)
    return None


# ── Model: DONCHIAN RETEST (chờ giá test lại mức phá vỡ rồi mới vào) — thiết kế Agent 1 ──
def _entry_retest(a, idx, p):
    g = _common_ok(a, idx, p)
    if not g:
        return None
    c, atr = g
    ef, es = a['ef'][idx], a['es'][idx]
    n = p.get('don_n', 20)
    w = p.get('retest_w', 12)
    pull = p.get('pull_atr', 1.0)
    don_hi, don_lo = a['don_hi'][n], a['don_lo'][n]
    if ef > es:                          # uptrend: tìm breakout-up gần đây rồi retest
        for b in range(idx - w, idx):
            if b < n:
                continue
            lvl = don_hi[b]              # = max(high[b-n:b]) (đã shift)
            if lvl == lvl and a['c'][b] > lvl:   # nến b đã phá kênh
                if lvl <= c <= lvl + pull * atr:  # giá nay retest giữ trên mức phá vỡ
                    return ('LONG', c, atr)
                break
    elif ef < es:
        for b in range(idx - w, idx):
            if b < n:
                continue
            lvl = don_lo[b]
            if lvl == lvl and a['c'][b] < lvl:
                if lvl - pull * atr <= c <= lvl:
                    return ('SHORT', c, atr)
                break
    return None


_ENTRY_MODELS = {
    'emacross':          _entry_emacross,
    'emacross_filtered': _entry_emacross_filtered,
    'breakout':          _entry_breakout,
    'pullback':          _entry_pullback,
    'pullback_mr':       _entry_pullback_mr,
    'retest':            _entry_retest,
    'momentum':          _entry_momentum,
}


# ════════════════════ REGIME GATE (các biến thể) ════════════════════
def _regime_ok(p, sig, htf_dir, htf_dipos, btc_dir, btc_dipos):
    mode = p['regime_mode']
    if sig == 'SHORT' and not p['enable_short']:
        return False
    if mode == 'none':
        return True
    if htf_dir is None or htf_dir == 'SIDE':
        return False
    if mode == 'current':                       # live: LONG htf UP & btc≠DOWN; SHORT htf DOWN & btc≠UP
        if sig == 'LONG':  return htf_dir == 'UP'   and btc_dir != 'DOWN'
        if sig == 'SHORT': return htf_dir == 'DOWN'  and btc_dir != 'UP'
    if mode == 'no_btc':                         # bỏ cổng BTC, chỉ cần coin 4H cùng chiều
        if sig == 'LONG':  return htf_dir == 'UP'
        if sig == 'SHORT': return htf_dir == 'DOWN'
    if mode == 'btc_strict':                     # cần BTC cùng chiều RÕ (không SIDE)
        if sig == 'LONG':  return htf_dir == 'UP'   and btc_dir == 'UP'
        if sig == 'SHORT': return htf_dir == 'DOWN'  and btc_dir == 'DOWN'
    if mode == 'price_confirm':                  # current + giá BTC xác nhận (chống EMA-cross trễ)
        if sig == 'LONG':  return htf_dir == 'UP'   and btc_dir != 'DOWN' and btc_dipos >= 0 and htf_dipos >= 0
        if sig == 'SHORT': return htf_dir == 'DOWN'  and btc_dir != 'UP'   and btc_dipos <= 0 and htf_dipos <= 0
    return False


# ════════════════════ SIMULATE ════════════════════
DEFAULTS = dict(
    stop_mult=S.STOP_ATR_MULT, trail_mult=S.TRAIL_ATR_MULT, use_entry_atr=True,
    hard_stop_pct=S.MAX_LOSS_PCT, be_trigger=None, be_offset=0.0,
    time_stop_h=None, time_stop_min_profit_pct=None,
    regime_mode='current', enable_short=True, entry_model='emacross',
    adx_min=S.ADX_MIN, cross_window=S.CROSS_WINDOW,
    risk_pct=S.RISK_PCT, max_pos_pct=S.MAX_POS_PCT, min_usdt=S.MIN_USDT, max_pos=5,
    initial_balance=10_000.0,
)


def simulate(D, params=None, ts_from=None, ts_to=None):
    p = dict(DEFAULTS); p.update(params or {})
    coins, idx_map, arrs, htf = D['coins'], D['idx_map'], D['arrs'], D['htf']
    if p.get('universe'):                       # giới hạn rổ coin (vd majors-only)
        uni = set(p['universe'])
        coins = [c for c in coins if c in uni]
    if p.get('exclude'):                         # loại coin (vd DOT phantom-price artifact)
        ex = set(p['exclude'])
        coins = [c for c in coins if c not in ex]
    btc = htf.get(BTC_COIN, ([], [], []))
    btc_keys, btc_dirs, btc_dipos = btc

    timeline = [t for t in D['timeline']
                if (ts_from is None or t >= ts_from) and (ts_to is None or t <= ts_to)]
    bal = p['initial_balance']
    pos = {}            # coin -> dict
    trades, equity = [], [{'ts': timeline[0]/1000, 'balance': bal}]
    total_pnl = 0.0
    lev = LEVERAGE_F

    def close(o, px, ts, reason):
        nonlocal bal, total_pnl
        qty = o['notional'] / o['entry_price']
        gross = (px - o['entry_price']) * qty if o['side'] == 'LONG' else (o['entry_price'] - px) * qty
        fee = (o['notional'] + px * qty) * TAKER_FEE
        pnl = gross - fee
        bal += o['margin'] + pnl
        total_pnl += pnl
        trades.append(dict(coin=o['coin'], side=o['side'], entry=o['entry_price'], exit=px,
                           notional=o['notional'], pnl=pnl, roi=pnl/o['notional']*100,
                           dur_h=(ts - o['open_ts'])/3_600_000, reason=reason,
                           open_ts=o['open_ts'], max_fav=o.get('max_fav', 0.0), max_adv=o.get('max_adv', 0.0)))
        del pos[o['coin']]

    for bi, ts in enumerate(timeline):
        # ── update open positions (intrabar) ──
        for coin in list(pos.keys()):
            im = idx_map.get(coin)
            if not im or ts not in im:
                continue
            a = arrs[coin]; ri = im[ts]
            h, l, c = a['h'][ri], a['l'][ri], a['c'][ri]
            o = pos[coin]
            atr = o['entry_atr'] if p['use_entry_atr'] else a['atr'][ri]
            tmult = p['trail_mult']
            if o['side'] == 'LONG':
                o['trail_anchor'] = max(o['trail_anchor'], h)
                o['stop'] = max(o['stop'], o['trail_anchor'] - tmult * atr)
                if p['be_trigger'] is not None and not o['be_done'] and h >= o['entry_price'] + p['be_trigger']*o['entry_atr']:
                    o['stop'] = max(o['stop'], o['entry_price'] + p['be_offset']*o['entry_atr']); o['be_done'] = True
                # hard-stop = -MAX_LOSS_PCT DI CHUYỂN GIÁ (live: pct=price_pnl/notional=(px-entry)/entry, KHÔNG nhân lev)
                hard_px = o['entry_price'] * (1 - p['hard_stop_pct']) if p['hard_stop_pct'] else None
                stop_px = o['stop'] if hard_px is None else max(o['stop'], hard_px)
                if l <= stop_px:
                    close(o, min(stop_px, c), ts, 'stop'); continue
                if p.get('tp_atr'):                  # take-profit (cho entry mean-revert)
                    tp = o['entry_price'] + p['tp_atr'] * o['entry_atr']
                    if h >= tp:
                        close(o, tp, ts, 'tp'); continue
            else:
                o['trail_anchor'] = min(o['trail_anchor'], l)
                o['stop'] = min(o['stop'], o['trail_anchor'] + tmult * atr)
                if p['be_trigger'] is not None and not o['be_done'] and l <= o['entry_price'] - p['be_trigger']*o['entry_atr']:
                    o['stop'] = min(o['stop'], o['entry_price'] - p['be_offset']*o['entry_atr']); o['be_done'] = True
                hard_px = o['entry_price'] * (1 + p['hard_stop_pct']) if p['hard_stop_pct'] else None
                stop_px = o['stop'] if hard_px is None else min(o['stop'], hard_px)
                if h >= stop_px:
                    close(o, max(stop_px, c), ts, 'stop'); continue
                if p.get('tp_atr'):                  # take-profit (cho entry mean-revert)
                    tp = o['entry_price'] - p['tp_atr'] * o['entry_atr']
                    if l <= tp:
                        close(o, tp, ts, 'tp'); continue
            # MFE/MAE (% notional, leverage-aware: dùng price move / entry * lev? — giữ % giá cho so sánh)
            fav = (h - o['entry_price'])/o['entry_price']*100 if o['side']=='LONG' else (o['entry_price']-l)/o['entry_price']*100
            adv = (l - o['entry_price'])/o['entry_price']*100 if o['side']=='LONG' else (o['entry_price']-h)/o['entry_price']*100
            o['max_fav'] = max(o.get('max_fav',0.0), fav); o['max_adv'] = min(o.get('max_adv',0.0), adv)

        # ── time-stop (xét ở đóng nến) ──
        if p['time_stop_h'] is not None:
            for coin in list(pos.keys()):
                im = idx_map.get(coin)
                if not im or ts not in im:
                    continue
                o = pos[coin]; a = arrs[coin]; ri = im[ts]; c = a['c'][ri]
                if (ts - o['open_ts'])/3_600_000 >= p['time_stop_h']:
                    if p['time_stop_min_profit_pct'] is None:
                        close(o, c, ts, 'time_stop'); continue
                    qty = o['notional']/o['entry_price']
                    cur = ((c-o['entry_price']) if o['side']=='LONG' else (o['entry_price']-c))*qty
                    if cur/o['notional']*100 < p['time_stop_min_profit_pct']:
                        close(o, c, ts, 'time_stop'); continue

        # ── EMA reverse (đóng nến) ──
        for coin in list(pos.keys()):
            im = idx_map.get(coin)
            if not im or ts not in im:
                continue
            a = arrs[coin]; ri = im[ts]
            if ri < 1:
                continue
            o = pos[coin]; c = a['c'][ri]
            cdn = a['ef'][ri-1] >= a['es'][ri-1] and a['ef'][ri] < a['es'][ri]
            cup = a['ef'][ri-1] <= a['es'][ri-1] and a['ef'][ri] > a['es'][ri]
            if o['side']=='LONG' and cdn:  close(o, c, ts, 'ema_reverse'); continue
            if o['side']=='SHORT' and cup: close(o, c, ts, 'ema_reverse'); continue

        # ── open new ──
        if len(pos) < p['max_pos']:
            for coin in coins:
                if len(pos) >= p['max_pos']:
                    break
                if coin in pos:
                    continue
                im = idx_map.get(coin)
                if not im or ts not in im:
                    continue
                ri = im[ts]
                ev = _entry(arrs[coin], ri, p)
                if not ev:
                    continue
                sig, entry, atr = ev
                if coin not in htf:
                    continue
                ck, cd, cdp = htf[coin]
                hd, hidx = _dir_at(ck, cd, ts)
                hdipos = cdp[hidx] if hidx >= 0 else 0
                bd, bidx = _dir_at(btc_keys, btc_dirs, ts)
                bdipos = btc_dipos[bidx] if (btc_dipos and bidx >= 0) else 0
                if not _regime_ok(p, sig, hd, hdipos, bd, bdipos):
                    continue
                # correlation
                corr = False
                for g in S.CORRELATED_GROUPS:
                    if coin in g and any(pc in g and po['side']==sig for pc,po in pos.items()):
                        corr = True; break
                if corr:
                    continue
                stop_dist = p['stop_mult']*atr
                stop_pct = stop_dist/entry
                notional = (bal*p['risk_pct']/stop_pct) if stop_pct > 0 else 0
                notional = min(notional, bal*p['max_pos_pct']*lev)
                margin = notional/lev
                if notional < p['min_usdt'] or margin < 1.0 or margin > bal:
                    continue
                bal -= margin
                pos[coin] = dict(coin=coin, side=sig, entry_price=entry, entry_atr=atr, open_ts=ts,
                                 stop=(entry-stop_dist) if sig=='LONG' else (entry+stop_dist),
                                 trail_anchor=entry, notional=notional, margin=margin, be_done=False)

        if bi % 6 == 0:
            unreal = 0.0
            for coin, o in pos.items():
                im = idx_map.get(coin)
                if im and ts in im:
                    c = arrs[coin]['c'][im[ts]]; qty = o['notional']/o['entry_price']
                    unreal += ((c-o['entry_price']) if o['side']=='LONG' else (o['entry_price']-c))*qty
            equity.append({'ts': ts/1000, 'balance': bal+unreal})

    for coin, o in list(pos.items()):
        a = arrs[coin]
        close(o, a['c'][-1], int(a['ts'][-1]), 'end_of_data')

    return _metrics(trades, equity, p['initial_balance'], timeline)


def _metrics(trades, equity, init_bal, timeline):
    n = len(trades)
    if n == 0:
        return dict(n=0, win_rate=0, total_pnl=0, roi=0, pf=0, max_dd=0, expectancy=0,
                    avg_win=0, avg_loss=0, sharpe=0, n_long=0, n_short=0, exit_reasons={}, per_coin={})
    pnls = [t['pnl'] for t in trades]
    wins = [x for x in pnls if x > 0]; losses = [x for x in pnls if x <= 0]
    gw = sum(wins); gl = abs(sum(losses)) or 1e-9
    wr = len(wins)/n*100
    aw = (sum(wins)/len(wins)) if wins else 0.0
    al = (sum(losses)/len(losses)) if losses else 0.0
    bals = [e['balance'] for e in equity]
    peak = bals[0]; mdd = 0.0
    for b in bals:
        peak = max(peak, b)
        if peak > 0:
            mdd = min(mdd, (b-peak)/peak*100)
    sharpe = 0.0
    if len(equity) > 5:
        rets = pd.Series(bals).pct_change().dropna()
        if rets.std() > 0:
            sharpe = round(rets.mean()/rets.std()*math.sqrt(6*365), 2)
    reasons = {}
    for t in trades:
        reasons[t['reason']] = reasons.get(t['reason'], 0)+1
    per_coin = {}
    for t in trades:
        s = per_coin.setdefault(t['coin'], {'n':0,'wins':0,'pnl':0.0})
        s['n']+=1; s['wins']+= 1 if t['pnl']>0 else 0; s['pnl']+=t['pnl']
    days = (timeline[-1]-timeline[0])/86_400_000 if len(timeline) > 1 else 0
    return dict(
        n=n, win_rate=round(wr,1), total_pnl=round(sum(pnls),2), roi=round(sum(pnls)/init_bal*100,2),
        pf=round(gw/gl,3), max_dd=round(mdd,2), expectancy=round((wr/100)*aw+(1-wr/100)*al,3),
        avg_win=round(aw,2), avg_loss=round(al,2), sharpe=sharpe,
        n_long=sum(1 for t in trades if t['side']=='LONG'), n_short=sum(1 for t in trades if t['side']=='SHORT'),
        short_pnl=round(sum(t['pnl'] for t in trades if t['side']=='SHORT'),2),
        long_pnl=round(sum(t['pnl'] for t in trades if t['side']=='LONG'),2),
        exit_reasons=reasons, per_coin={k:{'n':v['n'],'wr':round(v['wins']/v['n']*100),'pnl':round(v['pnl'],1)} for k,v in per_coin.items()},
        days=round(days,1),
    )


# ════════════════════ CLI ════════════════════
def _print(tag, m):
    print(f"\n[{tag}]  n={m['n']} ({m['n_long']}L/{m['n_short']}S) wr={m['win_rate']}%  "
          f"ROI={m['roi']}%  PF={m['pf']}  maxDD={m['max_dd']}%  exp={m['expectancy']}  sharpe={m['sharpe']}  days={m.get('days')}")
    print(f"     long_pnl={m.get('long_pnl')}  short_pnl={m.get('short_pnl')}  exits={m['exit_reasons']}")


def run_grid(D):
    trails   = [2.5, 3.0, 3.5, 4.0, 5.0]
    bes      = [None, (1.5, 0.1)]
    tstops   = [None, (48, None), (36, 0.0)]    # (giờ, min_profit_pct); None=tắt
    regshort = [('current', True), ('current', False), ('no_btc', True),
                ('no_btc', False), ('price_confirm', True)]
    adxs     = [30.0, 35.0]
    combos = []
    for tr in trails:
        for be in bes:
            for tsx in tstops:
                for (rm, es) in regshort:
                    for ax in adxs:
                        combos.append(dict(
                            trail_mult=tr,
                            be_trigger=(be[0] if be else None), be_offset=(be[1] if be else 0.0),
                            time_stop_h=(tsx[0] if tsx else None),
                            time_stop_min_profit_pct=(tsx[1] if tsx else None),
                            regime_mode=rm, enable_short=es, adx_min=ax))
    print(f"[grid] {len(combos)} combos × 0.4s ≈ {len(combos)*0.4:.0f}s ...")
    res = []
    for i, c in enumerate(combos):
        m = simulate(D, c)
        res.append({'params': c, 'm': m})
        if i % 50 == 0:
            print(f"  ...{i}/{len(combos)}")
    # OOS split top theo ROI (n>=40)
    half = D['timeline'][len(D['timeline'])//2]
    def score(r):
        m = r['m']
        return m['roi'] if m['n'] >= 40 else -999
    res.sort(key=score, reverse=True)
    print("\n========== TOP 25 theo ROI (n>=40) ==========")
    print(f"{'#':>2} {'ROI':>7} {'PF':>5} {'mDD':>7} {'n':>4} {'L/S':>9} {'sh_pnl':>8} {'trail':>5} {'be':>8} {'tstop':>9} {'regime':>13} {'short':>5} {'adx':>4}")
    top = []
    for r in res[:25]:
        m, c = r['m'], r['params']
        # OOS: 2 nửa
        m1 = simulate(D, c, ts_to=half)
        m2 = simulate(D, c, ts_from=half)
        r['oos'] = (m1['roi'], m2['roi'])
        top.append(r)
        be_s = f"{c['be_trigger']}/{c['be_offset']}" if c['be_trigger'] else "-"
        ts_s = f"{c['time_stop_h']}/{c['time_stop_min_profit_pct']}" if c['time_stop_h'] else "-"
        print(f"{len(top):>2} {m['roi']:>7.1f} {m['pf']:>5.2f} {m['max_dd']:>7.1f} {m['n']:>4} "
              f"{str(m['n_long'])+'/'+str(m['n_short']):>9} {m['short_pnl']:>8.0f} {c['trail_mult']:>5} {be_s:>8} {ts_s:>9} "
              f"{c['regime_mode']:>13} {str(c['enable_short']):>5} {c['adx_min']:>4.0f}  OOS={m1['roi']:.1f}/{m2['roi']:.1f}")
    # ghi JSON
    with open(os.path.join(_HERE, 'grid_results.json'), 'w', encoding='utf-8') as f:
        json.dump([{'params': r['params'], 'm': r['m'], 'oos': r.get('oos')} for r in res], f, ensure_ascii=False, indent=1)
    print(f"\n[grid] full results -> grid_results.json ({len(res)} combos)")
    # best by PF among n>=60 (robust)
    byp = [r for r in res if r['m']['n'] >= 60]
    byp.sort(key=lambda r: r['m']['pf'], reverse=True)
    print("\n========== TOP 8 theo PF (n>=60) ==========")
    for r in byp[:8]:
        m, c = r['m'], r['params']
        print(f"  PF={m['pf']:.2f} ROI={m['roi']:.1f} mDD={m['max_dd']:.1f} n={m['n']} reg={c['regime_mode']} short={c['enable_short']} adx={c['adx_min']:.0f} trail={c['trail_mult']} be={c['be_trigger']} ts={c['time_stop_h']}")


def run_entry_sweep(D, configs):
    """Đánh giá danh sách (name, cfg) trên full + 2 nửa OOS. Xếp hạng theo robust score.
    Robust score: ROI full nhưng PHẠT nặng nếu nửa nào âm (ưu tiên dương cả 2 nửa)."""
    half = D['timeline'][len(D['timeline'])//2]
    rows = []
    for name, cfg in configs:
        f  = simulate(D, cfg)
        h1 = simulate(D, cfg, ts_to=half)
        h2 = simulate(D, cfg, ts_from=half)
        both_pos = h1['roi'] > 0 and h2['roi'] > 0
        # robust score: trung bình 2 nửa, phạt nếu lệch dấu, thưởng nếu cả 2 dương
        score = min(h1['roi'], h2['roi']) + 0.3 * f['roi'] + (10 if both_pos else 0)
        rows.append(dict(name=name, cfg=cfg, full=f, h1=h1['roi'], h2=h2['roi'],
                         both_pos=both_pos, score=score))
    rows.sort(key=lambda r: r['score'], reverse=True)
    print(f"\n{'#':>2} {'model/name':24s} {'FULL':>7} {'PF':>5} {'mDD':>6} {'n':>4} {'L/S':>9} "
          f"{'Lpnl':>7} {'Spnl':>7} {'OOS h1/h2':>14} {'2pos':>5} {'score':>6}")
    for i, r in enumerate(rows[:30]):
        m = r['full']
        print(f"{i+1:>2} {r['name'][:24]:24s} {m['roi']:>7.1f} {m['pf']:>5.2f} {m['max_dd']:>6.0f} {m['n']:>4} "
              f"{str(m['n_long'])+'/'+str(m['n_short']):>9} {m['long_pnl']:>7.0f} {m['short_pnl']:>7.0f} "
              f"{r['h1']:>6.1f}/{r['h2']:<6.1f} {str(r['both_pos']):>5} {r['score']:>6.1f}")
    with open(os.path.join(_HERE, 'entry_sweep.json'), 'w', encoding='utf-8') as f:
        json.dump([{'name': r['name'], 'cfg': r['cfg'], 'full': r['full'],
                    'h1': r['h1'], 'h2': r['h2'], 'score': r['score']} for r in rows], f, ensure_ascii=False, indent=1)
    print(f"\n[entry_sweep] -> entry_sweep.json ({len(rows)} configs)")
    return rows


def _default_entry_configs():
    """Bộ config khởi đầu — sẽ mở rộng theo thiết kế từ workflow."""
    cfgs = []
    base = dict(regime_mode='price_confirm', adx_min=35.0)   # nền tốt nhất từ sweep trước
    cfgs.append(('emacross (baseline live)', dict(regime_mode='current', adx_min=30.0)))
    cfgs.append(('emacross+pc+adx35', dict(base)))
    for n in (10, 20, 30, 55):
        cfgs.append((f'breakout don{n}+pc', dict(base, entry_model='breakout', don_n=n)))
        cfgs.append((f'breakout don{n}+pc+vol', dict(base, entry_model='breakout', don_n=n, bo_vol=True)))
    for k in (0.3, 0.5, 1.0):
        cfgs.append((f'pullback k{k}+pc', dict(base, entry_model='pullback', pb_k=k)))
    for (n, thr) in ((12, 2.0), (24, 3.0), (24, 5.0), (48, 5.0)):
        cfgs.append((f'momentum roc{n}>{thr}+pc', dict(base, entry_model='momentum', roc_n=n, roc_thr=thr)))
    cfgs.append(('emacross_filt roc+pc', dict(base, entry_model='emacross_filtered', f_roc=True, f_roc_min=0.0)))
    cfgs.append(('emacross_filt rsi+pc', dict(base, entry_model='emacross_filtered', f_rsi=True)))
    # long-only biến thể (crypto drift)
    cfgs.append(('breakout don20 LONG-only', dict(base, entry_model='breakout', don_n=20, enable_short=False)))
    return cfgs


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['cache', 'baseline', 'grid', 'coverage', 'cfg', 'entrygrid'])
    ap.add_argument('--refetch', action='store_true')
    ap.add_argument('--json', default='{}', help='config JSON cho lệnh cfg')
    args = ap.parse_args()

    if args.cmd == 'cache':
        build_cache(refetch=True)
        sys.exit(0)

    data = build_cache(refetch=args.refetch)
    D = prep(data)
    cov_days = (D['timeline'][-1]-D['timeline'][0])/86_400_000
    print(f"[prep] {len(D['coins'])} coins, {len(D['timeline'])} bars 1H, ~{cov_days:.0f} ngày, htf {len(D['htf'])} coins")

    if args.cmd == 'coverage':
        for c in D['coins']:
            print(f"  {c:6s} {len(D['arrs'][c]['ts']):5d}")
        sys.exit(0)

    if args.cmd == 'baseline':
        m = simulate(D, {})
        _print('BASELINE (live params)', m)
        # so với backtest.py prod (atr động, không hard-stop)
        m2 = simulate(D, {'use_entry_atr': False, 'hard_stop_pct': None})
        _print('như backtest.py prod', m2)
        sys.exit(0)

    if args.cmd == 'grid':
        run_grid(D)
        sys.exit(0)

    if args.cmd == 'entrygrid':
        run_entry_sweep(D, _default_entry_configs())
        sys.exit(0)

    if args.cmd == 'cfg':
        cfg = json.loads(args.json)
        half = D['timeline'][len(D['timeline'])//2]
        m  = simulate(D, cfg)
        m1 = simulate(D, cfg, ts_to=half)
        m2 = simulate(D, cfg, ts_from=half)
        _print('FULL', m)
        _print('1st half (IS)', m1)
        _print('2nd half (OOS)', m2)
        print("\nper-coin:", json.dumps(m['per_coin'], ensure_ascii=False))
        sys.exit(0)
