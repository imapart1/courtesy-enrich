"""'sheet' pseudo-provider: answers from what the spreadsheet already taught us.

Free and instant. Uses known_emails + domain_patterns learned at `enrich pull` time.
"""

from __future__ import annotations

from ..models import Company
from ..patterns import candidate_locals, split_name
from .base import EmailHit, PatternHit, PersonHit, ProviderBase, register


@register
class Sheet(ProviderBase):
    name = "sheet"
    is_free = True
    rps = 1000.0

    async def identify(self, company: Company, role: str) -> list[PersonHit]:
        # The sheet stores emails, not titles — nothing reliable to say about *who* holds a role.
        return []

    async def domain_pattern(self, company: Company) -> PatternHit | None:
        """Best learned pattern (or shape-only knowledge) for the company's domain.

        A shape-only record (empty ``pattern``, non-empty ``shape``) returns
        ``pattern=''`` with the stored *shape* confidence passed through UNCHANGED — it
        must never be inflated into, or read downstream as, a proven ``{first}.{last}``
        convention. This is safe because stage1 seeds ``c.pattern=''`` from such a hit,
        and pipeline._pattern_proven only treats ``c.pattern_confidence`` as proof when
        ``c.pattern`` is non-empty; the empty pattern falls back to shape agreement
        instead. permute still reads the shape itself via store.get_domain_pattern.
        """
        for domain in filter(None, {company.email_domain, company.domain}):
            rec = self.ctx.store.get_domain_pattern(domain)
            if rec and (rec["pattern"] or rec["shape"]):
                return PatternHit(
                    pattern=rec["pattern"] or "",
                    confidence=float(rec["confidence"]),
                    source=f"sheet:{rec['source']}" if rec["source"] else "sheet",
                    sample_size=int(rec["sample_size"] or 0),
                )
        return None

    async def find_email(self, company: Company, person: PersonHit) -> list[EmailHit]:
        """If a known email for this domain matches the person's name under any pattern, reuse it."""
        domain = company.email_domain or company.domain
        if not domain:
            return []
        known = set(self.ctx.store.known_emails(domain))
        if not known:
            return []
        first, last = split_name(person.name)
        if not first:
            return []
        rec = self.ctx.store.get_domain_pattern(domain) or {}
        locals_ = candidate_locals(first, last, pattern=rec.get("pattern", ""), shape=rec.get("shape", ""), limit=8)
        hits = []
        for local in locals_:
            email = f"{local}@{domain}"
            if email in known:
                hits.append(
                    EmailHit(email=email, method="found", source="sheet", confidence=0.95, person_name=person.name)
                )
        return hits
