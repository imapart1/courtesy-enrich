# Courtesy Email Contact Enrichment

Pipeline that enriches Schallert PC's courtesy-email call sheet: for each defendant company it finds the **Legal decision-maker (GC/CLO), CEO, COO** (plus CFO and Privacy) with **name, title, and verified company email** — falling back to detecting the company's email naming convention and constructing verified guesses, exactly like the manual process but automated, cached, cost-capped, and multi-agent.

**Status: built (v0.1).** Core pipeline + 13 provider adapters + tests. Runs today in free mode with zero API keys; paid providers switch on the moment their key lands in `.env`.

## Quickstart

```bash
uv sync                                        # one-time install
uv run enrich pull "data/input/contact-research-2026-08-12.csv"   # learn 284 completed rows, queue 263
uv run enrich add "Blissy LLC" paw.com "Acme Corp | acme.com"     # or add companies by hand
uv run enrich doctor                           # see which providers are on
uv run enrich run --free-only                  # $0 pass over the queue
uv run enrich backtest                         # SPEC §7 gate against known-good rows
uv run enrich run                              # full waterfall (respects budget cap)
uv run enrich push                             # paste-ready CSV -> data/output/
```

`enrich add` takes names, domains, URLs, `Name | domain` pairs, files (`-f list.txt`), or piped stdin — one company per line.

## How it works (SPEC §3)

1. **Resolve** — website → registrable domain → real *email* domain (learned from the 1,200+ known emails, MX records, site/entity scraping; handles parent companies like USRx→axnygroup.com).
2. **Identify** — who holds each role: company site (JSON-LD + team pages) → LinkedIn-via-SERP → news RSS → Apollo people search (0 credits) → Exa → EDGAR → Claude researcher (`--llm`).
3. **Email** — sheet-learned patterns → Hunter (native pattern field) → Anymail (pay-only-verified) → Apollo reveal → permutation engine.
4. **Verify** — MillionVerifier / Reoon / Hunter, with explicit catch-all handling.
5. **Tier & export** — A verified / B strong guess / C review / D generic backstop; bounces blocklist themselves and requeue the company.

Every provider call is cached in SQLite (`data/cache.sqlite`) — re-runs are free — and metered against a per-run budget cap (`--budget`, default $10).

| Doc | What's in it |
|---|---|
| [SPEC.md](SPEC.md) | Full design: stages, tiers, waterfall config, sheet integration, costs, milestones |
| [docs/signup-checklist.md](docs/signup-checklist.md) | Which accounts to create, what's free vs paid, MCP endpoints |
| [docs/data-audit.md](docs/data-audit.md) | Audit of the Contact Research tab (550 companies, 263-row queue) |
| [docs/service-research.md](docs/service-research.md) | Provider comparison, pricing verified 2026-08-12/13 |

## Configuration

- `.env` — API keys + budget + per-credit cost overrides (see `.env.example`). No keys = free mode.
- `config.yaml` — waterfall order, role priorities, thresholds, budget. Waterfalls are config, not code.

## Development

```bash
uv run --no-sync pytest -q       # test suite
uv run ruff check src tests     # lint
```
