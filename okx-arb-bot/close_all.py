"""Đóng tất cả vị thế futures và bán spot còn lại."""
import logging, time
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

from config import account_api, trade_api

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
        r4 = trade_api.place_order(
            instId=f"{ccy}-USDT", tdMode="cash",
            side="sell", ordType="market", sz=str(round(sz, 8)),
        )
        code   = r4.get("code")
        detail = (r4.get("data") or [{}])[0]
        if code == "0":
            log.info(f"  OK ✓")
        else:
            log.error(f"  Lỗi [{detail.get('sCode')}]: {detail.get('sMsg')}")
        time.sleep(0.3)
else:
    log.info("Không có spot nào cần bán.")

log.info("\nHoàn tất. Chạy bot.py để bắt đầu lại.")
