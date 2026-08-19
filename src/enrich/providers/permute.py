"""'permute' provider: construct email guesses from the best-known pattern/shape. Free."""

from __future__ import annotations

from ..models import Company
from ..patterns import candidate_pairs, split_name
from .base import EmailHit, PersonHit, ProviderBase, register


@register
class Permute(ProviderBase):
    name = "permute"
    is_free = True
    rps = 1000.0

    async def find_email(self, company: Company, person: PersonHit) -> list[EmailHit]:
        domain = company.email_domain or company.domain
        first, last = split_name(person.name)
        if not domain or not first:
            return []
        limit = int(self.ctx.settings.thresholds.get("max_guesses_per_person", 3))
        rec = self.ctx.store.get_domain_pattern(domain) or {}
        pattern = company.pattern or rec.get("pattern", "")
        shape = rec.get("shape", "")
        base_conf = max(company.pattern_confidence, float(rec.get("confidence") or 0.0))
        hits = []
        # candidate_pairs marks whether each guess came from real domain knowledge
        # (informed) or the generic fallback list. Only informed guesses are labeled
        # method="pattern" — a fallback guess must never be tiered as a proven pattern.
        for i, (local, _pat, informed) in enumerate(
            candidate_pairs(first, last, pattern=pattern, shape=shape, limit=limit)
        ):
            if informed:
                conf = max(0.2, (base_conf or 0.4) - 0.15 * i)
                method = "pattern"
            else:
                conf = max(0.15, 0.3 - 0.1 * i)
                method = "constructed"
            hits.append(
                EmailHit(
                    email=f"{local}@{domain}",
                    method=method,
                    source="permute",
                    confidence=round(conf, 2),
                    person_name=person.name,
                )
            )
        return hits
