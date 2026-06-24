"""
Analytics & adaptive strategy.

Mỗi lần chạy bot:
- Mọi lần scan funding rate → ghi vào bảng `scans`
- Mọi vị thế mở → INSERT row vào `trades` (status='open')
- Mọi vị thế đóng → UPDATE row + tính ROI, duration, exit_reason
- coin_score_map() trả về điểm số dùng để re-rank cơ hội ở vòng quét tiếp theo

Tự suy ra "cách hoạt động tối ưu nhất":
- Per-coin: avg_roi, win_rate, n_trades → score (có shrinkage)
- Threshold sweet spot: nhóm trade theo dải funding_rate vào lúc mở
- Khuyến nghị threshold = dải thấp nhất có avg_roi > 0
"""
import os
import math
import time
import sqlite3
import threading
import logging
from contextlib import closing

log = logging.getLogger(__name__)

DB_FILE = os.path.join(os.path.dirname(__file__), 'analytics.db')
_lock   = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    coin          TEXT    NOT NULL,
    funding_rate  REAL,
    next_rate     REAL,
    annualized    REAL
);
CREATE INDEX IF NOT EXISTS idx_scans_coin_ts ON scans(coin, ts);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    coin          TEXT    NOT NULL,
    open_ts       REAL    NOT NULL,
    close_ts      REAL,
    entry_rate    REAL,
    entry_price   REAL,
    exit_price    REAL,
    contracts     REAL,
    ct_val        REAL,
    usdt_in       REAL,
    n_payments    INTEGER DEFAULT 0,
    funding_pnl   REAL DEFAULT 0,
    price_pnl     REAL DEFAULT 0,
    total_pnl     REAL DEFAULT 0,
    fee           REAL DEFAULT 0,
    net_pnl       REAL DEFAULT 0,
    roi_pct       REAL DEFAULT 0,
    net_roi_pct   REAL DEFAULT 0,
    duration_h    REAL DEFAULT 0,
    exit_reason   TEXT,
    status        TEXT DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_trades_coin   ON trades(coin);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_close  ON trades(close_ts);

CREATE TABLE IF NOT EXISTS position_ticks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id      INTEGER,
    coin          TEXT,
    ts            REAL,
    price         REAL,
    funding_pnl   REAL,
    price_pnl     REAL,
    total_pnl     REAL,
    funding_rate  REAL
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
    with _lock, closing(_conn()) as c:
        c.executescript(SCHEMA)
        # Migration: thêm cột fee/net_pnl cho DB cũ chưa có
        for col, defn in [('fee', 'REAL DEFAULT 0'), ('net_pnl', 'REAL DEFAULT 0'),
                          ('net_roi_pct', 'REAL DEFAULT 0')]:
            try:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col} {defn}")
            except Exception:
                pass


# ─────────────────────────── RECORDERS ────────────────────────────

def record_scan(opps):
    if not opps:
        return
    ts = time.time()
    rows = [
        (ts, o.get('coin'), o.get('funding_rate'),
         o.get('next_rate'), o.get('annualized'))
        for o in opps
    ]
    try:
        with _lock, closing(_conn()) as c:
            c.executemany(
                "INSERT INTO scans(ts,coin,funding_rate,next_rate,annualized) "
                "VALUES (?,?,?,?,?)",
                rows,
            )
    except Exception as e:
        log.warning(f"record_scan: {e}")


def record_open(pos):
    """Mutates pos by adding `_trade_id`."""
    try:
        usdt_in = pos['contracts'] * pos['ct_val'] * pos['entry_price']
        with _lock, closing(_conn()) as c:
            cur = c.execute(
                """INSERT INTO trades
                   (coin, open_ts, entry_rate, entry_price, contracts, ct_val, usdt_in, status)
                   VALUES (?,?,?,?,?,?,?, 'open')""",
                (pos['coin'], pos['open_time'], pos.get('entry_funding_rate'),
                 pos['entry_price'], pos['contracts'], pos['ct_val'], usdt_in),
            )
            pos['_trade_id'] = cur.lastrowid
    except Exception as e:
        log.warning(f"record_open: {e}")


def record_tick(pos, pnl, funding_rate=None):
    """Ghi snapshot giá + PnL của 1 vị thế đang mở. Throttle do caller quản lý."""
    tid = pos.get('_trade_id')
    if not tid or not pnl:
        return
    try:
        with _lock, closing(_conn()) as c:
            c.execute(
                """INSERT INTO position_ticks
                   (trade_id, coin, ts, price, funding_pnl, price_pnl, total_pnl, funding_rate)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (tid, pos['coin'], time.time(), pnl.get('price'),
                 pnl.get('funding_pnl', 0) or 0, pnl.get('price_pnl', 0) or 0,
                 pnl.get('total_pnl', 0) or 0, funding_rate),
            )
    except Exception as e:
        log.warning(f"record_tick: {e}")


def record_close(pos, pnl, reason='auto'):
    tid = pos.get('_trade_id')
    if not tid:
        return
    try:
        now    = time.time()
        dur_h  = (now - pos['open_time']) / 3600.0
        vin    = pos['contracts'] * pos['ct_val'] * pos['entry_price']
        pnl    = pnl or {}
        total  = pnl.get('total_pnl', 0) or 0
        fee    = pnl.get('fee_est', 0) or 0
        net    = total - fee
        roi    = (total / vin * 100) if vin else 0
        net_roi = (net / vin * 100) if vin else 0
        with _lock, closing(_conn()) as c:
            c.execute(
                """UPDATE trades SET
                       close_ts=?, exit_price=?, n_payments=?,
                       funding_pnl=?, price_pnl=?, total_pnl=?,
                       fee=?, net_pnl=?,
                       roi_pct=?, net_roi_pct=?,
                       duration_h=?, exit_reason=?, status='closed'
                   WHERE id=?""",
                (now, pnl.get('price'), pnl.get('n_payments', 0) or 0,
                 pnl.get('funding_pnl', 0) or 0, pnl.get('price_pnl', 0) or 0,
                 total, fee, net, roi, net_roi, dur_h, reason, tid),
            )
    except Exception as e:
        log.warning(f"record_close: {e}")


# ─────────────────────────── ANALYSIS ─────────────────────────────

def coin_stats(min_trades=1):
    """Per-coin aggregates with confidence-shrunk score."""
    try:
        with closing(_conn()) as c:
            rows = c.execute(
                """SELECT coin,
                          COUNT(*)                                       AS n,
                          SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END)  AS wins,
                          AVG(roi_pct)                                   AS avg_roi,
                          AVG(net_roi_pct)                               AS avg_net_roi,
                          AVG(duration_h)                                AS avg_h,
                          AVG(n_payments)                                AS avg_pays,
                          AVG(funding_pnl)                               AS avg_fund,
                          AVG(price_pnl)                                 AS avg_price,
                          SUM(total_pnl)                                 AS total_pnl,
                          SUM(fee)                                       AS total_fee,
                          SUM(net_pnl)                                   AS total_net_pnl,
                          AVG(entry_rate)                                AS avg_entry_rate
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
        confidence      = n / (n + 3.0)
        d['confidence'] = round(confidence, 3)
        d['score']      = round((d['avg_net_roi'] or 0) * confidence, 4)
        out.append(d)
    return out


def coin_score_map():
    """coin → score float. Coin chưa có lịch sử → không có key (caller mặc định 0)."""
    return {s['coin']: s['score'] for s in coin_stats()}


def prune(scan_days=14, tick_days=30):
    """Xóa dữ liệu telemetry CŨ để DB không phình vô hạn khi chạy dài tháng.
    - scans: ~25 coin × mỗi 3 phút ⇒ ~12k dòng/ngày, chỉ để rank → giữ 14 ngày là thừa.
    - position_ticks: snapshot giá/PnL mỗi 60s/vị thế → giữ 30 ngày cho biểu đồ.
    GIỮ NGUYÊN bảng `trades` (lịch sử lệnh + analytics) vĩnh viễn. Trả (n_scans, n_ticks) đã xóa."""
    now = time.time()
    try:
        with _lock, closing(_conn()) as c:
            cur1 = c.execute("DELETE FROM scans WHERE ts < ?", (now - scan_days * 86400,))
            n_scans = cur1.rowcount
            cur2 = c.execute("DELETE FROM position_ticks WHERE ts < ?", (now - tick_days * 86400,))
            n_ticks = cur2.rowcount
        return n_scans, n_ticks
    except Exception as e:
        log.warning(f"prune: {e}")
        return 0, 0


def wal_checkpoint():
    """Gộp WAL vào DB chính + truncate (chống file -wal phình vô hạn khi chạy dài ngày).
    Trả size WAL (bytes) sau checkpoint để caller cảnh báo nếu vẫn lớn."""
    try:
        with _lock, closing(_conn()) as c:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception as e:
        log.warning(f"wal_checkpoint: {e}")
    try:
        wal = DB_FILE + '-wal'
        return os.path.getsize(wal) if os.path.exists(wal) else 0
    except Exception:
        return 0


def global_stats():
    """Tổng quan + threshold sweet spot."""
    try:
        with closing(_conn()) as c:
            agg = c.execute(
                """SELECT COUNT(*)                                       AS n,
                          SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END)  AS wins,
                          AVG(roi_pct)                                   AS avg_roi,
                          AVG(net_roi_pct)                               AS avg_net_roi,
                          AVG(duration_h)                                AS avg_h,
                          SUM(total_pnl)                                 AS total_pnl,
                          SUM(fee)                                       AS total_fee,
                          SUM(net_pnl)                                   AS total_net_pnl,
                          AVG(entry_rate)                                AS avg_entry_rate,
                          AVG(CASE WHEN net_pnl > 0 THEN net_roi_pct END) AS avg_win,
                          AVG(CASE WHEN net_pnl < 0 THEN net_roi_pct END) AS avg_loss,
                          SUM(CASE WHEN net_pnl > 0 THEN net_pnl ELSE 0 END) AS gross_win,
                          SUM(CASE WHEN net_pnl < 0 THEN net_pnl ELSE 0 END) AS gross_loss,
                          MIN(open_ts)                                   AS first_ts,
                          MAX(close_ts)                                  AS last_ts
                     FROM trades WHERE status='closed'"""
            ).fetchone()
            buckets = c.execute(
                """SELECT
                       CASE
                         WHEN entry_rate < 0.0002 THEN '0.01–0.02%'
                         WHEN entry_rate < 0.0005 THEN '0.02–0.05%'
                         WHEN entry_rate < 0.0010 THEN '0.05–0.10%'
                         WHEN entry_rate < 0.0020 THEN '0.10–0.20%'
                         ELSE                          '0.20%+'
                       END                                                AS bucket,
                       MIN(entry_rate)                                    AS lo,
                       COUNT(*)                                           AS n,
                       AVG(roi_pct)                                       AS avg_roi,
                       AVG(net_roi_pct)                                   AS avg_net_roi,
                       SUM(fee)                                           AS total_fee,
                       AVG(net_pnl)                                       AS avg_net_pnl,
                       SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) * 1.0 / COUNT(*)
                                                                          AS win_rate
                     FROM trades WHERE status='closed'
                 GROUP BY bucket
                 ORDER BY lo""",
            ).fetchall()
            n_scans = c.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
            n_open  = c.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
    except Exception as e:
        log.warning(f"global_stats: {e}")
        return {}

    g = dict(agg) if agg else {}
    n = g.get('n') or 0
    g['win_rate']   = ((g.get('wins') or 0) / n * 100) if n else 0
    # avg_win/avg_loss có thể NULL (chưa có lệnh thắng/thua) → ép 0 cho gateway format an toàn
    g['avg_win']    = g.get('avg_win') or 0.0
    g['avg_loss']   = g.get('avg_loss') or 0.0
    # profit_factor = tổng lãi / |tổng lỗ| (net, sau fee). Không có lệnh lỗ → để 99.99 nếu có lãi.
    gw = g.get('gross_win') or 0.0
    gl = abs(g.get('gross_loss') or 0.0)
    g['profit_factor'] = round(gw / gl, 2) if gl > 0 else (99.99 if gw > 0 else 0.0)
    g['buckets']    = [
        {**dict(b), 'win_rate': round((dict(b)['win_rate'] or 0) * 100, 1)}
        for b in (buckets or [])
    ]
    g['n_scans']    = n_scans
    g['n_open']     = n_open
    # Threshold đề xuất = dải thấp nhất có avg_net_roi > 0
    rec_bucket = None
    for b in g['buckets']:
        if (b.get('avg_net_roi') or 0) > 0 and (b['n'] or 0) >= 3:
            rec_bucket = b['bucket']
            break
    g['recommended_bucket'] = rec_bucket
    return g


def recent_trades(limit=15):
    try:
        with closing(_conn()) as c:
            rows = c.execute(
                """SELECT coin, open_ts, close_ts, entry_rate, entry_price, exit_price,
                          n_payments, funding_pnl, price_pnl, total_pnl,
                          fee, net_pnl, roi_pct, net_roi_pct,
                          duration_h, exit_reason
                     FROM trades
                    WHERE status='closed'
                 ORDER BY close_ts DESC
                    LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning(f"recent_trades: {e}")
        return []


def equity_curve():
    """Equity curve theo net_pnl (sau fee) để hiển thị thực tế."""
    try:
        with closing(_conn()) as c:
            rows = c.execute(
                """SELECT close_ts, net_pnl FROM trades
                   WHERE status='closed' AND close_ts IS NOT NULL
                   ORDER BY close_ts ASC"""
            ).fetchall()
    except Exception:
        return []
    cum = 0.0
    out = []
    for r in rows:
        cum += r['net_pnl'] or 0
        out.append({'ts': r['close_ts'], 'cum_net_pnl': round(cum, 4)})
    return out


def backfill_fees():
    """Tính lại fee cho các trade cũ chưa có fee (fee=0).
    Fee = (SPOT 0.1% + SWAP 0.05%) × 2 legs × usdt_in.
    Chỉ cập nhật các trade có fee=0 và usdt_in > 0."""
    ROUND_TRIP = (0.001 + 0.0005) * 2  # spot buy+sell + swap open+close
    try:
        with _lock, closing(_conn()) as c:
            rows = c.execute(
                "SELECT id, usdt_in, total_pnl FROM trades WHERE status='closed' AND (fee=0 OR fee IS NULL) AND usdt_in > 0"
            ).fetchall()
            for r in rows:
                fee = (r['usdt_in'] or 0) * ROUND_TRIP
                net = (r['total_pnl'] or 0) - fee
                vin = r['usdt_in'] or 1
                net_roi = net / vin * 100
                c.execute(
                    "UPDATE trades SET fee=?, net_pnl=?, net_roi_pct=? WHERE id=?",
                    (fee, net, net_roi, r['id'])
                )
        return len(rows)
    except Exception as e:
        log.warning(f"backfill_fees: {e}")
        return 0


# ─────────────────────────── RANKING ──────────────────────────────

def adjust_priority(funding_rate, score):
    """Blend funding rate (objective signal) with historical score (subjective edge).

    score ≈ avg_roi * confidence. tanh giới hạn ảnh hưởng ±40%.
    Coin mới (score=0) giữ nguyên xếp hạng theo funding rate.
    """
    factor = 1.0 + 0.4 * math.tanh((score or 0) / 2.0)
    return funding_rate * factor


def rank_opps(opps):
    """Sắp xếp lại opps theo điểm hỗn hợp = funding_rate × factor(score)."""
    smap = coin_score_map()
    for o in opps:
        o['hist_score'] = smap.get(o['coin'], 0.0)
        o['priority']   = adjust_priority(o['funding_rate'], o['hist_score'])
    opps.sort(key=lambda x: x['priority'], reverse=True)
    return opps
