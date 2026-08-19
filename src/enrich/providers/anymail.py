"""Anymail Finder adapter (https://anymailfinder.com/email-finder-api/docs), API v5.1.

Key property: Anymail live-verifies before charging — only fully-verified
("valid") results cost credits; misses, "risky", and blacklisted results are
free. Every search response carries `credits_charged`, which we mirror into
the ledger (see cost accounting below).

Capabilities / endpoints (all POST unless noted; verified against the live
docs 2026-08-19):
- find_email  -> /v5.1/find-email/person         (1 credit when valid)
- identify    -> /v5.1/find-email/decision-maker (2 credits when valid)
- domain_pattern -> /v5.1/find-email/company     (1 credit, up to 20 emails)
- doctor      -> GET /v5.1/account               (free)

Auth: raw API key as the `Authorization` header value (the live docs say
"set your API key as its value" — no Bearer prefix).

Cost accounting:
- cached_json needs the cost *before* the response exists, so we budget-check
  and pre-charge the worst case (1 or 2 credits), then rewrite the ledger row
  to the actual `credits_charged` from a *fresh* response (cache_put is
  INSERT OR REPLACE on the same provider/op/params key). Cache hits are never
  rewritten, so old runs keep their real spend.

Decision-maker categories (verified live): ceo, engineering, finance, hr, it,
logistics, marketing, operations, buyer, sales. There is NO built-in legal or
privacy category, so identify() answers only ceo/coo/cfo by default
(ceo->ceo, coo->operations, cfo->finance). Accounts can define custom
categories in dashboard search settings; expose one to a role via
ANYMAIL_CATEGORY_<ROLE> (e.g. ANYMAIL_CATEGORY_LEGAL=legal).

identify()/find_email() interplay: the decision-maker response already
contains the person's verified email. identify() keeps the full payload in
PersonHit.raw AND the response is cached in SQLite, so a later find_email()
for the same person reuses it (raw first, then a direct cache peek) at zero
cost instead of spending another person-search credit.

email_status mapping: "valid"/"verified" -> verify_status "deliverable"
(Anymail's live verification already ran; the pipeline skips re-verification
because verify_status != "unverified"); "risky"/anything else -> "unknown";
"not_found"/"blacklisted" -> no result.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .. import net
from ..models import Company, title_matches_role, valid_email
from ..patterns import SHAPE_TO_PATTERNS, detect_pattern, learn_domain_shape, split_name
from .base import EmailHit, PatternHit, PersonHit, ProviderBase, ProviderError, register

BASE_URL = "https://api.anymailfinder.com/v5.1"

# Standard plan: $49/mo for 1,000 credits => $0.049/credit (docs/service-research.md).
# Env ANYMAIL_COST_PER_CREDIT overrides (0 = trial/free credits).
COST_PER_CREDIT_USD = 0.049
COST_ENV = "ANYMAIL_COST_PER_CREDIT"

CREDITS_PERSON = 1.0          # /find-email/person, charged only when valid
CREDITS_DECISION_MAKER = 2.0  # /find-email/decision-maker, charged only when valid
CREDITS_COMPANY = 1.0         # /find-email/company, charged only when valid

# Real-time searches can be slow (docs recommend a 180s client timeout).
SEARCH_TIMEOUT = 180.0

SOURCE_QUALITY = 0.9  # they return live-verified emails with LinkedIn evidence

# Built-in decision_maker_category values (no legal/privacy — see module docstring).
CATEGORY_BY_ROLE = {"ceo": "ceo", "coo": "operations", "cfo": "finance"}

VERIFIED_STATUSES = {"valid", "verified"}
NO_RESULT_STATUSES = {"not_found", "blacklisted"}


@register
class Anymail(ProviderBase):
    name = "anymail"
    key_env = "ANYMAILFINDER_API_KEY"
    is_free = False
    rps = 2.0

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
            raise ProviderError(f"anymail: no {self.key_env} configured")
        return key

    def _category_for(self, role: str) -> str | None:
        role = (role or "").strip().lower()
        if not role:
            return None
        return self.ctx.settings.key(f"ANYMAIL_CATEGORY_{role.upper()}") or CATEGORY_BY_ROLE.get(role)

    def _domain(self, company: Company) -> str:
        return company.email_domain or company.domain

    async def _call(
        self,
        op: str,
        params: dict,
        *,
        path: str,
        method: str = "POST",
        body: dict | None = None,
        max_credits: float = 0.0,
        ttl_days: int | None = None,
    ) -> dict:
        """cached_json wrapper that pre-charges the worst case, then rewrites the
        ledger row to the response's actual `credits_charged` (fresh calls only)."""
        price = self.cost_per_credit
        fresh: dict[str, Any] = {}

        async def fetch():
            data = await net.fetch_json(
                self.ctx.http,
                method,
                f"{BASE_URL}{path}",
                headers={"Authorization": self._require_key()},
                json_body=body,
                timeout=SEARCH_TIMEOUT,
            )
            fresh["hit"] = True
            return data

        data = await self.cached_json(
            op, params, fetch, cost_usd=max_credits * price, credits=max_credits, ttl_days=ttl_days
        )
        if fresh and isinstance(data, dict):
            actual = float(data.get("credits_charged") or 0)
            if actual != max_credits:
                self.ctx.store.cache_put(
                    self.name, op, params, data,
                    cost_usd=actual * price, credits=actual, run_id=self.ctx.run_id,
                )
        return data

    # ------------------------------------------------------- response mapping
    def _email_hits(self, data: dict, person_name: str) -> list[EmailHit]:
        status = (data.get("email_status") or "").lower()
        email = (data.get("email") or data.get("valid_email") or "").strip().lower()
        if status in NO_RESULT_STATUSES or not email or not valid_email(email):
            return []
        if status in VERIFIED_STATUSES:
            verify_status, conf = "deliverable", 0.97  # their live-verify already ran
        else:  # "risky" or anything unexpected: exists but unproven
            verify_status, conf = "unknown", 0.6
        return [
            EmailHit(
                email=email,
                method="found",
                source=self.name,
                confidence=conf,
                verify_status=verify_status,
                person_name=person_name,
                raw=data,
            )
        ]

    # ---------------------------------------------------------------- identify
    async def identify(self, company: Company, role: str) -> list[PersonHit]:
        category = self._category_for(role)
        domain = self._domain(company)
        if not category or not domain:
            return []  # no remote call: role has no Anymail category (legal/privacy)
        data = await self._call(
            "decision_maker",
            {"domain": domain, "category": category},
            path="/find-email/decision-maker",
            body={"domain": domain, "decision_maker_category": [category]},
            max_credits=CREDITS_DECISION_MAKER,
        )
        name = (data.get("person_full_name") or "").strip()
        title = (data.get("person_job_title") or "").strip()
        if not name:
            return []
        score = title_matches_role(title, role)
        if score <= 0:
            return []  # category matched but title doesn't support the requested role
        status = (data.get("email_status") or "").lower()
        quality = SOURCE_QUALITY if status in VERIFIED_STATUSES else SOURCE_QUALITY - 0.1
        return [
            PersonHit(
                name=name,
                title=title,
                role=role,
                source=self.name,
                url=data.get("person_linkedin_url") or "",
                confidence=round(quality * score, 2),
                raw=data,  # carries the verified email — find_email() reuses it
            )
        ]

    # --------------------------------------------------------------- find_email
    async def find_email(self, company: Company, person: PersonHit) -> list[EmailHit]:
        domain = self._domain(company)
        name = (person.name or "").strip()
        if not domain or not name:
            return []
        # Free path 1: identify() already returned the email in PersonHit.raw.
        # Free path 2: the decision-maker response for this role is in the SQLite
        # cache (survives _to_hit() dropping raw) — a pure cache peek, no spend.
        for payload in self._decision_maker_payloads(domain, person):
            if self._same_person(payload.get("person_full_name") or "", name):
                hits = self._email_hits(payload, name)
                if hits:
                    return hits
        data = await self._call(
            "person",
            {"domain": domain, "full_name": name},
            path="/find-email/person",
            body={"domain": domain, "full_name": name},
            max_credits=CREDITS_PERSON,
        )
        return self._email_hits(data, name)

    def _decision_maker_payloads(self, domain: str, person: PersonHit) -> list[dict]:
        out: list[dict] = []
        raw = person.raw or {}
        if raw.get("email") and raw.get("email_status"):
            out.append(raw)
        category = self._category_for(person.role)
        if category:
            cached = self.ctx.store.cache_get(
                self.name,
                "decision_maker",
                {"domain": domain, "category": category},
                self.ctx.settings.cache_ttl_days,
            )
            if isinstance(cached, dict):
                out.append(cached)
        return out

    @staticmethod
    def _same_person(a: str, b: str) -> bool:
        fa, la = split_name(a)
        fb, lb = split_name(b)
        return bool(fa) and (fa, la) == (fb, lb)

    # ------------------------------------------------------------ domain_pattern
    async def domain_pattern(self, company: Company) -> PatternHit | None:
        domain = self._domain(company)
        if not domain:
            return None
        data = await self._call(
            "company",
            {"domain": domain},
            path="/find-email/company",
            body={"domain": domain},
            max_credits=CREDITS_COMPANY,
        )
        locals_: list[str] = []
        named: list[tuple[str, str]] = []  # docs return plain strings; tolerate objects
        for entry in data.get("emails") or []:
            if isinstance(entry, dict):
                addr = (entry.get("email") or "").strip().lower()
                who = entry.get("full_name") or entry.get("name") or ""
            else:
                addr, who = str(entry).strip().lower(), ""
            local, _, dom = addr.partition("@")
            if not local or dom != domain.lower():
                continue
            locals_.append(local)
            if who:
                named.append((local, who))

        # Exact pattern when the payload includes names (rare; docs show bare strings).
        exact: Counter[str] = Counter()
        for local, who in named:
            first, last = split_name(who)
            p = detect_pattern(local, first, last)
            if p:
                exact[p] += 1
        if exact:
            pattern, n = exact.most_common(1)[0]
            return PatternHit(
                pattern=pattern,
                confidence=round(min(0.95, 0.7 + 0.05 * n), 2),
                source=self.name,
                sample_size=n,
            )

        shape, dominance, n = learn_domain_shape(locals_)
        if not n or shape == "unknown":
            return None
        pattern = SHAPE_TO_PATTERNS.get(shape, [""])[0]
        if not pattern:
            return None
        return PatternHit(
            pattern=pattern,
            confidence=round(min(0.85, dominance * (0.4 + 0.075 * n)), 2),
            source=self.name,
            sample_size=n,
        )

    # ------------------------------------------------------------------ doctor
    async def doctor(self) -> str:
        data = await self._call(
            "account", {}, path="/account", method="GET", max_credits=0.0, ttl_days=0
        )
        credits = data.get("credits_left")
        if credits is None:
            return "ok - account reachable (no credits_left in payload)"
        return f"ok - {int(credits):,} credits left"
