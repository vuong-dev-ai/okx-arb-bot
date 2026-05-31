"""OKX API config — load from `.env` (priority) or `test.env` (fallback), validate at import."""
import os
import sys
import logging
from dotenv import load_dotenv

import okx.Account as Account
import okx.MarketData as MarketData
import okx.Trade as Trade
import okx.PublicData as PublicData

log = logging.getLogger(__name__)

# Ưu tiên .env (prod), fallback test.env (dev) — không override biến môi trường có sẵn
_BASE = os.path.dirname(os.path.abspath(__file__))
for fname in ('.env', 'test.env'):
    p = os.path.join(_BASE, fname)
    if os.path.exists(p):
        load_dotenv(p, override=False)

API_KEY    = os.getenv("OKX_API_KEY", "").strip()
SECRET_KEY = os.getenv("OKX_SECRET_KEY", "").strip()
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "").strip()
SIMULATED  = os.getenv("OKX_SIMULATED", "false").lower() == "true"

# "1" = demo/testnet, "0" = live (tiền thật)
FLAG = "1" if SIMULATED else "0"


def _validate():
    missing = [name for name, val in
               (("OKX_API_KEY", API_KEY),
                ("OKX_SECRET_KEY", SECRET_KEY),
                ("OKX_PASSPHRASE", PASSPHRASE))
               if not val]
    if missing:
        msg = ("\n" + "="*60 +
               f"\n  ❌ Thiếu API key: {', '.join(missing)}\n"
               f"  Hãy tạo file .env trong {_BASE}\n"
               f"  Mẫu:\n"
               f"    OKX_API_KEY=your_key\n"
               f"    OKX_SECRET_KEY=your_secret\n"
               f"    OKX_PASSPHRASE=your_passphrase\n"
               f"    OKX_SIMULATED=false\n" +
               "="*60 + "\n")
        sys.stderr.write(msg)
        sys.exit(1)


_validate()

account_api = Account.AccountAPI(API_KEY, SECRET_KEY, PASSPHRASE, use_server_time=False, flag=FLAG)
market_api  = MarketData.MarketAPI(flag=FLAG)
trade_api   = Trade.TradeAPI(API_KEY, SECRET_KEY, PASSPHRASE, use_server_time=False, flag=FLAG)
public_api  = PublicData.PublicAPI(flag=FLAG)

log.info(f"OKX config loaded — mode: {'DEMO' if SIMULATED else 'LIVE'}, key=…{API_KEY[-4:]}")
