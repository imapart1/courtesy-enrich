# Signup Checklist

**Verified 2026-08-13.** Register every account with a **work-email domain** (Apollo's free tier silently drops search/enrichment on Gmail signups). Store keys in a password manager; paste them into `.env` locally (never commit, never paste keys into chat — the pipeline reads them from disk).

I can't create accounts or enter payment details for you — every row below is yours to click through. Once an account exists I can wire up the MCP connection and the API adapter.

---

## Phase 1 — Sign up now (6 accounts, all free, no credit card)

| # | Service | Plan | What we get | Card? |
|---|---|---|---|---|
| 1 | **[Apollo.io](https://apollo.io)** | Free | People Search API at **0 credits** (identify CEO/COO/GC by title+domain) + 75 credits/mo for email reveals + official MCP | No |
| 2 | **[Hunter.io](https://hunter.io)** | Free | 50 credits/mo — **the only native email-pattern detector**; also verifier + official MCP | No |
| 3 | **[Exa.ai](https://exa.ai)** | Free | $20 signup credits + $10/mo recurring — covers the entire long-tail search (~$8) + official MCP | No |
| 4 | **[Serper.dev](https://serper.dev)** | Free | 2,500 one-time queries — SERP lookups to confirm titles via public LinkedIn | No |
| 5 | **[MillionVerifier](https://www.millionverifier.com)** | Free | 100 verifications to pilot; buy the credit pack in Phase 2 | No |
| 6 | **[Firecrawl](https://www.firecrawl.dev)** | Free | 1,000 pages/mo for JS-rendered team pages (optional — plain fetch handles most) + official MCP | No |

### Apollo setup — three gotchas that matter

1. **Register with a work-email domain.** Apollo docs: free accounts registered with a *personal* email address can't use people/company search or enrichment. A work-email domain lifts that. This is the single most important step on the page.
2. **Be an admin on the workspace** — required to create an API key (Settings → Integrations → API).
3. **Turn off model training in whatever AI client connects to the MCP** — Apollo's terms prohibit AI model training through Apollo MCP integrations.

Also check `app.apollo.io/#/settings/credits/current` after signup: new accounts get the current credit system (900 credits/seat/year, granted ~75/month). Older writeups describing "unlimited free email credits" refer to a legacy system that new signups don't get.

---

## Phase 2 — Buy only when a paid backlog run starts (~$90–130)

Do **not** buy these yet. They come after the back-test gate passes on free tiers (SPEC §7), so we spend against a measured hit-rate rather than a guess.

| # | Service | Purchase | Cost | Trigger |
|---|---|---|---|---|
| 7 | **Hunter.io Starter** | Upgrade for one month, 2,000 credits | **$49** (month-to-month; $34/mo if we later go annual) | Paid backlog run begins — 50 free credits/mo can't cover hundreds of domains |
| 8 | **MillionVerifier** | 10,000-credit pack | **~$39 one-time**, never expires (≈2 years of supply) | Same time as #7 |
| 9 | **Anymail Finder** | $49/mo tier, cancel after | **$49**, only in heavy months | Only if Hunter's hit-rate leaves >20% of companies unresolved. **Card required even for their trial.** |

Downgrade Hunter back to Free after the backlog clears if monthly volume stays under ~50 companies.

---

## Do NOT sign up for these

| Service | Why not |
|---|---|
| **ZoomInfo** | ~$15k/yr, 3-seat minimum, sales-gated — and weakest exactly where our targets live (small private DTC brands) |
| **Clay** | $185/mo for waterfall orchestration we're building ourselves |
| **RocketReach** | Monthly tier (100 lookups) too small for the backlog; annual export cap under our volume → $899/yr for parity |
| **People Data Labs** | Free tier obfuscates emails; $98/mo Pro is 2× Apollo for weaker small-company email coverage |
| **Lusha / ContactOut / Wiza** | APIs are sales-gated or higher-tier only; useful only as manual free fallbacks |
| **Snov.io** | No API on free tier; ~$0.078 per verified email |
| **NeverBounce** | Charges for "unknown" results — MillionVerifier refunds them |
| **Findymail** | Best per-email price but $99/mo minimum ≈ 10× our volume |
| **OpenCorporates** | No free commercial API tier (£2,250/yr entry) — use the free web UI manually when needed |

---

## MCP servers (verified first-party, 2026-08-13)

Four of our providers publish official MCP servers, so those integrations are largely configuration rather than adapter code — and you get the same tools interactively in Claude.

| Provider | Endpoint | Auth | Cost |
|---|---|---|---|
| **Apollo** | `https://mcp.apollo.io/mcp` | OAuth (no API key) | No extra cost; same credit pool |
| **Hunter** | `https://mcp.hunter.io/mcp` | OAuth or `X-API-Key` | Uses existing credits; works on free plan |
| **Exa** | `https://mcp.exa.ai/mcp` | API key or OAuth | Anonymous use allowed, rate-limited |
| **Firecrawl** | `https://mcp.firecrawl.dev/v2/mcp` | Bearer token | Keyless mode covers scrape/search |

```bash
claude mcp add --transport http apollo https://mcp.apollo.io/mcp
```

Then run `/mcp` in an interactive Claude Code session to complete OAuth. Same pattern for the other three.

**Warnings:** several community repos named "apollo-io-mcp" are *not* Apollo products and ask for your raw API key — use only `mcp.apollo.io`. `apollographql/apollo-mcp-server` is a different company entirely. Hunter's old `hunter-io/hunter-mcp` GitHub repo is archived; the remote endpoint replaced it. Anymail Finder, MillionVerifier, and Serper have no official MCP — those stay as plain API adapters.

---

## After signup, send me nothing — do this instead

1. Copy `.env.example` → `.env` and paste each key in.
2. Confirm which accounts are live, then run the free stack + back-test against known rows (`enrich backtest --free-only` — costs $0).
