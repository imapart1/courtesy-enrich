"""Waterfall behavior with fake providers — no network."""

import pytest

from enrich.context import BudgetExceeded
from enrich.models import Company, assign_tier
from enrich.pipeline import Waterfalls, enrich_company
from enrich.providers.base import EmailHit, PersonHit, ProviderBase, VerifyResult


class FakeIdentify(ProviderBase):
    name = "fake_identify"
    is_free = True

    async def identify(self, company, role):
        if role == "ceo":
            return [PersonHit(name="Jane Smith", title="CEO", role="ceo", source=self.name, confidence=0.9)]
        return []


class FakeEmail(ProviderBase):
    name = "fake_email"
    is_free = True

    async def find_email(self, company, person):
        return [EmailHit(email=f"jane@{company.email_domain}", method="found", source=self.name, confidence=0.9)]


class FakeVerify(ProviderBase):
    name = "fake_verify"
    is_free = True
    calls: list[str] = []

    async def verify(self, email):
        FakeVerify.calls.append(email)
        return VerifyResult(email=email, status="deliverable", source=self.name)


@pytest.fixture()
def wf(ctx, monkeypatch):
    monkeypatch.setitem(ctx.settings.cfg["stages"], "identify", ["fake_identify"])
    monkeypatch.setitem(ctx.settings.cfg["stages"], "pattern", [])
    monkeypatch.setitem(ctx.settings.cfg["stages"], "email", ["fake_email"])
    monkeypatch.setitem(ctx.settings.cfg["stages"], "verify", ["fake_verify"])
    from enrich.providers.base import REGISTRY
    for cls in (FakeIdentify, FakeEmail, FakeVerify):
        REGISTRY[cls.name] = cls
    return Waterfalls(ctx)


async def test_happy_path(ctx, wf, monkeypatch):
    ctx.flags.verify = True
    # avoid real DNS in tests
    import enrich.pipeline as pl
    async def fake_mx(domain):
        return True, "mx.test"
    monkeypatch.setattr(pl, "mx_domain", fake_mx)

    c = Company(key="manual:acme", name="Acme", website="acme.com")
    res = await enrich_company(ctx, wf, c)
    assert c.status == "enriched"
    assert any(p.role == "ceo" and p.name == "Jane Smith" for p in res.people)
    ceo = [e for e in res.emails if e.role == "ceo"]
    assert ceo and ceo[0].tier == "A" and ceo[0].email == "jane@acme.com"
    # generic fallback filled the legal role at tier A (fake verifier says deliverable)
    legal = [e for e in res.emails if e.role == "legal"]
    assert legal and legal[0].method == "generic"
    # persisted
    assert ctx.store.get_company("manual:acme").status == "enriched"


async def test_blocklisted_email_skipped(ctx, wf, monkeypatch):
    import enrich.pipeline as pl
    async def fake_mx(domain):
        return True, "mx.test"
    monkeypatch.setattr(pl, "mx_domain", fake_mx)
    ctx.store.block_email("jane@acme.com", "bounced")
    c = Company(key="manual:acme2", name="Acme", website="acme.com")
    res = await enrich_company(ctx, wf, c)
    assert not [e for e in res.emails if e.email == "jane@acme.com"]


def test_assign_tier_matrix():
    def t(**kw):
        return assign_tier(**{"method": "found", "verify_status": "unknown",
                              "found_confidence": 0.5, "pattern_proven": False, **kw})
    assert t(verify_status="deliverable") == "A"
    assert t(verify_status="undeliverable") is None
    assert t(verify_status="catch_all", pattern_proven=True) == "B"
    assert t(verify_status="catch_all", method="found", found_confidence=0.9) == "B"
    assert t(verify_status="catch_all", method="pattern", found_confidence=0.3) == "C"
    assert t(method="generic", verify_status="catch_all") == "D"
    assert t(method="generic", verify_status="deliverable") == "A"


async def test_budget_exhaustion_disables_paid(ctx, wf, monkeypatch):
    import enrich.pipeline as pl
    async def fake_mx(domain):
        return True, "mx.test"
    monkeypatch.setattr(pl, "mx_domain", fake_mx)

    class PaidBoom(ProviderBase):
        name = "paid_boom"
        is_free = False
        async def identify(self, company, role):
            raise BudgetExceeded("cap")
    from enrich.providers.base import REGISTRY
    REGISTRY["paid_boom"] = PaidBoom
    ctx.flags.free_only = False
    monkeypatch.setitem(ctx.settings.cfg["stages"], "identify", ["paid_boom", "fake_identify"])
    wf2 = Waterfalls(ctx)
    c = Company(key="manual:b1", name="B1", website="b1.com")
    await enrich_company(ctx, wf2, c)
    assert wf2._paid_disabled is True
    # free provider still ran
    assert ctx.store.people_for("manual:b1", "ceo")
