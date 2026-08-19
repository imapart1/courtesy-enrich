"""Hunter.io adapter (https://hunter.io/api-documentation/v2).

Capabilities: domain_pattern, identify, find_email, verify, doctor.

Endpoints (all GET, key as ?api_key= — passed only inside fetch, never cached):
- /v2/domain-search   pattern + indexed people for a domain (1 search credit / 10 emails)
- /v2/email-finder    name+domain -> email (1 credit on hit, misses free)
- /v2/email-verifier  deliverability (0.5 credit)
- /v2/account         plan + remaining quota (free) — used by doctor()

Cost accounting notes:
- Domain search is billed by Hunter per 10 emails returned. cached_json needs the
  cost *before* the response exists, so we request a fixed DOMAIN_SEARCH_LIMIT and
  charge ceil(limit/10) credits — exact for the default limit of 10 (1 credit, as
  budgeted in SPEC stage 3).
- Email finder misses are free per Hunter. We charge 1 credit up front (budget-safe),
  then rewrite the ledger row to 0 when a *fresh* call comes back empty
  (cache_put is INSERT OR REPLACE on the same provider/op/params key).
- identify() and find_email() reuse the cached /domain-search response for the same
  domain (same op+params -> cache hit / direct peek, zero extra cost).
"""

from __future__ import annotations

import math

from .. import net
from ..models import Company, title_matches_role
from ..patterns import norm_name, split_name
from .base import (
    EmailHit,
    PatternHit,
    PersonHit,
    ProviderBase,
    ProviderError,
    VerifyResult,
    register,
)

BASE_URL = "https://api.hunter.io/v2"

# Starter plan: $49/mo for 2,000 credits. Env HUNTER_COST_PER_CREDIT overrides (0 = free tier).
COST_PER_CREDIT_USD = 0.0245
COST_ENV = "HUNTER_COST_PER_CREDIT"

DOMAIN_SEARCH_LIMIT = 10                                     # emails per domain-search call
CREDITS_DOMAIN_SEARCH = math.ceil(DOMAIN_SEARCH_LIMIT / 10)  # 1 credit per 10 emails, rounded up
CREDITS_EMAIL_FINDER_HIT = 1.0
CREDITS_VERIFY = 0.5

# Hunter verifier status -> our VerifyResult taxonomy. webmail/disposable count as
# undeliverable for this pipeline (we only want corporate addresses); noted in raw.
VERIFY_STATUS_MAP = {
    "valid": "deliverable",
    "invalid": "undeliverable",
    "accept_all": "catch_all",
    "webmail": "undeliverable",
    "disposable": "undeliverable",
    "unknown": "unknown",
}


def _pattern_confidence(total_emails: int, has_org: bool) -> float:
    """Trust in Hunter's native pattern grows with the size of its email corpus
    for the domain (meta.results). No recognized organization = weaker corpus."""
    if total_emails >= 10:
        conf = 0.9
    elif total_emails >= 3:
        conf = 0.8
    elif total_emails >= 1:
        conf = 0.65
    else:
        conf = 0.55  # pattern inferred from Hunter's history alone
    if not has_org:
        conf -= 0.05
    return round(conf, 2)


@register
class Hunter(ProviderBase):
    name = "hunter"
    key_env = "HUNTER_API_KEY"
    is_free = False
    rps = 5.0

    # ------------------------------------------------------------- plumbing
    @property
    def cost_per_credit(self) -> float:
        raw = self.ctx.settings.key(COST_ENV)
        if raw is None:
            return COST_PER_CREDIT_USD
        try:
            return float(raw)
        except ValueError:
            raise ProviderError(f"{COST_ENV} must be a number, got {raw!r}") from None

    def _require_key(self) -> str:
        key = self.api_key
        if not key:
            raise ProviderError(f"hunter: no {self.key_env} configured")
        return key

    async def _get(self, path: str, params: dict) -> dict:
        return await net.fetch_json(
            self.ctx.http,
            "GET",
            f"{BASE_URL}/{path}",
            params={**params, "api_key": self._require_key()},
        )

    def _domain(self, company: Company) -> str:
        return company.email_domain or company.domain

    async def _domain_search(self, domain: str) -> dict:
        params = {"domain": domain, "limit": DOMAIN_SEARCH_LIMIT}

        async def fetch():
            return await self._get("domain-search", params)

        return await self.cached_json(
            "domain-search",
            params,
            fetch,
            cost_usd=CREDITS_DOMAIN_SEARCH * self.cost_per_credit,
            credits=float(CREDITS_DOMAIN_SEARCH),
        )

    def _peek_domain_search(self, domain: str) -> dict | None:
        """Cached /domain-search response for this domain, without spending anything."""
        if not domain:
            return None
        return self.ctx.store.cache_get(
            self.name,
            "domain-search",
            {"domain": domain, "limit": DOMAIN_SEARCH_LIMIT},
            self.ctx.settings.cache_ttl_days,
        )

    # ---------------------------------------------------------- domain_pattern
    async def domain_pattern(self, company: Company) -> PatternHit | None:
        domain = self._domain(company)
        if not domain:
            return None
        resp = await self._domain_search(domain)
        data = resp.get("data") or {}
        pattern = data.get("pattern") or ""
        if "{" not in pattern:
            return None
        emails = data.get("emails") or []
        total = int((resp.get("meta") or {}).get("results") or 0)
        sample = max(total, len(emails))
        return PatternHit(
            pattern=pattern,
            confidence=_pattern_confidence(sample, bool(data.get("organization"))),
            source="hunter",
            sample_size=sample,
        )

    # ---------------------------------------------------------------- identify
    async def identify(self, company: Company, role: str) -> list[PersonHit]:
        domain = self._domain(company)
        if not domain:
            return []
        resp = await self._domain_search(domain)  # cache hit if domain_pattern already ran
        hits: list[PersonHit] = []
        for e in (resp.get("data") or {}).get("emails") or []:
            title = e.get("position") or ""
            score = title_matches_role(title, role)
            if score <= 0:
                continue
            name = " ".join(filter(None, (e.get("first_name"), e.get("last_name")))).strip()
            if not name:
                continue
            sources = e.get("sources") or []
            hits.append(
                PersonHit(
                    name=name,
                    title=title,
                    role=role,
                    source="hunter",
                    url=(sources[0].get("uri") or "") if sources else "",
                    confidence=round((e.get("confidence") or 50) / 100 * score, 2),
                    raw={k: e.get(k) for k in
                         ("value", "type", "confidence", "position", "seniority", "department")},
                )
            )
        hits.sort(key=lambda h: h.confidence, reverse=True)
        return hits

    # --------------------------------------------------------------- find_email
    async def find_email(self, company: Company, person: PersonHit) -> list[EmailHit]:
        domain = self._domain(company)
        first, last = split_name(person.name)
        if not domain or not first or not last:
            return []

        # Free path: the person may already be in a cached /domain-search response.
        cached = self._peek_domain_search(domain)
        if cached:
            for e in (cached.get("data") or {}).get("emails") or []:
                if (
                    e.get("value")
                    and norm_name(e.get("first_name") or "") == first
                    and norm_name(e.get("last_name") or "") == last
                ):
                    return [self._email_hit(e["value"], e.get("confidence"),
                                            e.get("verification"), person.name, e)]

        params = {"domain": domain, "first_name": first, "last_name": last}

        async def fetch():
            return await self._get("email-finder", params)

        fresh = self.ctx.store.cache_get(
            self.name, "email-finder", params, self.ctx.settings.cache_ttl_days
        ) is None
        resp = await self.cached_json(
            "email-finder",
            params,
            fetch,
            cost_usd=CREDITS_EMAIL_FINDER_HIT * self.cost_per_credit,
            credits=CREDITS_EMAIL_FINDER_HIT,
        )
        data = resp.get("data") or {}
        if not data.get("email"):
            if fresh:  # Hunter does not charge for misses — zero out the ledger row
                self.ctx.store.cache_put(
                    self.name, "email-finder", params, resp,
                    cost_usd=0.0, credits=0.0, run_id=self.ctx.run_id,
                )
            return []
        return [self._email_hit(data["email"], data.get("score"),
                                data.get("verification"), person.name, data)]

    def _email_hit(self, email: str, score, verification, person_name: str, raw: dict) -> EmailHit:
        status = "unverified"
        if isinstance(verification, dict) and verification.get("status"):
            status = VERIFY_STATUS_MAP.get(verification["status"], "unknown")
        return EmailHit(
            email=email,
            method="found",
            source="hunter",
            confidence=round((score or 50) / 100, 2),
            verify_status=status,
            person_name=person_name,
            raw={k: v for k, v in raw.items() if k != "sources"},
        )

    # ------------------------------------------------------------------- verify
    async def verify(self, email: str) -> VerifyResult:
        params = {"email": email}

        async def fetch():
            return await self._get("email-verifier", params)

        resp = await self.cached_json(
            "email-verifier",
            params,
            fetch,
            cost_usd=CREDITS_VERIFY * self.cost_per_credit,
            credits=CREDITS_VERIFY,
        )
        data = resp.get("data") or {}
        hunter_status = data.get("status") or ""
        status = VERIFY_STATUS_MAP.get(hunter_status, "unknown")
        raw = dict(data)
        if hunter_status in ("webmail", "disposable"):
            raw["mapping_note"] = (
                f"hunter status {hunter_status!r} mapped to undeliverable: "
                "this pipeline only sends to corporate addresses"
            )
        return VerifyResult(email=email, status=status, source="hunter", raw=raw)

    # ------------------------------------------------------------------- doctor
    async def doctor(self) -> str:
        async def fetch():
            return await self._get("account", {})

        # ttl_days=0 -> effectively uncached, and the call is free per Hunter.
        resp = await self.cached_json("account", {}, fetch, ttl_days=0)
        d = resp.get("data") or {}
        plan = d.get("plan_name") or "unknown plan"
        parts = []
        for kind in ("credits", "searches", "verifications"):
            info = (d.get("requests") or {}).get(kind) or {}
            remaining = info.get("remaining")
            if remaining is not None:
                parts.append(f"{remaining:,.0f} {kind} left")
        detail = ", ".join(parts) if parts else "no request quota reported"
        return f"ok - plan {plan}, {detail}"
