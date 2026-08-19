# Example inputs

Synthetic data so a fresh clone can run the pipeline. Do not commit real contact lists — `data/` is gitignored for that reason.

## Quick path (no CSV)

```bash
uv run enrich add -f examples/demo-companies.txt
uv run enrich run --free-only --limit 3
uv run enrich push
```

## Sheet-import path

```bash
uv run enrich pull examples/demo-contact-research.csv
uv run enrich run --free-only --limit 2
uv run enrich push
```

`--free-only` uses site scrape, news RSS, SEC EDGAR, and pattern guessing. Expect names more often than verified emails. Paid providers (Hunter, Apollo, verifiers) need keys in `.env` and a normal `enrich run` (watch the budget cap).
