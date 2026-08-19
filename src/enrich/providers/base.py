"""Provider contract. Every adapter subclasses ProviderBase and registers itself.

Capabilities are duck-typed — implement any subset of:

    async def identify(self, company: Company, role: str) -> list[PersonHit]
    async def find_email(self, company: Company, person: PersonHit) -> list[EmailHit]
    async def domain_pattern(self, company: Company) -> PatternHit | None
    async def verify(self, email: str) -> VerifyResult
    async def company_info(self, company: Company) -> CompanyInfo | None
    async def doctor(self) -> str          # cheap health/quota check for `enrich doctor`

Rules for implementations:
- Wrap every remote call in `self.cached_json(...)` so results cache in SQLite and
  costs hit the run ledger/budget. Pass the *money* cost via cost_usd and provider
  credit units via credits.
- Respect self.rps (the base helper does this for you inside cached_json's fetch).
- Never raise on "no result" — return [] / None. Raise ProviderError only for
  misconfiguration; let net.HttpError propagate for genuine API failures.
- Never log or store API keys.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..context import Ctx

REGISTRY: dict[str, type[ProviderBase]] = {}


def register(cls: type[ProviderBase]) -> type[ProviderBase]:
    REGISTRY[cls.name] = cls
    return cls


class ProviderError(Exception):
    pass


@dataclass
class PersonHit:
    name: str
    title: str
    role: str
    source: str
    url: str = ""
    confidence: float = 0.6   # CONTRACT: includes title fit — providers fold title_matches_role
                              # into this; the pipeline does NOT multiply it in again.
    raw: dict = field(default_factory=dict)


@dataclass
class EmailHit:
    email: str
    method: str            # found|pattern|constructed|generic
    source: str
    confidence: float = 0.5
    verify_status: str = "unverified"   # provider-supplied signal, if any
    person_name: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class PatternHit:
    pattern: str           # e.g. "{first}.{last}"
    confidence: float
    source: str
    sample_size: int = 0


@dataclass
class VerifyResult:
    email: str
    status: str            # deliverable|catch_all|undeliverable|unknown|error
    source: str
    raw: dict = field(default_factory=dict)


@dataclass
class CompanyInfo:
    entity_name: str = ""
    parent_company: str = ""
    employee_count: int | None = None
    revenue_hint: str = ""
    email_domain: str = ""
    source: str = ""
    raw: dict = field(default_factory=dict)


class ProviderBase:
    name: str = "base"
    key_env: str | None = None     # env var with the API key; None = keyless provider
    is_free: bool = False          # True = never spends money (may still use free-tier credits)
    rps: float = 3.0               # polite request rate

    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    @property
    def api_key(self) -> str | None:
        return self.ctx.settings.key(self.key_env)

    def available(self) -> tuple[bool, str]:
        """(usable, reason-if-not). free_only runs exclude non-free providers."""
        if self.key_env and not self.api_key:
            return False, f"no {self.key_env} in .env"
        if self.ctx.flags.free_only and not self.is_free:
            return False, "--free-only run"
        return True, ""

    async def cached_json(
        self,
        op: str,
        params: dict,
        fetch: Callable[[], Awaitable[Any]],
        *,
        cost_usd: float = 0.0,
        credits: float = 0.0,
        ttl_days: int | None = None,
        is_failure: Callable[[Any], bool] | None = None,
        failure_ttl_days: float = 1.0,
    ) -> Any:
        """Cache-through JSON call: SQLite cache -> budget check -> rate limit -> fetch -> ledger."""
        ttl = ttl_days if ttl_days is not None else self.ctx.settings.cache_ttl_days
        hit = self.ctx.store.cache_get_with_age(self.name, op, params)
        if hit is not None:
            data, age = hit
            effective_ttl = ttl
            if is_failure is not None and is_failure(data):
                effective_ttl = min(ttl, failure_ttl_days)
            if age <= effective_ttl:
                return data
        assert self.ctx.budget is not None
        self.ctx.budget.reserve(cost_usd)  # raises BudgetExceeded; visible to concurrent checks
        try:
            await self.ctx.limiter.acquire(self.name, self.rps)
            data = await fetch()
            self.ctx.store.cache_put(
                self.name, op, params, data, cost_usd=cost_usd, credits=credits, run_id=self.ctx.run_id
            )
        finally:
            self.ctx.budget.release(cost_usd)
        return data


def get_providers(names: list[str], ctx: Ctx) -> tuple[list[ProviderBase], list[tuple[str, str]]]:
    """Instantiate registered providers by name, in order. Returns (usable, skipped[(name, reason)])."""
    usable: list[ProviderBase] = []
    skipped: list[tuple[str, str]] = []
    for n in names:
        cls = REGISTRY.get(n)
        if cls is None:
            skipped.append((n, "not implemented"))
            continue
        p = cls(ctx)
        ok, why = p.available()
        if ok:
            usable.append(p)
        else:
            skipped.append((n, why))
    return usable, skipped
