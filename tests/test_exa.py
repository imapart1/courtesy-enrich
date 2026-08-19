"""Exa adapter tests — respx-mocked HTTP, realistic fixture payloads."""

from __future__ import annotations

import json

import pytest
import respx
from httpx import Response

from enrich import net
from enrich.models import Company
from enrich.providers import exa as exa_mod
from enrich.providers.base import ProviderError
from enrich.providers.exa import COST_ENV, DEFAULT_COST_PER_SEARCH_USD, Exa

SEARCH_URL = "https://api.exa.ai/search"


def company(**kw) -> Company:
    base = dict(
        key="manual:acme", name="Acme Skincare",
        website="https://acmeskincare.com", domain="acmeskincare.com",
    )
    base.update(kw)
    return Company(**base)


@pytest.fixture()
def exa(ctx, monkeypatch) -> Exa:
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.delenv(COST_ENV, raising=False)
    ctx.flags.free_only = False
    return Exa(ctx)


LINKEDIN_PAYLOAD = {
    "requestId": "r1",
    "results": [
        {
            "title": "Jane Smith - General Counsel - Acme Skincare | LinkedIn",
            "url": "https://www.linkedin.com/in/jane-smith-12345",
            "id": "d1", "author": None, "publishedDate": None,
            "text": "Jane Smith. General Counsel at Acme Skincare. Greater Los Angeles Area.",
        },
        {
            "title": "Bob Jones - Warehouse Associate - Acme Skincare | LinkedIn",
            "url": "https://www.linkedin.com/in/bob-jones",
            "id": "d2",
            "text": "Bob Jones. Warehouse Associate at Acme Skincare.",
        },
        {
            "title": "Acme Skincare hiring paralegal | LinkedIn",
            "url": "https://www.linkedin.com/jobs/view/12345",
            "id": "d3",
            "text": "Acme Skincare is hiring.",
        },
    ],
    "costDollars": {"total": 0.007, "search": {"neural": 0.007}},
}

NEWS_PAYLOAD = {
    "requestId": "r2",
    "results": [
        {
            "title": "Acme Skincare announces leadership changes",
            "url": "https://www.prnews.example/acme-leadership",
            "id": "d4",
            "text": (
                "NEW YORK - Acme Skincare today announced that Maria Garcia has been "
                "named General Counsel, effective July 1. Garcia joins from BigCo."
            ),
        },
    ],
    "costDollars": {"total": 0.007},
}

EMPTY_PAYLOAD = {"requestId": "r0", "results": [], "costDollars": {"total": 0.007}}

ACQ_PAYLOAD = {
    "requestId": "r3",
    "results": [
        {
            "title": "MegaCorp completes acquisition of Acme Skincare",
            "url": "https://news.example/deal",
            "id": "d5",
            "text": (
                "MegaCorp Holdings has acquired Acme Skincare for $120 million. "
                "Following the deal, Acme Skincare is now a subsidiary of MegaCorp Holdings."
            ),
        },
    ],
    "costDollars": {"total": 0.007},
}


# ------------------------------------------------------------------- identify

@respx.mock
async def test_identify_parses_linkedin_serp_and_caches(exa, ctx):
    route = respx.post(SEARCH_URL).mock(return_value=Response(200, json=LINKEDIN_PAYLOAD))
    async with ctx:
        hits = await exa.identify(company(), "legal")
        again = await exa.identify(company(), "legal")

    # confident SERP hit -> second query variant skipped
    assert route.call_count == 1

    assert len(hits) == 1
    h = hits[0]
    assert h.name == "Jane Smith"
    assert h.role == "legal" and h.source == "exa"
    assert "linkedin.com/in" in h.url
    assert 0.55 <= h.confidence <= 0.75

    # request shape per 2026 docs
    body = json.loads(route.calls[0].request.content)
    assert body["type"] == "auto" and body["numResults"] == 5
    assert "site:linkedin.com/in" in body["query"]
    assert body["contents"]["text"]["maxCharacters"] > 0
    assert route.calls[0].request.headers["x-api-key"] == "test-key"

    # spend hit the ledger exactly once (second call was a cache hit)
    rows = [r for r in ctx.store.spend_by_provider() if r["provider"] == "exa"]
    assert rows and rows[0]["calls"] == 1
    assert rows[0]["usd"] == pytest.approx(DEFAULT_COST_PER_SEARCH_USD)
    assert rows[0]["credits"] == pytest.approx(1.0)

    # cache round-trip parses identically
    assert [p.name for p in again] == ["Jane Smith"]


@respx.mock
async def test_identify_falls_back_to_neural_query_and_text_parse(exa, ctx):
    route = respx.post(SEARCH_URL).mock(
        side_effect=[Response(200, json=EMPTY_PAYLOAD), Response(200, json=NEWS_PAYLOAD)]
    )
    async with ctx:
        hits = await exa.identify(company(), "legal")

    assert route.call_count == 2
    body2 = json.loads(route.calls[1].request.content)
    assert "site:" not in body2["query"]
    assert "who is the general counsel of acme skincare" in body2["query"].lower()

    assert [h.name for h in hits] == ["Maria Garcia"]
    assert hits[0].title.lower() == "general counsel"
    assert hits[0].confidence < 0.70  # page-text evidence stays below SERP-title grade

    rows = [r for r in ctx.store.spend_by_provider() if r["provider"] == "exa"]
    assert rows[0]["calls"] == 2
    assert rows[0]["usd"] == pytest.approx(2 * DEFAULT_COST_PER_SEARCH_USD)


@respx.mock
async def test_identify_filters_to_requested_role(exa, ctx):
    # A General Counsel SERP hit must NOT satisfy a "ceo" request.
    route = respx.post(SEARCH_URL).mock(
        side_effect=[Response(200, json=LINKEDIN_PAYLOAD), Response(200, json=EMPTY_PAYLOAD)]
    )
    async with ctx:
        hits = await exa.identify(company(), "ceo")
    assert hits == []
    assert route.call_count == 2


@respx.mock
async def test_identify_no_results_returns_empty(exa, ctx):
    respx.post(SEARCH_URL).mock(return_value=Response(200, json=EMPTY_PAYLOAD))
    async with ctx:
        hits = await exa.identify(company(), "legal")
    assert hits == []


@respx.mock
async def test_identify_http_error_propagates(exa, ctx):
    respx.post(SEARCH_URL).mock(return_value=Response(401, json={"error": "invalid api key"}))
    async with ctx:
        with pytest.raises(net.HttpError) as ei:
            await exa.identify(company(), "legal")
    assert ei.value.status == 401
    # failed calls never reach the ledger
    assert not [r for r in ctx.store.spend_by_provider() if r["provider"] == "exa"]


# ------------------------------------------------------- availability & cost

def test_available_false_without_key(ctx, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    ctx.flags.free_only = False
    ok, why = Exa(ctx).available()
    assert not ok and "EXA_API_KEY" in why


def test_available_false_on_free_only_run(ctx, monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    ctx.flags.free_only = True
    ok, why = Exa(ctx).available()
    assert not ok and "free-only" in why


@respx.mock
async def test_cost_env_override_zero(exa, ctx, monkeypatch):
    monkeypatch.setenv(COST_ENV, "0")
    respx.post(SEARCH_URL).mock(return_value=Response(200, json=LINKEDIN_PAYLOAD))
    async with ctx:
        hits = await exa.identify(company(), "legal")
    assert hits
    rows = [r for r in ctx.store.spend_by_provider() if r["provider"] == "exa"]
    assert rows[0]["usd"] == pytest.approx(0.0)


async def test_cost_env_override_invalid_raises(exa, ctx, monkeypatch):
    monkeypatch.setenv(COST_ENV, "free")
    async with ctx:
        with pytest.raises(ProviderError):
            await exa.identify(company(), "legal")


@respx.mock
async def test_fresh_call_rewrites_ledger_with_real_cost(exa, ctx):
    # Exa returns the authoritative per-call price at costDollars.total; the ledger
    # must reflect it, not the flat $0.007 estimate reserved pre-call.
    payload = {**LINKEDIN_PAYLOAD, "costDollars": {"total": 0.012, "search": {"neural": 0.012}}}
    respx.post(SEARCH_URL).mock(return_value=Response(200, json=payload))
    async with ctx:
        hits = await exa.identify(company(), "legal")  # confident SERP hit -> one call
    assert hits
    rows = [r for r in ctx.store.spend_by_provider() if r["provider"] == "exa"]
    assert rows[0]["calls"] == 1
    assert rows[0]["usd"] == pytest.approx(0.012)
    assert rows[0]["usd"] != pytest.approx(DEFAULT_COST_PER_SEARCH_USD)
    assert rows[0]["credits"] == pytest.approx(1.0)


@respx.mock
async def test_missing_cost_dollars_keeps_flat_estimate(exa, ctx):
    # No costDollars field -> guard keeps the reserved flat estimate on the ledger.
    payload = {k: v for k, v in LINKEDIN_PAYLOAD.items() if k != "costDollars"}
    respx.post(SEARCH_URL).mock(return_value=Response(200, json=payload))
    async with ctx:
        hits = await exa.identify(company(), "legal")
    assert hits
    rows = [r for r in ctx.store.spend_by_provider() if r["provider"] == "exa"]
    assert rows[0]["calls"] == 1
    assert rows[0]["usd"] == pytest.approx(DEFAULT_COST_PER_SEARCH_USD)


# --------------------------------------------------------------- company_info

@respx.mock
async def test_company_info_skips_without_acquisition_hint(exa, ctx):
    route = respx.post(SEARCH_URL).mock(return_value=Response(200, json=ACQ_PAYLOAD))
    async with ctx:
        info = await exa.company_info(company(notes="demand letter sent 3/2026"))
    assert info is None
    assert route.call_count == 0  # gate must prevent the paid call entirely


@respx.mock
async def test_company_info_parses_acquirer(exa, ctx):
    route = respx.post(SEARCH_URL).mock(return_value=Response(200, json=ACQ_PAYLOAD))
    async with ctx:
        info = await exa.company_info(company(notes="sheet note: acquired by someone?"))
    assert route.call_count == 1
    body = json.loads(route.calls[0].request.content)
    assert body["numResults"] == 3
    assert info is not None
    assert info.parent_company == "MegaCorp Holdings"
    assert info.source == "exa"
    assert info.raw["evidence_url"] == "https://news.example/deal"


@respx.mock
async def test_company_info_none_when_unclear(exa, ctx):
    vague = {
        "requestId": "r4",
        "results": [{
            "title": "Acme Skincare raises Series A",
            "url": "https://news.example/series-a",
            "text": "Acme Skincare raised a Series A round led by VentureFund Partners.",
        }],
        "costDollars": {"total": 0.007},
    }
    respx.post(SEARCH_URL).mock(return_value=Response(200, json=vague))
    async with ctx:
        info = await exa.company_info(company(notes="maybe acquired?"))
    assert info is None


# --------------------------------------------------------------------- doctor

@respx.mock
async def test_doctor_ok(exa, ctx):
    payload = {
        "requestId": "r5",
        "results": [{"title": "Exa", "url": "https://exa.ai", "id": "d9"}],
        "costDollars": {"total": 0.007},
    }
    route = respx.post(SEARCH_URL).mock(return_value=Response(200, json=payload))
    async with ctx:
        msg = await exa.doctor()
    assert msg.startswith("ok")
    assert "$0.0070" in msg
    body = json.loads(route.calls[0].request.content)
    assert body["numResults"] == 1 and "contents" not in body


@respx.mock
async def test_doctor_reports_http_error(exa, ctx):
    respx.post(SEARCH_URL).mock(return_value=Response(403, json={"error": "forbidden"}))
    async with ctx:
        msg = await exa.doctor()
    assert msg.startswith("error") and "403" in msg


# ----------------------------------------------------------- parsing helpers

def test_parse_linkedin_title_variants():
    f = exa_mod._parse_linkedin_title
    assert f("Jane Smith - General Counsel - Acme | LinkedIn") == ("Jane Smith", "General Counsel")
    assert f("Jane Q. Smith – Chief Executive Officer – Acme | LinkedIn") == (
        "Jane Q. Smith", "Chief Executive Officer"
    )
    assert f("Jane Smith, General Counsel at Acme | LinkedIn") == ("Jane Smith", "General Counsel at Acme")
    assert f("Acme Skincare | LinkedIn") is None            # no title segment
    assert f("Legal Department - Acme | LinkedIn") is None  # not a person
    assert f("Jane - General Counsel - Acme | LinkedIn") is None  # single-token name


def test_extract_people_is_conservative():
    text = (
        "Former General Counsel John Old left in 2020. "
        "Maria Garcia serves as General Counsel of Acme. "
        "Contact the Legal Department with questions."
    )
    people = exa_mod._extract_people(text, "legal")
    names = [n for n, _t, _e in people]
    assert "Maria Garcia" in names
    assert "John Old" not in names          # 'former' guard
    assert all("Department" not in n for n in names)
