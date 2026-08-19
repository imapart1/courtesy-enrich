"""edgar provider: respx-mocked HTTP against the shapes verified live 2026-08-19.
Keyless + free, so no API-key env or free_only toggling is needed."""

import pytest
import respx

from enrich.models import Company
from enrich.net import HttpError
from enrich.providers.edgar import Edgar

ATOM_SINGLE = b"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <company-info>
    <cik>0000075288</cik>
    <conformed-name>OXFORD INDUSTRIES INC</conformed-name>
  </company-info>
  <entry><title>10-K - Annual report</title></entry>
</feed>
"""

ATOM_EMPTY = b"""<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Company Search Feed</title>
</feed>
"""

FTS_JSON = {
    "hits": {
        "total": {"value": 2, "relation": "eq"},
        "hits": [
            {
                "_id": "0000075288-19-000001:old8k.htm",
                "_source": {
                    "adsh": "0000075288-19-000001", "file_date": "2019-01-01",
                    "items": ["1.01"], "form": "8-K", "ciks": ["0000075288"],
                },
            },
            {
                "_id": "0000075288-25-000002:new8k.htm",
                "_source": {
                    "adsh": "0000075288-25-000002", "file_date": "2025-06-30",
                    "items": ["5.02", "9.01"], "form": "8-K", "ciks": ["0000075288"],
                },
            },
        ],
    }
}

NEW_8K = (
    "<html><body><p>On June 30, 2025, the Board of Directors appointed Jane Q. Smith as its "
    "General Counsel, effective immediately.</p>"
    "<p>By: /s/ John A. Doe John A. Doe Chief Executive Officer</p></body></html>"
)
OLD_8K = "<html><body><p>The Company entered into a credit agreement.</p></body></html>"


def company(name: str = "Oxford Industries") -> Company:
    return Company(key="manual:oxford", name=name, website="oxfordinc.com", domain="oxfordinc.com")


def provider(ctx) -> Edgar:
    p = Edgar(ctx)
    p.rps = 10_000.0  # keep the polite rate limiter out of unit tests
    return p


def mock_edgar(atom: bytes = ATOM_SINGLE) -> dict:
    return {
        "browse": respx.get("https://www.sec.gov/cgi-bin/browse-edgar").respond(200, content=atom),
        "fts": respx.get("https://efts.sec.gov/LATEST/search-index").respond(200, json=FTS_JSON),
        "new": respx.get(
            "https://www.sec.gov/Archives/edgar/data/75288/000007528825000002/new8k.htm"
        ).respond(200, text=NEW_8K),
        "old": respx.get(
            "https://www.sec.gov/Archives/edgar/data/75288/000007528819000001/old8k.htm"
        ).respond(200, text=OLD_8K),
    }


def test_available_keyless(ctx):
    # keyless + is_free: usable even in the conftest ctx's --free-only mode, no key needed
    assert Edgar(ctx).available() == (True, "")


@respx.mock
async def test_company_info_match(ctx):
    routes = mock_edgar()
    async with ctx:
        info = await provider(ctx).company_info(company())
    assert info is not None
    assert info.entity_name == "OXFORD INDUSTRIES INC"
    assert info.revenue_hint == "SEC filer (public or public parent)"
    assert info.raw["cik"] == "0000075288"
    params = routes["browse"].calls[0].request.url.params
    assert params["company"] == "Oxford Industries"
    assert params["type"] == "10-K"
    assert params["output"] == "atom"
    assert routes["browse"].calls[0].request.headers["user-agent"] == f"courtesy-enrich {ctx.settings.contact_email}"


@respx.mock
async def test_company_info_none_when_no_filer_or_fuzzy_miss(ctx):
    respx.get("https://www.sec.gov/cgi-bin/browse-edgar").respond(200, content=ATOM_EMPTY)
    async with ctx:
        assert await provider(ctx).company_info(company()) is None
    respx.get("https://www.sec.gov/cgi-bin/browse-edgar").respond(200, content=ATOM_SINGLE)
    async with ctx:
        # a filer exists but doesn't fuzzy-match this name
        assert await provider(ctx).company_info(company("Zebra Networks")) is None


@respx.mock
async def test_identify_appointment_and_signature(ctx):
    routes = mock_edgar()
    async with ctx:
        p = provider(ctx)
        legal = await p.identify(company(), "legal")
        assert [h.name for h in legal] == ["Jane Q. Smith"]  # appointment sentence
        assert legal[0].role == "legal"
        assert legal[0].confidence == 0.6
        assert legal[0].title.lower() == "general counsel"
        assert legal[0].url.endswith("new8k.htm")
        assert legal[0].raw["adsh"] == "0000075288-25-000002"

        ceo = await p.identify(company(), "ceo")  # signature block
        assert [h.name for h in ceo] == ["John A. Doe"]
        assert ceo[0].title.lower() == "chief executive officer"
    params = routes["fts"].calls[0].request.url.params
    assert params["q"] == '"general counsel"'
    assert params["ciks"] == "0000075288"
    assert params["forms"] == "8-K"


@respx.mock
async def test_private_company_fast_path_skips_fulltext(ctx):
    routes = mock_edgar(atom=ATOM_EMPTY)
    async with ctx:
        hits = await provider(ctx).identify(company("Totally Private LLC"), "legal")
    assert hits == []
    assert routes["fts"].call_count == 0  # never touched full-text search


@respx.mock
async def test_http_error_propagates(ctx):
    respx.get("https://www.sec.gov/cgi-bin/browse-edgar").respond(200, content=ATOM_SINGLE)
    respx.get("https://efts.sec.gov/LATEST/search-index").respond(501)
    async with ctx:
        with pytest.raises(HttpError):
            await provider(ctx).identify(company(), "legal")


@respx.mock
async def test_cache_and_ledger(ctx, monkeypatch):
    monkeypatch.delenv("EDGAR_COST_PER_UNIT", raising=False)
    routes = mock_edgar()
    async with ctx:
        p = provider(ctx)
        await p.identify(company(), "legal")
        rows = {r["provider"]: r for r in ctx.store.spend_by_provider()}
        assert rows["edgar"]["calls"] == 4  # browse + fts + 2 filing docs
        assert rows["edgar"]["credits"] == 4.0
        assert rows["edgar"]["usd"] == 0.0
        await p.identify(company(), "legal")  # fully served from the SQLite cache
    assert routes["browse"].call_count == 1
    assert routes["fts"].call_count == 1
    rows = {r["provider"]: r for r in ctx.store.spend_by_provider()}
    assert rows["edgar"]["calls"] == 4


@respx.mock
async def test_cost_override_via_env(ctx, monkeypatch):
    monkeypatch.setenv("EDGAR_COST_PER_UNIT", "0.01")
    mock_edgar()
    async with ctx:
        await provider(ctx).company_info(company())  # one browse call
    row = {r["provider"]: r for r in ctx.store.spend_by_provider()}["edgar"]
    assert row["usd"] == pytest.approx(0.01)


@respx.mock
async def test_doctor(ctx):
    respx.get("https://efts.sec.gov/LATEST/search-index").respond(
        200, json={"hits": {"total": {"value": 123456, "relation": "eq"}, "hits": []}}
    )
    async with ctx:
        msg = await provider(ctx).doctor()
    assert msg.startswith("ok")
    assert "123,456" in msg
