import httpx
import logging
import time
import asyncio
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# POS Promo endpoint
POS_PROMO_URL = "http://localhost:9002/api/promotions/active"

# Simple in-memory cache to avoid repeated POS calls
_PROMOS_CACHE: Optional[List[Dict]] = None
_PROMOS_CACHE_AT: float = 0.0
_PROMOS_TTL_SECONDS = 30.0
_PROMOS_LOCK = asyncio.Lock()


async def fetch_active_promos(token: str) -> List[Dict]:
    """
    Fetch ACTIVE promotions from POS.
    This function MUST NOT apply any promo logic.
    """
    global _PROMOS_CACHE, _PROMOS_CACHE_AT
    
    headers = {"Authorization": f"Bearer {token}"}

    # Check cache first
    now = time.time()
    if _PROMOS_CACHE is not None and (now - _PROMOS_CACHE_AT) < _PROMOS_TTL_SECONDS:
        return _PROMOS_CACHE

    async with _PROMOS_LOCK:
        # Double-check after acquiring the lock
        now = time.time()
        if _PROMOS_CACHE is not None and (now - _PROMOS_CACHE_AT) < _PROMOS_TTL_SECONDS:
            return _PROMOS_CACHE

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(POS_PROMO_URL, headers=headers)
                response.raise_for_status()
                data = response.json()
                # Update cache
                _PROMOS_CACHE = data
                _PROMOS_CACHE_AT = time.time()
                return data

        except httpx.RequestError as e:
            logger.error(f"[PROMO] POS unreachable: {e}")
        except Exception as e:
            logger.exception(f"[PROMO] Unexpected error fetching promos: {e}")

        # Fallback to stale cache if available
        if _PROMOS_CACHE is not None:
            return _PROMOS_CACHE

        # Fail safe — NO PROMOS
        return []
