"""CHẨN ĐOÁN READ-ONLY — không đặt/hủy lệnh nào. Xem vị thế kẹt + trạng thái instrument."""
from config import account_api, public_api, trade_api, SIMULATED

print(f"\n=== MODE: {'DEMO' if SIMULATED else 'LIVE'} ===\n")

# 1) Vị thế SWAP đang mở
r = account_api.get_positions(instType="SWAP")
poss = [p for p in (r.get("data") or []) if float(p.get("pos") or 0) != 0]
print(f"--- {len(poss)} vị thế SWAP đang mở ---")
for p in poss:
    inst = p["instId"]
    # trạng thái instrument
    ri = public_api.get_instruments(instType="SWAP", instId=inst)
    di = (ri.get("data") or [{}])[0]
    state = di.get("state")
    print(f"  {inst:22} pos={p['pos']:>10} upl={p.get('upl'):>10} avgPx={p.get('avgPx')}")
    print(f"      instrument.state = {state!r}  (live=giao dịch được, suspend/preopen/expired=KHÔNG)")

# 2) Lệnh đang chờ (pending) — gồm cả lệnh đóng bị treo
ro = trade_api.get_order_list(instType="SWAP")
orders = ro.get("data") or []
print(f"\n--- {len(orders)} lệnh SWAP đang pending ---")
for o in orders:
    print(f"  {o.get('instId'):22} side={o.get('side')} ordType={o.get('ordType')} "
          f"sz={o.get('sz')} state={o.get('state')} px={o.get('px')}")

# 3) Lệnh algo (stop) đang chờ
try:
    ra = trade_api.order_algos_list(ordType="conditional", instType="SWAP")
    algos = ra.get("data") or []
    print(f"\n--- {len(algos)} stop algo đang treo ---")
    for a in algos:
        print(f"  {a.get('instId'):22} state={a.get('state')} slTriggerPx={a.get('slTriggerPx')} algoId={a.get('algoId')}")
except Exception as e:
    print(f"  (không đọc được algo list: {e})")

# 4) Spot balance != USDT
rb = account_api.get_account_balance()
dets = (rb.get("data") or [{}])[0].get("details", [])
spots = [d for d in dets if d["ccy"] != "USDT" and float(d.get("availEq") or d.get("eq") or 0) > 1e-8]
print(f"\n--- {len(spots)} coin spot còn lại ---")
for d in spots:
    print(f"  {d['ccy']:8} availBal={d.get('availBal')} eq={d.get('eq')}")
print()
