"""Build equity_data.json cho trang docs — query 2 DB analytics, tính cumulative PnL 30 ngày.

Chạy thủ công khi muốn cập nhật biểu đồ:
    python build_equity.py
"""
import json
import os
import sys
import sqlite3
import time
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ARB_DB   = os.path.join(HERE, '..', 'okx-arb-bot',     'analytics.db')
TREND_DB = os.path.join(HERE, '..', 'okx-trending-bot', 'analytics.db')
OUT      = os.path.join(HERE, 'equity_data.json')

DAYS = 30
now  = time.time()
since = now - DAYS * 86400


def _query_trades(db_path: str, pnl_col: str):
    """Trả list (close_ts, pnl) các trade đã đóng trong DAYS ngày, sort theo close_ts."""
    if not os.path.exists(db_path):
        return []
    c = sqlite3.connect(db_path, timeout=10)
    try:
        rows = c.execute(
            f"SELECT close_ts, {pnl_col} FROM trades "
            f"WHERE status='closed' AND close_ts IS NOT NULL AND close_ts >= ? "
            f"ORDER BY close_ts ASC",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        c.close()
    return [(float(r[0]), float(r[1] or 0)) for r in rows]


def _cum_curve(trades: list, anchor_ts: float):
    """Trả list [{ts, cum}] với điểm đầu là (anchor_ts, 0) và mỗi điểm sau cộng dồn pnl."""
    out = [{'ts': anchor_ts, 'cum': 0.0}]
    cum = 0.0
    for ts, pnl in trades:
        cum += pnl
        out.append({'ts': ts, 'cum': round(cum, 4)})
    # Thêm điểm cuối = now (giữ flat tới hiện tại)
    if out[-1]['ts'] < now:
        out.append({'ts': now, 'cum': round(cum, 4)})
    return out


def _max_drawdown(curve: list):
    peak = 0.0
    max_dd = 0.0
    for p in curve:
        peak = max(peak, p['cum'])
        dd = peak - p['cum']
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 4)


def _merge_total(arb_curve: list, trend_curve: list):
    """Hợp nhất 2 series thành tổng cộng dồn theo timeline merge."""
    events = sorted(
        [(p['ts'], 'a', p['cum']) for p in arb_curve] +
        [(p['ts'], 't', p['cum']) for p in trend_curve],
        key=lambda x: x[0],
    )
    a_cur = t_cur = 0.0
    out = []
    for ts, k, v in events:
        if k == 'a': a_cur = v
        else:        t_cur = v
        out.append({'ts': ts, 'cum': round(a_cur + t_cur, 4)})
    return out


arb_trades   = _query_trades(ARB_DB,   'total_pnl')
trend_trades = _query_trades(TREND_DB, 'pnl')

anchor = since
arb_curve   = _cum_curve(arb_trades,   anchor)
trend_curve = _cum_curve(trend_trades, anchor)
total_curve = _merge_total(arb_curve, trend_curve)

payload = {
    'generated_at': now,
    'generated_iso': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
    'days': DAYS,
    'window_start': since,
    'window_end':   now,
    'arb': {
        'curve':    arb_curve,
        'n_trades': len(arb_trades),
        'total':    round(arb_curve[-1]['cum'], 4),
        'max_dd':   _max_drawdown(arb_curve),
    },
    'trend': {
        'curve':    trend_curve,
        'n_trades': len(trend_trades),
        'total':    round(trend_curve[-1]['cum'], 4),
        'max_dd':   _max_drawdown(trend_curve),
    },
    'total': {
        'curve':    total_curve,
        'n_trades': len(arb_trades) + len(trend_trades),
        'total':    round(total_curve[-1]['cum'] if total_curve else 0, 4),
        'max_dd':   _max_drawdown(total_curve),
    },
}

with open(OUT, 'w', encoding='utf-8') as f:
    json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))

print(f"OK -> {OUT}")
print(f"  Arb:   {payload['arb']['n_trades']:>3} trades  net {payload['arb']['total']:+.2f}$  max DD {payload['arb']['max_dd']:.2f}$")
print(f"  Trend: {payload['trend']['n_trades']:>3} trades  net {payload['trend']['total']:+.2f}$  max DD {payload['trend']['max_dd']:.2f}$")
print(f"  TOTAL: {payload['total']['n_trades']:>3} trades  net {payload['total']['total']:+.2f}$  max DD {payload['total']['max_dd']:.2f}$")
