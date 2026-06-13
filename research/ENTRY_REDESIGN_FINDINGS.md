# Đại tu ENTRY cho TREND bot — Kết quả nghiên cứu (13/06/2026)

**Mục tiêu:** tìm mô hình entry có cạnh dương BỀN cho bot trend-following (gốc lỗi: entry cũ cạnh âm).
**Phương pháp:** harness `bt_research.py` mô phỏng đúng live, **250 ngày 1H** (5999 nến, ~Oct/2025→Jun/2026, đa regime), 25 coin (loại DOT — phantom price 1441x). Tiêu chí chấp nhận **nghiêm**: ROI dương full + dương CẢ 2 nửa OOS + breadth ≥60% coin. Thiết kế từ workflow 5 agent (mỗi agent tự đo forward-return).

## Kết quả

| Hướng entry | Kết quả tốt nhất (250d, taker 0.05%) | Robust? |
|---|---|---|
| EMA-cross hiện tại | −65% (PF 0.44) | ✗ |
| Breakout (Donchian) | −24% (don55+vol) | ✗ |
| Breakout RETEST (Agent 1) | âm | ✗ |
| Pullback-resume (Agent 2) | âm | ✗ |
| Momentum / filter-stack | âm | ✗ |
| **Mean-reversion buy-dip (Agent 5) + exit trend** | −24% | ✗ |
| **Mean-reversion + exit MR (TP+stop)** | **−4% (PF 0.94, wr 51%)** | ✗ (0/2 nửa) |

**0 cấu hình** (6+ model × universe × chiều × exit) đạt chuẩn robust trên 250 ngày.

## Phát hiện cốt lõi

1. **Tape Oct2025–Jun2026 MEAN-REVERTING** (cả 5 agent đo độc lập đều xác nhận): "mua sức mạnh" (breakout/momentum/EMA-cross) có forward-return ÂM; "mua điều chỉnh giữ nền trong trend" có forward-return dương. Trend-following **âm cấu trúc** ở đây (−65%), không phải xui regime.

2. **Mean-reversion CÓ cạnh nhỏ THẬT** nhưng **≈ đúng cỡ phí taker**:

   | Phí | MR best | MR long-only | MR majors |
   |---|---|---|---|
   | Taker 0.05% | −4.0% | −0.3% | +0.9% (0/2 nửa) |
   | Maker 0.02% | −0.2% | **+1.0% (✓2 nửa)** | +3.9% |
   | Fee = 0 | +2.4% (✓2 nửa) | +1.8% (✓2 nửa) | +5.9% |

   → **Phí là rào cản binding.** Dùng **maker/limit order** thay market order, MR long-only lật sang dương-bền (mỏng).

## Khuyến nghị

- **TREND (trend-following + market order): KHÔNG có cạnh → giữ SHELVED.** Không deploy bất kỳ model nào ở đây.
- **Hướng duy nhất có cạnh đo được = mean-reversion + maker-order execution.** Nhưng đây là DỰ ÁN MỚI: cần (a) engine đặt limit order tại vùng dip + xử lý không-khớp (fill-risk CHƯA mô hình trong backtest), (b) validate thêm. Cạnh mỏng (vài %/250d), không phải máy in tiền.
- Trước khi bỏ công làm MR-maker: cân nhắc liệu có đáng so với tập trung ARB (cơ chế funding có cạnh rõ hơn).

Tái lập: `python bt_research.py cache && python bt_research.py entrygrid`. Models trong `_ENTRY_MODELS`.
