"""
Backtest engine — OKX Trend-Following Bot.
Simulate bar-by-bar (4H) với full risk management: stop loss, trailing, partial TP, EMA reverse.
Không gọi API trong simulation — chỉ fetch data 1 lần lúc đầu.
"""
import math
import time
import logging
import pandas as pd
from typing import Optional

import bisect
from config import market_api
from strategy import (
    SCAN_COINS, TIMEFRAME, ADX_MIN,
    STOP_ATR_MULT, TRAIL_ATR_MULT, RISK_PCT, MAX_POS_PCT, MIN_USDT,
    PARTIAL_TP_ATR_MULT, PARTIAL_TP_RATIO, ATR_PCT_MAX, ENABLE_PARTIAL_TP,
    CORRELATED_GROUPS, VOL_FACTOR, ADX_STRONG,
    add_indicators, GAP_PCT_MIN, GAP_PCT_MAX, CROSS_WINDOW,
    HTF_TIMEFRAME, HTF_EMA_FAST, HTF_EMA_SLOW, HTF_ADX_MIN,
    BTC_REGIME_COIN, regime_allows,
)

log = logging.getLogger(__name__)

LEVERAGE_F   = 5.0
# CROSS_WINDOW import từ strategy (KHÔNG đặt cứng — tránh lệch backtest vs live khi đổi tham số)
TAKER_FEE    = 0.0005   # 0.05% swap taker mỗi chiều — mô hình phí để backtest sát thực tế (trước đây bỏ qua phí → ROI ảo)


# ════════════════════ DATA FETCH ════════════════════

def _retry(fn, *, attempts=3, base_delay=0.3, what=''):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(base_delay * 2 ** i)
    if what:
        log.debug(f"retry exhausted ({what}): {last}")
    return None


def _fetch_history(inst_id: str, bar: str = '4H', target: int = 600) -> Optional[pd.DataFrame]:
    """Fetch lịch sử confirmed candles, paginate nếu target > 300."""
    all_rows: list = []
    after_ts: Optional[int] = None

    for _ in range(5):
        if len(all_rows) >= target:
            break
        n = min(300, target - len(all_rows) + 100)
        n = min(n, 300)
        kwargs = dict(instId=inst_id, bar=bar, limit=str(n))
        if after_ts:
            kwargs['after'] = str(after_ts)
        resp = _retry(lambda kw=kwargs: market_api.get_candlesticks(**kw),
                      attempts=3, base_delay=0.4, what=f'candles {inst_id}')
        if not resp or resp.get('code') != '0':
            break
        rows = []
        for r in (resp.get('data') or []):
            if len(r) < 9 or r[8] != '1':
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
            break
        all_rows.extend(rows)
        after_ts = min(r['ts'] for r in rows)
        time.sleep(0.12)

    if not all_rows:
        return None
    df = (pd.DataFrame(all_rows)
          .sort_values('ts')
          .drop_duplicates('ts')
          .reset_index(drop=True))
    return df


# ════════════════════ HTF REGIME (cho đại tu MTF) ════════════════════
def _htf_regime_arrays(df, bar=HTF_TIMEFRAME):
    """Từ df khung lớn (đã add_indicators) → (keys, dirs):
       keys = thời điểm ĐÓNG của mỗi nến (ms), dirs = 'UP'/'DOWN'/'SIDE'."""
    sec = {'4H': 4*3600, '6H': 6*3600, '12H': 12*3600, '1D': 86400, '1H': 3600}.get(bar, 4*3600)
    bar_ms = sec * 1000
    keys, dirs = [], []
    for i in range(len(df)):
        adx = float(df['adx'].iloc[i])
        ef  = float(df['ema_fast'].iloc[i]); es = float(df['ema_slow'].iloc[i])
        if adx < HTF_ADX_MIN:
            d = 'SIDE'
        elif ef > es:
            d = 'UP'
        elif ef < es:
            d = 'DOWN'
        else:
            d = 'SIDE'
        keys.append(int(df['ts'].iloc[i]) + bar_ms)   # close time = open + bar
        dirs.append(d)
    return keys, dirs


def _htf_dir_at(keys, dirs, ts_ms):
    """Direction của nến HTF gần nhất ĐÃ ĐÓNG trước ts_ms (không lookahead)."""
    if not keys:
        return None
    idx = bisect.bisect_right(keys, ts_ms) - 1
    return dirs[idx] if idx >= 0 else None


# ════════════════════ BAR EVALUATION (no API, no DataFrame copy) ════════════════════

def _eval_bar(df: pd.DataFrame, idx: int, adx_min: float = ADX_MIN) -> dict:
    """
    Evaluate signal tại bar `idx` dùng pre-computed indicators.
    df đã có cột: ema_fast, ema_slow, atr, adx, vol, close, high, low.
    """
    if idx < 70:
        return {'signal': None}

    last = df.iloc[idx]
    prev = df.iloc[idx - 1]

    c = float(last['close'])
    atr = float(last['atr'])
    atr_pct = atr / c * 100

    if atr_pct > ATR_PCT_MAX:
        return {'signal': None, 'atr_pct': atr_pct, 'close': c, 'atr': atr,
                'adx': float(last['adx'])}

    avg_vol = df['vol'].iloc[max(0, idx - 21):idx].mean()
    vol_ok  = bool(last['vol'] > VOL_FACTOR * avg_vol) if avg_vol > 0 else True

    cross_up = cross_down = False
    for i in range(min(CROSS_WINDOW, idx)):
        cur = df.iloc[idx - i]
        prv = df.iloc[idx - i - 1]
        if prv['ema_fast'] <= prv['ema_slow'] and cur['ema_fast'] > cur['ema_slow']:
            cross_up = True; break
        if prv['ema_fast'] >= prv['ema_slow'] and cur['ema_fast'] < cur['ema_slow']:
            cross_down = True; break

    ef = float(last['ema_fast'])
    es = float(last['ema_slow'])
    trend_up = ef > es
    trend_dn = ef < es
    strong   = float(last['adx']) >= adx_min
    adx_rising = bool(last['adx'] > prev['adx'])
    adx_ok_cont = bool(adx_rising or float(last['adx']) >= ADX_STRONG)

    gap_pct = (ef - es) / c * 100
    gap_ok_long  = GAP_PCT_MIN <=  gap_pct <= GAP_PCT_MAX
    gap_ok_short = GAP_PCT_MIN <= -gap_pct <= GAP_PCT_MAX

    signal = None
    if cross_up and strong and vol_ok:
        signal = 'LONG'
    elif cross_down and strong and vol_ok:
        signal = 'SHORT'
    elif trend_up and strong and adx_ok_cont and gap_ok_long and vol_ok:
        signal = 'LONG'
    elif trend_dn and strong and adx_ok_cont and gap_ok_short and vol_ok:
        signal = 'SHORT'

    return {
        'signal':      signal,
        'close':       c,
        'atr':         atr,
        'atr_pct':     atr_pct,
        'adx':         float(last['adx']),
        'cross_up':    cross_up,
        'cross_down':  cross_down,
    }


def _is_correlated(coin: str, side: str, open_positions: dict) -> bool:
    for group in CORRELATED_GROUPS:
        if coin in group:
            for c, p in open_positions.items():
                if c in group and p['side'] == side:
                    return True
    return False


# ════════════════════ MAIN ENGINE ════════════════════

def run(
    coins: list = None,
    bar: str = TIMEFRAME,
    target_candles: int = 600,
    initial_balance: float = 10_000.0,
    max_pos: int = 3,
    progress_cb=None,
) -> dict:
    """
    Chạy backtest toàn bộ watchlist.
    progress_cb(stage: str, done: int, total: int) được gọi để báo tiến độ.
    """
    coins = coins or SCAN_COINS

    # ── 1. Fetch & compute indicators ───────────────────────────
    if progress_cb:
        progress_cb('fetch', 0, len(coins))

    dfs: dict[str, pd.DataFrame] = {}
    for i, coin in enumerate(coins):
        inst_id = f"{coin}-USDT-SWAP"
        df = _fetch_history(inst_id, bar=bar, target=target_candles)
        if df is not None and len(df) >= 80:
            dfs[coin] = add_indicators(df)
        if progress_cb:
            progress_cb('fetch', i + 1, len(coins))

    if not dfs:
        return {'error': 'Không lấy được dữ liệu lịch sử'}

    # ── 1b. Fetch regime khung lớn (4H) cho đại tu MTF ──────────
    # Mỗi coin + BTC: tính direction 4H tại mỗi thời điểm để cổng regime giống live.
    htf_regime: dict = {}   # coin → (keys, dirs)
    htf_target = max(150, target_candles // 4 + 80)
    htf_coins = set(dfs.keys()) | {BTC_REGIME_COIN}
    for coin in htf_coins:
        hdf = _fetch_history(f"{coin}-USDT-SWAP", bar=HTF_TIMEFRAME, target=htf_target)
        if hdf is not None and len(hdf) >= 60:
            hdf = add_indicators(hdf, ema_fast=HTF_EMA_FAST, ema_slow=HTF_EMA_SLOW)
            htf_regime[coin] = _htf_regime_arrays(hdf, bar=HTF_TIMEFRAME)
    btc_keys, btc_dirs = htf_regime.get(BTC_REGIME_COIN, ([], []))

    # ── 2. Common timeline ───────────────────────────────────────
    all_ts = sorted(set().union(*[set(df['ts']) for df in dfs.values()]))
    WARMUP = 100
    if len(all_ts) <= WARMUP:
        return {'error': 'Không đủ data (cần > 100 bars warmup)'}
    trade_ts = all_ts[WARMUP:]

    # Index mỗi df theo ts để tra cứu O(1)
    idx_map: dict[str, dict] = {}    # coin → {ts: row_index}
    for coin, df in dfs.items():
        idx_map[coin] = {int(ts): i for i, ts in enumerate(df['ts'])}

    # ── 3. Walk-forward simulation ───────────────────────────────
    balance  = initial_balance
    positions: dict[str, dict] = {}
    trades:  list[dict] = []
    equity:  list[dict] = [{'ts': trade_ts[0] / 1000, 'balance': balance, 'cum_pnl': 0.0}]
    total_pnl = 0.0

    if progress_cb:
        progress_cb('simulate', 0, len(trade_ts))
    progress_step = max(1, len(trade_ts) // 20)

    def _close(pos, exit_price, exit_ts_ms, reason):
        nonlocal balance, total_pnl
        qty = pos['notional'] / pos['entry_price']
        gross = (exit_price - pos['entry_price']) * qty if pos['side'] == 'LONG' \
                else (pos['entry_price'] - exit_price) * qty
        # Phí round-trip taker trên notional còn lại (entry-fee phần còn lại + exit-fee)
        fee = (pos['notional'] + exit_price * qty) * TAKER_FEE
        pnl = gross - fee
        roi = pnl / pos['notional'] * 100
        balance += pos['margin'] + pnl
        total_pnl += pnl
        dur_h = (exit_ts_ms - pos['open_ts_ms']) / 3_600_000
        trades.append({
            'coin':        pos['coin'],
            'side':        pos['side'],
            'open_ts':     pos['open_ts_ms'] / 1000,
            'close_ts':    exit_ts_ms / 1000,
            'entry_price': round(pos['entry_price'], 6),
            'exit_price':  round(exit_price, 6),
            'notional':    round(pos['notional'], 2),
            'pnl':         round(pnl, 4),
            'roi_pct':     round(roi, 3),
            'duration_h':  round(dur_h, 1),
            'exit_reason': reason,
            'max_fav':     round(pos.get('max_fav', 0), 2),
            'max_adv':     round(pos.get('max_adv', 0), 2),
        })
        del positions[pos['coin']]

    for bar_i, ts in enumerate(trade_ts):
        if progress_cb and bar_i % progress_step == 0:
            progress_cb('simulate', bar_i, len(trade_ts))

        # ── Update open positions (intrabar H/L) ────────────────
        for coin in list(positions.keys()):
            if coin not in idx_map or ts not in idx_map[coin]:
                continue
            df  = dfs[coin]
            row_i = idx_map[coin][ts]
            row = df.iloc[row_i]
            h, l, c = float(row['high']), float(row['low']), float(row['close'])
            atr = float(row['atr'])
            pos = positions[coin]

            if pos['side'] == 'LONG':
                # Trailing stop ratchet
                pos['trail_anchor'] = max(pos['trail_anchor'], h)
                new_stop = pos['trail_anchor'] - TRAIL_ATR_MULT * atr
                pos['stop'] = max(pos['stop'], new_stop)
                # Partial TP (intrabar HIGH) — chỉ khi BẬT
                if ENABLE_PARTIAL_TP and not pos['tp_fired']:
                    tp_px = pos['entry_price'] + PARTIAL_TP_ATR_MULT * pos['entry_atr']
                    if h >= tp_px:
                        half_qty = (pos['notional'] / pos['entry_price']) * PARTIAL_TP_RATIO
                        half_fee = (pos['notional'] * PARTIAL_TP_RATIO + tp_px * half_qty) * TAKER_FEE
                        half_pnl = (tp_px - pos['entry_price']) * half_qty - half_fee
                        balance     += pos['margin'] * PARTIAL_TP_RATIO + half_pnl
                        total_pnl   += half_pnl
                        pos['notional'] *= (1 - PARTIAL_TP_RATIO)
                        pos['margin']   *= (1 - PARTIAL_TP_RATIO)
                        pos['tp_fired']  = True
                # Stop (intrabar LOW)
                if l <= pos['stop']:
                    _close(pos, min(pos['stop'], c), ts, 'stop'); continue
            else:  # SHORT
                pos['trail_anchor'] = min(pos['trail_anchor'], l)
                new_stop = pos['trail_anchor'] + TRAIL_ATR_MULT * atr
                pos['stop'] = min(pos['stop'], new_stop)
                if ENABLE_PARTIAL_TP and not pos['tp_fired']:
                    tp_px = pos['entry_price'] - PARTIAL_TP_ATR_MULT * pos['entry_atr']
                    if l <= tp_px:
                        half_qty = (pos['notional'] / pos['entry_price']) * PARTIAL_TP_RATIO
                        half_fee = (pos['notional'] * PARTIAL_TP_RATIO + tp_px * half_qty) * TAKER_FEE
                        half_pnl = (pos['entry_price'] - tp_px) * half_qty - half_fee
                        balance     += pos['margin'] * PARTIAL_TP_RATIO + half_pnl
                        total_pnl   += half_pnl
                        pos['notional'] *= (1 - PARTIAL_TP_RATIO)
                        pos['margin']   *= (1 - PARTIAL_TP_RATIO)
                        pos['tp_fired']  = True
                if h >= pos['stop']:
                    _close(pos, max(pos['stop'], c), ts, 'stop'); continue

            # MFE / MAE tracking
            fav = (h - pos['entry_price']) / pos['entry_price'] * 100 if pos['side'] == 'LONG' \
                  else (pos['entry_price'] - l) / pos['entry_price'] * 100
            adv = (l - pos['entry_price']) / pos['entry_price'] * 100 if pos['side'] == 'LONG' \
                  else (pos['entry_price'] - h) / pos['entry_price'] * 100
            pos['max_fav'] = max(pos.get('max_fav', 0.0), fav)
            pos['max_adv'] = min(pos.get('max_adv', 0.0), adv)

        # ── EMA reverse exits ────────────────────────────────────
        for coin in list(positions.keys()):
            if coin not in idx_map or ts not in idx_map[coin]:
                continue
            df  = dfs[coin]
            ri  = idx_map[coin][ts]
            if ri < 1:
                continue
            cur = df.iloc[ri];  prv = df.iloc[ri - 1]
            c   = float(cur['close'])
            pos = positions[coin]
            cross_dn = prv['ema_fast'] >= prv['ema_slow'] and cur['ema_fast'] < cur['ema_slow']
            cross_up = prv['ema_fast'] <= prv['ema_slow'] and cur['ema_fast'] > cur['ema_slow']
            if pos['side'] == 'LONG'  and cross_dn: _close(pos, c, ts, 'ema_reverse'); continue
            if pos['side'] == 'SHORT' and cross_up:  _close(pos, c, ts, 'ema_reverse'); continue

        # ── Open new signals ─────────────────────────────────────
        if len(positions) < max_pos:
            for coin in coins:
                if len(positions) >= max_pos:
                    break
                if coin in positions or coin not in idx_map:
                    continue
                if ts not in idx_map[coin]:
                    continue
                ri = idx_map[coin][ts]
                if ri < 70:
                    continue
                snap = _eval_bar(dfs[coin], ri)
                sig  = snap.get('signal')
                if not sig:
                    continue
                # ── CỔNG REGIME ĐA KHUNG (đại tu) ──
                # 1H signal chỉ là TIMING; chỉ vào khi 4H của coin trending cùng chiều
                # VÀ regime BTC không nghịch. Coin thiếu dữ liệu 4H → bỏ (giống live fetch fail).
                if coin not in htf_regime:
                    continue
                ck, cd = htf_regime[coin]
                htf_dir = _htf_dir_at(ck, cd, ts)
                btc_dir = _htf_dir_at(btc_keys, btc_dirs, ts)
                if not regime_allows(sig, htf_dir, btc_dir):
                    continue
                if _is_correlated(coin, sig, positions):
                    continue
                entry = snap['close']
                atr   = snap['atr']
                if atr <= 0 or entry <= 0:
                    continue
                # Position sizing
                risk_amt  = balance * RISK_PCT
                stop_dist = STOP_ATR_MULT * atr
                stop_pct  = stop_dist / entry
                notional  = (risk_amt / stop_pct) if stop_pct > 0 else 0
                cap       = balance * MAX_POS_PCT * LEVERAGE_F
                notional  = min(notional, cap)
                margin    = notional / LEVERAGE_F
                if notional < MIN_USDT or margin < 1.0 or margin > balance:
                    continue
                stop = (entry - stop_dist) if sig == 'LONG' else (entry + stop_dist)
                balance -= margin
                positions[coin] = {
                    'coin':        coin,
                    'side':        sig,
                    'entry_price': entry,
                    'entry_atr':   atr,
                    'open_ts_ms':  ts,
                    'stop':        stop,
                    'trail_anchor': entry,
                    'notional':    notional,
                    'margin':      margin,
                    'tp_fired':    False,
                }

        # ── Equity snapshot (every 6 bars ≈ 1 day for 4H) ───────
        if bar_i % 6 == 0:
            unreal = 0.0
            for coin, pos in positions.items():
                if coin in idx_map and ts in idx_map[coin]:
                    c = float(dfs[coin].iloc[idx_map[coin][ts]]['close'])
                    qty = pos['notional'] / pos['entry_price']
                    unreal += ((c - pos['entry_price']) if pos['side'] == 'LONG'
                               else (pos['entry_price'] - c)) * qty
            equity.append({
                'ts':      ts / 1000,
                'balance': round(balance + unreal, 2),
                'cum_pnl': round(total_pnl + unreal, 4),
            })

    if progress_cb:
        progress_cb('simulate', len(trade_ts), len(trade_ts))

    # ── Close remaining positions at last price ──────────────────
    for coin, pos in list(positions.items()):
        if coin in idx_map:
            df  = dfs[coin]
            last_ts_ms = int(df.iloc[-1]['ts'])
            last_close = float(df.iloc[-1]['close'])
            _close(pos, last_close, last_ts_ms, 'end_of_data')

    return _build_result(trades, equity, initial_balance, len(all_ts), bar, coins)


# ════════════════════ METRICS ════════════════════

def _build_result(trades, equity, initial_balance, total_bars, bar, coins):
    n = len(trades)
    if n == 0:
        return {
            'n_trades': 0, 'win_rate': 0, 'total_pnl': 0, 'roi_pct': 0,
            'profit_factor': 0, 'max_drawdown': 0, 'sharpe': 0, 'expectancy': 0,
            'avg_win': 0, 'avg_loss': 0,
            'trades': [], 'equity': equity, 'per_coin': {}, 'exit_reasons': {},
            'params': {'bar': bar, 'total_bars': total_bars, 'initial_balance': initial_balance},
        }

    pnls  = [t['pnl'] for t in trades]
    wins  = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / n * 100
    gross_win  = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 1e-9
    pf = gross_win / gross_loss
    total_pnl = sum(pnls)
    avg_win  = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    expectancy = (win_rate / 100) * avg_win + (1 - win_rate / 100) * avg_loss

    # Max drawdown
    balances = [e['balance'] for e in equity]
    max_dd = 0.0
    peak = balances[0] if balances else initial_balance
    for b in balances:
        peak = max(peak, b)
        if peak > 0:
            max_dd = min(max_dd, (b - peak) / peak * 100)

    # Sharpe (annualized, per-bar returns)
    sharpe = 0.0
    if len(equity) > 5:
        bals = pd.Series([e['balance'] for e in equity])
        rets = bals.pct_change().dropna()
        std  = rets.std()
        if std > 0:
            bars_per_year = {'4H': 6 * 365, '1D': 365, '1H': 24 * 365}.get(bar, 6 * 365)
            sharpe = round(rets.mean() / std * math.sqrt(bars_per_year), 2)

    # Per-coin
    per_coin: dict = {}
    for t in trades:
        s = per_coin.setdefault(t['coin'], {'n': 0, 'wins': 0, 'pnl': 0.0})
        s['n'] += 1
        s['wins'] += 1 if t['pnl'] > 0 else 0
        s['pnl']  = round(s['pnl'] + t['pnl'], 4)
    for s in per_coin.values():
        s['win_rate'] = round(s['wins'] / s['n'] * 100, 1) if s['n'] else 0

    # Exit reasons
    reasons: dict = {}
    for t in trades:
        reasons[t['exit_reason']] = reasons.get(t['exit_reason'], 0) + 1

    return {
        'n_trades':      n,
        'win_rate':      round(win_rate, 1),
        'total_pnl':     round(total_pnl, 4),
        'roi_pct':       round(total_pnl / initial_balance * 100, 2),
        'profit_factor': round(pf, 2),
        'max_drawdown':  round(max_dd, 2),
        'sharpe':        sharpe,
        'expectancy':    round(expectancy, 4),
        'avg_win':       round(avg_win, 4),
        'avg_loss':      round(avg_loss, 4),
        'gross_win':     round(gross_win, 4),
        'gross_loss':    round(gross_loss, 4),
        'trades':        sorted(trades, key=lambda x: x['close_ts'], reverse=True),
        'equity':        equity,
        'per_coin':      dict(sorted(per_coin.items(), key=lambda x: x[1]['pnl'], reverse=True)),
        'exit_reasons':  reasons,
        'params': {
            'bar':             bar,
            'total_bars':      total_bars,
            'initial_balance': initial_balance,
            'coins':           coins,
        },
    }
