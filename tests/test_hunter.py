"""Hunter.io adapter tests — respx-mocked HTTP, no network."""

from __future__ import annotations

import httpx
import pytest
import respx

from enrich import net
from enrich.models import Company
from enrich.providers.base import PersonHit, ProviderError
from enrich.providers.hunter import (
    BASE_URL,
    COST_PER_CREDIT_USD,
    CREDITS_DOMAIN_SEARCH,
    CREDITS_EMAIL_FINDER_HIT,
    CREDITS_VERIFY,
    DOMAIN_SEARCH_LIMIT,
    Hunter,
)

DOMAIN_RESP = {
    "data": {
        "domain": "acme.com",
        "disposable": False,
        "webmail": False,
        "accept_all": False,
        "pattern": "{first}",
        "organization": "Acme",
        "emails": [
            {
                "value": "jane@acme.com",
                "type": "personal",
                "confidence": 92,
                "first_name": "Jane",
                "last_name": "Smith",
                "position": "Chief Executive Officer",
                "seniority": "executive",
                "department": "executive",
                "sources": [{"domain": "acme.com", "uri": "https://acme.com/team"}],
                "verification": {"date": "2026-01-05", "status": "valid"},
            },
            {
                "value": "bob@acme.com",
                "type": "personal",
                "confidence": 80,
                "first_name": "Bob",
                "last_name": "Jones",
                "position": "Support Engineer",
                "seniority": "senior",
                "department": "support",
                "sources": [],
            },
            {
                "value": "info@acme.com",
                "type": "generic",
                "confidence": 70,
                "first_name": None,
                "last_name": None,
                "position": None,
                "sources": [],
            },
        ],
    },
    "meta": {"results": 3, "limit": DOMAIN_SEARCH_LIMIT, "offset": 0, "params": {"domain": "acme.com"}},
}


@pytest.fixture()
def hctx(ctx, monkeypatch):
    ctx.flags.free_only = False
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    return ctx


def company(domain="acme.com") -> Company:
    return Company(key=f"manual:{domain}", name="Acme", website=domain, domain=domain)


def spend(ctx) -> dict:
    rows = [r for r in ctx.store.spend_by_provider() if r["provider"] == "hunter"]
    return rows[0] if rows else {"calls": 0, "usd": 0.0, "credits": 0.0}


# ------------------------------------------------------------- domain_pattern

@respx.mock
async def test_domain_pattern_parses_and_spends(hctx):
    route = respx.get(f"{BASE_URL}/domain-search").mock(
        return_value=httpx.Response(200, json=DOMAIN_RESP)
    )
    async with hctx:
        hit = await Hunter(hctx).domain_pattern(company())
    assert hit is not None
    assert hit.pattern == "{first}"
    assert hit.source == "hunter"
    assert hit.sample_size == 3
    assert 0.5 <= hit.confidence <= 1.0
    # key went on the wire but not into the cache params
    assert route.calls.last.request.url.params["api_key"] == "test-key"
    assert route.calls.last.request.url.params["domain"] == "acme.com"
    row = spend(hctx)
    assert row["credits"] == CREDITS_DOMAIN_SEARCH
    assert row["usd"] == pytest.approx(CREDITS_DOMAIN_SEARCH * COST_PER_CREDIT_USD)


@respx.mock
async def test_domain_pattern_no_result(hctx):
    empty = {"data": {"domain": "nil.com", "pattern": None, "organization": None, "emails": []},
             "meta": {"results": 0}}
    respx.get(f"{BASE_URL}/domain-search").mock(return_value=httpx.Response(200, json=empty))
    async with hctx:
        assert await Hunter(hctx).domain_pattern(company("nil.com")) is None


async def test_domain_pattern_without_domain(hctx):
    async with hctx:
        assert await Hunter(hctx).domain_pattern(Company(key="manual:x", name="X")) is None


# ------------------------------------------------------------------- identify

@respx.mock
async def test_identify_filters_role_and_reuses_cache(hctx):
    route = respx.get(f"{BASE_URL}/domain-search").mock(
        return_value=httpx.Response(200, json=DOMAIN_RESP)
    )
    async with hctx:
        p = Hunter(hctx)
        await p.domain_pattern(company())
        people = await p.identify(company(), "ceo")
        assert await p.identify(company(), "cfo") == []
    assert route.call_count == 1  # identify reused the cached domain-search
    assert len(people) == 1
    jane = people[0]
    assert jane.name == "Jane Smith"
    assert jane.role == "ceo"
    assert jane.title == "Chief Executive Officer"
    assert jane.url == "https://acme.com/team"
    assert jane.confidence == pytest.approx(0.92)  # 92/100 * title score 1.0
    row = spend(hctx)
    assert row["calls"] == 1 and row["credits"] == CREDITS_DOMAIN_SEARCH


# ----------------------------------------------------------------- find_email

@respx.mock
async def test_find_email_reuses_cached_domain_search_for_free(hctx):
    respx.get(f"{BASE_URL}/domain-search").mock(return_value=httpx.Response(200, json=DOMAIN_RESP))
    finder = respx.get(f"{BASE_URL}/email-finder").mock(
        return_value=httpx.Response(200, json={"data": {"email": None}})
    )
    async with hctx:
        p = Hunter(hctx)
        await p.domain_pattern(company())
        hits = await p.find_email(company(), PersonHit(name="Jane Smith", title="CEO", role="ceo", source="t"))
    assert not finder.called
    assert len(hits) == 1
    assert hits[0].email == "jane@acme.com"
    assert hits[0].method == "found"
    assert hits[0].verify_status == "deliverable"  # hunter "valid" mapped
    assert spend(hctx)["credits"] == CREDITS_DOMAIN_SEARCH  # nothing beyond the pattern call


@respx.mock
async def test_find_email_hit(hctx):
    resp = {
        "data": {
            "email": "pat@beta.com", "first_name": "Pat", "last_name": "Lee", "score": 97,
            "domain": "beta.com", "position": "Founder",
            "verification": {"date": "2026-02-02", "status": "accept_all"},
            "sources": [],
        },
        "meta": {"params": {"domain": "beta.com"}},
    }
    respx.get(f"{BASE_URL}/email-finder").mock(return_value=httpx.Response(200, json=resp))
    async with hctx:
        hits = await Hunter(hctx).find_email(
            company("beta.com"), PersonHit(name="Pat Lee", title="Founder", role="ceo", source="t")
        )
    assert len(hits) == 1
    h = hits[0]
    assert h.email == "pat@beta.com"
    assert h.confidence == pytest.approx(0.97)
    assert h.verify_status == "catch_all"
    assert h.person_name == "Pat Lee"
    assert "sources" not in h.raw
    row = spend(hctx)
    assert row["credits"] == CREDITS_EMAIL_FINDER_HIT
    assert row["usd"] == pytest.approx(CREDITS_EMAIL_FINDER_HIT * COST_PER_CREDIT_USD)


@respx.mock
async def test_find_email_miss_is_free_but_cached(hctx):
    route = respx.get(f"{BASE_URL}/email-finder").mock(
        return_value=httpx.Response(200, json={"data": {"email": None, "score": None}, "meta": {}})
    )
    async with hctx:
        p = Hunter(hctx)
        person = PersonHit(name="No Body", title="CEO", role="ceo", source="t")
        assert await p.find_email(company("beta.com"), person) == []
        assert await p.find_email(company("beta.com"), person) == []  # served from cache
    assert route.call_count == 1
    row = spend(hctx)
    assert row["credits"] == 0.0 and row["usd"] == 0.0


async def test_find_email_needs_full_name(hctx):
    async with hctx:
        hits = await Hunter(hctx).find_email(
            company(), PersonHit(name="Cher", title="CEO", role="ceo", source="t")
        )
    assert hits == []


# --------------------------------------------------------------------- verify

@respx.mock
@pytest.mark.parametrize(
    "hunter_status,ours",
    [
        ("valid", "deliverable"),
        ("invalid", "undeliverable"),
        ("accept_all", "catch_all"),
        ("webmail", "undeliverable"),
        ("disposable", "undeliverable"),
        ("unknown", "unknown"),
    ],
)
async def test_verify_status_mapping(hctx, hunter_status, ours):
    email = f"x-{hunter_status}@acme.com"
    respx.get(f"{BASE_URL}/email-verifier").mock(
        return_value=httpx.Response(
            200, json={"data": {"status": hunter_status, "result": "n/a", "score": 50, "email": email}}
        )
    )
    async with hctx:
        res = await Hunter(hctx).verify(email)
    assert res.status == ours
    assert res.source == "hunter"
    if hunter_status in ("webmail", "disposable"):
        assert "mapping_note" in res.raw
    row = spend(hctx)
    assert row["credits"] == CREDITS_VERIFY
    assert row["usd"] == pytest.approx(CREDITS_VERIFY * COST_PER_CREDIT_USD)


# ------------------------------------------------------------ errors & config

@respx.mock
async def test_http_error_propagates(hctx):
    respx.get(f"{BASE_URL}/domain-search").mock(
        return_value=httpx.Response(401, json={"errors": [{"id": "unauthorized", "code": 401}]})
    )
    async with hctx:
        with pytest.raises(net.HttpError):
            await Hunter(hctx).domain_pattern(company())
    assert spend(hctx)["calls"] == 0  # failed call never hit the ledger


def test_available_without_key(ctx, monkeypatch):
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)
    ctx.flags.free_only = False
    ok, why = Hunter(ctx).available()
    assert not ok and "HUNTER_API_KEY" in why


@respx.mock
async def test_cost_override_env(hctx, monkeypatch):
    monkeypatch.setenv("HUNTER_COST_PER_CREDIT", "0")
    respx.get(f"{BASE_URL}/domain-search").mock(return_value=httpx.Response(200, json=DOMAIN_RESP))
    async with hctx:
        await Hunter(hctx).domain_pattern(company())
    row = spend(hctx)
    assert row["usd"] == 0.0
    assert row["credits"] == CREDITS_DOMAIN_SEARCH  # credits still tracked


def test_cost_override_invalid(hctx, monkeypatch):
    monkeypatch.setenv("HUNTER_COST_PER_CREDIT", "cheap")
    with pytest.raises(ProviderError):
        _ = Hunter(hctx).cost_per_credit


# --------------------------------------------------------------------- doctor

@respx.mock
async def test_doctor(hctx):
    respx.get(f"{BASE_URL}/account").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "plan_name": "Starter",
                    "plan_level": 1,
                    "requests": {
                        "searches": {"used": 150, "available": 2000, "remaining": 1850},
                        "verifications": {"used": 100, "available": 4000, "remaining": 3900},
                    },
                }
            },
        )
    )
    async with hctx:
        msg = await Hunter(hctx).doctor()
    assert msg.startswith("ok - plan Starter")
    assert "1,850 searches left" in msg
    assert "3,900 verifications left" in msg
    assert spend(hctx)["usd"] == 0.0
