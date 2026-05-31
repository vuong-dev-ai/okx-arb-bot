# Telegram Gateway

Một process Telegram duy nhất kết nối với cả 2 bot OKX (`okx-arb-bot` :5000, `okx-trending-bot` :5001) qua Flask HTTP API.

## Tại sao cần gateway?

Telegram chỉ cho phép **1 process polling** trên mỗi bot token. Hai bot trading chạy song song không thể cùng listen — gateway giải quyết bằng cách:
- Push notifications: mỗi bot vẫn tự gửi qua `notifier.py` (chỉ POST, không polling) — animated GIF / sticker / dice.
- Pull commands: chỉ gateway polling, forward command tới HTTP API của bot tương ứng.

## Setup

```powershell
cd D:\PYTHON\telegram_gateway
pip install -r requirements.txt
```

Tạo `.env` (hoặc dùng chung `.env` của 1 trong 2 bot — gateway tự đọc):

```env
TELEGRAM_TOKEN=123456:ABC...
TELEGRAM_ALLOWED_CHAT_IDS=11111111,22222222
# Optional override
GATEWAY_ARB_URL=http://localhost:5000
GATEWAY_TREND_URL=http://localhost:5001
GATEWAY_DAILY_HOUR=22
GATEWAY_ALERT_INTERVAL=300
GATEWAY_DD_THRESHOLD=-5
GATEWAY_PROFIT_THRESHOLD=10
```

**Lấy chat_id**: gửi tin nhắn cho [@userinfobot](https://t.me/userinfobot).

## Chạy

```powershell
python gateway.py
```

3 process chạy song song:
```powershell
# Cửa sổ 1
cd D:\PYTHON\okx-arb-bot && python app.py

# Cửa sổ 2
cd D:\PYTHON\okx-trending-bot && python app.py

# Cửa sổ 3
cd D:\PYTHON\telegram_gateway && python gateway.py
```

## Lệnh

| Lệnh | Mô tả |
|---|---|
| `/start`, `/help` | Giới thiệu, danh sách lệnh |
| `/menu` | Bảng nút inline |
| `/ping` | Kiểm tra gateway + 2 bot |
| `/status [trend\|arb\|all]` | Tóm tắt bot + vị thế |
| `/positions [trend\|arb]` | Chi tiết vị thế |
| `/balance` | USDT khả dụng |
| `/stats [trend\|arb]` | Win rate, profit factor, top coins |
| `/equity [trend\|arb]` | Biểu đồ equity (PNG qua quickchart.io) |
| `/daily` | Báo cáo PnL hôm nay |
| `/summary` | Báo cáo 7 ngày |
| `/start_trend`, `/stop_trend` | Điều khiển trending bot |
| `/start_arb`, `/stop_arb` | Điều khiển arb bot |
| `/close <bot> <coin>` | VD `/close trend BTC` |
| `/close_all <bot>` | Đóng tất cả vị thế của bot đó |
| `/sync <bot>` | Reconcile với OKX |

## Scheduler

- **Daily summary**: 22:00 giờ VN (cấu hình `GATEWAY_DAILY_HOUR`).
- **Conditional alerts**: poll mỗi 5 phút (`GATEWAY_ALERT_INTERVAL`):
  - Drawdown alert khi `pct ≤ GATEWAY_DD_THRESHOLD` (mặc định -5%).
  - Profit milestone khi `pct ≥ GATEWAY_PROFIT_THRESHOLD` (mặc định +10%) — kèm animated dice 🎰.
  - Mỗi (bot, coin, type) chỉ alert 1 lần; reset khi vị thế đóng hoặc rời threshold.

## Animated icons

Push notifications (từ `notifier.py` của mỗi bot, không qua gateway):

| Event | Mặc định |
|---|---|
| OPEN_LONG / OPEN_SHORT | 🎯 dice |
| CLOSE_WIN | 🎰 dice |
| CLOSE_LOSS | 🎳 dice |
| STOP_HIT | 🎲 dice |
| PARTIAL_TP | 🎯 dice |
| BOT_CRASH | 🎳 dice |

Override mỗi event bằng env var:
```env
ICON_OPEN_LONG_GIF=https://media.tenor.com/.../rocket.gif
ICON_CLOSE_WIN_STICKER=CAACAgIAAxkBA...   # file_id của sticker animated
```

Lấy `file_id` của sticker: gửi sticker đó cho [@RawDataBot](https://t.me/RawDataBot), copy field `sticker.file_id`.

## Bảo mật

- Chỉ `TELEGRAM_ALLOWED_CHAT_IDS` mới gọi được command — mọi handler có auth decorator.
- Mọi chat_id ngoài whitelist nhận phản hồi "⛔ không có quyền".
- Gateway chỉ gọi HTTP localhost của 2 bot — không expose ra ngoài. Nếu chạy trên server, đóng port 5000/5001 trên firewall.

## Troubleshooting

- **"Conflict: terminated by other getUpdates"**: bạn đang chạy >1 process cùng token. Kill các process cũ.
- **GIF/animation không hiện**: URL phải trỏ thẳng tới file `.gif` công khai (không phải landing page). Test bằng cách paste URL vào trình duyệt — phải tự load GIF.
- **`/equity` không gửi được ảnh**: quickchart.io free tier giới hạn rate. Self-host nếu cần.
