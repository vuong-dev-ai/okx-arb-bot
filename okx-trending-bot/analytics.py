"""
Analytics — log mọi signal + trade vào SQLite, suy ra coin/timeframe nào hiệu quả.

Tables:
  - signals: (id, ts, coin, signal, adx, atr_pct, gap_pct, close)
  - trades:  (id, coin, side, open_ts, close_ts, entry_price, exit_price,
              notional, n_periods, pnl, roi_pct, exit_reason, status, max_favorable, max_adverse)
"""
import os
import math
import time
import sqlite3
import threading
import logging

log = logging.getLogger(__name__)

DB_FILE = os.path.join(os.path.dirname(__file__), 'analytics.db')
_lock   = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL,
    coin      TEXT,
    signal    TEXT,
    trend     TEXT,
    adx       REAL,
    atr_pct   REAL,
    gap_pct   REAL,
    close     REAL
);
CREATE INDEX IF NOT EXISTS idx_signals_coin_ts ON signals(coin, ts);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    coin            TEXT NOT NULL,
    side            TEXT NOT NULL,
    open_ts         REAL NOT NULL,
    close_ts        REAL,
    entry_price     REAL,
    exit_price      REAL,
    contracts       REAL,
    ct_val          REAL,
    notional        REAL,
    entry_atr       REAL,
    stop_initial    REAL,
    pnl             REAL DEFAULT 0,
    fee             REAL DEFAULT 0,
    net_pnl         REAL DEFAULT 0,
    roi_pct         REAL DEFAULT 0,
    net_roi_pct     REAL DEFAULT 0,
    duration_h      REAL DEFAULT 0,
    max_favorable   REAL DEFAULT 0,
    max_adverse     REAL DEFAULT 0,
    exit_reason     TEXT,
    status          TEXT DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_trades_coin   ON trades(coin);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);

CREATE TABLE IF NOT EXISTS position_ticks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id    INTEGER,
    coin        TEXT,
    ts          REAL,
    price       REAL,
    pnl         REAL,
    pct         REAL,
    stop_price  REAL,
    trail_anchor REAL
);
CREATE INDEX IF NOT EXISTS idx_ticks_trade  ON position_ticks(trade_id);
CREATE INDEX IF NOT EXISTS idx_ticks_coin_ts ON position_ticks(coin, ts);
"""


def _conn():
    c = sqlite3.connect(DB_FILE, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL;")
    return c


def init():
    with _lock, _conn() as c:
        c.executescript(SCHEMA)
        for col, defn in [('fee', 'REAL DEFAULT 0'), ('net_pnl', 'REAL DEFAULT 0'),
                          ('net_roi_pct', 'REAL DEFAULT 0')]:
            try:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col} {defn}")
            except Exception:
                pass


# ─────────────────── RECORDERS ───────────────────
def record_signal(coin, snap):
    try:
        with _lock, _conn() as c:
            c.execute(
                "INSERT INTO signals(ts, coin, signal, trend, adx, atr_pct, gap_pct, close) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), coin, snap.get('signal'), snap.get('trend'),
                 snap.get('adx'), snap.get('atr_pct'), snap.get('gap_pct'),
                 snap.get('close')),
            )
    except Exception as e:
        log.warning(f"record_signal: {e}")


def record_open(pos):
    try:
        with _lock, _conn() as c:
            cur = c.execute(
                """INSERT INTO trades
                   (coin, side, open_ts, entry_price, contracts, ct_val,
                    notional, entry_atr, stop_initial, status)
                   VALUES (?,?,?,?,?,?,?,?,?, 'open')""",
                (pos['coin'], pos['side'], pos['open_time'], pos['entry_price'],
                 pos['contracts'], pos['ct_val'], pos['notional'],
                 pos['entry_atr'], pos['stop_price']),
            )
            pos['_trade_id'] = cur.lastrowid
    except Exception as e:
        log.warning(f"record_open: {e}")


def record_tick(pos, price, pnl, pct):
    """Snapshot vị thế đang mở cho dataset granular."""
    tid = pos.get('_trade_id')
    if not tid:
        return
    try:
        with _lock, _conn() as c:
            c.execute(
                """INSERT INTO position_ticks
                   (trade_id, coin, ts, price, pnl, pct, stop_price, trail_anchor)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (tid, pos['coin'], time.time(), price, pnl, pct,
                 pos.get('stop_price'), pos.get('trail_anchor')),
            )
    except Exception as e:
        log.warning(f"record_tick: {e}")


SWAP_FEE_ROUND_TRIP = 0.001  # 0.05% taker × 2 (mở + đóng)

def record_close(pos, exit_price, reason='auto', max_fav=None, max_adv=None):
    tid = pos.get('_trade_id')
    if not tid:
        return
    try:
        now = time.time()
        dur = (now - pos['open_time']) / 3600.0
        qty = pos['contracts'] * pos['ct_val']
        if pos['side'] == 'LONG':
            pnl = (exit_price - pos['entry_price']) * qty
        else:
            pnl = (pos['entry_price'] - exit_price) * qty
        notional = pos.get('notional') or (pos['contracts'] * pos['ct_val'] * pos['entry_price'])
        fee    = notional * SWAP_FEE_ROUND_TRIP
        net    = pnl - fee
        roi    = (pnl / notional * 100) if notional else 0
        net_roi = (net / notional * 100) if notional else 0
        with _lock, _conn() as c:
            c.execute(
                """UPDATE trades SET
                       close_ts=?, exit_price=?, pnl=?,
                       fee=?, net_pnl=?,
                       roi_pct=?, net_roi_pct=?,
                       duration_h=?, exit_reason=?,
                       max_favorable=COALESCE(?, max_favorable),
                       max_adverse  =COALESCE(?, max_adverse),
                       status='closed'
                   WHERE id=?""",
                (now, exit_price, pnl, fee, net,
                 roi, net_roi, dur, reason, max_fav, max_adv, tid),
            )
    except Exception as e:
        log.warning(f"record_close: {e}")


# ─────────────────── ANALYSIS ───────────────────
def coin_stats(min_trades=1):
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT coin,
                          COUNT(*)                                         AS n,
                          SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END)    AS wins,
                          AVG(roi_pct)                                     AS avg_roi,
                          AVG(net_roi_pct)                                 AS avg_net_roi,
                          AVG(duration_h)                                  AS avg_h,
                          SUM(pnl)                                         AS total_pnl,
                          SUM(fee)                                         AS total_fee,
                          SUM(net_pnl)                                     AS total_net_pnl,
                          AVG(CASE WHEN net_pnl > 0 THEN net_roi_pct END)  AS avg_win,
                          AVG(CASE WHEN net_pnl < 0 THEN net_roi_pct END)  AS avg_loss
                     FROM trades
                    WHERE status='closed'
                 GROUP BY coin
                   HAVING COUNT(*) >= ?
                 ORDER BY avg_net_roi DESC""",
                (min_trades,),
            ).fetchall()
    except Exception as e:
        log.warning(f"coin_stats: {e}")
        return []

    out = []
    for r in rows:
        d = dict(r)
        n = d['n'] or 0
        d['win_rate']   = (d['wins'] / n * 100) if n else 0
        conf            = n / (n + 3.0)
        d['confidence'] = round(conf, 3)
        d['score']      = round((d['avg_net_roi'] or 0) * conf, 4)
        d['profit_factor'] = round(
            abs((d['avg_win'] or 0) * (d['wins'] or 0)) /
            max(abs((d['avg_loss'] or 0) * ((n or 0) - (d['wins'] or 0))), 1e-9),
            2)
        out.append(d)
    return out


def coin_score_map():
    return {s['coin']: s['score'] for s in coin_stats()}


def global_stats():
    try:
        with _conn() as c:
            agg = c.execute(
                """SELECT COUNT(*)                                         AS n,
                          SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END)    AS wins,
                          AVG(roi_pct)                                     AS avg_roi,
                          AVG(net_roi_pct)                                 AS avg_net_roi,
                          AVG(duration_h)                                  AS avg_h,
                          SUM(pnl)                                         AS total_pnl,
                          SUM(fee)                                         AS total_fee,
                          SUM(net_pnl)                                     AS total_net_pnl,
                          AVG(CASE WHEN net_pnl > 0 THEN net_roi_pct END)  AS avg_win,
                          AVG(CASE WHEN net_pnl < 0 THEN net_roi_pct END)  AS avg_loss
                     FROM trades WHERE status='closed'"""
            ).fetchone()
            sides = c.execute(
                """SELECT side,
                          COUNT(*)                                 AS n,
                          AVG(roi_pct)                             AS avg_roi,
                          SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                          SUM(pnl)                                 AS total_pnl
                     FROM trades WHERE status='closed'
                 GROUP BY side"""
            ).fetchall()
            reasons = c.execute(
                """SELECT exit_reason,
                          COUNT(*)     AS n,
                          AVG(roi_pct) AS avg_roi
                     FROM trades WHERE status='closed'
                 GROUP BY exit_reason"""
            ).fetchall()
            n_signals = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
            n_open    = c.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
    except Exception as e:
        log.warning(f"global_stats: {e}")
        return {}

    g = dict(agg) if agg else {}
    n = g.get('n') or 0
    g['win_rate']      = ((g.get('wins') or 0) / n * 100) if n else 0
    g['avg_win']       = g.get('avg_win') or 0
    g['avg_loss']      = g.get('avg_loss') or 0
    g['expectancy']    = (g['win_rate']/100) * g['avg_win'] + (1 - g['win_rate']/100) * g['avg_loss']
    g['profit_factor'] = round(
        abs((g['avg_win'] or 0) * (g.get('wins') or 0)) /
        max(abs((g['avg_loss'] or 0) * (n - (g.get('wins') or 0))), 1e-9),
        2) if n else 0
    g['sides']         = [dict(s) for s in (sides or [])]
    g['reasons']       = [dict(r) for r in (reasons or [])]
    g['n_signals']     = n_signals
    g['n_open']        = n_open
    g['total_fee']     = g.get('total_fee') or 0
    g['total_net_pnl'] = g.get('total_net_pnl') or 0
    return g


def recent_trades(limit=20):
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT coin, side, open_ts, close_ts, entry_price, exit_price,
                          notional, pnl, fee, net_pnl, roi_pct, net_roi_pct,
                          duration_h, exit_reason
                     FROM trades WHERE status='closed'
                 ORDER BY close_ts DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning(f"recent_trades: {e}")
        return []


def equity_curve():
    """Equity curve theo net_pnl (sau fee)."""
    try:
        with _conn() as c:
            rows = c.execute(
                """SELECT close_ts, pnl, fee, net_pnl FROM trades
                   WHERE status='closed' AND close_ts IS NOT NULL
                   ORDER BY close_ts ASC"""
            ).fetchall()
    except Exception:
        return []
    cum_gross = 0.0
    cum_net   = 0.0
    out = []
    for r in rows:
        cum_gross += r['pnl'] or 0
        cum_net   += r['net_pnl'] or 0
        out.append({
            'ts':       r['close_ts'],
            'cum_pnl':  round(cum_gross, 4),
            'cum_net':  round(cum_net, 4),
        })
    return out


def backfill_fees():
    """Tính lại fee cho trade cũ chưa có (fee=0). Swap only: 0.05% × 2."""
    try:
        with _lock, _conn() as c:
            rows = c.execute(
                "SELECT id, notional, entry_price, contracts, ct_val, pnl FROM trades "
                "WHERE status='closed' AND (fee=0 OR fee IS NULL)"
            ).fetchall()
            updated = 0
            for r in rows:
                notional = r['notional'] or (r['contracts'] or 0) * (r['ct_val'] or 0) * (r['entry_price'] or 0)
                if not notional:
                    continue
                fee     = notional * SWAP_FEE_ROUND_TRIP
                net     = (r['pnl'] or 0) - fee
                net_roi = net / notional * 100
                c.execute(
                    "UPDATE trades SET fee=?, net_pnl=?, net_roi_pct=? WHERE id=?",
                    (fee, net, net_roi, r['id'])
                )
                updated += 1
        return updated
    except Exception as e:
        log.warning(f"backfill_fees: {e}")
        return 0
