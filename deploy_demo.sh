#!/usr/bin/env bash
# Deploy AN TOÀN bản sửa lớn + fix "market đóng cửa" lên server DEMO.
# Đẩy TOÀN BỘ code 2 bot + telegram_gateway, NHƯNG loại trừ mọi file trạng thái
# (state.json / .env / analytics.db* / bot.log / pnl_log.xlsx / docs data) để
# KHÔNG ghi đè vị thế, key, lịch sử trade trên server.
#
# Cách dùng:
#   ./deploy_demo.sh <user>@134.185.80.111 [REMOTE_DIR]
#   REMOTE_DIR mặc định: /home/ubuntu (thư mục THẬT chứa okx-arb-bot/, okx-trending-bot/)
#
# VD: ./deploy_demo.sh ubuntu@134.185.80.111
set -euo pipefail

TARGET="${1:?Cần: ./deploy_demo.sh <user>@host [remote_dir]}"
REMOTE_DIR="${2:-/home/ubuntu}"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Deploy '$LOCAL_DIR'  ->  $TARGET:$REMOTE_DIR"
echo "    (loại trừ: state.json, .env, *.db*, *.log, pnl_log.xlsx, __pycache__, docs data)"

# 1) Backup trạng thái server TRƯỚC khi đụng vào (an toàn rollback)
echo "==> [1/5] Backup state.json + .env trên server..."
ssh "$TARGET" "cd '$REMOTE_DIR' 2>/dev/null && \
  ts=\$(date +%Y%m%d-%H%M%S) && \
  for f in okx-arb-bot/state.json okx-trending-bot/state.json okx-arb-bot/.env okx-trending-bot/.env; do \
    [ -f \"\$f\" ] && cp \"\$f\" \"\$f.bak-\$ts\" && echo \"   backup \$f.bak-\$ts\"; \
  done; true"

# 2) Tar code (exclude file trạng thái) rồi giải nén trên server
echo "==> [2/5] Đẩy code (tar-over-ssh, exclude trạng thái)..."
tar -C "$LOCAL_DIR" \
    --exclude='*/state.json' \
    --exclude='*/.env' \
    --exclude='*/test.env' \
    --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
    --exclude='*.log' \
    --exclude='*/pnl_log.xlsx' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*/docs/data.json' \
    --exclude='bot-docs/equity_data.json' \
    --exclude='*.bak-*' \
    --exclude='deploy_demo.sh' \
    -czf - okx-arb-bot okx-trending-bot telegram_gateway 2>/dev/null \
  | ssh "$TARGET" "mkdir -p '$REMOTE_DIR' && tar -C '$REMOTE_DIR' -xzf -"
echo "   code đã giải nén."

# 3) Đảm bảo .env có các biến mới (CHỈ THÊM nếu thiếu, không sửa giá trị sẵn có)
echo "==> [3/5] Bổ sung env mới nếu thiếu (giữ nguyên giá trị cũ)..."
ssh "$TARGET" "cd '$REMOTE_DIR' && for d in okx-arb-bot okx-trending-bot; do \
  env=\"\$d/.env\"; [ -f \"\$env\" ] || { echo \"   ⚠ THIẾU \$env — bỏ qua\"; continue; }; \
  grep -q '^AUTO_START_BOT='        \"\$env\" || echo 'AUTO_START_BOT=true'   >> \"\$env\"; \
  done; \
  grep -q '^ARB_CAPITAL_FRACTION='   okx-arb-bot/.env      2>/dev/null || echo 'ARB_CAPITAL_FRACTION=0.5'   >> okx-arb-bot/.env; \
  grep -q '^TREND_CAPITAL_FRACTION=' okx-trending-bot/.env 2>/dev/null || echo 'TREND_CAPITAL_FRACTION=0.5' >> okx-trending-bot/.env; \
  echo '   env OK'"

# 4) Restart service
echo "==> [4/5] Restart systemd..."
ssh "$TARGET" "sudo systemctl restart okx-arb okx-trending && sleep 4 && \
  systemctl is-active okx-arb okx-trending || true"

# 5) Health check
echo "==> [5/5] Health check..."
ssh "$TARGET" "curl -s localhost:5000/api/health; echo; curl -s localhost:5001/api/health; echo" || true

echo "==> XONG. Kiểm tra: state.json KHÔNG bị reset, /api/health = running:true, không alert CRITICAL lạ."
echo "    Nếu cần bật bot thủ công (không bật AUTO_START): ssh $TARGET 'curl -X POST localhost:5000/api/start; curl -X POST localhost:5001/api/start'"
