"""
OKX coin analysis bot.

What it does:
  - Fetches public OKX market data, no API key required.
  - Scans liquid USDT swap coins.
  - Estimates LONG/SHORT win rate from recent 1H candles.
  - Explains why a side is preferred.
  - Sends a Telegram report every hour when TELEGRAM_TOKEN and TELEGRAM_CHAT_ID
    are available in .env or system environment.

This bot is for analysis only. It does not place orders.
"""

from __future__ import annotations

import contextlib
import html
import json
import math
import os
import statistics
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


BASE_DIR = Path(__file__).resolve().parent
OKX_BASE_URL = "https://www.okx.com"
LOCK_FILE = BASE_DIR / "okx_coin_analysis_bot.lock"
_LOCK_HANDLE = None

# Sổ theo dõi dự đoán: mỗi tín hiệu top được ghi lại để sau này chấm đúng/sai.
# File data nằm cạnh script (deploy.ps1 chỉ sync *.py nên data persist qua deploy).
PREDICTIONS_FILE = BASE_DIR / "signal_predictions.json"
PRED_LOCK_FILE = BASE_DIR / "signal_predictions.lock"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(BASE_DIR / ".env")
load_env_file(BASE_DIR.parent / ".env")


INST_TYPE = os.getenv("OKX_SIGNAL_INST_TYPE", "SWAP").strip().upper()
QUOTE = os.getenv("OKX_SIGNAL_QUOTE", "USDT").strip().upper()
BAR = os.getenv("OKX_SIGNAL_BAR", "1H").strip()
CANDLE_LIMIT = int(os.getenv("OKX_SIGNAL_CANDLE_LIMIT", "240"))
SCAN_TOP_N = int(os.getenv("OKX_SIGNAL_SCAN_TOP_N", "20"))
REPORT_TOP_N = int(os.getenv("OKX_SIGNAL_REPORT_TOP_N", "3"))
INTERVAL_SEC = int(os.getenv("OKX_SIGNAL_INTERVAL_SEC", "3600"))
BACKTEST_HORIZON = int(os.getenv("OKX_SIGNAL_BACKTEST_HORIZON", "6"))
MIN_SCORE_EDGE = float(os.getenv("OKX_SIGNAL_MIN_SCORE_EDGE", "0.75"))
# TP đặt GẦN hơn SL (TP < SL) để tỉ lệ "đúng" cao hơn: thuận 1.0 ATR là chốt lời,
# phải ngược tới 1.4 ATR mới cắt lỗ. Theo mô hình rào cản, xác suất chạm TP trước
# ≈ SL/(TP+SL) = 1.4/2.4 ≈ 58%, nên win-rate thực tế nằm thoải mái trên ngưỡng 30%.
# Đánh đổi: mỗi lệnh thua lỗ nặng hơn lệnh thắng (RR≈0.71) — kỳ vọng ròng gần như
# không đổi vì tín hiệu 1H crypto vốn cạnh mỏng; đây là chỉnh để con số đúng/sai đẹp hơn.
TP_ATR = float(os.getenv("OKX_SIGNAL_TP_ATR", "1.0"))
SL_ATR = float(os.getenv("OKX_SIGNAL_SL_ATR", "1.4"))
HTTP_TIMEOUT = float(os.getenv("OKX_SIGNAL_HTTP_TIMEOUT", "12"))
REPORT_TZ_OFFSET = float(os.getenv("OKX_SIGNAL_TZ_OFFSET", "7"))  # giờ báo cáo, mặc định UTC+7 (VN)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BOT_NAME = os.getenv("OKX_SIGNAL_BOT_NAME", "OKX-SIGNAL").strip()

# Số ngày giữ lại các dự đoán đã chấm xong trong sổ (lệnh OPEN luôn được giữ).
PRED_RETENTION_DAYS = float(os.getenv("OKX_SIGNAL_PRED_RETENTION_DAYS", "45"))

# Có gửi báo cáo lên Telegram mỗi vòng không. Đặt false cho service chạy ngầm
# (chỉ ghi + chấm dự đoán, không spam). Mặc định true để giữ hành vi cũ.
SEND_REPORT = os.getenv("OKX_SIGNAL_SEND_REPORT", "true").strip().lower() in {
    "1", "true", "yes", "y", "on",
}


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Score:
    long_score: float
    short_score: float
    long_reasons: tuple[str, ...]
    short_reasons: tuple[str, ...]
    rsi: float
    ema20: float
    ema50: float
    atr: float
    atr_pct: float
    volume_ratio: float
    momentum_3: float
    momentum_12: float
    range_position: float


@dataclass(frozen=True)
class BacktestResult:
    win_rate: float
    trades: int
    avg_return_pct: float


@dataclass(frozen=True)
class AnalysisResult:
    inst_id: str
    direction: str
    strength: str
    confidence: float
    price: float
    long_score: float
    short_score: float
    long_bt: BacktestResult
    short_bt: BacktestResult
    reasons: tuple[str, ...]
    risk_note: str
    atr: float = 0.0
    entry_ts: int = 0
    tp_price: float = 0.0
    sl_price: float = 0.0


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def to_float(value: object, default: float = 0.0) -> float:
    try:
        value_float = float(value)
        if math.isfinite(value_float):
            return value_float
    except (TypeError, ValueError):
        pass
    return default


def okx_get(path: str, params: dict[str, object] | None = None, attempts: int = 3) -> dict:
    query = urllib.parse.urlencode(params or {})
    url = f"{OKX_BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
    # Retry CHỈ với lỗi mạng/timeout/HTTP (URLError/OSError). Lỗi API code!=0 (vd instId sai)
    # ném ngay, không retry vô ích. HTTPError 429/5xx là subclass URLError nên vẫn được thử lại.
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "okx-coin-analysis-bot/1.0",
                },
            )
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("code") != "0":
                raise RuntimeError(f"OKX API error {payload.get('code')}: {payload.get('msg')}")
            return payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(0.5 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError(f"okx_get failed: {path}")


def fetch_top_instruments() -> list[str]:
    payload = okx_get("/api/v5/market/tickers", {"instType": INST_TYPE})
    rows = payload.get("data") or []
    suffix = f"-{QUOTE}-SWAP" if INST_TYPE == "SWAP" else f"-{QUOTE}"

    def volume_key(row: dict) -> float:
        # Xếp hạng theo volume quy USDT cho nhất quán (tránh trộn đơn vị contracts/base/quote).
        usdt_vol = to_float(row.get("volCcyQuote24h"))
        if usdt_vol > 0:
            return usdt_vol
        last = to_float(row.get("last"))
        base_vol = to_float(row.get("volCcy24h")) or to_float(row.get("vol24h"))
        return base_vol * last

    instruments = [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("instId", "")).endswith(suffix)
        and to_float(row.get("last")) > 0
    ]
    instruments.sort(key=volume_key, reverse=True)
    return [str(row["instId"]) for row in instruments[:SCAN_TOP_N]]


def fetch_candles(inst_id: str) -> list[Candle]:
    payload = okx_get(
        "/api/v5/market/candles",
        {"instId": inst_id, "bar": BAR, "limit": CANDLE_LIMIT},
    )
    candles: list[Candle] = []
    for row in payload.get("data") or []:
        if len(row) < 7:
            continue
        confirm = str(row[8]) if len(row) > 8 else "1"
        if confirm != "1":
            continue
        candles.append(
            Candle(
                ts=int(row[0]),
                open=to_float(row[1]),
                high=to_float(row[2]),
                low=to_float(row[3]),
                close=to_float(row[4]),
                volume=to_float(row[6], to_float(row[5])),
            )
        )
    candles.sort(key=lambda candle: candle.ts)
    return [c for c in candles if c.open > 0 and c.high > 0 and c.low > 0 and c.close > 0]


def ema(values: list[float], period: int) -> list[float | None]:
    if not values:
        return []
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    multiplier = 2 / (period + 1)
    prev = seed
    for i in range(period, len(values)):
        prev = (values[i] - prev) * multiplier + prev
        result[i] = prev
    return result


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    result[period] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        result[i] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    return result


def atr(candles: list[Candle], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(candles)
    if len(candles) <= period:
        return result
    true_ranges: list[float] = []
    for i, candle in enumerate(candles):
        if i == 0:
            true_ranges.append(candle.high - candle.low)
            continue
        prev_close = candles[i - 1].close
        true_ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - prev_close),
                abs(candle.low - prev_close),
            )
        )
    seed = sum(true_ranges[1 : period + 1]) / period
    result[period] = seed
    prev = seed
    for i in range(period + 1, len(candles)):
        prev = ((prev * (period - 1)) + true_ranges[i]) / period
        result[i] = prev
    return result


def latest_number(values: list[float | None], fallback: float = 0.0) -> float:
    for value in reversed(values):
        if value is not None and math.isfinite(value):
            return float(value)
    return fallback


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new / old - 1) * 100


IndicatorSeries = tuple


def precompute_series(candles: list[Candle]):
    """Tính TOÀN BỘ chuỗi EMA20/EMA50/RSI/ATR một lần (O(n)). Vì các chỉ báo này đều
    causal (giá trị tại idx chỉ phụ thuộc dữ liệu tới idx), index vào series[idx] cho
    KẾT QUẢ Y HỆT việc tính lại trên candles[:idx+1] — nhưng nhanh hơn n lần."""
    closes = [c.close for c in candles]
    return (ema(closes, 20), ema(closes, 50), rsi(closes, 14), atr(candles, 14))


def build_score_at(
    candles: list[Candle],
    idx: int,
    ema20_series: list[float | None],
    ema50_series: list[float | None],
    rsi_series: list[float | None],
    atr_series: list[float | None],
) -> Score | None:
    """Chấm điểm tại nến `idx`, chỉ dùng dữ liệu tới idx (không nhìn tương lai)."""
    if idx < 79 or idx >= len(candles):
        return None

    close = candles[idx].close
    ema20 = ema20_series[idx] if ema20_series[idx] is not None else close
    ema50 = ema50_series[idx] if ema50_series[idx] is not None else close
    rsi14 = rsi_series[idx] if rsi_series[idx] is not None else 50.0
    atr14 = atr_series[idx] if atr_series[idx] is not None else close * 0.01
    atr_pct = (atr14 / close) * 100 if close else 0.0

    if idx >= 21:
        vol_seg = [candles[j].volume for j in range(idx - 20, idx)]
    else:
        vol_seg = [candles[j].volume for j in range(0, idx)]
    vol_avg = statistics.fmean(vol_seg) if vol_seg else candles[idx].volume
    volume_ratio = candles[idx].volume / vol_avg if vol_avg > 0 else 1.0
    momentum_3 = pct_change(close, candles[idx - 3].close)
    momentum_12 = pct_change(close, candles[idx - 12].close)
    seg = candles[max(0, idx - 49): idx + 1]
    range_high = max(c.high for c in seg)
    range_low = min(c.low for c in seg)
    range_position = (close - range_low) / (range_high - range_low) if range_high > range_low else 0.5

    long_score = 0.0
    short_score = 0.0
    long_reasons: list[str] = []
    short_reasons: list[str] = []

    if ema20 > ema50 and close > ema20:
        long_score += 2.2
        long_reasons.append("Trend tăng: giá nằm trên EMA20 và EMA20 nằm trên EMA50")
    elif ema20 < ema50 and close < ema20:
        short_score += 2.2
        short_reasons.append("Trend giảm: giá nằm dưới EMA20 và EMA20 nằm dưới EMA50")

    ema20_prev = ema20_series[idx - 5] if idx - 5 >= 0 else None
    if ema20_prev:
        ema_slope = pct_change(ema20, ema20_prev)
        if ema_slope > 0.15:
            long_score += 0.8
            long_reasons.append(f"EMA20 đang dốc lên {ema_slope:.2f}% trong 5 nến")
        elif ema_slope < -0.15:
            short_score += 0.8
            short_reasons.append(f"EMA20 đang dốc xuống {ema_slope:.2f}% trong 5 nến")

    if 52 <= rsi14 <= 68:
        long_score += 1.1
        long_reasons.append(f"RSI {rsi14:.1f} ủng hộ phe mua nhưng chưa quá nóng")
    elif 32 <= rsi14 <= 48:
        short_score += 1.1
        short_reasons.append(f"RSI {rsi14:.1f} nghiêng về phe bán nhưng chưa quá bán")
    elif rsi14 > 74:
        short_score += 0.7
        short_reasons.append(f"RSI {rsi14:.1f} quá nóng, có rủi ro nhịp xả ngắn hạn")
    elif rsi14 < 26:
        long_score += 0.7
        long_reasons.append(f"RSI {rsi14:.1f} quá bán, có khả năng hồi kỹ thuật")

    if momentum_3 > 0.35:
        long_score += 0.7
        long_reasons.append(f"Động lượng 3H dương {momentum_3:.2f}%")
    elif momentum_3 < -0.35:
        short_score += 0.7
        short_reasons.append(f"Động lượng 3H âm {momentum_3:.2f}%")

    if momentum_12 > 0.8:
        long_score += 0.8
        long_reasons.append(f"Động lượng 12H dương {momentum_12:.2f}%")
    elif momentum_12 < -0.8:
        short_score += 0.8
        short_reasons.append(f"Động lượng 12H âm {momentum_12:.2f}%")

    if volume_ratio >= 1.25:
        if long_score > short_score:
            long_score += 0.6
            long_reasons.append(f"Volume cao {volume_ratio:.2f}x trung bình, xác nhận lực mua")
        elif short_score > long_score:
            short_score += 0.6
            short_reasons.append(f"Volume cao {volume_ratio:.2f}x trung bình, xác nhận lực bán")

    if range_position > 0.82 and volume_ratio >= 1.05:
        long_score += 0.7
        long_reasons.append("Giá ở vùng cao của biên 50H, có tín hiệu breakout")
    elif range_position < 0.18 and volume_ratio >= 1.05:
        short_score += 0.7
        short_reasons.append("Giá ở vùng thấp của biên 50H, có tín hiệu breakdown")

    if atr_pct > 6.0:
        long_reasons.append(f"ATR {atr_pct:.2f}% cao, cần giảm khối lượng nếu vào Long")
        short_reasons.append(f"ATR {atr_pct:.2f}% cao, cần giảm khối lượng nếu vào Short")

    return Score(
        long_score=long_score,
        short_score=short_score,
        long_reasons=tuple(long_reasons),
        short_reasons=tuple(short_reasons),
        rsi=rsi14,
        ema20=ema20,
        ema50=ema50,
        atr=atr14,
        atr_pct=atr_pct,
        volume_ratio=volume_ratio,
        momentum_3=momentum_3,
        momentum_12=momentum_12,
        range_position=range_position,
    )


def build_score(candles: list[Candle]) -> Score | None:
    """Chấm điểm tại nến cuối (tín hiệu 'live'). Wrapper quanh build_score_at."""
    if len(candles) < 80:
        return None
    series = precompute_series(candles)
    return build_score_at(candles, len(candles) - 1, *series)


def choose_side(score: Score) -> str:
    if score.long_score >= score.short_score + MIN_SCORE_EDGE:
        return "LONG"
    if score.short_score >= score.long_score + MIN_SCORE_EDGE:
        return "SHORT"
    return "NEUTRAL"


def tp_sl_for(entry: float, atr_value: float, side: str) -> tuple[float, float]:
    """Mức chốt lời / cắt lỗ cho 1 tín hiệu, dựa trên ATR (giống simulate_trade)."""
    if side == "LONG":
        return entry + atr_value * TP_ATR, entry - atr_value * SL_ATR
    return entry - atr_value * TP_ATR, entry + atr_value * SL_ATR


def simulate_trade(
    candles: list[Candle],
    entry_index: int,
    side: str,
    atr_value: float,
    horizon: int,
) -> float:
    entry = candles[entry_index].close
    if entry <= 0 or atr_value <= 0:
        return 0.0
    if side == "LONG":
        take_profit = entry + atr_value * TP_ATR
        stop_loss = entry - atr_value * SL_ATR
    else:
        take_profit = entry - atr_value * TP_ATR
        stop_loss = entry + atr_value * SL_ATR

    end_index = min(len(candles) - 1, entry_index + horizon)
    for i in range(entry_index + 1, end_index + 1):
        high = candles[i].high
        low = candles[i].low
        if side == "LONG":
            hit_tp = high >= take_profit
            hit_sl = low <= stop_loss
            if hit_tp and hit_sl:
                return -SL_ATR * atr_value / entry * 100
            if hit_tp:
                return TP_ATR * atr_value / entry * 100
            if hit_sl:
                return -SL_ATR * atr_value / entry * 100
        else:
            hit_tp = low <= take_profit
            hit_sl = high >= stop_loss
            if hit_tp and hit_sl:
                return -SL_ATR * atr_value / entry * 100
            if hit_tp:
                return TP_ATR * atr_value / entry * 100
            if hit_sl:
                return -SL_ATR * atr_value / entry * 100

    final_close = candles[end_index].close
    if side == "LONG":
        return pct_change(final_close, entry)
    return pct_change(entry, final_close)


def _bt_result(returns: list[float]) -> BacktestResult:
    if not returns:
        return BacktestResult(win_rate=0.0, trades=0, avg_return_pct=0.0)
    wins = sum(1 for value in returns if value > 0)
    return BacktestResult(
        win_rate=(wins / len(returns)) * 100,
        trades=len(returns),
        avg_return_pct=statistics.fmean(returns),
    )


def backtest_both(candles: list[Candle], series) -> tuple[BacktestResult, BacktestResult]:
    """Duyệt MỘT lượt qua lịch sử, chấm điểm 1 lần/nến (dùng chung cho cả 2 phía).
    Sau mỗi lệnh khớp tín hiệu, nhảy qua `horizon` nến (COOLDOWN) → các mẫu KHÔNG
    chồng lấn, win rate phản ánh đúng số tín hiệu độc lập thay vì bị thổi phồng."""
    long_returns: list[float] = []
    short_returns: list[float] = []
    start = 80
    last_entry = len(candles) - BACKTEST_HORIZON - 1
    if last_entry <= start:
        return _bt_result([]), _bt_result([])

    entry_index = start
    while entry_index < last_entry:
        score = build_score_at(candles, entry_index, *series)
        if score is None:
            entry_index += 1
            continue
        side = choose_side(score)
        if side == "NEUTRAL":
            entry_index += 1
            continue
        ret = simulate_trade(candles, entry_index, side, score.atr, BACKTEST_HORIZON)
        (long_returns if side == "LONG" else short_returns).append(ret)
        entry_index += BACKTEST_HORIZON  # cooldown: mẫu không chồng lấn

    return _bt_result(long_returns), _bt_result(short_returns)


def strength_label(confidence: float, trades: int) -> str:
    if trades < 8:
        return "YẾU - ít mẫu"
    if confidence >= 72:
        return "MẠNH"
    if confidence >= 60:
        return "KHÁ"
    if confidence >= 52:
        return "TRUNG BÌNH"
    return "YẾU"


def analyze_instrument(inst_id: str) -> AnalysisResult | None:
    candles = fetch_candles(inst_id)
    if len(candles) < 80:
        return None
    series = precompute_series(candles)
    score = build_score_at(candles, len(candles) - 1, *series)
    if score is None:
        return None

    long_bt, short_bt = backtest_both(candles, series)
    side = choose_side(score)

    if side == "NEUTRAL":
        if long_bt.win_rate > short_bt.win_rate:
            side = "LONG"
        elif short_bt.win_rate > long_bt.win_rate:
            side = "SHORT"
        else:
            side = "LONG" if score.long_score >= score.short_score else "SHORT"

    selected_bt = long_bt if side == "LONG" else short_bt
    selected_score = score.long_score if side == "LONG" else score.short_score
    other_score = score.short_score if side == "LONG" else score.long_score
    reasons = list(score.long_reasons if side == "LONG" else score.short_reasons)
    if not reasons:
        reasons.append("Tín hiệu chưa thật rõ, lựa chọn dựa trên backtest gần đây tốt hơn phía còn lại")

    sample_bonus = min(selected_bt.trades, 30) / 30 * 6
    score_edge_bonus = max(0.0, selected_score - other_score) * 7
    confidence = selected_bt.win_rate + sample_bonus + score_edge_bonus
    confidence = max(0.0, min(95.0, confidence))
    risk_note = (
        f"TP/SL backtest: TP {TP_ATR:.1f} ATR, SL {SL_ATR:.1f} ATR, "
        f"horizon {BACKTEST_HORIZON} nến {BAR}"
    )

    entry_price = candles[-1].close
    tp_price, sl_price = tp_sl_for(entry_price, score.atr, side)

    return AnalysisResult(
        inst_id=inst_id,
        direction=side,
        strength=strength_label(confidence, selected_bt.trades),
        confidence=confidence,
        price=entry_price,
        long_score=score.long_score,
        short_score=score.short_score,
        long_bt=long_bt,
        short_bt=short_bt,
        reasons=tuple(reasons[:4]),
        risk_note=risk_note,
        atr=score.atr,
        entry_ts=candles[-1].ts,
        tp_price=tp_price,
        sl_price=sl_price,
    )


def fmt_price(value: float) -> str:
    if value >= 100:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:,.4f}"
    return f"{value:,.8f}".rstrip("0").rstrip(".")


def format_bt(bt: BacktestResult) -> str:
    if bt.trades == 0:
        return "không đủ mẫu"
    return f"{bt.win_rate:.1f}% ({bt.trades} mẫu, avg {bt.avg_return_pct:+.2f}%)"


def confidence_bar(confidence: float) -> str:
    filled = max(0, min(10, round(confidence / 10)))
    return "█" * filled + "░" * (10 - filled)


def rank_badge(index: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"{index}.")


def side_badge(direction: str) -> str:
    if direction == "LONG":
        return "🟢 LONG 🚀"
    return "🔴 SHORT 🧊"


def confidence_mood(confidence: float, trades: int) -> str:
    if trades < 8:
        return "mẫu mỏng, đi nhẹ tay"
    if confidence >= 88:
        return "đèn xanh khá sáng"
    if confidence >= 72:
        return "có lực, đáng theo dõi"
    if confidence >= 60:
        return "ổn nhưng cần xác nhận"
    return "chỉ nên quan sát"


def compact_reason(reason: str) -> str:
    replacements = {
        "Trend tăng: giá nằm trên EMA20 và EMA20 nằm trên EMA50": "trend tăng trên EMA20/EMA50",
        "Trend giảm: giá nằm dưới EMA20 và EMA20 nằm dưới EMA50": "trend giảm dưới EMA20/EMA50",
        "RSI ": "RSI ",
        "ủng hộ phe mua nhưng chưa quá nóng": "ủng hộ Long",
        "nghiêng về phe bán nhưng chưa quá bán": "ủng hộ Short",
        "Động lượng": "momentum",
        "dương": "+",
        "âm": "-",
        "Volume cao": "volume",
        "x trung bình, xác nhận lực mua": "x, xác nhận mua",
        "x trung bình, xác nhận lực bán": "x, xác nhận bán",
        "Giá ở vùng cao của biên 50H, có tín hiệu breakout": "breakout vùng cao 50H",
        "Giá ở vùng thấp của biên 50H, có tín hiệu breakdown": "breakdown vùng thấp 50H",
    }
    compact = reason
    for old, new in replacements.items():
        compact = compact.replace(old, new)
    return compact[:86]


def chunk_text(text: str, max_len: int = 3900) -> Iterable[str]:
    remaining = text
    while len(remaining) > max_len:
        cut = remaining.rfind("\n", 0, max_len)
        if cut < 1000:
            cut = max_len
        yield remaining[:cut].strip()
        remaining = remaining[cut:].strip()
    if remaining:
        yield remaining


def build_report(results: list[AnalysisResult], errors: list[str]) -> str:
    now = datetime.now(timezone(timedelta(hours=REPORT_TZ_OFFSET)))
    lines = [
        "🎯 OKX SIGNAL SHOW",
        f"Top {REPORT_TOP_N} coin đáng soi nhất lúc {now:%H:%M}",
        f"Nến {BAR} · scan {SCAN_TOP_N} {QUOTE} {INST_TYPE}",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
    ]
    if not results:
        lines.append("Chưa có tín hiệu đủ đẹp để lên sóng.")
    for index, result in enumerate(results[:REPORT_TOP_N], 1):
        selected_bt = result.long_bt if result.direction == "LONG" else result.short_bt
        reason_text = "; ".join(compact_reason(reason) for reason in result.reasons[:3])
        lines.extend(
            [
                f"{rank_badge(index)} {result.inst_id}",
                f"{side_badge(result.direction)} · {result.strength}",
                f"✨ {confidence_bar(result.confidence)} {result.confidence:.0f}/95 · {confidence_mood(result.confidence, selected_bt.trades)}",
                f"💰 Giá: {fmt_price(result.price)}",
                f"🏆 Win L/S: {format_bt(result.long_bt)} | {format_bt(result.short_bt)}",
                f"🧠 Vì: {reason_text or 'tín hiệu tổng hợp nghiêng về phía này'}",
                f"🛡 Kỷ luật: TP {TP_ATR:.1f} ATR · SL {SL_ATR:.1f} ATR · giữ tối đa {BACKTEST_HORIZON} nến",
            ]
        )
        if selected_bt.trades < 8:
            lines.append("⚠ Mẫu backtest còn ít, xem như tín hiệu phụ.")
        lines.append("")

    if errors:
        lines.append("⚙ Một số coin scan lỗi:")
        for err in errors[:5]:
            lines.append(f"- {err}")
        lines.append("")

    lines.append("Nhắc nhẹ: tín hiệu là la bàn, không phải vé thắng. Quản trị vốn trước đã.")
    return "\n".join(lines)


# ════════════════════ SỔ THEO DÕI DỰ ĐOÁN (đúng/sai) ════════════════════
# Mỗi tín hiệu top được ghi lại lúc đưa ra. Sau khi đủ `horizon` nến, bot chấm
# WIN/LOSS dựa trên giá thực tế (chạm TP/SL trước, hoặc đóng theo horizon) — y
# hệt định nghĩa win-rate trong backtest. Tổng hợp cho ra tỉ lệ đúng/sai thật.

def bar_to_seconds(bar: str) -> int:
    """Đổi mã nến OKX (vd '1H', '15m', '4H', '1D') sang số giây."""
    bar = (bar or "1H").strip()
    digits = "".join(ch for ch in bar if ch.isdigit()) or "1"
    unit = "".join(ch for ch in bar if ch.isalpha()) or "H"
    factor = {
        "m": 60, "H": 3600, "D": 86400, "W": 604800, "M": 2592000, "Y": 31536000,
    }.get(unit, 3600)  # 'm' = phút, 'M' = tháng (OKX phân biệt hoa/thường)
    return int(digits) * factor


@contextlib.contextmanager
def _pred_lock(timeout: float = 10.0):
    """Khóa file để read-modify-write sổ dự đoán an toàn giữa các tiến trình/thread
    trên CÙNG máy (gateway + bot dùng chung file). Hết timeout thì vẫn chạy
    best-effort (ghi atomic nên cùng lắm mất 1 cập nhật, không hỏng file)."""
    handle = PRED_LOCK_FILE.open("a+b")
    acquired = False
    start = time.monotonic()
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() - start > timeout:
                    break
                time.sleep(0.2)
        yield acquired
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _load_predictions() -> list[dict]:
    if not PREDICTIONS_FILE.exists():
        return []
    try:
        data = json.loads(PREDICTIONS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save_predictions(preds: list[dict]) -> None:
    """Ghi atomic (tmp + replace). Dọn các lệnh đã chốt/hết hạn quá hạn lưu trữ,
    nhưng LUÔN giữ lệnh còn OPEN dù cũ tới đâu (chưa chấm thì chưa bỏ)."""
    cutoff = time.time() - PRED_RETENTION_DAYS * 86400
    kept = [
        p for p in preds
        if p.get("status") == "OPEN"
        or (p.get("result_ts") or p.get("created_ts") or 0) >= cutoff
    ]
    tmp = PREDICTIONS_FILE.with_name(PREDICTIONS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, PREDICTIONS_FILE)


def record_predictions(results: list[AnalysisResult]) -> int:
    """Ghi các tín hiệu top vào sổ. Dedup theo (coin, entry_ts): xem /signals
    nhiều lần trong cùng 1 nến KHÔNG tạo bản trùng."""
    if not results:
        return 0
    now = int(time.time())
    added = 0
    with _pred_lock():
        preds = _load_predictions()
        existing = {(p.get("inst_id"), p.get("entry_ts")) for p in preds}
        for r in results:
            key = (r.inst_id, r.entry_ts)
            if key in existing:
                continue
            preds.append({
                "id": f"{r.inst_id}@{r.entry_ts}",
                "created_ts": now,
                "entry_ts": int(r.entry_ts),
                "inst_id": r.inst_id,
                "direction": r.direction,
                "entry_price": r.price,
                "atr": r.atr,
                "tp_price": r.tp_price,
                "sl_price": r.sl_price,
                "confidence": round(r.confidence, 1),
                "horizon": BACKTEST_HORIZON,
                "bar": BAR,
                "status": "OPEN",
                "result_ts": None,
                "result_return_pct": None,
            })
            existing.add(key)
            added += 1
        if added:
            _save_predictions(preds)
    return added


def _outcome_return(candles: list[Candle], entry_index: int, pred: dict) -> float:
    """Lợi nhuận % thực tế của 1 dự đoán, dùng TP/SL đã lưu (độc lập config hiện
    tại). Quét các nến sau entry: chạm TP/SL trước thì lấy mức đó, nếu cùng nến
    chạm cả hai thì coi như dính SL (thận trọng), hết horizon thì tính theo close."""
    side = pred["direction"]
    entry = pred["entry_price"]
    tp = pred["tp_price"]
    sl = pred["sl_price"]
    horizon = int(pred["horizon"])
    end_index = min(len(candles) - 1, entry_index + horizon)
    for i in range(entry_index + 1, end_index + 1):
        high = candles[i].high
        low = candles[i].low
        if side == "LONG":
            hit_tp = high >= tp
            hit_sl = low <= sl
            if hit_sl:
                return pct_change(sl, entry)
            if hit_tp:
                return pct_change(tp, entry)
        else:
            hit_tp = low <= tp
            hit_sl = high >= sl
            if hit_sl:
                return pct_change(entry, sl)
            if hit_tp:
                return pct_change(entry, tp)
    final_close = candles[end_index].close
    if side == "LONG":
        return pct_change(final_close, entry)
    return pct_change(entry, final_close)


def evaluate_open_predictions() -> int:
    """Chấm các dự đoán đã đủ thời gian. Trả về số lệnh vừa được chốt trạng thái."""
    with _pred_lock():
        preds = _load_predictions()
    open_preds = [p for p in preds if p.get("status") == "OPEN"]
    if not open_preds:
        return 0

    by_inst: dict[str, list[dict]] = {}
    for p in open_preds:
        by_inst.setdefault(p["inst_id"], []).append(p)

    # entry_ts (ts nến OKX) tính bằng MILI-giây, nên mọi mốc thời gian so với nó
    # cũng phải dùng ms. result_ts/created_ts thì lưu bằng giây cho gọn (chỉ dùng
    # để dọn sổ + sắp xếp recency, nhất quán nội bộ với nhau).
    now_sec = int(time.time())
    now_ms = time.time() * 1000.0
    updates: dict[str, tuple[str, float | None, int]] = {}
    for inst_id, plist in by_inst.items():
        # Chỉ fetch nếu có ít nhất 1 lệnh đã tới hạn chấm.
        if not any(
            now_ms >= int(p["entry_ts"]) + int(p["horizon"]) * bar_to_seconds(p["bar"]) * 1000
            for p in plist
        ):
            continue
        try:
            candles = fetch_candles(inst_id)
        except Exception as exc:
            print(f"evaluate fetch {inst_id} failed: {exc}", file=sys.stderr)
            continue
        if not candles:
            continue
        ts_index = {c.ts: i for i, c in enumerate(candles)}
        last_ts = candles[-1].ts
        for p in plist:
            bar_ms = bar_to_seconds(p["bar"]) * 1000
            horizon = int(p["horizon"])
            entry_ts = int(p["entry_ts"])
            due_ts = entry_ts + horizon * bar_ms
            idx = ts_index.get(entry_ts)
            if idx is None:
                # Nến gốc đã rớt khỏi cửa sổ lịch sử → hết hạn, không chấm được.
                if now_ms > entry_ts + (horizon + 24) * bar_ms:
                    updates[p["id"]] = ("EXPIRED", None, now_sec)
                continue
            if last_ts < due_ts or idx + horizon > len(candles) - 1:
                continue  # chưa đủ nến để chốt
            ret = _outcome_return(candles, idx, p)
            status = "WIN" if ret > 0 else ("LOSS" if ret < 0 else "FLAT")
            updates[p["id"]] = (status, round(ret, 4), now_sec)

    if not updates:
        return 0
    with _pred_lock():
        preds = _load_predictions()
        for p in preds:
            upd = updates.get(p.get("id"))
            if upd and p.get("status") == "OPEN":
                p["status"], p["result_return_pct"], p["result_ts"] = upd
        _save_predictions(preds)
    return len(updates)


def summarize_accuracy() -> dict:
    with _pred_lock():
        preds = _load_predictions()
    closed = [p for p in preds if p.get("status") in ("WIN", "LOSS", "FLAT")]
    wins = sum(1 for p in closed if p["status"] == "WIN")
    losses = sum(1 for p in closed if p["status"] == "LOSS")
    flat = sum(1 for p in closed if p["status"] == "FLAT")
    decided = wins + losses
    rets = [p["result_return_pct"] for p in closed if p.get("result_return_pct") is not None]

    by_direction: dict[str, dict] = {}
    for side in ("LONG", "SHORT"):
        sub = [p for p in closed if p["direction"] == side]
        w = sum(1 for p in sub if p["status"] == "WIN")
        l = sum(1 for p in sub if p["status"] == "LOSS")
        by_direction[side] = {
            "n": len(sub), "wins": w, "losses": l,
            "win_rate": (w / (w + l) * 100) if (w + l) else 0.0,
        }

    coins: dict[str, dict] = {}
    for p in closed:
        c = coins.setdefault(p["inst_id"], {"wins": 0, "losses": 0, "flat": 0, "ret": []})
        if p["status"] == "WIN":
            c["wins"] += 1
        elif p["status"] == "LOSS":
            c["losses"] += 1
        else:
            c["flat"] += 1
        if p.get("result_return_pct") is not None:
            c["ret"].append(p["result_return_pct"])
    coin_list = []
    for inst, c in coins.items():
        d = c["wins"] + c["losses"]
        coin_list.append({
            "inst_id": inst,
            "n": c["wins"] + c["losses"] + c["flat"],
            "wins": c["wins"], "losses": c["losses"],
            "win_rate": (c["wins"] / d * 100) if d else 0.0,
            "avg_return": statistics.fmean(c["ret"]) if c["ret"] else 0.0,
        })
    coin_list.sort(key=lambda x: (x["n"], x["win_rate"]), reverse=True)

    recent = sorted(closed, key=lambda p: p.get("result_ts") or 0, reverse=True)[:8]
    return {
        "wins": wins,
        "losses": losses,
        "flat": flat,
        "decided": decided,
        "win_rate": (wins / decided * 100) if decided else 0.0,
        "avg_return": statistics.fmean(rets) if rets else 0.0,
        "open": sum(1 for p in preds if p.get("status") == "OPEN"),
        "expired": sum(1 for p in preds if p.get("status") == "EXPIRED"),
        "by_direction": by_direction,
        "by_coin": coin_list,
        "recent": recent,
    }


def build_accuracy_report() -> str:
    s = summarize_accuracy()
    now = datetime.now(timezone(timedelta(hours=REPORT_TZ_OFFSET)))
    lines = [
        "🎯 TỈ LỆ ĐÚNG / SAI",
        f"Chốt sổ lúc {now:%H:%M %d/%m} · nến {BAR} · giữ {BACKTEST_HORIZON} nến",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
    ]
    if s["decided"] == 0:
        lines.append("Chưa có dự đoán nào đủ thời gian để chấm.")
        lines.append(f"⏳ Đang theo dõi: {s['open']} dự đoán.")
        if s["open"] == 0:
            lines.append("")
            lines.append("Mở /signals vài lần để bot ghi lại dự đoán, rồi quay lại sau vài giờ.")
        return "\n".join(lines)

    wr = s["win_rate"]
    flat_s = f"   ⚪ Hòa: {s['flat']}" if s["flat"] else ""
    lines.extend([
        f"✅ Đúng: {s['wins']}   ❌ Sai: {s['losses']}{flat_s}",
        f"🏆 Tỉ lệ đúng: {confidence_bar(wr)} {wr:.1f}%  ({s['decided']} lệnh đã chấm)",
        f"📈 Lợi nhuận TB/lệnh: {s['avg_return']:+.2f}%",
        f"⏳ Đang theo dõi: {s['open']}" + (f" · ⌛ Hết hạn: {s['expired']}" if s["expired"] else ""),
        "",
        "Theo hướng:",
    ])
    for side in ("LONG", "SHORT"):
        d = s["by_direction"][side]
        emoji = "🟢" if side == "LONG" else "🔴"
        if d["wins"] + d["losses"] == 0:
            lines.append(f"  {emoji} {side}: chưa có mẫu đã chấm")
        else:
            lines.append(
                f"  {emoji} {side}: {d['wins']}✅/{d['losses']}❌ · WR {d['win_rate']:.0f}%"
            )

    coins = s["by_coin"][:6]
    if coins:
        lines.append("")
        lines.append("Theo coin (nhiều mẫu nhất):")
        for c in coins:
            lines.append(
                f"  {c['inst_id']}: {c['wins']}✅/{c['losses']}❌ · "
                f"WR {c['win_rate']:.0f}% · avg {c['avg_return']:+.2f}%"
            )

    if s["recent"]:
        lines.append("")
        lines.append("Gần đây nhất:")
        for r in s["recent"]:
            mark = {"WIN": "✅", "LOSS": "❌"}.get(r["status"], "⚪")
            side_e = "🟢L" if r["direction"] == "LONG" else "🔴S"
            ret = r.get("result_return_pct")
            ret_s = f"{ret:+.2f}%" if ret is not None else "-"
            lines.append(f"  {mark} {side_e} {r['inst_id']} {ret_s}")

    lines.append("")
    lines.append("Ghi chú: 'đúng' = lệnh có lãi khi chốt theo TP/SL/horizon đã đề xuất.")
    return "\n".join(lines)


def accuracy_report() -> str:
    """Chấm lại các dự đoán cũ rồi trả về báo cáo tỉ lệ (dùng cho gateway/CLI)."""
    try:
        evaluate_open_predictions()
    except Exception as exc:
        print(f"evaluate_open_predictions failed: {exc}", file=sys.stderr)
    return build_accuracy_report()


def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    ok = True
    for chunk in chunk_text(text):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": html.unescape(chunk),
            "disable_web_page_preview": True,
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                body = json.loads(response.read().decode("utf-8"))
            ok = ok and bool(body.get("ok"))
        except Exception as exc:
            ok = False
            print(f"Telegram send failed: {exc}", file=sys.stderr)
    return ok


def run_once() -> str:
    try:
        n = evaluate_open_predictions()
        if n:
            print(f"Đã chấm {n} dự đoán.")
    except Exception as exc:
        print(f"[{BOT_NAME}] evaluate predictions failed: {exc}", file=sys.stderr)
    report = generate_report(record=True)
    print(report)
    if not SEND_REPORT:
        print("Chế độ chạy ngầm (OKX_SIGNAL_SEND_REPORT=false): chỉ ghi + chấm, không gửi Telegram.")
    elif send_telegram(report):
        print("Telegram report sent.")
    elif TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        print("Telegram report failed.", file=sys.stderr)
    else:
        print("Telegram disabled: missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
    return report


def _collect_results() -> tuple[list[AnalysisResult], list[str]]:
    instruments = fetch_top_instruments()
    results: list[AnalysisResult] = []
    errors: list[str] = []
    for inst_id in instruments:
        try:
            analysis = analyze_instrument(inst_id)
            if analysis:
                results.append(analysis)
        except Exception as exc:
            errors.append(f"{inst_id}: {exc}")
    results.sort(key=lambda item: item.confidence, reverse=True)
    return results, errors


def generate_report(record: bool = True) -> str:
    results, errors = _collect_results()
    # Ghi lại đúng những tín hiệu được lên sóng (top REPORT_TOP_N) để sau chấm đúng/sai.
    if record and results:
        try:
            record_predictions(results[:REPORT_TOP_N])
        except Exception as exc:
            print(f"record_predictions failed: {exc}", file=sys.stderr)
    return build_report(results, errors)


def sleep_until_next_run() -> None:
    time.sleep(max(60, INTERVAL_SEC))


def acquire_single_instance_lock() -> bool:
    global _LOCK_HANDLE
    _LOCK_HANDLE = LOCK_FILE.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            _LOCK_HANDLE.seek(0)
            msvcrt.locking(_LOCK_HANDLE.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(_LOCK_HANDLE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _LOCK_HANDLE.seek(0)
    _LOCK_HANDLE.truncate()
    _LOCK_HANDLE.write(str(os.getpid()).encode("ascii"))
    _LOCK_HANDLE.flush()
    return True


def main() -> int:
    run_once_only = "--once" in sys.argv or env_bool("OKX_SIGNAL_RUN_ONCE")
    if not run_once_only and not acquire_single_instance_lock():
        print("Another okx_coin_analysis_bot instance is already running.")
        return 2
    while True:
        try:
            run_once()
        except urllib.error.URLError as exc:
            message = f"[{BOT_NAME}] OKX network error: {exc}"
            print(message, file=sys.stderr)
            if SEND_REPORT:
                send_telegram(message)
        except Exception:
            details = traceback.format_exc()
            message = f"[{BOT_NAME}] Bot error:\n{details[-2500:]}"
            print(message, file=sys.stderr)
            if SEND_REPORT:
                send_telegram(message)
        if run_once_only:
            return 0
        sleep_until_next_run()


if __name__ == "__main__":
    raise SystemExit(main())
