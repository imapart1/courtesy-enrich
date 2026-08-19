"""serp (Serper.dev) adapter — respx-mocked, no network."""

from __future__ import annotations

import httpx
import pytest
import respx

from enrich.models import Company
from enrich.net import HttpError
from enrich.providers.serp import COST_PER_QUERY_USD, SEARCH_URL, Serp


@pytest.fixture()
def serp_ctx(ctx, monkeypatch):
    ctx.flags.free_only = False
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    monkeypatch.delenv("SERPER_COST_PER_QUERY", raising=False)
    return ctx


@pytest.fixture()
def company() -> Company:
    return Company(key="manual:acme", name="Acme Skincare", website="acmeskincare.com", domain="acmeskincare.com")


def serper_payload() -> dict:
    return {
        "searchParameters": {"q": "test"},
        "organic": [
            {   # canonical "Name - Title - Company | LinkedIn" shape
                "title": "Jane Smith - General Counsel - Acme Skincare | LinkedIn",
                "link": "https://www.linkedin.com/in/janesmith",
                "snippet": "Jane Smith. General Counsel at Acme Skincare.",
                "position": 1,
            },
            {   # "Name | LinkedIn" shape, title only in the snippet
                "title": "John Doe | LinkedIn",
                "link": "https://www.linkedin.com/in/johndoe",
                "snippet": "John Doe. Deputy General Counsel at Acme Skincare in New York.",
                "position": 2,
            },
            {   # same title, wrong company -> must be excluded
                "title": "Ann Prior - General Counsel - Other Corp | LinkedIn",
                "link": "https://www.linkedin.com/in/annprior",
                "snippet": "General Counsel at Other Corp.",
                "position": 3,
            },
            {   # right company, wrong role -> must be excluded
                "title": "Bob Roll - Marketing Manager - Acme Skincare | LinkedIn",
                "link": "https://www.linkedin.com/in/bobroll",
                "snippet": "Marketing at Acme Skincare.",
                "position": 4,
            },
        ],
    }


@respx.mock
async def test_identify_parses_people(serp_ctx, company):
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=serper_payload()))
    async with serp_ctx as ctx:
        hits = await Serp(ctx).identify(company, "legal")

    assert {h.name for h in hits} == {"Jane Smith", "John Doe"}
    by_name = {h.name: h for h in hits}
    jane = by_name["Jane Smith"]
    assert jane.role == "legal"
    assert jane.title == "General Counsel"
    assert jane.url == "https://www.linkedin.com/in/janesmith"
    assert jane.confidence == 0.7
    john = by_name["John Doe"]
    assert john.title == "deputy general counsel"     # longest snippet phrase wins
    assert 0.55 <= john.confidence < jane.confidence  # snippet-derived = weaker
    for h in hits:
        assert 0.55 <= h.confidence <= 0.7
    # key travels in the header, never in the cached params
    req = route.calls.last.request
    assert req.headers["X-API-KEY"] == "test-key"
    assert b"test-key" not in req.content


@respx.mock
async def test_identify_no_results(serp_ctx, company):
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"organic": []}))
    async with serp_ctx as ctx:
        assert await Serp(ctx).identify(company, "ceo") == []


@respx.mock
async def test_http_4xx_raises(serp_ctx, company):
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(403, json={"message": "Unauthorized."}))
    async with serp_ctx as ctx:
        with pytest.raises(HttpError) as exc:
            await Serp(ctx).identify(company, "legal")
    assert exc.value.status == 403


@respx.mock
async def test_cost_credits_and_cache(serp_ctx, company):
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=serper_payload()))
    async with serp_ctx as ctx:
        p = Serp(ctx)
        await p.identify(company, "legal")
        await p.identify(company, "legal")   # identical query -> served from cache
    assert route.call_count == 1
    rows = {r["provider"]: r for r in serp_ctx.store.spend_by_provider(serp_ctx.run_id)}
    assert rows["serp"]["calls"] == 1
    assert rows["serp"]["usd"] == pytest.approx(COST_PER_QUERY_USD)
    assert rows["serp"]["credits"] == 1


@respx.mock
async def test_cost_env_override_free_tier(serp_ctx, company, monkeypatch):
    monkeypatch.setenv("SERPER_COST_PER_QUERY", "0")
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json=serper_payload()))
    async with serp_ctx as ctx:
        await Serp(ctx).identify(company, "legal")
    rows = {r["provider"]: r for r in serp_ctx.store.spend_by_provider(serp_ctx.run_id)}
    assert rows["serp"]["usd"] == 0.0


def test_available_requires_key(ctx, monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    ctx.flags.free_only = False
    ok, why = Serp(ctx).available()
    assert not ok and "SERPER_API_KEY" in why


def test_available_blocked_on_free_only(ctx, monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "test-key")
    ok, why = Serp(ctx).available()   # conftest ctx has free_only=True
    assert not ok and "free-only" in why


@respx.mock
async def test_doctor(serp_ctx):
    respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"organic": [{"title": "x", "link": "y", "snippet": "z"}]})
    )
    async with serp_ctx as ctx:
        msg = await Serp(ctx).doctor()
    assert msg.startswith("ok") and "$" in msg
