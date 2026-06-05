"""
Dọn sạch tài khoản OKX để bắt đầu validate từ trạng thái trắng.

Thứ tự an toàn:
  1. Hủy MỌI algo order đang treo (stop/conditional…) — tránh fire giữa chừng.
  2. Đóng MỌI vị thế SWAP (reduceOnly market, đúng mgnMode của từng vị thế).
  3. Bán MỌI spot non-USDT (dùng availBal + buffer 0.05% + retry khi 51008).
  4. Reset state.json của cả 2 bot (chỉ khi --execute) để restart không khôi phục vị thế cũ.

CHẠY KHI 2 BOT ĐÃ TẮT.
  Xem trước (không đặt lệnh):   python cleanup_demo.py
  Thực thi:                     python cleanup_demo.py --execute
"""
import os
import sys
import json
import time
import logging

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("cleanup")

from config import account_api, trade_api, SIMULATED

EXECUTE   = "--execute" in sys.argv
DUST_USD  = 1.0          # bỏ qua spot có giá trị < $1 (không đáng / dưới min order)
_BASE     = os.path.dirname(os.path.abspath(__file__))
STATE_FILES = [
    os.path.join(_BASE, "state.json"),                                   # arb
    os.path.join(os.path.dirname(_BASE), "okx-trending-bot", "state.json"),  # trend
]


def _safe(fn, what=""):
    try:
        return fn()
    except Exception as e:
        log.warning(f"  ! {what}: {e}")
        return None


def cancel_all_algos():
    log.info("\n=== 1) Hủy algo order đang treo ===")
    found = []
    for ot in ("conditional", "trigger", "oco", "move_order_stop"):
        r = _safe(lambda ot=ot: trade_api.order_algos_list(ordType=ot), f"list {ot}")
        for a in ((r or {}).get("data") or []):
            if a.get("algoId"):
                found.append({"algoId": a["algoId"], "instId": a.get("instId", "")})
    if not found:
        log.info("  (không có algo nào treo)")
        return
    for a in found:
        log.info(f"  hủy algo {a['algoId']}  {a['instId']}")
    if EXECUTE:
        # cancel_algo_order nhận tối đa 10/lần
        for i in range(0, len(found), 10):
            r = _safe(lambda b=found[i:i + 10]: trade_api.cancel_algo_order(b), "cancel batch")
            log.info(f"  → batch {i//10+1}: code={(r or {}).get('code')}")
            time.sleep(0.2)


def close_all_swaps():
    log.info("\n=== 2) Đóng vị thế SWAP ===")
    r = _safe(lambda: account_api.get_positions(instType="SWAP"), "get_positions")
    rows = [p for p in ((r or {}).get("data") or []) if float(p.get("pos") or 0) != 0]
    if not rows:
        log.info("  (không có vị thế swap)")
        return
    for p in rows:
        pos   = float(p["pos"])
        sz    = abs(pos)
        side  = "buy" if pos < 0 else "sell"          # đóng ngược chiều
        inst  = p["instId"]
        mode  = p.get("mgnMode") or "isolated"        # ĐÚNG mgnMode, không hardcode
        log.info(f"  {inst:<18} pos={pos:>10}  → {side} {sz} ({mode})  upl={p.get('upl')}")
        if EXECUTE:
            r2 = _safe(lambda: trade_api.place_order(
                instId=inst, tdMode=mode, side=side, ordType="market",
                sz=str(sz), reduceOnly="true",
            ), f"close {inst}")
            d = ((r2 or {}).get("data") or [{}])[0]
            log.info(f"    → code={(r2 or {}).get('code')} sCode={d.get('sCode')} {d.get('sMsg') or ''}")
            time.sleep(0.3)


def _avail(ccy):
    r = _safe(lambda: account_api.get_account_balance(ccy=ccy), f"bal {ccy}")
    for d in (((r or {}).get("data") or [{}])[0].get("details") or []):
        if d["ccy"] == ccy:
            return float(d.get("availBal") or 0)
    return 0.0


def sell_all_spot():
    log.info("\n=== 3) Bán spot non-USDT ===")
    r = _safe(lambda: account_api.get_account_balance(), "balance")
    det = ((r or {}).get("data") or [{}])[0].get("details") or []
    targets = []
    for d in det:
        ccy = d["ccy"]
        if ccy == "USDT":
            continue
        avail = float(d.get("availBal") or 0)
        eq_usd = float(d.get("eqUsd") or 0)
        if avail > 0 and eq_usd >= DUST_USD:
            targets.append((ccy, avail, eq_usd))
    if not targets:
        log.info("  (không có spot nào đáng bán)")
        return
    for ccy, avail, eq_usd in targets:
        log.info(f"  {ccy:<8} avail={avail:<18} (~${eq_usd:,.2f})  → bán {ccy}-USDT")
        if not EXECUTE:
            continue
        inst = f"{ccy}-USDT"
        ok = False
        for attempt in range(3):
            amt = round((avail if attempt == 0 else _avail(ccy)) * 0.9995, 8)
            if amt <= 0:
                ok = True
                break
            r2 = _safe(lambda: trade_api.place_order(
                instId=inst, tdMode="cash", side="sell", ordType="market",
                sz=str(amt),
            ), f"sell {inst}")
            d = ((r2 or {}).get("data") or [{}])[0]
            code = (r2 or {}).get("code")
            log.info(f"    lần {attempt+1}: code={code} sCode={d.get('sCode')} {d.get('sMsg') or ''}")
            if code == "0":
                ok = True
                break
            time.sleep(0.6)
        if not ok:
            log.warning(f"    ! {ccy} chưa bán được hết — kiểm tra thủ công")


def reset_states():
    log.info("\n=== 4) Reset state.json ===")
    for f in STATE_FILES:
        log.info(f"  {f}  → positions: []")
        if EXECUTE:
            try:
                with open(f, "w", encoding="utf-8") as fh:
                    json.dump({"positions": [], "ts": time.time()}, fh, ensure_ascii=False)
            except Exception as e:
                log.warning(f"    ! ghi {f}: {e}")


if __name__ == "__main__":
    mode = "THỰC THI (--execute)" if EXECUTE else "XEM TRƯỚC (dry-run)"
    acct = "DEMO" if SIMULATED else "LIVE ⚠⚠⚠"
    log.info("=" * 60)
    log.info(f"  CLEANUP TÀI KHOẢN {acct}  —  {mode}")
    log.info("=" * 60)
    if not SIMULATED and EXECUTE:
        log.error("\n⛔ Tài khoản LIVE — từ chối tự đóng. Bỏ chặn thủ công nếu thực sự muốn.")
        sys.exit(1)
    cancel_all_algos()
    close_all_swaps()
    if EXECUTE:
        time.sleep(1.0)   # cho swap settle trước khi bán spot
    sell_all_spot()
    reset_states()
    log.info("\n" + "=" * 60)
    log.info("  Hoàn tất." + ("" if EXECUTE else "  (chưa đặt lệnh nào — thêm --execute để chạy thật)"))
    log.info("=" * 60)
