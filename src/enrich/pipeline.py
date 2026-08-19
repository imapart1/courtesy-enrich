"""Stage orchestration: waterfalls per SPEC §3. Cheapest-first, stop on confident answer."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .context import BudgetExceeded, Ctx
from .domains import mx_domain, normalize_domain
from .models import Company, EmailRec, PersonRec, assign_tier, title_matches_role, utcnow
from .patterns import classify_shape, detect_pattern, split_name
from .providers import get_providers
from .providers.base import EmailHit, PersonHit, ProviderBase, VerifyResult

log = logging.getLogger(__name__)


@dataclass
class CompanyResult:
    company: Company
    people: list[PersonRec] = field(default_factory=list)
    emails: list[EmailRec] = field(default_factory=list)
    provider_notes: list[str] = field(default_factory=list)


class Waterfalls:
    """Providers instantiated once per run, grouped by stage."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.skipped: dict[str, list[tuple[str, str]]] = {}
        s = ctx.settings
        identify = s.stage("identify")
        if not ctx.flags.llm and "llm_researcher" in identify:
            identify = [p for p in identify if p != "llm_researcher"]
        self.identify, self.skipped["identify"] = get_providers(identify, ctx)
        self.pattern, self.skipped["pattern"] = get_providers(s.stage("pattern"), ctx)
        self.email, self.skipped["email"] = get_providers(s.stage("email"), ctx)
        self.verify, self.skipped["verify"] = get_providers(s.stage("verify"), ctx)
        self._paid_disabled = False

    def usable(self, providers: list[ProviderBase]) -> list[ProviderBase]:
        if self._paid_disabled:
            return [p for p in providers if p.is_free]
        return providers

    def disable_paid(self, why: str) -> None:
        if not self._paid_disabled:
            log.warning("paid providers disabled for rest of run: %s", why)
            self._paid_disabled = True


async def _safe(coro, provider: ProviderBase, wf: Waterfalls, notes: list[str], default):
    """Run one provider call; budget exhaustion disables paid providers, errors are recorded."""
    try:
        return await coro
    except BudgetExceeded as e:
        wf.disable_paid(str(e))
        notes.append(f"{provider.name}: budget cap hit")
        return default
    except Exception as e:  # noqa: BLE001 - a flaky provider must not kill the company
        # log the exception class/status only — HttpError bodies can echo a submitted key
        detail = type(e).__name__
        status = getattr(e, "status", None)
        if status:
            detail += f"({status})"
        log.warning("%s failed: %s", provider.name, detail)
        notes.append(f"{provider.name}: {detail}")
        return default


# --------------------------------------------------------------------- stages
async def stage1_resolve(ctx: Ctx, wf: Waterfalls, c: Company, notes: list[str]) -> None:
    if not c.domain:
        c.domain = normalize_domain(c.website or c.name)
    if not c.domain:
        # name-only intake: ask an identify provider that can resolve a website
        for p in wf.usable(wf.identify):
            if not hasattr(p, "resolve_domain"):
                continue
            got = await _safe(p.resolve_domain(c), p, wf, notes, "")
            if got:
                c.domain = normalize_domain(got)
                c.website = c.website or got
                notes.append(f"domain resolved via {p.name}")
                break
    if not c.domain:
        c.status = "anomaly"
        c.error = "no usable website/domain (name-only intake unresolved)"
        return
    # emails already known for this domain? adopt their domain as email_domain
    if not c.email_domain:
        known = ctx.store.known_emails(c.domain)
        c.email_domain = known[0].split("@")[1] if known else c.domain
    has_mx, mail_host = await mx_domain(c.email_domain)
    if not has_mx and c.email_domain != c.domain:
        has_mx, mail_host = await mx_domain(c.domain)
        if has_mx:
            c.email_domain = c.domain
    if not has_mx:
        notes.append(f"no MX on {c.email_domain}")
    # company info (entity, parent, size, alternate email domain)
    for p in wf.usable(wf.identify):
        if not hasattr(p, "company_info"):
            continue
        info = await _safe(p.company_info(c), p, wf, notes, None)
        if not info:
            continue
        c.entity_name = c.entity_name or info.entity_name
        c.parent_company = c.parent_company or info.parent_company
        if info.email_domain and not ctx.store.known_emails(c.email_domain):
            c.email_domain = info.email_domain
        if info.employee_count:
            c.size_flag = c.size_flag or f"~{info.employee_count} employees"
        if info.revenue_hint:
            c.size_flag = (c.size_flag + f" {info.revenue_hint}").strip()
        break
    # best-known pattern
    for p in wf.usable(wf.pattern):
        if not hasattr(p, "domain_pattern"):
            continue
        hit = await _safe(p.domain_pattern(c), p, wf, notes, None)
        if hit and hit.confidence > c.pattern_confidence:
            c.pattern, c.pattern_confidence = hit.pattern, round(hit.confidence, 2)
            # only persist a genuinely new pattern; store.save merges and never clobbers
            # the sheet-learned shape, but re-saving a sheet hit's empty pattern is a pure no-op
            if not hit.source.startswith("sheet"):
                ctx.store.save_domain_pattern(
                    c.email_domain, pattern=hit.pattern, confidence=hit.confidence,
                    source=hit.source, sample_size=hit.sample_size,
                )
        # only an explicit, non-empty pattern (e.g. Hunter's) is confident enough to stop early
        if c.pattern and c.pattern_confidence >= 0.8:
            break


async def stage2_identify(ctx: Ctx, wf: Waterfalls, c: Company, notes: list[str]) -> list[PersonRec]:
    threshold = float(ctx.settings.thresholds["identity_confidence"])
    per_role = int(ctx.settings.thresholds.get("people_per_role", 2))
    out: list[PersonRec] = []
    for role in ctx.settings.roles:
        # Collect candidates across the whole waterfall, keep the best `per_role`.
        # PersonHit.confidence already folds in title fit (base.py contract) — do NOT
        # multiply title_score in again; use it only as a role gate.
        cands: dict[str, PersonRec] = {}
        for p in wf.usable(wf.identify):
            if not hasattr(p, "identify"):
                continue
            hits: list[PersonHit] = await _safe(p.identify(c, role), p, wf, notes, [])
            for h in hits:
                if not h.name.strip():
                    continue
                title_score = title_matches_role(h.title, role)
                if title_score == 0.0 and h.role != role:
                    continue  # provider offered someone whose title doesn't fit this role
                conf = round(min(1.0, h.confidence), 2)
                if conf < threshold:
                    continue
                key = h.name.strip().lower()
                rec = PersonRec(
                    company_key=c.key, role=role, name=h.name.strip(), title=h.title.strip(),
                    provider=p.name, url=h.url, confidence=conf, raw=h.raw or {},
                )
                if key not in cands or conf > cands[key].confidence:
                    cands[key] = rec
            # stop early once we have a strong, unambiguous pick from an early provider
            top = sorted(cands.values(), key=lambda r: -r.confidence)
            if top and top[0].confidence >= 0.85 and len(cands) >= per_role:
                break
        out.extend(sorted(cands.values(), key=lambda r: -r.confidence)[:per_role])
    return out


def _pattern_proven(ctx: Ctx, c: Company, email: str, first: str = "", last: str = "") -> bool:
    """True only if THIS email's local part matches the domain's proven convention —
    not merely because the domain has a high-confidence pattern on file."""
    threshold = float(ctx.settings.thresholds["pattern_proven"])
    local, _, domain = email.partition("@")
    # explicit pattern known (e.g. Hunter): the candidate must actually match it
    if c.pattern and c.pattern_confidence >= threshold and first:
        return detect_pattern(local, first, last) == c.pattern
    # otherwise fall back to shape agreement with known emails on the domain
    known = ctx.store.known_emails(domain)
    if known:
        shapes = {classify_shape(k.split("@")[0]) for k in known}
        return classify_shape(local) in shapes
    return False


async def _verify_email(ctx: Ctx, wf: Waterfalls, email: str, notes: list[str]) -> VerifyResult:
    if not ctx.flags.verify:
        return VerifyResult(email=email, status="unknown", source="skipped")
    for p in wf.usable(wf.verify):
        if not hasattr(p, "verify"):
            continue
        res = await _safe(p.verify(email), p, wf, notes, None)
        if res and res.status != "error":
            return res
    return VerifyResult(email=email, status="unknown", source="none")


async def stage3_emails(
    ctx: Ctx, wf: Waterfalls, c: Company, people: list[PersonRec], notes: list[str]
) -> list[EmailRec]:
    out: list[EmailRec] = []
    seen: set[str] = set()

    cap = int(ctx.settings.thresholds.get("max_emails_per_company", 8))

    async def consider(hit: EmailHit, role: str, person_name: str, provider: str) -> str | None:
        email = hit.email.lower().strip()
        if email in seen or ctx.store.is_blocked(email):
            return None
        if len(out) >= cap:
            return None
        seen.add(email)
        vs = hit.verify_status
        # a provider "unknown" is unresolved — send it through the verifier waterfall too
        conclusive = ("deliverable", "catch_all", "undeliverable")
        vp = provider if vs in conclusive else ""
        if vs not in conclusive:
            res = await _verify_email(ctx, wf, email, notes)
            vs, vp = res.status, res.source
        if vs == "catch_all":
            c.catch_all = True
        elif vs == "deliverable" and c.catch_all is None:
            c.catch_all = False
        pf, pl = split_name(person_name)
        tier = assign_tier(
            method=hit.method, verify_status=vs, found_confidence=hit.confidence,
            pattern_proven=_pattern_proven(ctx, c, email, pf, pl),
        )
        out.append(
            EmailRec(
                company_key=c.key, role=role, person_name=person_name, email=email,
                method=hit.method, provider=provider, verify_status=vs, verify_provider=vp,
                tier=tier or "", confidence=hit.confidence, run_id=ctx.run_id,
            )
        )
        return tier

    for person in people:
        best = None
        for p in wf.usable(wf.email):
            if not hasattr(p, "find_email"):
                continue
            hits: list[EmailHit] = await _safe(p.find_email(c, _to_hit(person)), p, wf, notes, [])
            for h in hits:
                tier = await consider(h, person.role, person.name, p.name)
                if tier in ("A", "B"):
                    best = tier
                    break
            if best:
                break

    # tier-D generic backstops for unfilled fallback roles
    filled = {e.role for e in out if e.tier in ("A", "B", "C")}
    domain = c.email_domain or c.domain
    if domain:
        for role, locals_ in ctx.settings.generic_fallbacks.items():
            if role in filled:
                continue
            for local in locals_:
                tier = await consider(
                    EmailHit(email=f"{local}@{domain}", method="generic", source="fallback", confidence=0.3),
                    role, "", "fallback",
                )
                if tier:
                    break
    return out


def _to_hit(p: PersonRec) -> PersonHit:
    return PersonHit(
        name=p.name, title=p.title, role=p.role, source=p.provider, url=p.url,
        confidence=p.confidence, raw=p.raw or {},
    )


# ------------------------------------------------------------------ top level
async def enrich_company(ctx: Ctx, wf: Waterfalls, c: Company, *, persist: bool = True) -> CompanyResult:
    notes: list[str] = []
    res = CompanyResult(company=c, provider_notes=notes)
    await stage1_resolve(ctx, wf, c, notes)
    if c.status == "anomaly":
        if persist:
            ctx.store.upsert_company(c)
        return res
    res.people = await stage2_identify(ctx, wf, c, notes)
    res.emails = await stage3_emails(ctx, wf, c, res.people, notes)

    tiers = {e.tier for e in res.emails}
    if tiers & {"A", "B"}:
        c.status = "enriched"
    elif tiers & {"C", "D"}:
        c.status = "partial"
    else:
        c.status = "no_contacts"
    c.enriched_at = utcnow()
    if notes:
        c.notes = (c.notes + " | " if c.notes else "") + "; ".join(sorted(set(notes)))[:500]

    if persist:
        ctx.store.upsert_company(c)
        for person in res.people:
            ctx.store.save_person(person)
        for e in res.emails:
            ctx.store.save_email(e)
    return res


async def run_pipeline(
    ctx: Ctx, companies: list[Company], *, concurrency: int = 6, persist: bool = True, on_done=None,
) -> list[CompanyResult]:
    wf = Waterfalls(ctx)
    sem = asyncio.Semaphore(concurrency)
    results: list[CompanyResult] = []

    async def one(c: Company):
        async with sem:
            try:
                r = await enrich_company(ctx, wf, c, persist=persist)
            except Exception as e:  # noqa: BLE001 - one bad company must not kill the run
                log.error("enrich failed for %s: %s", c.name, e)
                c.status, c.error = "error", f"{type(e).__name__}: {e}"[:300]
                if persist:
                    try:
                        ctx.store.upsert_company(c)
                    except Exception:  # noqa: BLE001
                        pass
                r = CompanyResult(company=c)
            results.append(r)
            if on_done:
                on_done(r)

    await asyncio.gather(*(one(c) for c in companies), return_exceptions=True)
    return results
