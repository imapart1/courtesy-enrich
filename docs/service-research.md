# Service Research — Contact Enrichment Providers

Pricing verified against official pages **2026-08-12** (exceptions flagged ⚠️). Volume model: ~800 person-lookups upfront (a few hundred companies × ~3 roles), then ~150–450/mo.

## TL;DR — recommended stack

| Layer | Pick | Why | Cost |
|---|---|---|---|
| Identify (who is CEO/COO/GC) | Site scrape + **Serper.dev** SERP + **Apollo Free** People Search + Google News RSS | All free; Apollo title+domain search costs 0 credits | $0 |
| Email pattern | **Hunter.io** | Only service returning a native `pattern` field per domain | Free 50/mo → Starter $49/mo in heavy months |
| Email find | **Hunter Email Finder** → **Anymail Finder** fallback | Anymail charges only for verified-deliverable emails; its decision-maker search (2 cr) finds "the CEO of X" without a name | $49 mo-1; $29/mo heavy months |
| Long tail | **Exa** search+contents | ~$8 for entire backlog — inside free credits ($20 signup + $10/mo) | ~$0 |
| Verify | **MillionVerifier** 10k pack | ~$39 one-time, never expires, explicit `catch_all`, refunds unknowns | $39 |
| Optional researcher | Claude + web search | $10/1k searches → ~$13–40 for backlog | as needed |

**Backlog estimate: ~$90–130 one-time. Steady state: $0–49/mo.**

---

## Email finders

| Service | Free tier | Entry paid | $/successful lookup | Pattern discovery | Notes |
|---|---|---|---|---|---|
| **Hunter.io** | 50 credits/mo recurring | Starter $49/mo = 2,000 credits ($34/mo annual) | ~$0.017–0.025; verify 0.5 credit | **Native `pattern` field** on Domain Search | Finder misses free; verifier returns `accept_all`; 15 req/s. The pattern-detection backbone. |
| **Anymail Finder** | 100-credit/14-day trial (card required) | $29/mo = 400 cr; $49/mo = 1,000 cr; credits roll over uncapped | $0.049–0.0725 **per verified-deliverable email only** | Company Search (1 cr → 20 emails) infers pattern | Live-verifies even catch-all (M365/GWS) before charging; **decision-maker search by role = 2 cr**; hard-bounce refund. |
| **Prospeo.io** | 75 credits/mo recurring | Starter $49/mo = 1,000 finder + 2,000 verifier | ~$0.049, verification included | No native field | `enrich-person` (name+site or LinkedIn URL); no charge on miss; 90-day dedupe free. ⚠️ pricing page JS-blocked; $49 from their own comparison page. |
| **Findymail** | 10 one-time | $99/mo = 5,000 finder + 5,000 verify | **$0.0198 verified** | No; domain-search endpoint deprecated | Best $/verified + accuracy reputation, but 10× our volume — only if bounce rates disappoint. |
| **LeadMagic** | unclear | $49.99/mo = 2,000 cr | $0.025 valid email; validation 0.25 cr | No | Fine fallback API; no confirmed free tier. |
| **Snov.io** | 50 trial credits, **no API on free** | $39/mo = 1,000 cr | ~$0.078 verified (find+verify each 1 cr) | No | Outreach-suite-weighted; skip. |

## Email verifiers

| Service | Free | Pay-as-you-go | Catch-all | Unknowns charged? |
|---|---|---|---|---|
| **MillionVerifier** | 100 one-time | 10k ≈ $37–39 ⚠️(slider bot-blocked; 50k=$89 official); **never expire** | explicit `catch_all` | **Refunded** ("Risky Email Refund") |
| **ZeroBounce** | **100/mo recurring** | 2k=$16 · 10k=$65 ⚠️(JS calculator; 3rd-party) | `catch-all` + rich sub-statuses | Not charged |
| **Reoon** | **~600/mo recurring** (most generous, has API) | — | yes | — |
| **NeverBounce** | ⚠️ conflicting | ~$8/1k ⚠️(site 403'd, all 3rd-party) | `catchall` | **Charged regardless** — skip |

## People databases (identify + reveal)

| Service | Free tier | Entry paid | API | Verdict |
|---|---|---|---|---|
| **Apollo.io** | **900 credits/seat/yr (~75/mo)**; **API confirmed on Free** (pricing comparison table + developer FAQ: "all Apollo plans include access to our API"), 600 req/day, record-selection limit 25 | Basic $49/seat/mo annual (30k cr/yr) or **$69 monthly**; email reveal = 1 cr, phone = 8 | All tiers; **People Search `POST /api/v1/mixed_people/api_search` = 0 credits**; `people/match` enrichment spends credits (0 if nothing found); **org search costs 1 cr/page** | **Best SMB/DTC coverage odds**; free tier is our Stage-2 identify workhorse. **Free plan requires signup with a work-email domain** to use search/enrich at all. Master API key is a per-key toggle, not a plan gate. Add Basic if hit-rate disappoints. |
| **RocketReach** | 5 lookups | Essentials $69/mo = 100 lookups; $399/yr = unlimited lookups but 1,200 exports/yr; API on paid | Person Search + refund-if-not-found | Monthly tier too small for backlog; annual export cap < our volume → Pro $899/yr. Pass. |
| **People Data Labs** | 100 records/mo **but emails obfuscated on free** | Pro $98/mo (350 records) | Person Search API (SQL, `job_title_levels: cxo` + domain) | Great schema/search ergonomics; weak fresh-email coverage on tiny LLCs; pricier than Apollo. Pass for v1. |
| **ContactOut** | 5 emails+5 phones/day | $49/mo ⚠️promo, 300 exports | **API sales-gated** | LinkedIn-sourced; fine as manual free fallback only. |
| **Lusha** | 40 credits/mo | Starter $37.45/mo ⚠️promo, 4,800 cr/yr | **API only from Pro** ($48.95/mo ⚠️promo); search+reveal double-billing | Manual free fallback only. |
| **Wiza** | 20 valid emails ⚠️recurrence unclear | $49/mo = 100 valid emails; pay-only-for-valid | **API sales-gated (Team)** | UI-first; occasional heavy-month helper. |
| **Clay** | 100 credits/mo, 200-row tables | Launch **$185/mo** | HTTP API only at Growth $495/mo | Orchestrator — we're building the waterfall ourselves. Free tier ok as a hit-rate pilot. Skip paid. |
| **ZoomInfo** | — | ⚠️ ~$15k/yr, 3-seat min, sales-gated (3rd-party) | — | Wrong size & coverage profile. Skip. |

## Exa.ai (user-suggested)

Verified at [exa.ai/pricing](https://exa.ai/pricing) + docs:

- **Search $7/1k requests** (contents of top-10 results now included); deep variants $12–15/1k; contents $1/1k pages; Answer $5/1k.
- **Agent API** (replaced the deprecated `/research` endpoint, Apr 2026): $0.012–$1.00/request by effort or $0.10/ACU; adders: search $0.005, **email enrichment $0.02/email**, phone $0.07.
- **Free credits: $20 signup + $10/mo** — our whole-backlog search usage (~$8) fits inside.
- **Websets** (managed enrichment lists): ⚠️ billing page 429'd — search-sourced: Starter $49/mo = 8,000 credits, 100 results/webset (a few hundred companies ⇒ a handful of websets), 2 credits/enrichment row. Managed one-shot for a backlog ≈ **$49**. Docs confirm enrichments can return contact info/emails.
- Rate limits: search 10 QPS. SDKs: `exa-py`, `exa-js`. Structured outputs supported.
- **Role in our pipeline:** long-tail resolver after free methods (Stage 2 step 5), and Websets as a no-build managed alternative if we'd rather not run M2 ourselves.

## Free / near-free methods

| Method | Cost | Use | Caveats |
|---|---|---|---|
| Company site /about /team scrape | $0 (plain fetch; **Firecrawl free 1k pages/mo** for JS sites) | CEO/founder, some COO | Many small brands have no team page; respect robots.txt |
| SERP → public LinkedIn (`site:linkedin.com/in`) | **Serper.dev 2,500 free queries**, then ~$0.30–1/1k; SerpAPI 250/mo free, $10–25/1k | Title confirmation | Never scrape LinkedIn directly (ToS); ⚠️ Google CSE API closed to new users (retires Jan 2027); Bing API retired Aug 2025 |
| Google News RSS + GDELT | $0, no key | "appoints General Counsel/COO" announcements — best free GC source | Noisy; small brands often have zero press |
| SEC EDGAR full-text API | $0 | Officers of the public-company minority | Useless for private LLCs; ≤10 req/s + UA header |
| State SoS registries / OpenCorporates | $0 manual | Entity confirmation, managing members | Officer names vary by state; **OpenCorporates API has no free commercial tier** (£2,250/yr entry) — manual only |
| Permutation + SMTP probe | $0 | Guess construction | M365 ~51% reliable for probes, GWS ~91%; catch-all undetectable via SMTP → use verifier APIs instead |
| Claude web-search tool | **$10/1k searches** + tokens | LLM researcher for stubborn cases | ~5 searches/company ⇒ ~$13 + ~$20–40 tokens for backlog |

## Official MCP servers (verified 2026-08-13)

| Provider | Endpoint | Auth | Notes |
|---|---|---|---|
| **Apollo** | `https://mcp.apollo.io/mcp` | OAuth 2.0 only | First-party; 53 actions; **no additional cost**, same credits; People Search 0 credits here too; MCP rate limits unpublished; Apollo ToS **prohibits AI model training** via MCP |
| **Hunter** | `https://mcp.hunter.io/mcp` | OAuth or `X-API-Key` | Domain Search, Email Finder, Verifier, enrichment; works on free plan; old `hunter-io/hunter-mcp` repo is **archived** |
| **Exa** | `https://mcp.exa.ai/mcp` | API key or OAuth | Also `npx -y exa-mcp-server`; hosted server allows anonymous rate-limited use |
| **Firecrawl** | `https://mcp.firecrawl.dev/v2/mcp` | Bearer token | Keyless mode covers scrape/search/parse; docs insist keys go in headers, never the URL |
| Anymail Finder / MillionVerifier / Serper | — | — | **No official MCP** — plain REST adapters |

⚠️ Community repos named `apollo-io-mcp` are **not** Apollo products (they request raw API keys); `apollographql/apollo-mcp-server` is an unrelated company.

## Verification caveats (carry into any purchase decision)

- ⚠️ Exa **Websets** plan pricing is search-sourced (billing page rate-limited) — reconfirm before relying on it.
- ⚠️ NeverBounce (403), ZoomInfo (blocked), MillionVerifier 10k-slider, Prospeo pricing page: third-party-sourced numbers as flagged above.
- ⚠️ Lusha/ContactOut prices are active promos; Wiza/RocketReach free-tier recurrence unstated.
- Accuracy claims (97–98%, bounce <5%) are vendor claims, not independently verified.

## Sources

Hunter [pricing](https://hunter.io/pricing)/[API](https://hunter.io/api-documentation/v2) · Anymail [pricing](https://anymailfinder.com/pricing)/[API](https://anymailfinder.com/api) · Prospeo [docs](https://prospeo.io/api-docs/enrich-person) · Findymail [pricing](https://www.findymail.com/pricing/) · LeadMagic [pricing](https://leadmagic.io/pricing) · Snov [pricing](https://snov.io/pricing) · MillionVerifier [site](https://www.millionverifier.com/)/[API](https://developer.millionverifier.com) · ZeroBounce [pricing](https://www.zerobounce.net/email-validation-pricing) · Apollo [pricing](https://www.apollo.io/pricing)/[rate limits](https://docs.apollo.io/docs/rate-limits) · RocketReach [pricing](https://rocketreach.co/pricing)/[docs](https://docs.rocketreach.co) · PDL [pricing](https://www.peopledatalabs.com/pricing)/[docs](https://docs.peopledatalabs.com) · ContactOut [pricing](https://contactout.com/pricing) · Lusha [pricing](https://www.lusha.com/pricing/) · Wiza [pricing](https://wiza.co/pricing) · Clay [pricing](https://www.clay.com/pricing) · Exa [pricing](https://exa.ai/pricing)/[docs pricing](https://exa.ai/docs/reference/pricing)/[websets](https://exa.ai/docs/websets/overview) · Firecrawl [pricing](https://www.firecrawl.dev/pricing) · Serper [site](https://serper.dev/) · SerpAPI [pricing](https://serpapi.com/pricing) · Anthropic [web search tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool) · EDGAR [FTS](https://www.sec.gov/edgar/search/) · OpenCorporates [pricing](https://opencorporates.com/pricing/) · ZoomInfo ballpark: UpLead/Factors/Lead411/Cleanlist 2026 guides · Benchmarks: Openbenchmarks Apollo-vs-PDL.
