"""apollo adapter — Apollo.io people database.

Endpoints (verified against docs.apollo.io 2026-08-19):
- POST /mixed_people/api_search  — identify people by domain + titles. 0 credits.
  NOTE: since ~2025 this endpoint OBFUSCATES last names ("Hu***n") and returns no
  linkedin_url or email for net-new people — only `first_name`, `last_name_obfuscated`,
  `title`, `has_email` and the Apollo person `id`. Saved contacts (the `contacts`
  array) still carry full data. We keep the Apollo id in PersonHit.raw so our own
  find_email() can reveal via id instead of a fuzzy name match.
- POST /people/match             — email reveal. 1 credit when demographics/email
  returned (0 if nothing found; we ledger the credit conservatively up front).
- GET  /organizations/enrich     — firmographics. 1 credit per organization.
- GET  /auth/health + POST /usage_stats/api_usage_stats (master key only) — doctor.

Auth: X-Api-Key header, env APOLLO_API_KEY. Free tier is rate-limited (600 req/day,
record-selection limit 25) -> rps 1.
"""

from __future__ import annotations

from .. import net
from ..models import ROLE_TITLES, Company, title_matches_role, valid_email
from ..patterns import split_name
from .base import CompanyInfo, EmailHit, PersonHit, ProviderBase, ProviderError, register

BASE_URL = "https://api.apollo.io/api/v1"

# Apollo Basic $49/seat/mo annual = 30k credits/yr -> ~$0.0196/credit; conservative $0.02.
# Free-tier grants make the marginal price ~0 — set APOLLO_COST_PER_CREDIT=0 in .env.
COST_PER_CREDIT = 0.02
MATCH_CREDITS = 1.0     # people/match: 1 credit when an email/demographics is returned
ORG_CREDITS = 1.0       # organizations/enrich: "1 credit per organization" (docs)
SEARCH_CREDITS = 0.0    # mixed_people/api_search: 0 credits (docs)

SOURCE_QUALITY = 0.85   # database quality prior for Apollo identify hits
OBFUSCATED_PENALTY = 0.9  # api_search hid the last name -> identity slightly less usable
PER_PAGE = 10


@register
class Apollo(ProviderBase):
    name = "apollo"
    key_env = "APOLLO_API_KEY"
    is_free = False
    rps = 1.0  # free tier is tightly rate-limited; be gentle

    # ------------------------------------------------------------- helpers
    def _headers(self) -> dict:
        key = self.api_key
        if not key:
            raise ProviderError("APOLLO_API_KEY missing")
        return {"X-Api-Key": key, "Content-Type": "application/json", "Cache-Control": "no-cache"}

    def _cost_per_credit(self) -> float:
        raw = self.ctx.settings.key("APOLLO_COST_PER_CREDIT")
        if raw is None:
            return COST_PER_CREDIT
        try:
            return float(raw)
        except ValueError:
            raise ProviderError(f"APOLLO_COST_PER_CREDIT must be a number, got {raw!r}") from None

    # ------------------------------------------------------------ identify
    async def identify(self, company: Company, role: str) -> list[PersonHit]:
        domain = company.email_domain or company.domain
        titles = ROLE_TITLES.get(role, [])[:6]
        if not domain or not titles:
            return []

        async def _fetch():
            # Apollo takes these as query params (array params use the `[]` suffix).
            q = {
                "q_organization_domains_list[]": [domain],
                "person_titles[]": titles,
                "page": 1,
                "per_page": PER_PAGE,
            }
            return await net.fetch_json(
                self.ctx.http, "POST", f"{BASE_URL}/mixed_people/api_search",
                headers=self._headers(), params=q,
            )

        data = await self.cached_json(
            "people.search", {"domain": domain, "titles": titles}, _fetch,
            cost_usd=0.0, credits=SEARCH_CREDITS,
        )
        rows = list((data or {}).get("contacts") or []) + list((data or {}).get("people") or [])
        hits: list[PersonHit] = []
        seen: set[str] = set()
        for p in rows:
            if not isinstance(p, dict):
                continue
            title = p.get("title") or ""
            match = title_matches_role(title, role)
            if match <= 0:
                continue
            first = (p.get("first_name") or "").strip()
            last = (p.get("last_name") or "").strip()
            full = (p.get("name") or "").strip() or f"{first} {last}".strip()
            # Net-new people come back with the last name masked ("Hu***n", or the whole
            # name as "Ben Hu***n", or last_name itself holding the mask). Keep the FIRST
            # NAME ONLY so downstream pattern-guessers build a clean "{first}@" guess instead
            # of junk like "bhun@" (split_name("Ben Hu***n") -> ("ben", "hun")); the Apollo id
            # lives in raw for our own find_email() to reveal via id.
            obfuscated = bool(p.get("last_name_obfuscated")) or "*" in full or "*" in last
            if obfuscated:
                name = first or (full.split()[0] if full else "")
            else:
                name = full
            if not name:
                continue
            dedupe = f"{name.lower()}|{title.lower()}"
            if dedupe in seen:
                continue
            seen.add(dedupe)
            conf = SOURCE_QUALITY * match * (OBFUSCATED_PENALTY if obfuscated else 1.0)
            hits.append(
                PersonHit(
                    name=name,
                    title=title,
                    role=role,
                    source=self.name,
                    url=p.get("linkedin_url") or "",
                    confidence=round(conf, 2),
                    raw={
                        "apollo_id": p.get("id"),
                        "id": p.get("id"),
                        "has_email": p.get("has_email"),
                        "last_name_obfuscated": p.get("last_name_obfuscated"),
                        "organization": (p.get("organization") or {}).get("name"),
                    },
                )
            )
        hits.sort(key=lambda h: h.confidence, reverse=True)
        return hits

    # ---------------------------------------------------------- find_email
    async def find_email(self, company: Company, person: PersonHit) -> list[EmailHit]:
        domain = company.email_domain or company.domain
        if not domain:
            return []
        # Reveal via the Apollo person id that identify() stashed in raw (survives the
        # PersonRec.raw round-trip). Prefer 'apollo_id', fall back to 'id'; if neither is
        # present we fall back to a name+domain match below.
        apollo_id = ""
        if person.source == self.name:
            apollo_id = str(person.raw.get("apollo_id") or person.raw.get("id") or "")
        first, last = split_name(person.name)
        if not apollo_id and not (first and last):
            return []  # partial name and no Apollo id: don't burn a credit on a fuzzy match

        async def _fetch():
            q: dict = {"domain": domain, "reveal_personal_emails": "false"}
            if apollo_id:
                q["id"] = apollo_id
            if first and last:
                q["name"] = person.name
            return await net.fetch_json(
                self.ctx.http, "POST", f"{BASE_URL}/people/match",
                headers=self._headers(), params=q,
            )

        params = {"domain": domain, "name": person.name, "id": apollo_id}
        fresh = self.ctx.store.cache_get(
            self.name, "people.match", params, self.ctx.settings.cache_ttl_days
        ) is None
        data = await self.cached_json(
            "people.match", params, _fetch,
            cost_usd=self._cost_per_credit() * MATCH_CREDITS,
            credits=MATCH_CREDITS,
        )
        pobj = (data or {}).get("person") or {}
        if not pobj:
            # No person returned -> Apollo charges 0 credits. On a fresh (non-cached) miss,
            # rewrite the pre-ledgered credit row down to $0 (mirrors hunter.py).
            if fresh:
                self.ctx.store.cache_put(
                    self.name, "people.match", params, data,
                    cost_usd=0.0, credits=0.0, run_id=self.ctx.run_id,
                )
            return []
        email = (pobj.get("email") or "").strip().lower()
        if not email or "email_not_unlocked" in email or not valid_email(email):
            return []
        status = (pobj.get("email_status") or "").lower()
        verified = status == "verified"
        return [
            EmailHit(
                email=email,
                method="found",
                source=self.name,
                confidence=0.9 if verified else 0.6,
                verify_status="deliverable" if verified else "unverified",
                person_name=(pobj.get("name") or "").strip() or person.name,
                raw={
                    "email_status": status,
                    "id": pobj.get("id"),
                    "linkedin_url": pobj.get("linkedin_url"),
                },
            )
        ]

    # -------------------------------------------------------- company_info
    async def company_info(self, company: Company) -> CompanyInfo | None:
        domain = company.domain or company.email_domain
        if not domain:
            return None

        async def _fetch():
            return await net.fetch_json(
                self.ctx.http, "GET", f"{BASE_URL}/organizations/enrich",
                headers=self._headers(), params={"domain": domain},
            )

        params = {"domain": domain}
        fresh = self.ctx.store.cache_get(
            self.name, "organizations.enrich", params, self.ctx.settings.cache_ttl_days
        ) is None
        data = await self.cached_json(
            "organizations.enrich", params, _fetch,
            cost_usd=self._cost_per_credit() * ORG_CREDITS,
            credits=ORG_CREDITS,
        )
        org = (data or {}).get("organization") or {}
        if not org:
            # No organization returned -> Apollo charges 0 credits. On a fresh miss, rewrite
            # the pre-ledgered credit row down to $0 (mirrors hunter.py).
            if fresh:
                self.ctx.store.cache_put(
                    self.name, "organizations.enrich", params, data,
                    cost_usd=0.0, credits=0.0, run_id=self.ctx.run_id,
                )
            return None
        emp = org.get("estimated_num_employees")
        printed = org.get("annual_revenue_printed")
        rev = org.get("annual_revenue")
        if printed:
            revenue_hint = f"~${printed} revenue"
        elif isinstance(rev, (int, float)) and rev:
            revenue_hint = f"~${rev:,.0f} revenue"
        else:
            revenue_hint = ""
        return CompanyInfo(
            entity_name=org.get("name") or "",
            employee_count=int(emp) if isinstance(emp, (int, float)) else None,
            revenue_hint=revenue_hint,
            email_domain=org.get("primary_domain") or "",
            source=self.name,
            raw=org,
        )

    # -------------------------------------------------------------- doctor
    async def doctor(self) -> str:
        if not self.api_key:
            return "no APOLLO_API_KEY in .env"

        async def _health():
            return await net.fetch_json(
                self.ctx.http, "GET", f"{BASE_URL}/auth/health", headers=self._headers()
            )

        data = await self.cached_json("doctor.health", {"check": "auth_health"}, _health, ttl_days=0)
        if not (isinstance(data, dict) and data.get("is_logged_in")):
            return f"key rejected by auth/health: {data!r}"

        # Usage stats need a *master* API key; regular keys get 401/403. Best effort.
        try:
            async def _usage():
                return await net.fetch_json(
                    self.ctx.http, "POST", f"{BASE_URL}/usage_stats/api_usage_stats",
                    headers=self._headers(),
                )

            stats = await self.cached_json("doctor.usage", {"check": "usage"}, _usage, ttl_days=0)
        except net.HttpError as e:
            if e.status in (401, 403):
                return "ok - key valid (usage stats need a master API key)"
            raise
        line = _summarize_usage(stats)
        return f"ok - {line}" if line else "ok - key valid"


def _summarize_usage(stats: object) -> str:
    """Pull a daily-quota line for people/match out of the usage-stats payload (shape is
    lightly documented; parse defensively and fall back to a generic ok)."""
    if not isinstance(stats, dict):
        return ""
    for key, val in stats.items():
        if "people/match" in str(key) and isinstance(val, dict):
            day = val.get("day")
            if isinstance(day, dict):
                left, limit = day.get("left_over"), day.get("limit")
                if isinstance(left, (int, float)):
                    if isinstance(limit, (int, float)):
                        return f"{int(left):,} of {int(limit):,} daily people/match requests left"
                    return f"{int(left):,} daily people/match requests left"
    return ""
