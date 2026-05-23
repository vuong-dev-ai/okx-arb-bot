import math
import time
import logging
from config import public_api, market_api, account_api, trade_api

log = logging.getLogger(__name__)

SCAN_COINS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX",
    "DOT", "LINK", "ARB", "OP", "SUI", "TRX", "ATOM",
    "LTC", "BCH", "NEAR", "TON", "PEPE", "FLOKI",
]

MIN_FUNDING_RATE = 0.0001    # 0.01%/8h = 0.03%/ngày (~11% pa) — sàn OKX
POSITION_PCT     = 0.3       # 30% số dư mỗi vị thế
MIN_USDT         = 15.0
LEVERAGE         = "10"


def get_funding_rates():
    results = []
    for coin in SCAN_COINS:
        inst_id = f"{coin}-USDT-SWAP"
        try:
            resp = public_api.get_funding_rate(instId=inst_id)
            if resp.get('code') == '0' and resp.get('data'):
                d = resp['data'][0]
                results.append({
                    'coin':         coin,
                    'swap_id':      inst_id,
                    'spot_id':      f"{coin}-USDT",
                    'funding_rate': float(d['fundingRate']),
                    'next_rate':    float(d.get('nextFundingRate') or 0),
                    'annualized':   float(d['fundingRate']) * 3 * 365 * 100,
                })
            time.sleep(0.05)
        except Exception as e:
            log.debug(f"Skip {inst_id}: {e}")
    results.sort(key=lambda x: x['funding_rate'], reverse=True)
    return results


def get_available_usdt():
    try:
        resp = account_api.get_account_balance(ccy="USDT")
        if resp.get('code') == '0':
            for d in resp['data'][0].get('details', []):
                if d['ccy'] == 'USDT':
                    return float(d['availEq'])
    except Exception as e:
        log.error(f"Lỗi số dư: {e}")
    return 0.0


def get_spot_price(inst_id):
    try:
        resp = market_api.get_ticker(instId=inst_id)
        if resp.get('code') == '0':
            return float(resp['data'][0]['last'])
    except Exception as e:
        log.error(f"Lỗi giá {inst_id}: {e}")
    return None


def get_swap_info(inst_id):
    try:
        resp = public_api.get_instruments(instType="SWAP", instId=inst_id)
        if resp.get('code') == '0' and resp.get('data'):
            d = resp['data'][0]
            return float(d['ctVal']), float(d['minSz']), float(d['lotSz'])
    except Exception as e:
        log.error(f"Lỗi instrument {inst_id}: {e}")
    return None, None, None


def _set_leverage(swap_id):
    try:
        account_api.set_leverage(instId=swap_id, lever=LEVERAGE, mgnMode="isolated")
    except Exception as e:
        log.warning(f"set_leverage {swap_id}: {e}")


def _fmt_contracts(contracts, lot_sz):
    """Format contracts theo độ chính xác của lot_sz."""
    decimals = len(str(lot_sz).rstrip('0').split('.')[-1]) if '.' in str(lot_sz) else 0
    return str(round(contracts, decimals))


def open_position(opportunity, usdt_amount):
    coin    = opportunity['coin']
    spot_id = opportunity['spot_id']
    swap_id = opportunity['swap_id']

    price = get_spot_price(spot_id)
    if not price:
        return None

    ct_val, min_sz, lot_sz = get_swap_info(swap_id)
    if ct_val is None:
        return None

    # Tính contracts, làm tròn xuống theo lot_sz dùng math.floor tránh float error
    raw_contracts = (usdt_amount / price) / ct_val
    steps         = math.floor(round(raw_contracts / lot_sz, 8))
    contracts     = steps * lot_sz

    if contracts < min_sz:
        min_cost = min_sz * ct_val * price
        log.warning(f"  [{coin}] Bỏ qua — vốn ${usdt_amount:.2f} < tối thiểu ${min_cost:.2f}")
        return None

    spot_usdt  = round(contracts * ct_val * price, 2)
    sz_str     = _fmt_contracts(contracts, lot_sz)

    _set_leverage(swap_id)

    # Mua spot bằng USDT
    r_spot = trade_api.place_order(
        instId=spot_id, tdMode="cash",
        side="buy", ordType="market",
        sz=str(spot_usdt), tgtCcy="quote_ccy",
    )
    if r_spot.get('code') != '0':
        detail = (r_spot.get('data') or [{}])[0]
        log.error(f"  [{coin}] Spot buy lỗi [{detail.get('sCode')}]: {detail.get('sMsg') or r_spot.get('msg')}")
        return None

    time.sleep(0.3)

    # Short futures isolated 1x
    r_swap = trade_api.place_order(
        instId=swap_id, tdMode="isolated",
        side="sell", ordType="market", sz=sz_str,
    )
    if r_swap.get('code') != '0':
        detail = (r_swap.get('data') or [{}])[0]
        log.error(f"  [{coin}] Futures short lỗi [{detail.get('sCode')}]: {detail.get('sMsg') or r_swap.get('msg')} — hoàn spot...")
        time.sleep(1)
        r_rb = _sell_spot(spot_id, round(contracts * ct_val, 8))
        if not r_rb:
            log.critical(f"  [{coin}] ROLLBACK THẤT BẠI — kiểm tra thủ công spot {spot_id}!")
        return None

    return {
        'coin':               coin,
        'spot_id':            spot_id,
        'swap_id':            swap_id,
        'coin_amount':        round(contracts * ct_val, 8),
        'contracts':          contracts,
        'lot_sz':             lot_sz,
        'ct_val':             ct_val,
        'entry_price':        price,
        'entry_funding_rate': opportunity['funding_rate'],
        'open_time':          time.time(),
    }


def close_position(position):
    coin   = position['coin']
    sz_str = _fmt_contracts(position['contracts'], position['lot_sz'])

    r_swap = trade_api.place_order(
        instId=position['swap_id'], tdMode="isolated",
        side="buy", ordType="market",
        sz=sz_str, reduceOnly="true",
    )
    if r_swap.get('code') != '0':
        detail = (r_swap.get('data') or [{}])[0]
        log.error(f"  [{coin}] Đóng futures lỗi [{detail.get('sCode')}]: {detail.get('sMsg') or r_swap.get('msg')}")

    time.sleep(0.3)
    _sell_spot(position['spot_id'], position['coin_amount'])


def _sell_spot(spot_id, amount):
    r = trade_api.place_order(
        instId=spot_id, tdMode="cash",
        side="sell", ordType="market", sz=str(round(amount, 8)),
    )
    if r.get('code') != '0':
        detail = (r.get('data') or [{}])[0]
        log.error(f"  Spot sell lỗi {spot_id} [{detail.get('sCode')}]: {detail.get('sMsg') or r.get('msg')}")
        return False
    return True


def check_exit_conditions(position):
    try:
        resp = public_api.get_funding_rate(instId=position['swap_id'])
        if resp.get('code') == '0' and resp.get('data'):
            rate = float(resp['data'][0]['fundingRate'])
            return rate < 0, rate
    except Exception as e:
        log.warning(f"check_exit {position['coin']}: {e}")
    return False, None


def estimate_pnl(position):
    price = get_spot_price(position['spot_id'])
    if not price:
        return None

    elapsed_h   = (time.time() - position['open_time']) / 3600
    n_payments  = int(elapsed_h / 8)
    funding_pnl = (position['entry_funding_rate']
                   * position['contracts']
                   * position['ct_val']
                   * price * n_payments)
    price_change  = price - position['entry_price']
    net_price_pnl = (price_change * position['coin_amount']
                     - price_change * position['contracts'] * position['ct_val'])

    return {
        'price':       price,
        'funding_pnl': funding_pnl,
        'price_pnl':   net_price_pnl,
        'total_pnl':   funding_pnl + net_price_pnl,
        'n_payments':  n_payments,
    }
