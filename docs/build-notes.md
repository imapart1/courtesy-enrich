# Build Notes — v0.2 (2026-08-13)

What got built, how it was built, what it does and doesn't do yet. Pairs with [SPEC.md](../SPEC.md).

## Status

Working Python package (`src/enrich/`), **187 tests passing**, ruff-clean. Runs today in free mode with **zero API keys**; every paid provider activates the moment its key is in `.env`. Milestones M0–M3 from SPEC §9 are implemented; M4 (scheduled runs, dashboards) is partial (`enrich report` exists).

## How it was built (multi-agent)

1. **Core, by hand:** models, SQLite store + cost ledger, budget guard, pipeline waterfall, patterns engine, intake, CLI, sheet I/O, backtest harness — plus the provider contract (`providers/base.py`).
2. **13 provider adapters, 8 parallel agents** (one Workflow fan-out): hunter, apollo, anymail, exa, serp+newsrss, sitescrape+edgar, the two verifiers, and the LLM researcher. Each verified its API against live docs and shipped its own tests.
3. **4 parallel adversarial reviewers** (wiring / robustness / cost-safety / backtest-improvement) — surfaced ~25 probe-confirmed defects.
4. **Fixes:** ~20 applied by hand to core modules; 3 provider-internal fixes by a second agent fan-out. Every fix has a regression test.

## What works

- **Free stack end-to-end:** company-site scrape (JSON-LD + prose founder pass), Google News RSS, SEC EDGAR, MX/entity resolution, pattern learning from the 1,222 known sheet emails, permutation guessing, tiered output, CSV export. A `--free-only` run over real companies costs **$0** and correctly pulls e.g. Sonder's CEO/CFO from EDGAR.
- **Paid waterfall wired and tested** (Hunter, Apollo, Anymail, Exa, MillionVerifier, Reoon, Serper) behind keys, with a hard per-run budget cap, in-flight budget *reservations* (concurrency-safe), and per-call cost metering (Exa/Hunter/Anymail/Apollo rewrite the ledger to real cost on fresh calls).
- **Safety rails:** catch-all guesses never tier above B; bounced addresses blocklist themselves and re-queue the company; secrets never logged or cached; all of `data/` gitignored (PII); LinkedIn never scraped directly (SERP snippets only); SEC UA header + robots.txt honored.

## Known limitations / next steps

1. **Keyless recall is low** (~15–20% on the backtest) — identify is the bottleneck; most small-brand execs are only findable via LinkedIn/Apollo. The gate is designed to pass under the **Serper + Apollo free tiers**, which need keys. Run `enrich backtest` once those keys are in `.env` to get the real number before the paid backlog run.
2. **Prose founder pass is noisy** — on pages quoting outside figures it can surface e.g. "Steve Ballmer" or a headline fragment as a CEO candidate. These land in **tier C (review-required)** and never auto-send, but they add review load and can waste a verifier credit. Tightening (require the name near the company's own brand tokens) is a good follow-up.
3. **Google Sheets write-back** (`enrich push --gsheet`) is implemented but needs a service account with the sheet shared to it (SPEC §4). CSV mode is the default and needs nothing.
4. **LLM researcher** (`--llm`) is off by default (most expensive identify step). It works via the Anthropic API or, with no key, the local `claude` CLI at $0 marginal.
5. **No live provider integration test yet** — adapters are covered by recorded-fixture tests (respx); a first real-key smoke run per provider is worth doing before the backlog.

## Backtest gate (SPEC §7)

`enrich backtest` runs identify+email against companies whose emails are already known, excludes the answer-key `sheet` provider and generic-only rows, and never spends verifier credits. Gate = ≥70% of rows reproduce ≥1 known address. **Run it with the Serper+Apollo keys present** — that's the stack it's designed to measure.
