# BẢN SỬA LỚN — OKX bots (06/2026)

Mục tiêu: chạy ~1 tháng KHÔNG đụng tay, lợi nhuận cao nhất với rủi ro vừa phải.
Dựa trên audit đa tác nhân (57 vấn đề được phản biện xác nhận) + 4 quyết định của bạn:
**đại tu logic TREND · giữ chung tài khoản + khóa chống tranh chấp · validate DEMO trước LIVE · chỉ alert CRITICAL.**

---

## 1. NHỮNG GÌ ĐÃ SỬA

### P0 — Chặn đứng mất tiền (execution safety)
- **ARB không còn bỏ rơi spot khi đóng:** `_close_one` / `_reconcile_remove` CHỈ xóa vị thế local khi
  **futures đã hết VÀ spot đã bán sạch**. Nếu spot chưa bán được → giữ local + cờ `_pending_spot_cleanup`
  + **alert CRITICAL** + tự retry (qua monitor & reconcile). Đây là gốc của unhedged/51008 (vụ ATOM).
- **ARB mở lệnh xác nhận khớp thật:** tăng retry đọc `accFillSz` (8×0.5s); nếu không xác nhận được spot →
  **abort, KHÔNG short** (rollback bán lại) thay vì dùng số ước lượng → hết lệch hedge.
- **ARB hedge khít hơn:** bỏ buffer kép `×0.9995`; lượng coin = số dư thực sau settle; `sell_spot` cap theo
  available. `sell_spot` khi API đọc số dư fail → trả False (không bán mù).
- **TREND partial-close xác nhận khớp** trước khi trừ `contracts`; nếu sửa size stop fail → alert (chống lệch 51169).

### P1 — Ổn định để chạy 1 tháng
- **Đua luồng (cả 2 bot):** vòng monitor giờ tính toán/network NGOÀI lock rồi **mutate vị thế TRONG lock**
  (chống race với persist/SSE/close). EMA-reverse & partial-TP mutate dưới lock. (Không giữ lock khi gọi mạng.)
- **Khóa liên-bot (`cross_bot_lock.py`):** vì 2 bot chung 1 tài khoản — mọi lần ĐẶT LỆNH (mở) được nối tiếp
  qua file-lock liên-tiến-trình (tự phá khóa mồ côi nếu process chết). Hết tranh margin/spot khi 2 bot cùng mở.
- **Capital fraction:** mỗi bot chỉ triển khai tối đa `CAPITAL_FRACTION × equity` (mặc định 0.5 + 0.5) →
  không còn cảnh cả 2 bot tưởng mình sở hữu 100% số dư → over-leverage.
- **Coin-ownership:** bot bỏ qua coin đã có vị thế trên OKX của bot kia → tránh NET vào nhau / reduceOnly đóng nhầm.
- **Never-die:** `_bot_safe` retry KHÔNG giới hạn với exp backoff + **alert CRITICAL mỗi lần crash** (reset khi
  chạy ổn >5'). Bỏ giới hạn "3 lần rồi chết im".
- **Watchdog khởi động lại (TREND):** ngoài đóng vị thế quá-stop, còn **đặt lại native stop nếu thiếu** (gốc vụ
  BNB −525$); nếu đặt lại fail → alert. Mở lệnh mà không đặt được stop → alert.
- **Mất giá / WS chết:** native stop trên sàn vẫn bảo vệ → KHÔNG force-close mù; chỉ **alert** nếu mất giá >60s
  hoặc WS im lặng >5'.
- **Reconcile nhanh hơn** (150s→45s) để phát hiện stop/liquidation/unhedged sớm.
- **WAL checkpoint** mỗi giờ (chống `analytics.db-wal` phình); alert nếu vẫn >50MB.
- **Auto-start** tùy chọn (`AUTO_START_BOT=true`) để systemd `Restart=always` tự hồi phục hoàn toàn.

### Đại tu CHIẾN LƯỢC TREND (đa khung + regime)
Triết lý mới: **1H chỉ là TIMING**, chỉ vào khi 3 tầng đồng thuận:
1. **Regime 4H của coin:** EMA21/55 trên 4H cùng chiều VÀ 4H đang trending (`HTF_ADX_MIN=22`) → loại chop.
2. **Regime thị trường (BTC 4H):** không LONG alt khi BTC giảm, không SHORT alt khi BTC tăng → cắt rủi ro tương quan.
3. **Trigger 1H:** cú cắt/continuation EMA (như cũ) — chỉ là điểm vào trong trend đã xác lập.
- Mở rộng `CORRELATED_GROUPS` (phủ thêm cụm beta-BTC: APT/SUI/SEI, LINK/AAVE/LDO/INJ, DOT/ATOM/TIA…).
- `MAX_POS 6→5` (sau lọc, tín hiệu ít & chất hơn). Partial-TP tắt rõ ràng bằng `ENABLE_PARTIAL_TP=False`.
- **Backtest (600 nến 1H ≈ 25 ngày, 29 coin):** n=81 · win 35.8% · ROI +7.5% · PF 1.3 · expectancy +9.26$/lệnh.
  → Lật từ ÂM (−16%/50d cũ) sang DƯƠNG. ⚠️ `maxDD −34%`, mẫu 1 cửa sổ → **BẮT BUỘC validate DEMO**.

---

## 2. BIẾN MÔI TRƯỜNG MỚI (.env của MỖI bot)
```
# Chống tranh chấp khi chung tài khoản (arb + trend nên cộng ≤ 1.0)
ARB_CAPITAL_FRACTION=0.5      # đặt trong .env của okx-arb-bot
TREND_CAPITAL_FRACTION=0.5    # đặt trong .env của okx-trending-bot
# (tùy chọn) đường file khóa liên-bot — mặc định = okx-bot/.okx_trade.lock (CẢ 2 bot phải trỏ CÙNG file)
# OKX_LOCK_PATH=/duong/dan/chung/.okx_trade.lock
# Alert CRITICAL qua Telegram — đã có sẵn TELEGRAM_TOKEN / TELEGRAM_CHAT_ID / BOT_NAME
# Tự bật bot khi service khởi động (khuyến nghị cho systemd Restart=always)
AUTO_START_BOT=true
```
> Lưu ý: nếu chạy 2 bot trên 2 MÁY khác nhau thì file-lock KHÔNG dùng được — lúc đó mới thực sự cần tách
> sub-account. Hiện 2 bot cùng 1 máy (server Oracle) nên file-lock là đủ.

---

## 3. TRIỂN KHAI (DEMO trước)
1. `scp` toàn bộ 2 thư mục bot + `cross_bot_lock.py` lên server Oracle.
2. Bổ sung biến môi trường mới vào `.env` mỗi bot (mục 2). GIỮ `OKX_SIMULATED=true` (DEMO).
3. systemd unit nên có `Restart=always` (+ `AUTO_START_BOT=true` để khỏi cần `curl /api/start`).
4. `systemctl restart okx-arb-bot okx-trending-bot` (rồi `curl -X POST :5000/api/start` & `:5001/api/start`
   nếu KHÔNG bật AUTO_START_BOT).
5. Kiểm tra `/api/health` cả 2 cổng = `running:true`.

## 4. CHECKLIST VALIDATE DEMO (1–2 tuần) trước khi LIVE
- [ ] Không còn log `unhedged` / `51008` lặp; không có alert CRITICAL về unhedged-spot.
- [ ] ARB: số lệnh `funding_flip`/`external_close` đa số **net ≥ 0** (không còn bleed phí).
- [ ] TREND: equity curve đi lên, **maxDD thực tế chấp nhận được** (nếu DD > ~20% → hạ `MAX_POS` 5→3
      hoặc `RISK_PCT` 0.75%→0.5%).
- [ ] Thử kill 1 process → systemd tự dựng lại + bot tự `running` (AUTO_START) + watchdog đặt lại stop.
- [ ] WAL file ổn định (<50MB), bot.log xoay vòng bình thường, không crash-loop.

## 5. CHUYỂN LIVE (khi DEMO đạt)
- [ ] Đặt `OKX_SIMULATED=false` **và** `ALLOW_LIVE=I_UNDERSTAND` (cả 2 bắt buộc) trên .env mỗi bot.
- [ ] Vốn nhỏ ban đầu; theo dõi alert CRITICAL 2–3 ngày đầu.
- [ ] TREND nếu DD lớn → giảm `MAX_POS`/`RISK_PCT` như trên.

## 6. ALERT CRITICAL sẽ nhận (Telegram)
unhedged-spot · bot crash (mỗi lần) · WS chết >5' khi có vị thế · mất giá >60s · mở lệnh không đặt được stop ·
khôi phục vị thế thiếu stop & đặt lại fail · partial-close lệch size stop · WAL phình >50MB.
(Đều có dedup chống spam.)

---

## 7. THẲNG THẮN (góc nhìn trader)
- **ARB** là nguồn lãi thật, giờ đã sạch execution — biên vẫn mỏng, lãi đến từ KỶ LUẬT phí (giữ `MIN_FUNDING_RATE`
  đủ cao). Đây là cỗ máy "đặt đúng & để yên" hợp mục tiêu 1 tháng.
- **TREND** đã được đại tu để có EV dương trong backtest, NHƯNG một crossover-trend vẫn là chiến lược DD sâu;
  con số +7.5%/25d là 1 cửa sổ, chưa phải chân lý. Hãy để DEMO phán xử; đừng tăng vốn trước khi nó tự chứng minh.
- **Tách sub-account** vẫn là nâng cấp đáng làm về sau (khi muốn chạy 2 máy, hoặc tách rủi ro hoàn toàn) — file-lock
  hiện tại chỉ giải quyết tranh chấp KHI CÙNG MỘT MÁY.
