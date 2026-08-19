"""sitescrape provider: respx-mocked HTTP, no real network. Keyless + free, so no
API-key env or free_only toggling is needed (available() is asserted directly)."""

import pytest
import respx

from enrich.models import Company
from enrich.net import HttpError
from enrich.providers.sitescrape import SiteScrape, _jsonld_authors, _prose_founders

HOME_HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Organization","name":"Acme Widgets",
 "founder":{"@type":"Person","name":"Alice Womack"}}
</script>
</head><body>
<nav><a href="/team">Team</a><a href="/about">About</a><a href="/contact">Contact</a></nav>
<footer>
<a href="/privacy-policy">Privacy Policy</a>
<a href="/terms">Terms of Service</a>
<a href="https://twitter.com/acme">Twitter</a>
</footer>
</body></html>
"""

TEAM_HTML = """
<html><head>
<script type="application/ld+json">
[{"@type":"Person","name":"Carol Chen","jobTitle":"Chief Executive Officer"}]
</script>
</head><body>
<h2>Meet the team</h2>
<div><h3>Bob Loblaw</h3><p>General Counsel</p></div>
</body></html>
"""

ABOUT_HTML = "<html><body><p>We make widgets with love.</p></body></html>"
PRIVACY_HTML = (
    "<html><body><p>This website is operated by Acme Widgets, LLC.</p>"
    '<p>Questions? <a href="mailto:legal@acmegroupco.com">email legal</a></p></body></html>'
)
TERMS_HTML = "<html><body><p>(c) 2026 Acme Widgets Inc. All rights reserved.</p></body></html>"
CONTACT_HTML = '<html><body><a href="mailto:hello@acmegroupco.com">say hello</a></body></html>'
ROBOTS_OK = "User-agent: *\nDisallow: /admin\n"


def company() -> Company:
    return Company(key="manual:acme", name="Acme Widgets", website="acme.com", domain="acme.com")


def provider(ctx) -> SiteScrape:
    p = SiteScrape(ctx)
    p.rps = 10_000.0  # keep the polite rate limiter out of unit tests
    return p


def mock_site(robots: str = ROBOTS_OK) -> dict:
    respx.get("https://acme.com/robots.txt").respond(200, text=robots)
    return {
        "home": respx.get("https://acme.com/").respond(200, text=HOME_HTML),
        "team": respx.get("https://acme.com/team").respond(200, text=TEAM_HTML),
        "about": respx.get("https://acme.com/about").respond(200, text=ABOUT_HTML),
        "privacy": respx.get("https://acme.com/privacy-policy").respond(200, text=PRIVACY_HTML),
        "terms": respx.get("https://acme.com/terms").respond(200, text=TERMS_HTML),
        "contact": respx.get("https://acme.com/contact").respond(200, text=CONTACT_HTML),
    }


def test_available_keyless(ctx):
    # keyless + is_free: usable even in the conftest ctx's --free-only mode, no key needed
    assert SiteScrape(ctx).available() == (True, "")


async def test_doctor(ctx):
    msg = await SiteScrape(ctx).doctor()
    assert msg.startswith("ok")


@respx.mock
async def test_identify_jsonld_and_dom(ctx):
    mock_site()
    async with ctx:
        p = provider(ctx)
        ceo = await p.identify(company(), "ceo")
        by_name = {h.name: h for h in ceo}
        assert set(by_name) == {"Alice Womack", "Carol Chen"}
        assert by_name["Alice Womack"].title == "Founder"  # founder entry without jobTitle
        assert by_name["Alice Womack"].confidence == 0.85
        assert by_name["Carol Chen"].confidence == 0.85
        assert all(h.role == "ceo" and h.source == "sitescrape" for h in ceo)

        legal = await p.identify(company(), "legal")  # DOM heuristic: name line + title line
        assert [h.name for h in legal] == ["Bob Loblaw"]
        assert legal[0].title == "General Counsel"
        assert 0.6 <= legal[0].confidence <= 0.75
        assert legal[0].url == "https://acme.com/team"


@respx.mock
async def test_identify_no_people_returns_empty(ctx):
    respx.get("https://acme.com/robots.txt").respond(404)  # no robots.txt -> allow all
    respx.get("https://acme.com/").respond(200, text=ABOUT_HTML)
    async with ctx:
        assert await provider(ctx).identify(company(), "ceo") == []


@respx.mock
async def test_site_403_returns_empty(ctx):
    respx.get("https://acme.com/robots.txt").respond(200, text=ROBOTS_OK)
    respx.get("https://acme.com/").respond(403)
    async with ctx:
        p = provider(ctx)
        assert await p.identify(company(), "ceo") == []
        assert await p.company_info(company()) is None


@respx.mock
async def test_http_5xx_propagates(ctx):
    respx.get("https://acme.com/robots.txt").respond(200, text=ROBOTS_OK)
    respx.get("https://acme.com/").respond(501)  # non-retryable 5xx: a real failure
    async with ctx:
        with pytest.raises(HttpError):
            await provider(ctx).identify(company(), "ceo")


@respx.mock
async def test_robots_disallowed_paths_skipped(ctx):
    routes = mock_site(robots="User-agent: *\nDisallow: /team\n")
    async with ctx:
        p = provider(ctx)
        legal = await p.identify(company(), "legal")
    assert legal == []  # Bob Loblaw lives on the disallowed /team page
    assert routes["team"].call_count == 0
    assert routes["about"].call_count == 1


@respx.mock
async def test_company_info(ctx):
    mock_site()
    async with ctx:
        info = await provider(ctx).company_info(company())
    assert info is not None
    assert info.entity_name == "Acme Widgets, LLC"  # "operated by" beats the copyright line
    assert info.email_domain == "acmegroupco.com"  # mailto: domain differing from acme.com
    assert info.source == "sitescrape"


@respx.mock
async def test_cache_and_ledger(ctx, monkeypatch):
    monkeypatch.delenv("SITESCRAPE_COST_PER_UNIT", raising=False)
    routes = mock_site()
    async with ctx:
        p = provider(ctx)
        await p.identify(company(), "ceo")
        rows = {r["provider"]: r for r in ctx.store.spend_by_provider()}
        assert rows["sitescrape"]["calls"] == 4  # robots + home + /team + /about
        assert rows["sitescrape"]["credits"] == 4.0
        assert rows["sitescrape"]["usd"] == 0.0
        await p.identify(company(), "legal")  # same pages -> served from the SQLite cache
    assert routes["home"].call_count == 1
    assert routes["team"].call_count == 1
    rows = {r["provider"]: r for r in ctx.store.spend_by_provider()}
    assert rows["sitescrape"]["calls"] == 4


@respx.mock
async def test_cost_override_via_env(ctx, monkeypatch):
    monkeypatch.setenv("SITESCRAPE_COST_PER_UNIT", "0.002")
    mock_site()
    async with ctx:
        await provider(ctx).identify(company(), "ceo")
    row = {r["provider"]: r for r in ctx.store.spend_by_provider()}["sitescrape"]
    assert row["usd"] == pytest.approx(0.008)  # 4 fetched URLs x $0.002


# --------------------------------------------------------- recall widenings (Fix A)

FOUNDER_HOME_HTML = '<html><body><nav><a href="/about">About</a></nav></body></html>'
FOUNDER_ABOUT_HTML = """
<html><body>
<nav><a href="/">Home</a></nav>
<main>
<h1>Our Story</h1>
<p>Acme Widgets was founded by Dana Wells in 2015. Our co-founders Eric Martinez
and Jen Thorson lead product and design.</p>
</main>
<footer><a href="/careers">Careers</a></footer>
</body></html>
"""

AUTHOR_HOME_HTML = '<html><body><a href="/about-us">About</a></body></html>'
AUTHOR_ABOUT_HTML = """
<html><head>
<script type="application/ld+json">
{"@type":"AboutPage","headline":"Our Story","author":{"@type":"Person","name":"Sam Rivera"}}
</script>
</head><body><main><h1>Our Story</h1>
<p>Sam started us. As founder, this is my letter.</p></main></body></html>
"""
NOAUTHOR_ABOUT_HTML = """
<html><head>
<script type="application/ld+json">
{"@type":"Article","author":{"@type":"Person","name":"Guest Blogger"}}
</script>
</head><body><main><p>Ten tips for better widgets.</p></main></body></html>
"""
TEAM_AUTHOR_HOME_HTML = '<html><body><a href="/team">Team</a></body></html>'
TEAM_AUTHOR_HTML = """
<html><head>
<script type="application/ld+json">
{"@type":"Article","author":"News Byline"}
</script>
</head><body><main><p>Our founder story, retold by the press.</p></main></body></html>
"""


def test_prose_founder_pass_extracts_names():
    # plural keyword -> both names; must survive the _looks_like_name/_NAME_STOP gate
    both = _prose_founders("Our co-founders Eric Martinez and Jen Thorson built this.")
    assert {n for n, _, _ in both} == {"Eric Martinez", "Jen Thorson"}
    assert all(conf == 0.6 for _, _, conf in both)
    assert [t for _, t, _ in both] == ["Co-Founder", "Co-Founder"]
    # 'founded by' and name-before-role phrasings
    assert ("Dana Wells", "Founder", 0.6) in _prose_founders("Acme was founded by Dana Wells.")
    assert ("Jane Smith", "Founder", 0.6) in _prose_founders("Jane Smith, founder and CEO, leads.")


def test_prose_founder_pass_ignores_nav_and_boilerplate():
    # a wall of nav labels carries no founder/name pairing -> nothing
    nav = "Home About Team Careers Blog Privacy Founders Leadership Our Story Contact Us"
    assert _prose_founders(nav) == []
    # a role with no adjacent capitalized name yields nothing
    assert _prose_founders("A message from our founder.") == []
    # a non-ceo role line does not leak in
    assert _prose_founders("The General Counsel Bob Loblaw joined in 2020.") == []


def test_jsonld_author_walk_shapes():
    a = _jsonld_authors('<script type="application/ld+json">'
                        '{"@type":"AboutPage","author":["A B",{"name":"C D"}]}</script>')
    assert a == ["A B", "C D"]
    # non-Article/Page nodes carry no byline
    assert _jsonld_authors('<script type="application/ld+json">'
                           '{"@type":"Organization","name":"Acme"}</script>') == []


@respx.mock
async def test_identify_prose_founders(ctx):
    respx.get("https://acme.com/robots.txt").respond(404)  # allow all
    respx.get("https://acme.com/").respond(200, text=FOUNDER_HOME_HTML)
    respx.get("https://acme.com/about").respond(200, text=FOUNDER_ABOUT_HTML)
    async with ctx:
        ceo = await provider(ctx).identify(company(), "ceo")
    by_name = {h.name: h for h in ceo}
    assert {"Dana Wells", "Eric Martinez", "Jen Thorson"} <= set(by_name)
    assert "Acme Widgets" not in by_name  # the company itself is never a person
    prose = [h for h in ceo if h.raw.get("via") == "prose"]
    assert prose and all(h.confidence == 0.6 and h.role == "ceo" for h in prose)
    assert all(h.url == "https://acme.com/about" for h in prose)


@respx.mock
async def test_identify_prose_founders_absent_for_other_roles(ctx):
    respx.get("https://acme.com/robots.txt").respond(404)
    respx.get("https://acme.com/").respond(200, text=FOUNDER_HOME_HTML)
    respx.get("https://acme.com/about").respond(200, text=FOUNDER_ABOUT_HTML)
    async with ctx:
        legal = await provider(ctx).identify(company(), "legal")
    assert legal == []  # the prose founder pass only feeds the ceo role


@respx.mock
async def test_jsonld_author_founder_on_about_page(ctx):
    respx.get("https://acme.com/robots.txt").respond(404)
    respx.get("https://acme.com/").respond(200, text=AUTHOR_HOME_HTML)
    respx.get("https://acme.com/about-us").respond(200, text=AUTHOR_ABOUT_HTML)
    async with ctx:
        ceo = await provider(ctx).identify(company(), "ceo")
    by_name = {h.name: h for h in ceo}
    assert "Sam Rivera" in by_name
    assert by_name["Sam Rivera"].confidence == 0.6
    assert by_name["Sam Rivera"].raw["via"] == "jsonld-author"


@respx.mock
async def test_jsonld_author_ignored_without_founder_text(ctx):
    respx.get("https://acme.com/robots.txt").respond(404)
    respx.get("https://acme.com/").respond(200, text='<html><body><a href="/about">About</a></body></html>')
    respx.get("https://acme.com/about").respond(200, text=NOAUTHOR_ABOUT_HTML)
    async with ctx:
        ceo = await provider(ctx).identify(company(), "ceo")
    assert ceo == []  # about page, but no founder/owner mention -> byline is not a principal


@respx.mock
async def test_jsonld_author_ignored_off_about_category(ctx):
    respx.get("https://acme.com/robots.txt").respond(404)
    respx.get("https://acme.com/").respond(200, text=TEAM_AUTHOR_HOME_HTML)
    respx.get("https://acme.com/team").respond(200, text=TEAM_AUTHOR_HTML)
    async with ctx:
        ceo = await provider(ctx).identify(company(), "ceo")
    assert ceo == []  # /team is not an about-category page -> Article byline ignored
