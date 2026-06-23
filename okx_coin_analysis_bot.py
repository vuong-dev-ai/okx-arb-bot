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
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


BASE_DIR = Path(__file__).resolve().parent
OKX_BASE_URL = "https://www.okx.com"
LOCK_FILE = BASE_DIR / "okx_coin_analysis_bot.lock"
_LOCK_HANDLE = None


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
TP_ATR = float(os.getenv("OKX_SIGNAL_TP_ATR", "1.2"))
SL_ATR = float(os.getenv("OKX_SIGNAL_SL_ATR", "0.9"))
HTTP_TIMEOUT = float(os.getenv("OKX_SIGNAL_HTTP_TIMEOUT", "12"))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BOT_NAME = os.getenv("OKX_SIGNAL_BOT_NAME", "OKX-SIGNAL").strip()


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


def okx_get(path: str, params: dict[str, object] | None = None) -> dict:
    query = urllib.parse.urlencode(params or {})
    url = f"{OKX_BASE_URL}{path}"
    if query:
        url = f"{url}?{query}"
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


def fetch_top_instruments() -> list[str]:
    payload = okx_get("/api/v5/market/tickers", {"instType": INST_TYPE})
    rows = payload.get("data") or []
    suffix = f"-{QUOTE}-SWAP" if INST_TYPE == "SWAP" else f"-{QUOTE}"

    def volume_key(row: dict) -> float:
        return max(
            to_float(row.get("volCcy24h")),
            to_float(row.get("vol24h")),
            to_float(row.get("volCcyQuote24h")),
        )

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


def build_score(candles: list[Candle]) -> Score | None:
    if len(candles) < 80:
        return None

    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]
    ema20_series = ema(closes, 20)
    ema50_series = ema(closes, 50)
    rsi_series = rsi(closes, 14)
    atr_series = atr(candles, 14)

    close = closes[-1]
    ema20 = latest_number(ema20_series, close)
    ema50 = latest_number(ema50_series, close)
    rsi14 = latest_number(rsi_series, 50.0)
    atr14 = latest_number(atr_series, close * 0.01)
    atr_pct = (atr14 / close) * 100 if close else 0.0
    vol_avg = statistics.fmean(volumes[-21:-1]) if len(volumes) >= 22 else statistics.fmean(volumes)
    volume_ratio = volumes[-1] / vol_avg if vol_avg > 0 else 1.0
    momentum_3 = pct_change(closes[-1], closes[-4])
    momentum_12 = pct_change(closes[-1], closes[-13])
    range_high = max(c.high for c in candles[-50:])
    range_low = min(c.low for c in candles[-50:])
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

    ema20_prev = ema20_series[-6] if len(ema20_series) >= 6 else None
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


def choose_side(score: Score) -> str:
    if score.long_score >= score.short_score + MIN_SCORE_EDGE:
        return "LONG"
    if score.short_score >= score.long_score + MIN_SCORE_EDGE:
        return "SHORT"
    return "NEUTRAL"


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


def backtest_side(candles: list[Candle], side: str) -> BacktestResult:
    returns: list[float] = []
    start = 80
    last_entry = len(candles) - BACKTEST_HORIZON - 1
    if last_entry <= start:
        return BacktestResult(win_rate=0.0, trades=0, avg_return_pct=0.0)

    for entry_index in range(start, last_entry):
        history = candles[: entry_index + 1]
        score = build_score(history)
        if score is None:
            continue
        if choose_side(score) != side:
            continue
        atr_value = score.atr
        returns.append(simulate_trade(candles, entry_index, side, atr_value, BACKTEST_HORIZON))

    if not returns:
        return BacktestResult(win_rate=0.0, trades=0, avg_return_pct=0.0)
    wins = sum(1 for value in returns if value > 0)
    return BacktestResult(
        win_rate=(wins / len(returns)) * 100,
        trades=len(returns),
        avg_return_pct=statistics.fmean(returns),
    )


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
    score = build_score(candles)
    if score is None:
        return None

    long_bt = backtest_side(candles, "LONG")
    short_bt = backtest_side(candles, "SHORT")
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

    return AnalysisResult(
        inst_id=inst_id,
        direction=side,
        strength=strength_label(confidence, selected_bt.trades),
        confidence=confidence,
        price=candles[-1].close,
        long_score=score.long_score,
        short_score=score.short_score,
        long_bt=long_bt,
        short_bt=short_bt,
        reasons=tuple(reasons[:4]),
        risk_note=risk_note,
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
    now = datetime.now(timezone.utc).astimezone()
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
    report = generate_report()
    print(report)
    if send_telegram(report):
        print("Telegram report sent.")
    elif TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        print("Telegram report failed.", file=sys.stderr)
    else:
        print("Telegram disabled: missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
    return report


def generate_report() -> str:
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
            send_telegram(message)
        except Exception:
            details = traceback.format_exc()
            message = f"[{BOT_NAME}] Bot error:\n{details[-2500:]}"
            print(message, file=sys.stderr)
            send_telegram(message)
        if run_once_only:
            return 0
        sleep_until_next_run()


if __name__ == "__main__":
    raise SystemExit(main())
