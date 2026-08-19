"""Run context shared by pipeline and providers: settings, store, budget, flags, http."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

import httpx

from .config import Settings
from .store import Store


class BudgetExceeded(Exception):
    """Raised when a paid call would push run spend past the cap."""


class Budget:
    """Ledgered spend (SQLite) + in-flight reservations, so concurrent paid calls
    can't all pass the same pre-spend check (single event loop -> plain attr is safe)."""

    def __init__(self, store: Store, run_id: str, limit_usd: float):
        self.store, self.run_id, self.limit_usd = store, run_id, limit_usd
        self.reserved = 0.0

    def spent(self) -> float:
        return self.store.run_spend(self.run_id)

    def check(self, cost_usd: float) -> None:
        if cost_usd > 0 and self.spent() + self.reserved + cost_usd > self.limit_usd:
            raise BudgetExceeded(
                f"run spend ${self.spent():.2f} + reserved ${self.reserved:.2f} + ${cost_usd:.2f} "
                f"would exceed cap ${self.limit_usd:.2f}"
            )

    def reserve(self, cost_usd: float) -> None:
        self.check(cost_usd)
        self.reserved += cost_usd

    def release(self, cost_usd: float) -> None:
        self.reserved = max(0.0, self.reserved - cost_usd)


class RateLimiter:
    """Min-interval limiter per provider (simple and sufficient at our volumes)."""

    def __init__(self):
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def acquire(self, name: str, rps: float) -> None:
        lock = self._locks.setdefault(name, asyncio.Lock())
        min_interval = 1.0 / max(rps, 0.1)
        async with lock:
            now = time.monotonic()
            wait = self._last.get(name, 0.0) + min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[name] = time.monotonic()


@dataclass
class Flags:
    free_only: bool = False
    llm: bool = False
    dry_run: bool = False
    verify: bool = True


@dataclass
class Ctx:
    settings: Settings
    store: Store
    flags: Flags = field(default_factory=Flags)
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    http: httpx.AsyncClient | None = None
    budget: Budget | None = None
    limiter: RateLimiter = field(default_factory=RateLimiter)

    def __post_init__(self):
        if self.budget is None:
            self.budget = Budget(self.store, self.run_id, self.settings.budget_per_run)

    async def __aenter__(self) -> Ctx:
        if self.http is None:
            self.http = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
                headers={"User-Agent": "courtesy-enrich/0.1 (contact research; ops@schallertpc.com)"},
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self.http is not None:
            await self.http.aclose()
            self.http = None
