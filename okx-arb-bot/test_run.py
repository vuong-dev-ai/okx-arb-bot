import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from strategy import get_funding_rates, get_available_usdt, MIN_FUNDING_RATE

usdt = get_available_usdt()
print(f"\nSo du: ${usdt:.2f} USDT\n")

opps = get_funding_rates()
print(f"Lay duoc {len(opps)} funding rates\n")
print(f"  {'COIN':<7} {'Rate/8h':>9}  {'APY':>8}  {'Next':>9}")
print("  " + "-"*42)
for o in opps[:10]:
    mark = ">" if o["funding_rate"] >= MIN_FUNDING_RATE else " "
    print(
        f"  {mark} {o['coin']:<6} {o['funding_rate']*100:>8.4f}%"
        f"  {o['annualized']:>7.1f}%"
        f"  {o['next_rate']*100:>8.4f}%"
    )

print()
amount = usdt * 0.3
print(f"Von/lenh (30%): ${amount:.2f} USDT")
if amount < 15:
    print("CANH BAO: Von qua thap de trade (< $15)")
