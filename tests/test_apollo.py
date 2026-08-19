"""Apollo adapter: parse shapes, credit ledger, availability, doctor — all HTTP mocked."""

import httpx
import pytest
import respx

from enrich import net
from enrich.models import Company
from enrich.providers.apollo import BASE_URL, Apollo
from enrich.providers.base import PersonHit

SEARCH_URL = f"{BASE_URL}/mixed_people/api_search"
MATCH_URL = f"{BASE_URL}/people/match"
ORG_URL = f"{BASE_URL}/organizations/enrich"

SEARCH_JSON = {
    "total_entries": 3,
    # saved contacts still come back with full data
    "contacts": [
        {
            "id": "c1",
            "first_name": "Jane",
            "last_name": "Smith",
            "name": "Jane Smith",
            "title": "Chief Executive Officer",
            "linkedin_url": "https://linkedin.com/in/janesmith",
            "organization": {"name": "Acme"},
        }
    ],
    # net-new people: last name obfuscated, no linkedin_url (current API behavior)
    "people": [
        {
            "id": "p1",
            "first_name": "Andrew",
            "last_name_obfuscated": "Hu***n",
            "title": "CEO",
            "has_email": True,
            "organization": {"name": "Acme"},
        },
        {
            "id": "p2",
            "first_name": "Bob",
            "last_name_obfuscated": "Jo***s",
            "title": "Sales Manager",
            "has_email": True,
            "organization": {"name": "Acme"},
        },
    ],
}


@pytest.fixture()
def provider(ctx, monkeypatch) -> Apollo:
    ctx.flags.free_only = False
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    return Apollo(ctx)


@pytest.fixture()
def company() -> Company:
    return Company(key="manual:acme", name="Acme", website="acme.com",
                   domain="acme.com", email_domain="acme.com")


def _spend(ctx):
    rows = ctx.store.spend_by_provider(ctx.run_id)
    return {r["provider"]: r for r in rows}


# ---------------------------------------------------------------- identify

@respx.mock
async def test_identify_parses_and_filters_role(ctx, provider, company):
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=SEARCH_JSON))
    async with ctx:
        hits = await provider.identify(company, "ceo")

    # Sales Manager filtered out; results sorted by confidence
    assert [h.name for h in hits] == ["Jane Smith", "Andrew"]
    jane, andrew = hits
    assert all(h.role == "ceo" and h.source == "apollo" for h in hits)
    assert jane.url == "https://linkedin.com/in/janesmith"
    assert jane.confidence == pytest.approx(0.85)  # 0.85 quality x 1.0 title match
    # obfuscated person: first name only, apollo id kept in raw, penalized confidence
    assert andrew.url == ""
    assert andrew.raw["id"] == "p1"
    assert andrew.raw["last_name_obfuscated"] == "Hu***n"
    assert andrew.confidence == pytest.approx(0.75)  # 0.85 x 0.98 x 0.9, rounded

    # request shape: query params, X-Api-Key header, 0 credits ledgered
    req = route.calls.last.request
    assert req.headers["x-api-key"] == "test-key"
    assert req.url.params.get_list("q_organization_domains_list[]") == ["acme.com"]
    assert "chief executive officer" in req.url.params.get_list("person_titles[]")
    assert len(req.url.params.get_list("person_titles[]")) == 6
    row = _spend(ctx)["apollo"]
    assert row["calls"] == 1 and row["usd"] == 0.0 and row["credits"] == 0.0


@respx.mock
async def test_identify_masked_name_variants(ctx, provider, company):
    # Apollo now masks net-new last names in several shapes: the whole `name` field
    # ("Ben Hu***n"), or the mask living directly in `last_name`. Either way we must keep
    # the FIRST NAME ONLY so downstream split_name doesn't build junk locals
    # (split_name("Ben Hu***n") -> ("ben", "hun") -> "bhun@").
    payload = {
        "people": [
            {"id": "p9", "name": "Ben Hu***n", "title": "CEO", "has_email": True,
             "organization": {"name": "Acme"}},
            {"id": "p8", "first_name": "Cara", "last_name": "Jo***s",
             "last_name_obfuscated": True, "title": "Chief Executive Officer",
             "organization": {"name": "Acme"}},
        ]
    }
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=payload))
    async with ctx:
        hits = await provider.identify(company, "ceo")

    by_id = {h.raw["apollo_id"]: h for h in hits}
    assert by_id["p9"].name == "Ben"      # name-field mask stripped to first name
    assert by_id["p8"].name == "Cara"     # last_name-field mask stripped to first name
    assert all("*" not in h.name for h in hits)   # no mask leaks downstream
    # Apollo id kept in raw (both keys) for the reveal path
    assert by_id["p9"].raw["apollo_id"] == "p9" and by_id["p9"].raw["id"] == "p9"
    assert by_id["p8"].raw["id"] == "p8"


@respx.mock
async def test_identify_no_results(ctx, provider, company):
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"people": [], "contacts": []}))
    async with ctx:
        assert await provider.identify(company, "legal") == []


@respx.mock
async def test_identify_http_error_propagates(ctx, provider, company):
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(401, json={"error": "invalid key"}))
    async with ctx:
        with pytest.raises(net.HttpError) as ei:
            await provider.identify(company, "ceo")
    assert ei.value.status == 401


# -------------------------------------------------------------- find_email

@respx.mock
async def test_find_email_verified_and_ledgered(ctx, provider, company):
    route = respx.post(MATCH_URL).mock(return_value=httpx.Response(200, json={
        "person": {"id": "c1", "name": "Jane Smith", "email": "jane@acme.com",
                   "email_status": "verified", "linkedin_url": "https://linkedin.com/in/janesmith"}
    }))
    person = PersonHit(name="Jane Smith", title="CEO", role="ceo", source="apollo", raw={"id": "c1"})
    async with ctx:
        hits = await provider.find_email(company, person)
        # second call hits the SQLite cache, not the network
        again = await provider.find_email(company, person)

    assert len(hits) == 1
    hit = hits[0]
    assert hit.email == "jane@acme.com"
    assert hit.method == "found" and hit.source == "apollo"
    assert hit.verify_status == "deliverable"  # apollo "verified" -> deliverable
    assert hit.confidence == pytest.approx(0.9)
    assert hit.person_name == "Jane Smith"
    assert again == hits
    assert route.call_count == 1

    req = route.calls.last.request
    assert req.url.params["reveal_personal_emails"] == "false"
    assert req.url.params["domain"] == "acme.com"
    assert req.url.params["id"] == "c1"
    row = _spend(ctx)["apollo"]
    assert row["calls"] == 1
    assert row["usd"] == pytest.approx(0.02)   # default $0.02/credit
    assert row["credits"] == pytest.approx(1.0)


@respx.mock
async def test_find_email_reveals_via_apollo_id_from_raw(ctx, provider, company):
    # An obfuscated identify() hit: first-name-only PersonHit, Apollo id in raw['apollo_id']
    # (the key survives the PersonRec.raw round-trip). find_email must reveal by id.
    route = respx.post(MATCH_URL).mock(return_value=httpx.Response(200, json={
        "person": {"id": "p1", "name": "Andrew Hunton", "email": "andrew@acme.com",
                   "email_status": "verified"}
    }))
    person = PersonHit(name="Andrew", title="CEO", role="ceo", source="apollo",
                       raw={"apollo_id": "p1"})
    async with ctx:
        hits = await provider.find_email(company, person)

    assert hits and hits[0].email == "andrew@acme.com"
    req = route.calls.last.request
    assert req.url.params["id"] == "p1"        # revealed by id from raw, not a fuzzy name
    assert "name" not in req.url.params         # partial (first-only) name -> no name param


@respx.mock
async def test_find_email_fresh_miss_ledgers_zero(ctx, provider, company):
    # people/match charges 0 credits when no person is returned; a fresh miss must rewrite
    # the pre-ledgered credit down to $0/0 credits.
    respx.post(MATCH_URL).mock(return_value=httpx.Response(200, json={"person": None}))
    person = PersonHit(name="Jane Smith", title="CEO", role="ceo", source="apollo",
                       raw={"id": "c1"})
    async with ctx:
        assert await provider.find_email(company, person) == []
    row = _spend(ctx)["apollo"]
    assert row["calls"] == 1                     # one row (INSERT OR REPLACE), rewritten
    assert row["usd"] == 0.0 and row["credits"] == 0.0


@respx.mock
async def test_find_email_unverified_status(ctx, provider, company):
    respx.post(MATCH_URL).mock(return_value=httpx.Response(200, json={
        "person": {"name": "Jane Smith", "email": "jane@acme.com", "email_status": "guessed"}
    }))
    person = PersonHit(name="Jane Smith", title="CEO", role="ceo", source="apollo")
    async with ctx:
        hits = await provider.find_email(company, person)
    assert hits[0].verify_status == "unverified"
    assert hits[0].confidence == pytest.approx(0.6)


@respx.mock
async def test_find_email_no_result_and_placeholder(ctx, provider, company):
    respx.post(MATCH_URL).mock(side_effect=[
        httpx.Response(200, json={"person": None}),
        httpx.Response(200, json={"person": {"name": "A B", "email": "email_not_unlocked@domain.com"}}),
    ])
    a = PersonHit(name="Jane Smith", title="CEO", role="ceo", source="apollo")
    b = PersonHit(name="John Doe", title="CEO", role="ceo", source="apollo")
    async with ctx:
        assert await provider.find_email(company, a) == []
        assert await provider.find_email(company, b) == []


@respx.mock
async def test_find_email_skips_partial_name_without_id(ctx, provider, company):
    route = respx.post(MATCH_URL).mock(return_value=httpx.Response(200, json={"person": None}))
    # first-name-only hit (obfuscated search result) with no apollo id: no credit burned
    person = PersonHit(name="Andrew", title="CEO", role="ceo", source="sitescrape")
    async with ctx:
        assert await provider.find_email(company, person) == []
    assert not route.called


@respx.mock
async def test_cost_env_override(ctx, provider, company, monkeypatch):
    monkeypatch.setenv("APOLLO_COST_PER_CREDIT", "0")  # free-tier grant
    respx.post(MATCH_URL).mock(return_value=httpx.Response(200, json={
        "person": {"name": "Jane Smith", "email": "jane@acme.com", "email_status": "verified"}
    }))
    person = PersonHit(name="Jane Smith", title="CEO", role="ceo", source="apollo")
    async with ctx:
        hits = await provider.find_email(company, person)
    assert hits and _spend(ctx)["apollo"]["usd"] == 0.0
    assert _spend(ctx)["apollo"]["credits"] == pytest.approx(1.0)  # credits still tracked


# ------------------------------------------------------------ company_info

@respx.mock
async def test_company_info(ctx, provider, company):
    route = respx.get(ORG_URL).mock(return_value=httpx.Response(200, json={
        "organization": {"name": "Acme Inc", "estimated_num_employees": 25,
                         "annual_revenue_printed": "10M", "annual_revenue": 10000000,
                         "primary_domain": "acme.com"}
    }))
    async with ctx:
        info = await provider.company_info(company)
    assert info is not None
    assert info.entity_name == "Acme Inc"
    assert info.employee_count == 25
    assert info.revenue_hint == "~$10M revenue"
    assert info.email_domain == "acme.com"
    assert route.calls.last.request.url.params["domain"] == "acme.com"
    row = _spend(ctx)["apollo"]
    assert row["usd"] == pytest.approx(0.02) and row["credits"] == pytest.approx(1.0)


@respx.mock
async def test_company_info_miss(ctx, provider, company):
    respx.get(ORG_URL).mock(return_value=httpx.Response(200, json={"organization": None}))
    async with ctx:
        assert await provider.company_info(company) is None
    # fresh miss: organizations/enrich charges 0 credits -> ledger row rewritten to $0
    row = _spend(ctx)["apollo"]
    assert row["calls"] == 1 and row["usd"] == 0.0 and row["credits"] == 0.0


# ------------------------------------------------------- availability/doctor

def test_available_without_key(ctx, monkeypatch):
    ctx.flags.free_only = False
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    ok, why = Apollo(ctx).available()
    assert not ok and "APOLLO_API_KEY" in why


def test_available_blocked_by_free_only(ctx, monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    ctx.flags.free_only = True
    ok, why = Apollo(ctx).available()
    assert not ok and "free-only" in why


@respx.mock
async def test_doctor_regular_key(ctx, provider):
    respx.get(f"{BASE_URL}/auth/health").mock(
        return_value=httpx.Response(200, json={"is_logged_in": True}))
    respx.post(f"{BASE_URL}/usage_stats/api_usage_stats").mock(
        return_value=httpx.Response(403, json={"error": "master key required"}))
    async with ctx:
        out = await provider.doctor()
    assert out.startswith("ok")
    assert "master" in out


@respx.mock
async def test_doctor_master_key_quota(ctx, provider):
    respx.get(f"{BASE_URL}/auth/health").mock(
        return_value=httpx.Response(200, json={"is_logged_in": True}))
    respx.post(f"{BASE_URL}/usage_stats/api_usage_stats").mock(
        return_value=httpx.Response(200, json={
            "api/v1/people/match": {"day": {"limit": 600, "consumed": 10, "left_over": 590}}
        }))
    async with ctx:
        out = await provider.doctor()
    assert out == "ok - 590 of 600 daily people/match requests left"
