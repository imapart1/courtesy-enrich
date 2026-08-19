"""newsrss (Google News RSS) adapter — respx-mocked, no network."""

from __future__ import annotations

import httpx
import pytest
import respx

from enrich.models import Company
from enrich.net import HttpError
from enrich.providers.newsrss import NewsRss


@pytest.fixture()
def company() -> Company:
    return Company(key="manual:acme", name="Acme Skincare", website="acmeskincare.com", domain="acmeskincare.com")


def rss(items: list[tuple[str, str]]) -> str:
    body = "".join(
        f"<item><title>{t}</title><link>{u}</link>"
        f"<pubDate>Mon, 17 Aug 2026 12:00:00 GMT</pubDate></item>"
        for t, u in items
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel><title>Google News</title>' + body + "</channel></rss>"
    )


LEGAL_FEED = rss(
    [
        ("Acme Skincare Appoints Jane Smith as General Counsel - PR Newswire",
         "https://news.google.com/rss/articles/abc123"),
        # non-appointment noise -> ignored
        ("Acme Skincare sued over ad claims - Reuters",
         "https://news.google.com/rss/articles/def456"),
        # appointment language, wrong company -> ignored
        ("Widget Corp Appoints Bob Jones as General Counsel - BusinessWire",
         "https://news.google.com/rss/articles/jkl000"),
        # right company, no person name extractable -> ignored
        ("Acme Skincare Names New General Counsel - Law360",
         "https://news.google.com/rss/articles/mno111"),
    ]
)


def mock_rss(text: str, status: int = 200) -> respx.Route:
    return respx.get(host="news.google.com", path="/rss/search").mock(
        return_value=httpx.Response(status, text=text)
    )


@respx.mock
async def test_identify_parses_appointment(ctx, company):
    mock_rss(LEGAL_FEED)
    async with ctx:
        hits = await NewsRss(ctx).identify(company, "legal")
    assert len(hits) == 1
    h = hits[0]
    assert h.name == "Jane Smith"
    assert h.role == "legal"
    assert h.title.lower() == "general counsel"
    assert h.url == "https://news.google.com/rss/articles/abc123"
    assert 0.5 <= h.confidence <= 0.6
    assert h.source == "newsrss"


@respx.mock
async def test_joins_as_shape_for_cfo(ctx, company):
    mock_rss(rss([
        ("Maria Lopez Joins Acme Skincare as Chief Financial Officer - Yahoo Finance",
         "https://news.google.com/rss/articles/cfo1"),
    ]))
    async with ctx:
        hits = await NewsRss(ctx).identify(company, "cfo")
    assert [h.name for h in hits] == ["Maria Lopez"]
    assert hits[0].role == "cfo"


@respx.mock
async def test_role_gate_skips_ceo_without_fetch(ctx, company):
    route = mock_rss(LEGAL_FEED)
    async with ctx:
        assert await NewsRss(ctx).identify(company, "ceo") == []
        assert await NewsRss(ctx).identify(company, "privacy") == []
    assert not route.called


@respx.mock
async def test_noise_only_feed_returns_empty(ctx, company):
    mock_rss(rss([
        ("Acme Skincare launches new retinol line - Allure",
         "https://news.google.com/rss/articles/x1"),
    ]))
    async with ctx:
        assert await NewsRss(ctx).identify(company, "legal") == []


@respx.mock
async def test_malformed_xml_returns_empty(ctx, company):
    mock_rss("<rss><channel><item><title>broken")
    async with ctx:
        assert await NewsRss(ctx).identify(company, "legal") == []


@respx.mock
async def test_http_error_propagates(ctx, company):
    mock_rss("nope", status=404)
    async with ctx:
        with pytest.raises(HttpError) as exc:
            await NewsRss(ctx).identify(company, "legal")
    assert exc.value.status == 404


@respx.mock
async def test_free_available_and_zero_spend(ctx, company):
    route = mock_rss(LEGAL_FEED)
    p = NewsRss(ctx)
    ok, why = p.available()          # conftest ctx is free_only=True; keyless provider passes
    assert ok and why == ""
    async with ctx:
        await p.identify(company, "legal")
        await p.identify(company, "legal")   # cache hit
    assert route.call_count == 1
    rows = {r["provider"]: r for r in ctx.store.spend_by_provider(ctx.run_id)}
    assert rows["newsrss"]["calls"] == 1
    assert rows["newsrss"]["usd"] == 0.0
    assert rows["newsrss"]["credits"] == 0.0


@respx.mock
async def test_doctor(ctx):
    mock_rss(LEGAL_FEED)
    async with ctx:
        msg = await NewsRss(ctx).doctor()
    assert msg.startswith("ok (keyless)")
