"""HTTP helpers: retrying JSON fetch with sane backoff for provider APIs."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse Retry-After as seconds (int/float) or HTTP-date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from datetime import UTC, datetime
        from email.utils import parsedate_to_datetime

        return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
    except Exception:
        return None


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        self.status, self.body, self.url = status, body[:500], url
        super().__init__(f"HTTP {status} from {url}: {body[:200]}")


async def fetch(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    json_body: Any | None = None,
    attempts: int = 3,
    timeout: float = 30.0,
) -> httpx.Response:
    """Request with retries on transport errors and retryable statuses."""
    delay = 1.5
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = await http.request(
                method, url, headers=headers, params=params, json=json_body, timeout=timeout
            )
            if resp.status_code in RETRYABLE_STATUS and attempt < attempts - 1:
                wait = _retry_after_seconds(resp.headers.get("retry-after")) or delay
                await asyncio.sleep(min(wait, 120.0))
                delay *= 2
                continue
            if resp.status_code >= 400:
                raise HttpError(resp.status_code, resp.text, url)
            return resp
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last = e
            if attempt < attempts - 1:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            raise
    raise last if last else RuntimeError("unreachable")


async def fetch_json(http: httpx.AsyncClient, method: str, url: str, **kw) -> Any:
    resp = await fetch(http, method, url, **kw)
    try:
        return resp.json()
    except ValueError:
        raise HttpError(resp.status_code, f"non-JSON response: {resp.text[:200]}", url) from None
