"""HTTP clients cho 2 bot Flask API (arb @ :5000, trend @ :5001)."""
import logging
from typing import Optional

import httpx

from config import BOT_ENDPOINTS

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
_client: Optional[httpx.AsyncClient] = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_TIMEOUT)
    return _client


async def shutdown():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _endpoint(bot: str) -> Optional[str]:
    return BOT_ENDPOINTS.get(bot)


async def get_json(bot: str, path: str) -> Optional[dict]:
    base = _endpoint(bot)
    if not base:
        return None
    try:
        r = await client().get(base + path)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        log.warning(f"GET {bot}{path} fail: {e}")
        return None


async def post_json(bot: str, path: str, payload: Optional[dict] = None) -> Optional[dict]:
    base = _endpoint(bot)
    if not base:
        return None
    try:
        r = await client().post(base + path, json=payload or {})
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        log.warning(f"POST {bot}{path} fail: {e}")
        return None


# ════════════════════ HIGH-LEVEL API ════════════════════
async def status(bot: str) -> Optional[dict]:
    return await get_json(bot, "/api/status")


async def analytics(bot: str) -> Optional[dict]:
    return await get_json(bot, "/api/analytics")


async def health(bot: str) -> Optional[dict]:
    return await get_json(bot, "/api/health")


async def start_bot(bot: str) -> Optional[dict]:
    return await post_json(bot, "/api/start")


async def stop_bot(bot: str) -> Optional[dict]:
    return await post_json(bot, "/api/stop")


async def close_coin(bot: str, coin: str) -> Optional[dict]:
    return await post_json(bot, f"/api/close/{coin}")


async def close_all(bot: str) -> Optional[dict]:
    return await post_json(bot, "/api/close_all")


async def sync(bot: str) -> Optional[dict]:
    return await post_json(bot, "/api/sync")


async def run_backtest(candles: int = 600, balance: float = 10000.0) -> Optional[dict]:
    return await post_json('trend', '/api/backtest/run', {'candles': candles, 'balance': balance})


async def backtest_status() -> Optional[dict]:
    return await get_json('trend', '/api/backtest')
