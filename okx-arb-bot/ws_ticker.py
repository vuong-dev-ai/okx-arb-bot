"""
OKX public WebSocket — subscribe channel `tickers`, cache giá trong memory.

- Tự động reconnect khi mất kết nối
- Heartbeat / ping 25s
- Subscribe theo lô 30 để không vượt limit message size của OKX
- Trả None nếu cache stale → caller fallback sang REST
"""
import json
import time
import threading
import logging

try:
    import websocket  # from `websocket-client`
except ImportError:
    websocket = None

log = logging.getLogger(__name__)

URL_LIVE = "wss://ws.okx.com:8443/ws/v5/public"
URL_DEMO = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"


class TickerWS:
    def __init__(self, instruments, simulated=False, max_age=15):
        """instruments: list of OKX instId (vd ['BTC-USDT','ETH-USDT-SWAP']).
        max_age: số giây tối đa coi giá cache là tươi."""
        self.instruments = list(instruments)
        self.url       = URL_DEMO if simulated else URL_LIVE
        self.max_age   = max_age
        self.prices    = {}   # instId -> {'price': float, 'ts': float}
        self.lock      = threading.Lock()
        self.ws        = None
        self.thread    = None
        self.running   = False
        self.connected = False
        self.last_msg_ts = 0
        self.reconnects  = 0

    # ── lifecycle ──
    def start(self):
        if websocket is None:
            log.warning("websocket-client không có — real-time bị tắt")
            return False
        # Đợi thread cũ chết nếu đang trong tiến trình shutdown
        if self.thread and self.thread.is_alive():
            if self.running:
                return True
            self.thread.join(timeout=5)
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True, name='ws-ticker')
        self.thread.start()
        return True

    def stop(self):
        self.running = False
        if self.ws:
            try: self.ws.close()
            except Exception: pass

    # ── api ──
    def get_price(self, inst_id):
        """Trả giá nếu cache tươi (< max_age), None nếu stale/missing."""
        with self.lock:
            d = self.prices.get(inst_id)
        if not d:
            return None
        if (time.time() - d['ts']) > self.max_age:
            return None
        return d['price']

    def age(self, inst_id):
        with self.lock:
            d = self.prices.get(inst_id)
        return (time.time() - d['ts']) if d else None

    def health(self):
        with self.lock:
            cached = len(self.prices)
        return {
            'connected':  self.connected,
            'subs':       len(self.instruments),
            'cached':     cached,
            'reconnects': self.reconnects,
            'last_msg_age': (time.time() - self.last_msg_ts) if self.last_msg_ts else None,
        }

    # ── internal ──
    def _loop(self):
        backoff = 3  # exponential 3→6→12→24→30s (max)
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=lambda ws, err: log.debug(f"ws err: {err}"),
                    on_close=self._on_close,
                )
                self.ws.run_forever(ping_interval=25, ping_timeout=10)
            except Exception as e:
                log.warning(f"ws crash: {e}")
            self.connected = False
            if self.running:
                self.reconnects += 1
                # Nếu kết nối duy trì > 60s trước khi rớt, reset backoff
                if self.last_msg_ts and (time.time() - self.last_msg_ts) < 60:
                    backoff = min(backoff * 2, 30)
                else:
                    backoff = 3
                time.sleep(backoff)

    def _on_open(self, ws):
        self.connected = True
        # OKX cho phép gửi nhiều subscribe message — chunk thành lô 30
        args_all = [{"channel": "tickers", "instId": i} for i in self.instruments]
        for i in range(0, len(args_all), 30):
            try:
                ws.send(json.dumps({"op": "subscribe", "args": args_all[i:i+30]}))
            except Exception as e:
                log.warning(f"ws subscribe: {e}")

    def _on_close(self, ws, *_):
        self.connected = False

    def _on_message(self, ws, msg):
        try:
            data = json.loads(msg)
        except Exception:
            return
        self.last_msg_ts = time.time()
        if data.get('event'):                # 'subscribe' / 'error'
            if data.get('event') == 'error':
                log.warning(f"ws server error: {data}")
            return
        if data.get('arg', {}).get('channel') != 'tickers':
            return
        for tick in data.get('data') or []:
            inst = tick.get('instId')
            last = tick.get('last')
            if not inst or not last:
                continue
            try:
                price = float(last)
            except (ValueError, TypeError):
                continue
            with self.lock:
                self.prices[inst] = {'price': price, 'ts': time.time()}
