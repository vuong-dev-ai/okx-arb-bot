import os
from dotenv import load_dotenv
import okx.Account as Account
import okx.MarketData as MarketData
import okx.Trade as Trade
import okx.PublicData as PublicData

load_dotenv('test.env')

API_KEY    = os.getenv("OKX_API_KEY")
SECRET_KEY = os.getenv("OKX_SECRET_KEY")
PASSPHRASE = os.getenv("OKX_PASSPHRASE")
SIMULATED  = os.getenv("OKX_SIMULATED", "false").lower() == "true"

# "1" = demo/testnet, "0" = live (tiền thật)
FLAG = "1" if SIMULATED else "0"

account_api = Account.AccountAPI(API_KEY, SECRET_KEY, PASSPHRASE, use_server_time=False, flag=FLAG)
market_api  = MarketData.MarketAPI(flag=FLAG)
trade_api   = Trade.TradeAPI(API_KEY, SECRET_KEY, PASSPHRASE, use_server_time=False, flag=FLAG)
public_api  = PublicData.PublicAPI(flag=FLAG)
