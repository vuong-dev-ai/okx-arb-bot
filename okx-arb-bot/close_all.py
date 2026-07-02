"""Đóng tất cả vị thế futures và bán spot còn lại."""
import logging, time
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

from config import account_api, trade_api
from strategy import sell_spot   # FIX (audit 07/2026): dùng sell_spot đã vá lotSz/minSz/dust

# ── 1. Đóng futures ──────────────────────────────────────────────────────────
r = account_api.get_positions(instType="SWAP")
swaps = [p for p in r.get("data", []) if float(p.get("pos") or 0) != 0]

if swaps:
    log.info(f"Tìm thấy {len(swaps)} vị thế futures:")
    for p in swaps:
        sz    = abs(float(p["pos"]))
        side  = "buy" if float(p["pos"]) < 0 else "sell"   # đóng ngược chiều
        inst  = p["instId"]
        log.info(f"  Đóng {inst}  sz={sz}  pnl={p['upl']}")
        r2 = trade_api.place_order(
            instId=inst, tdMode="isolated",
            side=side, ordType="market",
            sz=str(sz), reduceOnly="true",
        )
        code   = r2.get("code")
        detail = (r2.get("data") or [{}])[0]
        if code == "0":
            log.info(f"  OK ✓")
        else:
            log.error(f"  Lỗi [{detail.get('sCode')}]: {detail.get('sMsg')}")
        time.sleep(0.3)

    # FIX (audit 07/2026): VERIFY futures đã thực sự hết — không tin mù code trả về.
    time.sleep(1.0)
    r_chk = account_api.get_positions(instType="SWAP")
    still = [p["instId"] for p in r_chk.get("data", []) if abs(float(p.get("pos") or 0)) > 1e-12]
    if still:
        log.error(f"  ⚠ VẪN CÒN futures chưa đóng: {still} — KIỂM TRA THỦ CÔNG trước khi bán spot!")
    else:
        log.info("  Xác nhận: đã hết vị thế futures ✓")
else:
    log.info("Không có vị thế futures nào.")

# ── 2. Bán spot còn lại ──────────────────────────────────────────────────────
time.sleep(0.5)
r3 = account_api.get_account_balance()
spots = [
    d for d in r3["data"][0].get("details", [])
    if d["ccy"] != "USDT" and float(d.get("availEq") or 0) > 0.00001
]

if spots:
    log.info(f"\nTìm thấy {len(spots)} coin spot:")
    for d in spots:
        ccy = d["ccy"]
        sz  = float(d["availEq"])
        log.info(f"  Bán {ccy}  sz={sz}")
        # sell_spot tự đọc available, làm tròn lotSz, bỏ qua dust <minSz → tránh 51020/51008
        if sell_spot(f"{ccy}-USDT", sz):
            log.info(f"  OK ✓")
        else:
            log.error(f"  Bán {ccy} CHƯA được — kiểm tra thủ công (có thể để lại spot trần)")
        time.sleep(0.3)
else:
    log.info("Không có spot nào cần bán.")

log.info("\nHoàn tất. Chạy app.py (web dashboard + bot) để bắt đầu lại.")
