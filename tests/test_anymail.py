"""Anymail Finder adapter tests — respx-mocked HTTP, no network."""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from enrich.models import Company
from enrich.net import HttpError
from enrich.providers.anymail import BASE_URL, COST_PER_CREDIT_USD, Anymail
from enrich.providers.base import PersonHit

PERSON_URL = f"{BASE_URL}/find-email/person"
DM_URL = f"{BASE_URL}/find-email/decision-maker"
COMPANY_URL = f"{BASE_URL}/find-email/company"
ACCOUNT_URL = f"{BASE_URL}/account"

VALID_PERSON = {
    "credits_charged": 1,
    "email": "jane.smith@acme.com",
    "email_status": "valid",
    "mx_domain": "outlook.com",
    "mx_host": "acme-com.mail.protection.outlook.com",
    "valid_email": "jane.smith@acme.com",
}

VALID_DM = {
    "credits_charged": 2,
    "decision_maker_category": "ceo",
    "email": "jane.smith@acme.com",
    "email_status": "valid",
    "person_first_name": "Jane",
    "person_full_name": "Jane Smith",
    "person_job_title": "Chief Executive Officer",
    "person_last_name": "Smith",
    "person_linkedin_url": "https://www.linkedin.com/in/janesmith/",
    "valid_email": "jane.smith@acme.com",
}


def _company() -> Company:
    return Company(key="manual:acme", name="Acme", website="acme.com",
                   domain="acme.com", email_domain="acme.com")


def _person(name: str = "Jane Smith", role: str = "ceo", **kw) -> PersonHit:
    return PersonHit(name=name, title="CEO", role=role, source="anymail", **kw)


def _spend(ctx) -> dict | None:
    rows = [r for r in ctx.store.spend_by_provider(ctx.run_id) if r["provider"] == "anymail"]
    return rows[0] if rows else None


@pytest.fixture()
def provider(ctx, monkeypatch) -> Anymail:
    monkeypatch.setenv("ANYMAILFINDER_API_KEY", "test-key")
    monkeypatch.delenv("ANYMAIL_COST_PER_CREDIT", raising=False)
    monkeypatch.setattr(Anymail, "rps", 1000.0)  # don't sleep between test calls
    ctx.flags.free_only = False
    return Anymail(ctx)


# ------------------------------------------------------------------ available
def test_available_without_key(ctx, monkeypatch):
    monkeypatch.delenv("ANYMAILFINDER_API_KEY", raising=False)
    ctx.flags.free_only = False
    ok, why = Anymail(ctx).available()
    assert ok is False and "ANYMAILFINDER_API_KEY" in why


def test_available_excluded_on_free_only(ctx, monkeypatch):
    monkeypatch.setenv("ANYMAILFINDER_API_KEY", "test-key")
    ctx.flags.free_only = True
    ok, why = Anymail(ctx).available()
    assert ok is False and "free-only" in why


# ------------------------------------------------------------------ find_email
@respx.mock
async def test_find_email_valid(ctx, provider):
    route = respx.post(PERSON_URL).mock(return_value=Response(200, json=VALID_PERSON))
    async with ctx:
        hits = await provider.find_email(_company(), _person())
    assert len(hits) == 1
    h = hits[0]
    assert h.email == "jane.smith@acme.com"
    assert h.method == "found" and h.source == "anymail"
    assert h.verify_status == "deliverable"  # live-verified: pipeline skips re-verification
    assert h.confidence >= 0.9 and h.person_name == "Jane Smith"
    req = route.calls.last.request
    assert req.headers["Authorization"] == "test-key"  # raw key, no Bearer prefix
    assert json.loads(req.content) == {"domain": "acme.com", "full_name": "Jane Smith"}
    row = _spend(ctx)
    assert row["calls"] == 1 and row["credits"] == 1
    assert row["usd"] == pytest.approx(COST_PER_CREDIT_USD)


@respx.mock
async def test_find_email_miss_is_free(ctx, provider):
    respx.post(PERSON_URL).mock(return_value=Response(200, json={
        "credits_charged": 0, "email": None, "email_status": "not_found",
        "mx_domain": None, "mx_host": None, "valid_email": None,
    }))
    async with ctx:
        hits = await provider.find_email(_company(), _person())
    assert hits == []
    row = _spend(ctx)  # pre-charged worst case, then rewritten to the actual 0
    assert row["calls"] == 1 and row["usd"] == 0.0 and row["credits"] == 0


@respx.mock
async def test_find_email_risky_maps_to_unknown(ctx, provider):
    respx.post(PERSON_URL).mock(return_value=Response(200, json={
        "credits_charged": 0, "email": "jane.smith@acme.com", "email_status": "risky",
        "valid_email": None,
    }))
    async with ctx:
        hits = await provider.find_email(_company(), _person())
    assert len(hits) == 1
    assert hits[0].verify_status == "unknown" and hits[0].confidence < 0.9
    assert _spend(ctx)["usd"] == 0.0  # risky results are not charged


@respx.mock
async def test_find_email_http_error_propagates(ctx, provider):
    respx.post(PERSON_URL).mock(return_value=Response(401, json={
        "error": "unauthorized", "message": "Missing or invalid API key."}))
    async with ctx:
        with pytest.raises(HttpError) as ei:
            await provider.find_email(_company(), _person())
    assert ei.value.status == 401
    assert _spend(ctx) is None  # nothing cached or charged on failure


@respx.mock
async def test_find_email_second_call_hits_cache(ctx, provider):
    route = respx.post(PERSON_URL).mock(return_value=Response(200, json=VALID_PERSON))
    async with ctx:
        await provider.find_email(_company(), _person())
        hits = await provider.find_email(_company(), _person())
    assert route.call_count == 1 and hits[0].email == "jane.smith@acme.com"
    assert _spend(ctx)["credits"] == 1  # charged once


@respx.mock
async def test_cost_env_override(ctx, provider, monkeypatch):
    monkeypatch.setenv("ANYMAIL_COST_PER_CREDIT", "0")  # free-tier/trial credits
    respx.post(PERSON_URL).mock(return_value=Response(200, json=VALID_PERSON))
    async with ctx:
        hits = await provider.find_email(_company(), _person())
    assert hits and _spend(ctx)["usd"] == 0.0 and _spend(ctx)["credits"] == 1


# -------------------------------------------------------------------- identify
@respx.mock
async def test_identify_ceo(ctx, provider):
    route = respx.post(DM_URL).mock(return_value=Response(200, json=VALID_DM))
    async with ctx:
        hits = await provider.identify(_company(), "ceo")
    assert len(hits) == 1
    h = hits[0]
    assert h.name == "Jane Smith" and h.role == "ceo"
    assert h.title == "Chief Executive Officer"
    assert h.url.endswith("/janesmith/")
    assert h.confidence >= 0.85  # source quality x strongest title match
    assert h.raw["email"] == "jane.smith@acme.com"  # kept for find_email reuse
    body = json.loads(route.calls.last.request.content)
    assert body == {"domain": "acme.com", "decision_maker_category": ["ceo"]}
    row = _spend(ctx)
    assert row["credits"] == 2 and row["usd"] == pytest.approx(2 * COST_PER_CREDIT_USD)


@respx.mock
async def test_identify_role_mapping_coo(ctx, provider):
    route = respx.post(DM_URL).mock(return_value=Response(200, json={
        **VALID_DM, "decision_maker_category": "operations",
        "person_job_title": "Chief Operating Officer",
    }))
    async with ctx:
        hits = await provider.identify(_company(), "coo")
    assert hits and hits[0].role == "coo"
    body = json.loads(route.calls.last.request.content)
    assert body["decision_maker_category"] == ["operations"]  # coo -> operations


@respx.mock
async def test_identify_unmapped_role_makes_no_call(ctx, provider):
    route = respx.post(DM_URL).mock(return_value=Response(200, json=VALID_DM))
    async with ctx:
        # Anymail has no built-in legal/privacy category (verified 2026-08-19)
        assert await provider.identify(_company(), "legal") == []
        assert await provider.identify(_company(), "privacy") == []
    assert route.call_count == 0 and _spend(ctx) is None


@respx.mock
async def test_identify_custom_category_via_env(ctx, provider, monkeypatch):
    monkeypatch.setenv("ANYMAIL_CATEGORY_LEGAL", "legal")  # account-defined category
    route = respx.post(DM_URL).mock(return_value=Response(200, json={
        **VALID_DM, "decision_maker_category": "legal",
        "person_job_title": "General Counsel",
    }))
    async with ctx:
        hits = await provider.identify(_company(), "legal")
    assert hits and hits[0].role == "legal" and hits[0].title == "General Counsel"
    body = json.loads(route.calls.last.request.content)
    assert body["decision_maker_category"] == ["legal"]


@respx.mock
async def test_identify_filters_title_mismatch(ctx, provider):
    respx.post(DM_URL).mock(return_value=Response(200, json={
        **VALID_DM, "person_job_title": "Marketing Manager",
    }))
    async with ctx:
        assert await provider.identify(_company(), "ceo") == []
    assert _spend(ctx)["credits"] == 2  # we still paid for the charged search


@respx.mock
async def test_identify_not_found(ctx, provider):
    respx.post(DM_URL).mock(return_value=Response(200, json={
        "credits_charged": 0, "email": None, "email_status": "not_found",
        "person_full_name": None, "person_job_title": None,
    }))
    async with ctx:
        assert await provider.identify(_company(), "ceo") == []
    assert _spend(ctx)["usd"] == 0.0


# ------------------------------------------- identify -> find_email reuse paths
@respx.mock
async def test_find_email_reuses_identify_raw(ctx, provider):
    respx.post(DM_URL).mock(return_value=Response(200, json=VALID_DM))
    person_route = respx.post(PERSON_URL).mock(return_value=Response(200, json=VALID_PERSON))
    async with ctx:
        [person] = await provider.identify(_company(), "ceo")
        hits = await provider.find_email(_company(), person)
    assert person_route.call_count == 0  # email came from PersonHit.raw, zero cost
    assert hits[0].email == "jane.smith@acme.com" and hits[0].verify_status == "deliverable"
    assert _spend(ctx)["credits"] == 2  # only the decision-maker charge


@respx.mock
async def test_find_email_reuses_cached_decision_maker_without_raw(ctx, provider):
    dm_route = respx.post(DM_URL).mock(return_value=Response(200, json=VALID_DM))
    person_route = respx.post(PERSON_URL).mock(return_value=Response(200, json=VALID_PERSON))
    async with ctx:
        await provider.identify(_company(), "ceo")
        # the pipeline's _to_hit() drops raw — the SQLite cache row still answers
        bare = PersonHit(name="Jane Smith", title="CEO", role="ceo", source="anymail")
        hits = await provider.find_email(_company(), bare)
    assert dm_route.call_count == 1 and person_route.call_count == 0
    assert hits[0].email == "jane.smith@acme.com"


@respx.mock
async def test_find_email_falls_through_when_cached_person_differs(ctx, provider):
    respx.post(DM_URL).mock(return_value=Response(200, json=VALID_DM))
    person_route = respx.post(PERSON_URL).mock(return_value=Response(200, json={
        **VALID_PERSON, "email": "bob.jones@acme.com", "valid_email": "bob.jones@acme.com",
    }))
    async with ctx:
        await provider.identify(_company(), "ceo")
        hits = await provider.find_email(_company(), _person(name="Bob Jones"))
    assert person_route.call_count == 1  # cached CEO is a different person
    assert hits[0].email == "bob.jones@acme.com"


# -------------------------------------------------------------- domain_pattern
@respx.mock
async def test_domain_pattern_learns_shape(ctx, provider):
    route = respx.post(COMPANY_URL).mock(return_value=Response(200, json={
        "credits_charged": 1,
        "email_status": "valid",
        "emails": ["susan.smith@acme.com", "john.doe@acme.com", "info@acme.com",
                   "jane@other.com"],
        "valid_emails": ["susan.smith@acme.com", "john.doe@acme.com"],
        "mx_domain": "outlook.com",
        "mx_host": "acme-com.mail.protection.outlook.com",
    }))
    async with ctx:
        hit = await provider.domain_pattern(_company())
    assert hit is not None
    assert hit.pattern == "{first}.{last}"  # generic + off-domain locals excluded
    assert hit.source == "anymail" and hit.sample_size == 2
    assert 0.0 < hit.confidence <= 0.85
    assert json.loads(route.calls.last.request.content) == {"domain": "acme.com"}
    row = _spend(ctx)
    assert row["credits"] == 1 and row["usd"] == pytest.approx(COST_PER_CREDIT_USD)


@respx.mock
async def test_domain_pattern_exact_from_named_entries(ctx, provider):
    respx.post(COMPANY_URL).mock(return_value=Response(200, json={
        "credits_charged": 1, "email_status": "valid",
        "emails": [{"email": "jsmith@acme.com", "full_name": "Jane Smith"},
                   {"email": "bdoe@acme.com", "full_name": "Bob Doe"}],
    }))
    async with ctx:
        hit = await provider.domain_pattern(_company())
    assert hit is not None and hit.pattern == "{f}{last}" and hit.sample_size == 2


@respx.mock
async def test_domain_pattern_not_found(ctx, provider):
    respx.post(COMPANY_URL).mock(return_value=Response(200, json={
        "credits_charged": 0, "email_status": "not_found", "emails": [], "valid_emails": [],
    }))
    async with ctx:
        assert await provider.domain_pattern(_company()) is None
    assert _spend(ctx)["usd"] == 0.0


# ---------------------------------------------------------------------- doctor
@respx.mock
async def test_doctor(ctx, provider):
    route = respx.get(ACCOUNT_URL).mock(return_value=Response(200, json={
        "credits_left": 1850, "email": "ops@schallertpc.com"}))
    async with ctx:
        out = await provider.doctor()
    assert out == "ok - 1,850 credits left"
    assert route.calls.last.request.headers["Authorization"] == "test-key"
    row = _spend(ctx)
    assert row["usd"] == 0.0  # account endpoint is free
