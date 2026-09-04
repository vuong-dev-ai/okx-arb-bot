# Telegram Gateway

Một process Telegram duy nhất kết nối với arb bot OKX (`okx-arb-bot` :5000) qua Flask HTTP API.

## Tại sao cần gateway?

Telegram chỉ cho phép **1 process polling** trên mỗi bot token. Bot trading dùng `notifier.py` để push (chỉ POST) nên không thể đồng thời tự polling lệnh — gateway giải quyết bằng cách:
- Push notifications: bot vẫn tự gửi qua `notifier.py` (chỉ POST, không polling) — animated GIF / sticker / dice.
- Pull commands: chỉ gateway polling, forward command tới HTTP API của bot.

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

2 process chạy song song:
```powershell
# Cửa sổ 1
cd D:\PYTHON\okx-arb-bot && python app.py

# Cửa sổ 2
cd D:\PYTHON\telegram_gateway && python gateway.py
```

## Lệnh

| Lệnh | Mô tả |
|---|---|
| `/menu` | Bảng lệnh được phép dùng |
| `/web` | Cấp tài khoản/mật khẩu web tạm thời |
| `/help` | Danh sách lệnh khả dụng |
| `/dsquyen` | Xem danh sách quyền |
| `/capquyen <id>` | Cấp quyền xem |
| `/thuquyen <id>` | Thu quyền xem |

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

- Chủ bot nằm trong `TELEGRAM_ALLOWED_CHAT_IDS`; người chỉ xem được lưu trong `viewers.json` hoặc `TELEGRAM_VIEWER_CHAT_IDS`.
- Lệnh quản lý quyền chỉ chủ bot dùng được; các chat_id ngoài danh sách quyền nhận phản hồi "⛔ không có quyền".
- Gateway chỉ gọi HTTP localhost của bot — không expose ra ngoài. Nếu chạy trên server, đóng port 5000 trên firewall.

## Troubleshooting

- **"Conflict: terminated by other getUpdates"**: bạn đang chạy >1 process cùng token. Kill các process cũ.
- **GIF/animation không hiện**: URL phải trỏ thẳng tới file `.gif` công khai (không phải landing page). Test bằng cách paste URL vào trình duyệt — phải tự load GIF.
