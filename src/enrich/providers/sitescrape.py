"""'sitescrape' provider: the company's own website. Keyless, free, robots.txt-aware.

company_info: homepage + up to 4 linked pages (privacy/terms/contact/about/company)
-> legal entity name from "operated by X, LLC" / copyright lines, plus an alternate
corporate email domain hint harvested from mailto: links.

identify: about/team/leadership/people pages. JSON-LD first (script
type="application/ld+json" with Person / Organization founder/employee entries
carrying a jobTitle) at confidence 0.85; then a heuristic DOM pass pairing
role-matching title lines with adjacent human-name lines at confidence 0.6-0.75.

Every page fetch goes through cached_json(op="page", params={"url": ...}) so
company_info and identify share fetches. robots.txt is fetched once per domain
(op="robots") and paths disallowed for User-agent * are skipped. Sites that 403
(or any 4xx / dead DNS) yield no results — the miss is cached so we don't hammer
them; genuine 5xx failures propagate as net.HttpError.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .. import net
from ..domains import normalize_domain
from ..models import Company, title_matches_role
from .base import CompanyInfo, PersonHit, ProviderBase, ProviderError, register

# Scraping the company's own site is a $0 method (docs/service-research.md, "Free /
# near-free methods"). Override with SITESCRAPE_COST_PER_UNIT (USD per fetched page)
# if a paid fetcher (e.g. Firecrawl paid tier) ever fronts these requests.
COST_PER_PAGE_USD = 0.0
COST_ENV = "SITESCRAPE_COST_PER_UNIT"

MAX_SUBPAGES = 4
# Heavy Shopify/Wix themes inline large JSON-LD + footer link maps well past 600 KB;
# truncating there dropped real founder markup, so cap generously (recall widening).
MAX_HTML_CHARS = 1_500_000
MAX_TEXT_CHARS = 200_000

# 'story'/'founder' catch Shopify '/pages/our-story' and '/founders'; 'press' catches
# founder mentions in press pages. All widen recall on real backtest misses.
IDENTIFY_CATS = ("team", "leadership", "people", "about", "story", "founder", "press")
INFO_CATS = ("privacy", "terms", "contact", "about", "company")

FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "me.com", "msn.com", "live.com", "protonmail.com", "proton.me", "comcast.net",
}

_ENTITY_SUFFIX = (
    r"(?:L\.L\.C\.|LLC|Incorporated|Inc\.?|Corporation|Corp\.?|Company|Co\.|"
    r"Limited|Ltd\.?|PLLC|LLP|L\.P\.|LP|PBC|GmbH|B\.V\.)"
)
_ENTITY_NAME = r"[A-Z][A-Za-z0-9&'’.\- ]{1,60}?"
_OPERATED_RX = re.compile(
    r"(?i:(?:owned\s+and\s+)?operated\s+by|a\s+service\s+of|provided\s+by|property\s+of)\s+"
    r"(?P<ent>" + _ENTITY_NAME + r"(?:,\s*)?" + _ENTITY_SUFFIX + r")(?=[\s.,;)]|$)"
)
_COPYRIGHT_RX = re.compile(
    r"(?:©|\(c\)|(?i:copyright))\s*(?:©\s*)?(?:20\d{2})?(?:\s*[-–—]\s*20\d{2})?[\s,]*(?:(?i:by)\s+)?"
    r"(?P<ent>" + _ENTITY_NAME + r"(?:,\s*)?" + _ENTITY_SUFFIX + r")(?=[\s.,;)]|$)"
)
_MAILTO_RX = re.compile(r"mailto:([A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,}))", re.I)

_NAME_TOKEN_RX = re.compile(r"^[A-Z][A-Za-z'’.]*(?:-[A-Z][A-Za-z'’.]*)?\.?$")
_NAME_STOP = {
    "about", "all", "advisors", "blog", "board", "careers", "chief", "co", "company", "contact",
    "corp", "counsel", "director", "directors", "executive", "faq", "founder", "general", "get",
    "head", "help", "home", "in", "inc", "join", "leadership", "learn", "legal", "llc", "ltd",
    "manager", "meet", "mission", "more", "new", "news", "officer", "our", "people", "policy",
    "president", "privacy", "read", "shop", "staff", "story", "support", "team", "terms", "the",
    "touch", "us", "vice", "view",
}
_LINE_SPLIT_RX = re.compile(r"\s*(?:\||•|·|–|—|:|,|\s-\s)\s*")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def _looks_like_name(s: str) -> bool:
    """2-3 capitalized tokens that read like a human name, not nav text."""
    s = s.strip().strip(".,")
    if not s or "@" in s or any(ch.isdigit() for ch in s):
        return False
    toks = s.split()
    if not (2 <= len(toks) <= 3):
        return False
    long_toks = 0
    for t in toks:
        t = t.strip(",")
        if t.lower().strip(".") in _NAME_STOP:
            return False
        if not _NAME_TOKEN_RX.match(t):
            return False
        if len(t.rstrip(".")) >= 2:
            long_toks += 1
    return long_toks >= 2


def _home_url(website: str, domain: str) -> str:
    site = (website or "").strip()
    if site.startswith(("http://", "https://")):
        return site
    return f"https://{site or domain}"


def _links(html: str, base_url: str, cats: tuple[str, ...]) -> list[str]:
    """Same-domain links whose path or anchor text mentions a category, best-ranked first."""
    base_domain = normalize_domain(urlparse(base_url).netloc)
    home = base_url.rstrip("/")
    soup = BeautifulSoup(html, "html.parser")
    scored: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base_url, href).split("#", 1)[0].rstrip("/")
        if not url or url == home:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            continue
        if normalize_domain(parsed.netloc) != base_domain:
            continue
        hay = (parsed.path + " " + a.get_text(" ", strip=True)).lower()
        for rank, cat in enumerate(cats):
            if cat in hay:
                scored[url] = min(scored.get(url, len(cats)), rank)
                break
    ranked = sorted(scored.items(), key=lambda kv: (kv[1], kv[0]))
    return [u for u, _ in ranked[:MAX_SUBPAGES]]


# ------------------------------------------------------------------ JSON-LD pass
_JSONLD_KEYS = (
    "@graph", "founder", "founders", "employee", "employees",
    "member", "members", "mainEntity", "itemListElement", "item",
)


def _jsonld_people(html: str) -> list[tuple[str, str]]:
    people: list[tuple[str, str]] = []
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = tag.string or tag.get_text()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        _walk_jsonld(data, "", people)
    return people


def _walk_jsonld(node, hint: str, out: list[tuple[str, str]]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_jsonld(item, hint, out)
        return
    if not isinstance(node, dict):
        return
    types = node.get("@type") or ""
    types = types if isinstance(types, list) else [types]
    if "Person" in types:
        name = str(node.get("name") or "").strip()
        title = node.get("jobTitle") or ""
        if isinstance(title, list):
            title = ", ".join(str(t) for t in title)
        title = str(title).strip()
        if not title and hint == "founder":
            title = "Founder"  # a founder entry without jobTitle is still a titled person
        if name and title:
            out.append((name, title))
    for key in _JSONLD_KEYS:
        if key in node:
            _walk_jsonld(node[key], "founder" if key in ("founder", "founders") else hint, out)


# ------------------------------------------------- JSON-LD author pass (about pages)
_AUTHOR_CONTAINER_KEYS = ("@graph", "mainEntity", "hasPart", "itemListElement", "item")


def _jsonld_authors(html: str) -> list[str]:
    """Author name(s) of any Article/*Page JSON-LD node. Caller must gate these on the
    page text actually mentioning a founder/owner — an Article author is otherwise just
    a blog byline, not a company principal."""
    out: list[str] = []
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = tag.string or tag.get_text()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw.strip())
        except ValueError:
            continue
        _walk_authors(data, out)
    return out


def _walk_authors(node, out: list[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_authors(item, out)
        return
    if not isinstance(node, dict):
        return
    types = node.get("@type") or ""
    types = [str(t) for t in (types if isinstance(types, list) else [types])]
    if any(t == "Article" or t.endswith("Article") or t.endswith("Page") for t in types):
        _collect_author_names(node.get("author"), out)
    for key in _AUTHOR_CONTAINER_KEYS:
        if key in node:
            _walk_authors(node[key], out)


def _collect_author_names(author, out: list[str]) -> None:
    if isinstance(author, list):
        for a in author:
            _collect_author_names(a, out)
    elif isinstance(author, str):
        if author.strip():
            out.append(author.strip())
    elif isinstance(author, dict):
        name = str(author.get("name") or "").strip()
        if name:
            out.append(name)


# ------------------------------------------------------------- prose founder pass
# Conservative free-text pass over about/team/story pages: a case-SENSITIVE 2-3
# capitalized-token name group sitting next to a case-insensitive founder/CEO/owner
# keyword. Gated through _looks_like_name/_NAME_STOP so nav/boilerplate yields nothing.
_CAPTOK = r"[A-Z][A-Za-z'’.\-]*"
_NAMERUN = _CAPTOK + r"(?:\s+" + _CAPTOK + r"){1,2}"  # 2-3 capitalized tokens
_ROLE_WORD = (
    r"(?:co[-\s]?founders?|cofounders?|founded\s+by|founders?|"
    r"chief\s+executive(?:\s+officer)?|ceo|owners?|presidents?)"
)
_ROLE_PHRASE = _ROLE_WORD + r"(?i:(?:\s*(?:and|&|,|/)\s*" + _ROLE_WORD + r")?)"

# keyword(s) then name, optionally two names joined by 'and' ("co-founders X and Y")
_RX_ROLE_NAMES = re.compile(
    r"(?P<rolekw>(?i:" + _ROLE_PHRASE + r"))"
    r"['’]?s?\b"
    r"(?i:(?:\s+(?:by|of|is|are|was|were|our|the|at|here|and))*?)"
    r"[\s:,\-–—()]+"
    r"(?P<n1>" + _NAMERUN + r")"
    r"(?:\s*(?:,|and|&)\s*(?P<n2>" + _NAMERUN + r"))?"
)
# name then keyword ("Jane Smith, founder and CEO")
_RX_NAME_ROLE = re.compile(
    r"(?P<n1>" + _NAMERUN + r")"
    r"\s*[,\-–—(]\s*"
    r"(?i:(?:the|our|an?)\s+)?"
    r"(?P<rolekw>(?i:" + _ROLE_PHRASE + r"))\b"
)
# does the page mention a founder/owner at all? gate for the JSON-LD author pass
_FOUNDER_TEXT_RX = re.compile(r"(?i:\b(?:co[-\s]?founders?|founded\s+by|founders?|owners?)\b)")


def _founder_title(kw: str) -> str:
    k = re.sub(r"\s+", " ", kw.lower().replace("’", "'")).strip()
    if "founder" in k or "founded" in k:
        return "Co-Founder" if k.startswith(("co-", "co ", "cofounder")) else "Founder"
    if "owner" in k:
        return "Owner"
    if "president" in k:
        return "President"
    return "CEO"


def _clean_name(s: str) -> str | None:
    """First 2-3 token window of s that reads as a human name (trims a stray leading
    verb like 'Meet' or a trailing noise token), else None."""
    s = re.sub(r"\s+", " ", s).strip().strip(".,")
    toks = s.split()
    cands = [s]
    if len(toks) >= 3:
        cands += [" ".join(toks[1:]), " ".join(toks[:-1])]
    for c in cands:
        if _looks_like_name(c):
            return c.strip().strip(".,")
    return None


def _prose_founders(text: str) -> list[tuple[str, str, float]]:
    """(name, title, 0.6) for founder/CEO/owner mentions in running text. Deduped by name."""
    out: list[tuple[str, str, float]] = []
    seen: set[str] = set()

    def emit(raw_name: str | None, kw: str) -> None:
        if not raw_name:
            return
        name = _clean_name(raw_name)
        if not name:
            return
        key = _norm(name)
        if key in seen:
            return
        seen.add(key)
        out.append((name, _founder_title(kw), 0.6))

    for m in _RX_ROLE_NAMES.finditer(text):
        emit(m.group("n1"), m.group("rolekw"))
        emit(m.group("n2"), m.group("rolekw"))
    for m in _RX_NAME_ROLE.finditer(text):
        emit(m.group("n1"), m.group("rolekw"))
    return out


def _visible_text(html: str) -> str:
    """Running text with script/style/nav/footer/etc. stripped, whitespace-collapsed."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "noscript", "form", "svg", "button", "select"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" "))[:MAX_TEXT_CHARS]


def _is_about_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return "about" in path or "story" in path


# ---------------------------------------------------------------- heuristic DOM pass
def _split_pair(line: str, role: str) -> tuple[str, str] | None:
    """'Jane Smith, CEO' / 'CEO — Jane Smith' style single-line pairs."""
    parts = _LINE_SPLIT_RX.split(line, maxsplit=1)
    if len(parts) != 2:
        return None
    for name_part, title_part in ((parts[0], parts[1]), (parts[1], parts[0])):
        if title_matches_role(title_part, role) and _looks_like_name(name_part):
            return name_part.strip().strip(".,"), title_part.strip()
    return None


def _nearby_name(lines: list[str], i: int) -> tuple[str, float] | None:
    for dist, conf in ((1, 0.7), (2, 0.6)):
        for j in (i - dist, i + dist):
            if 0 <= j < len(lines) and _looks_like_name(lines[j]):
                return lines[j].strip().strip(".,"), conf
    return None


def _dom_people(html: str, role: str) -> list[tuple[str, str, float]]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "noscript", "form", "svg", "button", "select"]):
        tag.decompose()
    lines = [ln.strip() for ln in soup.get_text("\n").split("\n")]
    lines = [ln for ln in lines if ln and len(ln) <= 90]
    hits: list[tuple[str, str, float]] = []
    for i, line in enumerate(lines):
        pair = _split_pair(line, role)
        if pair:
            hits.append((pair[0], pair[1], 0.75))
            continue
        if _looks_like_name(line) or not title_matches_role(line, role):
            continue
        neighbor = _nearby_name(lines, i)
        if neighbor:
            hits.append((neighbor[0], line, neighbor[1]))
    return hits


# --------------------------------------------------------------------------- provider
@register
class SiteScrape(ProviderBase):
    name = "sitescrape"
    key_env = None
    is_free = True
    rps = 2.0

    def _unit_cost(self) -> float:
        raw = self.ctx.settings.key(COST_ENV)
        if raw is None:
            return COST_PER_PAGE_USD
        try:
            return float(raw)
        except ValueError:
            raise ProviderError(f"{COST_ENV} must be a number, got {raw!r}") from None

    # ----------------------------------------------------------- fetching
    async def _page(self, url: str) -> dict:
        async def fetch():
            try:
                resp = await net.fetch(self.ctx.http, "GET", url)
            except net.HttpError as e:
                if 400 <= e.status < 500:  # 403/404/...: their site, their call — cache the miss
                    return {"url": url, "status": e.status, "html": ""}
                raise
            except (httpx.TransportError, httpx.TimeoutException):
                return {"url": url, "status": 0, "html": ""}  # dead domain: cache, don't re-probe
            return {"url": str(resp.url), "status": resp.status_code, "html": resp.text[:MAX_HTML_CHARS]}

        return await self.cached_json("page", {"url": url}, fetch, cost_usd=self._unit_cost(), credits=1.0)

    async def _allowed(self, domain: str, url: str) -> bool:
        async def fetch():
            robots_url = f"https://{domain}/robots.txt"
            try:
                resp = await net.fetch(self.ctx.http, "GET", robots_url)
            except net.HttpError as e:
                if 400 <= e.status < 500:
                    return {"status": e.status, "body": ""}
                raise
            except (httpx.TransportError, httpx.TimeoutException):
                return {"status": 0, "body": ""}
            return {"status": resp.status_code, "body": resp.text[:20_000]}

        robots = await self.cached_json(
            "robots", {"domain": domain}, fetch, cost_usd=self._unit_cost(), credits=1.0
        )
        body = robots.get("body") or ""
        if robots.get("status") != 200 or not body.strip():
            return True  # no robots.txt -> nothing disallowed
        rp = robotparser.RobotFileParser()
        rp.parse(body.splitlines())
        return rp.can_fetch("*", url)

    async def _pages(self, company: Company, cats: tuple[str, ...]) -> list[dict]:
        """Homepage plus up to MAX_SUBPAGES same-domain category pages. [0] is the homepage."""
        domain = company.domain or normalize_domain(company.website or company.name)
        if not domain:
            return []
        home_url = _home_url(company.website, domain)
        if not await self._allowed(domain, home_url):
            return []
        home = await self._page(home_url)
        if home["status"] != 200 or not home["html"]:
            return []
        pages = [home]
        for url in _links(home["html"], home["url"], cats):
            if not await self._allowed(domain, url):
                continue
            pg = await self._page(url)
            if pg["status"] == 200 and pg["html"]:
                pages.append(pg)
        return pages

    # -------------------------------------------------------- capabilities
    async def identify(self, company: Company, role: str) -> list[PersonHit]:
        pages = await self._pages(company, IDENTIFY_CATS)
        if not pages:
            return []
        company_norm = _norm(company.name)
        best: dict[str, PersonHit] = {}

        def add(name: str, title: str, conf: float, url: str, via: str) -> None:
            name = re.sub(r"\s+", " ", name).strip()
            title = re.sub(r"\s+", " ", title).strip()
            if not name or not title or not title_matches_role(title, role):
                return  # requested role only
            if not _looks_like_name(name) or _norm(name) == company_norm:
                return
            key = _norm(name)
            if key not in best or best[key].confidence < conf:
                best[key] = PersonHit(
                    name=name, title=title, role=role, source=self.name, url=url,
                    confidence=conf, raw={"via": via},
                )

        for pg in pages:  # JSON-LD: homepage often carries Organization/founder markup
            for name, title in _jsonld_people(pg["html"]):
                add(name, title, 0.85, pg["url"], "jsonld")
        if role == "ceo":  # founder recall only feeds the ceo role (founder -> ceo taxonomy)
            for pg in pages[1:]:
                text = _visible_text(pg["html"])
                for name, title, conf in _prose_founders(text):
                    add(name, title, conf, pg["url"], "prose")
                # Article/*Page author is a company principal only on about/story pages
                # that actually name a founder/owner — otherwise it's a blog byline.
                if _is_about_url(pg["url"]) and _FOUNDER_TEXT_RX.search(text):
                    for name in _jsonld_authors(pg["html"]):
                        add(name, "Founder", 0.6, pg["url"], "jsonld-author")
        for pg in pages[1:]:  # DOM heuristics only on team/about pages (homepage is too noisy)
            for name, title, conf in _dom_people(pg["html"], role):
                add(name, title, conf, pg["url"], "dom")
        return sorted(best.values(), key=lambda h: h.confidence, reverse=True)

    async def company_info(self, company: Company) -> CompanyInfo | None:
        pages = await self._pages(company, INFO_CATS)
        if not pages:
            return None
        own = {
            d for d in (
                normalize_domain(company.domain), normalize_domain(company.email_domain),
                normalize_domain(company.website),
            ) if d
        }
        entity = ""
        for pg in pages[1:] + pages[:1]:  # privacy/terms/contact/about first, homepage last
            text = re.sub(r"\s+", " ", BeautifulSoup(pg["html"], "html.parser").get_text(" "))
            text = text[:MAX_TEXT_CHARS]
            for rx in (_OPERATED_RX, _COPYRIGHT_RX):
                m = rx.search(text)
                if m:
                    entity = re.sub(r"\s+", " ", m.group("ent")).strip(" ,")
                    break
            if entity:
                break
        domain_votes: Counter[str] = Counter()
        for pg in pages:
            for _, dom in _MAILTO_RX.findall(pg["html"]):
                d = normalize_domain(dom)
                if d and d not in own and d not in FREEMAIL:
                    domain_votes[d] += 1
        email_domain = domain_votes.most_common(1)[0][0] if domain_votes else ""
        if not entity and not email_domain:
            return None
        return CompanyInfo(
            entity_name=entity, email_domain=email_domain, source=self.name,
            raw={"pages": [p["url"] for p in pages]},
        )

    async def doctor(self) -> str:
        return "ok - keyless site scraper (robots.txt-aware; no quota/account endpoint)"
