# Input format — contact-research CSV

The pipeline learns patterns from completed rows and queues the rest. Export your sheet as CSV and pass it to `enrich pull`. A synthetic example lives at `examples/demo-contact-research.csv`.

`data/` (real exports, SQLite cache, output CSVs) is gitignored — those files often contain PII.

## Structure

Row 1 may be a formula row; a header row is skipped if the card cell does not start with `http`. Columns, in order:

`Card URL | Contact First | Contact Last | Company | Email | Demand Date | Website | Notes | Status | Completed | Card Status`

- **Unique key:** card URL (must start with `http`). Company name should also be unique.
- **Email** and **Website** may be formulas in the live sheet. Write-back must not clobber formula columns — use a separate Enriched tab or paste from `enrich push`.
- Downstream send-tracking tabs are out of scope (SPEC: no sending).

## How rows are classified

| Kind | Rule | What happens |
|---|---|---|
| Learned | Email cell has parseable addresses | Training data: email domain + naming pattern |
| Queued | No emails, not a bounce | Work queue |
| Bounced | Status is `Emails Did Not Work`, or notes say "did not work" | Addresses in Notes are blocklisted; row is re-queued |
| Skipped | No company name, or card cell is not an `http` URL | Ignored (placeholders, formula-only rows) |

## Quirks the pipeline must handle

1. **Corporate email domain ≠ brand website domain** — learn the mailbox domain from known emails, not from the website host.
2. **Parent companies / acquisitions** — notes may name an owner or acquirer; entity resolution matters and data can go stale.
3. **No corporate email** — "Gmail addresses only" should be flagged, not guessed.
4. **Bounce feedback** — failed addresses in Notes become a per-domain blocklist and trigger a re-run.
5. **Size screening in Notes** — e.g. "Less than $10 Million"; a `size_flag` from firmographics automates this.
6. **Non-company rows** — forums or wrong-entity sites should surface as anomalies, not fabricated contacts.
7. **Naming-convention variety** — `{first}@` is common at small brands; `{first}.{last}@`, `{f}{last}@`, `{first}{l}@` also appear. Verification should catch typos.

## Implications baked into the SPEC

- Learn per-domain patterns from completed rows before spending credits (SPEC §3 Stage 1/3, §7 back-test).
- Stage 1 must resolve entity/parent/email-domain before any person search.
- Verification is mandatory; catch-all guesses are tiered, never "verified".
- Anomalies (forum, wrong entity, Gmail-only) get status flags for human review instead of fabricated contacts.
