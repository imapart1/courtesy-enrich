# courtesy-enrich

Multi-provider pipeline that finds B2B executive contacts — name, title, and work email — for a list of companies. Role priorities and provider waterfalls are config, not code. Results are cached, cost-capped, and exported for human review.

This tool does **not** send email. Output is tiered (A verified → D generic fallback) so a person can review before any outreach.

**Status: v0.2.** Core pipeline + 13 provider adapters + tests. Runs in free mode with zero API keys; paid providers switch on when their key is in `.env`.

Use only for lawful B2B contact research. You are responsible for privacy law, anti-spam rules, and each provider's terms of service.

## Quickstart

```bash
uv sync
cp .env.example .env          # optional; set CONTACT_EMAIL and any API keys
uv run enrich doctor          # which providers are on
uv run enrich add -f examples/demo-companies.txt
uv run enrich run --free-only --limit 3
uv run enrich push            # paste-ready CSV -> data/output/
```

Or import a contact-research CSV (schema in [docs/data-audit.md](docs/data-audit.md)):

```bash
uv run enrich pull examples/demo-contact-research.csv
uv run enrich add "Acme Corp | acme.com"          # or add companies by hand
uv run enrich run --free-only
uv run enrich backtest --free-only
uv run enrich run                                 # paid waterfall if keys are set; respects --budget
```

`enrich add` takes names, domains, URLs, `Name | domain` pairs, files (`-f list.txt`), or piped stdin — one company per line.

A paid `enrich run` prints which paid providers are enabled and the budget cap. Pass `--free-only` to skip spend.

## How it works ([SPEC.md](SPEC.md) §3)

1. **Resolve** — website → registrable domain → real *email* domain (learned from known emails, MX records, site/entity scraping; handles brand vs parent domains).
2. **Identify** — who holds each role: company site (JSON-LD + team pages) → LinkedIn-via-SERP → news RSS → Apollo people search (0 credits) → Exa → EDGAR → Claude researcher (`--llm`).
3. **Email** — learned patterns → Hunter (native pattern field) → Anymail (pay-only-verified) → Apollo reveal → permutation engine.
4. **Verify** — MillionVerifier / Reoon / Hunter, with explicit catch-all handling.
5. **Tier & export** — A verified / B strong guess / C review / D generic backstop; bounces blocklist themselves and requeue the company.

Every provider call is cached in SQLite (`data/cache.sqlite`) — re-runs are free — and metered against a per-run budget cap (`--budget`, default $10).

| Doc | What's in it |
|---|---|
| [SPEC.md](SPEC.md) | Design: stages, tiers, waterfall config, sheet integration, costs |
| [examples/README.md](examples/README.md) | Synthetic inputs that work on a fresh clone |
| [docs/signup-checklist.md](docs/signup-checklist.md) | Which accounts to create, free vs paid, MCP endpoints |
| [docs/data-audit.md](docs/data-audit.md) | CSV schema and the quirks intake has to handle |
| [docs/service-research.md](docs/service-research.md) | Provider comparison, pricing verified 2026-08-12/13 |

## Configuration

- `.env` — API keys + budget + per-credit cost overrides (see `.env.example`). No keys = free mode.
- `CONTACT_EMAIL` — identifying address for outbound User-Agent headers (SEC EDGAR requires this).
- `config.yaml` — waterfall order, role priorities, thresholds, budget.
- Google Sheets write-back is opt-in: `enrich push --gsheet` plus `SHEET_ID` and a gitignored service-account file. `uv sync --extra gsheets` installs the extra.

## Development

```bash
uv run --no-sync pytest -q       # test suite
uv run ruff check src tests     # lint
```

## License

MIT. See [LICENSE](LICENSE).
