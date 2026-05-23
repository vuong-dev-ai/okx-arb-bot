import time
import signal
import logging
from datetime import datetime

from strategy import (
    get_funding_rates, get_available_usdt,
    open_position, close_position,
    check_exit_conditions, estimate_pnl,
    MIN_FUNDING_RATE, POSITION_PCT, MIN_USDT,
)
from excel_logger import log_pnl_snapshot, export_json, push_to_github

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

SCAN_INTERVAL    = 300
MONITOR_INTERVAL = 60
MAX_POSITIONS    = 3
EXCEL_INTERVAL   = 8 * 3600   # ghi Excel mỗi 8 giờ (khớp kỳ funding)

positions: list[dict] = []
running = True


def handle_stop(sig, frame):
    global running
    running = False


def _divider(char="─", n=52):
    log.info(char * n)


def _print_opportunities(opps: list):
    log.info("  TOP FUNDING RATES:")
    log.info(f"  {'COIN':<7} {'Rate/8h':>9}  {'%/ngày':>7}  {'APY':>8}  {'Next':>9}")
    for o in opps[:8]:
        mark = "►" if o['funding_rate'] >= MIN_FUNDING_RATE else " "
        daily = o['funding_rate'] * 3 * 100
        log.info(
            f"  {mark} {o['coin']:<6} {o['funding_rate']*100:>8.4f}%"
            f"  {daily:>6.3f}%"
            f"  {o['annualized']:>7.1f}%"
            f"  {o['next_rate']*100:>8.4f}%"
        )


def _print_positions():
    if not positions:
        return
    log.info(f"  VỊ THẾ ({len(positions)}/{MAX_POSITIONS}):")
    for p in positions:
        should_exit, rate = check_exit_conditions(p)
        pnl = estimate_pnl(p)
        rate_str = f"{rate*100:.4f}%" if rate is not None else "  N/A  "
        pnl_str  = f"  PnL {pnl['total_pnl']:+.3f}$  ({pnl['n_payments']}x funding)" if pnl else ""
        flag = "  ⚠ THOÁT" if should_exit else ""
        log.info(f"  · {p['coin']:<6}  rate={rate_str}{pnl_str}{flag}")


def run():
    global running, positions

    signal.signal(signal.SIGINT,  handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    usdt = get_available_usdt()
    _divider("═")
    log.info(f"  OKX FUNDING ARB BOT  —  {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    log.info(f"  Số dư: ${usdt:.2f} USDT  |  Vốn/lệnh: {POSITION_PCT*100:.0f}%  |  Max: {MAX_POSITIONS} vị thế")
    log.info(f"  Ngưỡng vào: {MIN_FUNDING_RATE*100:.4f}%/8h  =  {MIN_FUNDING_RATE*3*100:.3f}%/ngày  (~{MIN_FUNDING_RATE*3*365*100:.0f}% pa)")
    _divider("═")

    last_scan       = 0
    last_excel_log  = 0

    while running:
        now = time.time()

        # ── Monitor vị thế ──────────────────────────────────────────
        if positions:
            _divider()
            _print_positions()

        to_close = [p for p in positions if check_exit_conditions(p)[0]]
        for p in to_close:
            log.info(f"  [{p['coin']}] Funding âm → đóng vị thế...")
            close_position(p)
            positions.remove(p)
            log.info(f"  [{p['coin']}] Đã đóng ✓")

        # ── Scan cơ hội ─────────────────────────────────────────────
        if now - last_scan >= SCAN_INTERVAL and len(positions) < MAX_POSITIONS:
            usdt = get_available_usdt()
            _divider()
            log.info(f"  SCAN  —  Số dư: ${usdt:.2f} USDT  |  {datetime.now().strftime('%H:%M:%S')}")

            opps = get_funding_rates()
            if opps:
                _print_opportunities(opps)
                open_coins = {p['coin'] for p in positions}

                entered = 0
                for opp in opps:
                    if len(positions) >= MAX_POSITIONS:
                        break
                    if opp['coin'] in open_coins:
                        continue
                    if opp['funding_rate'] < MIN_FUNDING_RATE:
                        break

                    usdt   = get_available_usdt()   # refresh sau mỗi lần trade
                    amount = usdt * POSITION_PCT
                    if amount < MIN_USDT:
                        log.warning(f"  Số dư thấp (${usdt:.2f}) — dừng scan")
                        break

                    log.info(f"  [{opp['coin']}] Vào lệnh  ${amount:.2f}  rate={opp['funding_rate']*100:.4f}%/8h ...")
                    pos = open_position(opp, amount)
                    if pos:
                        positions.append(pos)
                        open_coins.add(opp['coin'])
                        log.info(f"  [{opp['coin']}] Mở thành công ✓  contracts={pos['contracts']}  giá=${pos['entry_price']:.2f}")
                        entered += 1

                if entered == 0 and opps[0]['funding_rate'] < MIN_FUNDING_RATE:
                    log.info(f"  Không có cơ hội (rate cao nhất: {opps[0]['funding_rate']*100:.4f}%)")
            else:
                log.warning("  Không lấy được dữ liệu funding rate")

            last_scan = now

        # ── Ghi Excel mỗi 8 giờ ─────────────────────────────────────
        if positions and now - last_excel_log >= EXCEL_INTERVAL:
            usdt_bal  = get_available_usdt()
            pnl_list  = [estimate_pnl(p) for p in positions]
            try:
                log_pnl_snapshot(positions, pnl_list, usdt_bal)
                log.info(f"  [Excel] Đã ghi lời/lỗ → pnl_log.xlsx  ({datetime.now().strftime('%H:%M')})")
                export_json()
                if push_to_github():
                    log.info("  [GitHub] data.json → GitHub Pages ✓")
                else:
                    log.warning("  [GitHub] Push thất bại — kiểm tra git credentials")
            except Exception as e:
                log.warning(f"  [Excel] Lỗi ghi file: {e}")
            last_excel_log = now

        # ── Chờ ─────────────────────────────────────────────────────
        if running:
            next_scan = max(0, int(SCAN_INTERVAL - (time.time() - last_scan)))
            log.info(f"  Chờ {MONITOR_INTERVAL}s  (scan mới sau {next_scan}s  |  Ctrl+C dừng)")
            time.sleep(MONITOR_INTERVAL)

    # ── Shutdown ─────────────────────────────────────────────────────
    _divider("═")
    if positions:
        log.info(f"  Đóng {len(positions)} vị thế...")
        for p in list(positions):
            close_position(p)
            positions.remove(p)
            log.info(f"  [{p['coin']}] Đã đóng ✓")
    log.info("  Bot đã dừng.")
    _divider("═")


if __name__ == "__main__":
    run()
