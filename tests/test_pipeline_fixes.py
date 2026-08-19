"""Regression tests for reviewer-found pipeline defects (fixed in the hardening pass)."""

import pytest

from enrich.context import Budget, BudgetExceeded
from enrich.models import Company, PersonRec
from enrich.patterns import candidate_pairs
from enrich.pipeline import Waterfalls, _pattern_proven, _to_hit, stage2_identify
from enrich.providers.base import EmailHit, PersonHit, ProviderBase, VerifyResult


# ---- title score is NOT double-counted; best-of-N kept per role ---------------
class MultiIdentify(ProviderBase):
    name = "multi_identify"
    is_free = True

    async def identify(self, company, role):
        if role != "legal":
            return []
        # provider already folds title fit into confidence (contract)
        return [
            PersonHit(name="Al Counsel", title="Corporate Counsel", role="legal",
                      source=self.name, confidence=0.72),
            PersonHit(name="Bea Boss", title="General Counsel", role="legal",
                      source=self.name, confidence=0.88),
        ]


@pytest.fixture()
def wf_multi(ctx, monkeypatch):
    monkeypatch.setitem(ctx.settings.cfg["stages"], "identify", ["multi_identify"])
    monkeypatch.setitem(ctx.settings.cfg["stages"], "pattern", [])
    monkeypatch.setitem(ctx.settings.cfg["stages"], "email", [])
    monkeypatch.setitem(ctx.settings.cfg["stages"], "verify", [])
    from enrich.providers.base import REGISTRY
    REGISTRY[MultiIdentify.name] = MultiIdentify
    return Waterfalls(ctx)


async def test_title_not_double_penalized_and_topk(ctx, wf_multi):
    c = Company(key="k1", name="Acme", website="acme.com", email_domain="acme.com")
    people = await stage2_identify(ctx, wf_multi, c, [])
    names = {p.name for p in people}
    # both survive: 0.72 corporate-counsel would have been killed by the old double-multiply
    assert "Al Counsel" in names and "Bea Boss" in names
    assert people[0].name == "Bea Boss"  # sorted best-first
    assert all(p.confidence >= 0.6 for p in people)


# ---- _pattern_proven requires the candidate to match the known pattern --------
def test_pattern_proven_requires_matching_local(ctx):
    ctx.store.save_domain_pattern("x.com", pattern="{first}.{last}", confidence=0.9, source="hunter", sample_size=20)
    c = Company(key="k", name="X", domain="x.com", email_domain="x.com",
                pattern="{first}.{last}", pattern_confidence=0.9)
    # a contradicting guess is NOT proven, even though the domain has a strong pattern
    assert _pattern_proven(ctx, c, "jane@x.com", "jane", "smith") is False
    assert _pattern_proven(ctx, c, "jane.smith@x.com", "jane", "smith") is True


def test_pattern_proven_shape_agreement_without_explicit_pattern(ctx):
    ctx.store.save_known_email("y.com", "mike.sutterer@y.com", "k")
    c = Company(key="k", name="Y", domain="y.com", email_domain="y.com")
    assert _pattern_proven(ctx, c, "al.cheatham@y.com", "al", "cheatham") is True
    assert _pattern_proven(ctx, c, "al@y.com", "al", "cheatham") is False


# ---- permute labels fallback guesses as 'constructed', not 'pattern' ----------
def test_candidate_pairs_marks_informed():
    pairs = candidate_pairs("Jane", "Smith", pattern="{f}{last}", limit=3)
    assert pairs[0] == ("jsmith", "{f}{last}", True)
    # one-token name on a {last}-requiring pattern falls back to generic {first} (uninformed)
    pairs = candidate_pairs("Jane", "", pattern="{first}.{last}", limit=2)
    assert all(local == "jane" or not informed for local, _p, informed in pairs)


# ---- PersonRec.raw round-trips through _to_hit --------------------------------
def test_person_raw_roundtrip(ctx):
    p = PersonRec(company_key="k", role="ceo", name="Jane", title="CEO",
                  confidence=0.8, raw={"apollo_id": "abc123"})
    ctx.store.save_person(p)
    got = ctx.store.people_for("k", "ceo")[0]
    assert got.raw == {"apollo_id": "abc123"}
    hit = _to_hit(got)
    assert hit.raw["apollo_id"] == "abc123"


# ---- budget reservation prevents concurrent over-spend ------------------------
def test_budget_reservation(store):
    b = Budget(store, "run1", limit_usd=1.0)
    b.reserve(0.6)
    assert b.reserved == 0.6
    with pytest.raises(BudgetExceeded):
        b.reserve(0.6)  # 0.6 + 0.6 > 1.0
    b.release(0.6)
    b.reserve(0.3)  # now fits
    assert b.reserved == 0.3


# ---- provider 'unknown' status is re-verified, not trusted --------------------
class UnknownFinder(ProviderBase):
    name = "unknown_finder"
    is_free = True

    async def find_email(self, company, person):
        return [EmailHit(email=f"jane@{company.email_domain}", method="found",
                         source=self.name, confidence=0.8, verify_status="unknown")]


class RealVerify(ProviderBase):
    name = "real_verify"
    is_free = True
    seen: list[str] = []

    async def verify(self, email):
        RealVerify.seen.append(email)
        return VerifyResult(email=email, status="deliverable", source=self.name)


async def test_unknown_status_triggers_verifier(ctx, monkeypatch):
    import enrich.pipeline as pl

    async def fake_mx(domain):
        return True, "mx"
    monkeypatch.setattr(pl, "mx_domain", fake_mx)
    monkeypatch.setitem(ctx.settings.cfg["stages"], "identify", [])
    monkeypatch.setitem(ctx.settings.cfg["stages"], "pattern", [])
    monkeypatch.setitem(ctx.settings.cfg["stages"], "email", ["unknown_finder"])
    monkeypatch.setitem(ctx.settings.cfg["stages"], "verify", ["real_verify"])
    from enrich.providers.base import REGISTRY
    REGISTRY[UnknownFinder.name] = UnknownFinder
    REGISTRY[RealVerify.name] = RealVerify
    RealVerify.seen.clear()
    ctx.flags.verify = True

    wf = Waterfalls(ctx)
    c = Company(key="k", name="Acme", website="acme.com", email_domain="acme.com")
    # seed a person so stage3 has someone to find an email for
    ctx.store.save_person(PersonRec(company_key="k", role="ceo", name="Jane Doe", confidence=0.8))
    from enrich.pipeline import stage3_emails
    emails = await stage3_emails(ctx, wf, c, ctx.store.people_for("k"), [])
    assert "jane@acme.com" in RealVerify.seen  # 'unknown' was re-verified, not trusted
    assert any(e.verify_status == "deliverable" for e in emails)
