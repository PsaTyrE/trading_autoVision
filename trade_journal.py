#!/home/pi/latihan/venv/bin/python
"""Append new realized PnL rows to trade_journal.csv (dedup by tranId)."""
import csv, os, sys
sys.path.insert(0, "/home/pi/latihan")
from src.live_trader import BinanceFuturesTrader

CSV = "/home/pi/latihan/trade_journal.csv"
FIELDS = ["time", "symbol", "type", "income", "asset", "tranId"]

t = BinanceFuturesTrader()
inc = t.exchange.fapiPrivateGetIncome({"limit": 200})

seen = set()
if os.path.exists(CSV):
    with open(CSV) as f:
        seen = {r["tranId"] for r in csv.DictReader(f)}

new = []
for i in inc:
    if i["incomeType"] not in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
        continue
    tid = str(i["tranId"])
    if tid in seen:
        continue
    new.append({
        "time": i["time"], "symbol": i["symbol"], "type": i["incomeType"],
        "income": i["income"], "asset": i["asset"], "tranId": tid,
    })

if not new:
    print("no new records")
    sys.exit(0)

new.sort(key=lambda r: int(r["time"]))
write_header = not os.path.exists(CSV)
with open(CSV, "a", newline="") as f:
    w = csv.DictWriter(f, fieldnames=FIELDS)
    if write_header:
        w.writeheader()
    w.writerows(new)
print(f"appended {len(new)} rows")
