"""CLI: the operator surface. `enrich --help` for the tour."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer
from rich.console import Console

from . import __version__
from .config import Settings
from .context import Ctx, Flags
from .intake import import_sheet, load_sheet_csv, parse_freeform, read_freeform_file
from .models import Company
from .store import Store

app = typer.Typer(help="B2B executive contact enrichment pipeline.", no_args_is_help=True)
console = Console()


def _ctx(free_only: bool = False, llm: bool = False, verify: bool = True, budget: float | None = None) -> Ctx:
    settings = Settings()
    store = Store(settings.db_path)
    ctx = Ctx(settings=settings, store=store, flags=Flags(free_only=free_only, llm=llm, verify=verify))
    if budget is not None:
        ctx.budget.limit_usd = budget
    return ctx


@app.command()
def add(
    items: list[str] = typer.Argument(None, help='Companies: "Acme Corp", acme.com, or "Acme Corp | acme.com"'),
    file: Path = typer.Option(None, "--file", "-f", help="File with one company per line"),
    force: bool = typer.Option(False, help="Re-queue even if the company already exists"),
):
    """Queue companies by name and/or URL. Also reads stdin if piped (one per line)."""
    ctx = _ctx()
    inputs: list[Company] = []
    if items:
        inputs += parse_freeform(items)
    if file:
        inputs += read_freeform_file(file)
    if not sys.stdin.isatty() and not items and not file:
        inputs += parse_freeform(sys.stdin.read().splitlines())
    if not inputs:
        console.print("[yellow]Nothing to add. Pass names/domains, --file, or pipe stdin.[/]")
        raise typer.Exit(1)
    added, skipped = 0, 0
    for c in inputs:
        existing = ctx.store.get_company(c.key) or (c.domain and ctx.store.find_company_by_domain(c.domain))
        if existing and not force:
            skipped += 1
            console.print(f"  [dim]skip[/] {c.name} — already tracked ({existing.status}); use --force to redo")
            continue
        if existing and force:
            c.key = existing.key
            c.status = "pending"
        ctx.store.upsert_company(c)
        added += 1
        console.print(f"  [green]queued[/] {c.name}" + (f" ({c.domain})" if c.domain else " [dim](no domain yet — stage 1 will resolve)[/]"))
    console.print(f"\n{added} queued, {skipped} skipped. Next: [bold]enrich run[/]")


@app.command()
def pull(
    csvfile: Path = typer.Argument(..., help="Contact-research CSV export (see examples/)"),
    learn_only: bool = typer.Option(False, help="Only learn patterns from completed rows; don't queue work"),
):
    """Import the sheet CSV: queue rows without emails, learn patterns from rows with them."""
    ctx = _ctx()
    imp = load_sheet_csv(csvfile)
    if learn_only:
        imp.queued = []
    stats = import_sheet(ctx.store, imp)
    console.print(
        f"queued [bold]{stats['queued']}[/] companies | learned patterns for "
        f"[bold]{stats['learned_domains']}[/] domains from [bold]{stats['known_emails']}[/] known emails | "
        f"blocked [bold]{stats['bounced_blocked']}[/] bounced addresses, re-queued {stats['requeued_bounced']} rows"
    )


@app.command()
def plan():
    """What would run, with which providers, and roughly what it would cost."""
    from .pipeline import Waterfalls

    ctx = _ctx()
    pending = ctx.store.companies("pending")
    wf = Waterfalls(ctx)
    console.print(f"[bold]{len(pending)}[/] companies pending.\n")
    for stage in ("identify", "pattern", "email", "verify"):
        usable = [p.name for p in getattr(wf, stage)]
        skipped = [f"{n} [dim]({why})[/]" for n, why in wf.skipped[stage]]
        console.print(f"  [bold]{stage:9s}[/] -> {', '.join(usable) or '[red]none[/]'}"
                      + (f"   skipped: {', '.join(skipped)}" if skipped else ""))
    est = len(pending) * 0.10
    console.print(
        f"\nBudget cap: ${ctx.budget.limit_usd:.2f}/run. Rough worst-case paid spend: ~${est:.2f} "
        f"(most companies resolve on free providers first)."
    )


@app.command()
def run(
    limit: int = typer.Option(0, help="Max companies this run (0 = all pending)"),
    only: str = typer.Option("", help="Substring filter on company name/domain"),
    free_only: bool = typer.Option(False, "--free-only", help="Skip anything that costs money"),
    llm: bool = typer.Option(False, "--llm", help="Enable the Claude researcher fallback (most expensive step)"),
    no_verify: bool = typer.Option(False, "--no-verify", help="Skip email verification"),
    budget: float = typer.Option(None, help="Override per-run USD budget cap"),
    concurrency: int = typer.Option(6, help="Companies processed in parallel"),
):
    """Enrich pending companies through the full waterfall."""
    from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn

    from .pipeline import run_pipeline
    from .report import company_table, print_full_report

    ctx = _ctx(free_only=free_only, llm=llm, verify=not no_verify, budget=budget)
    pending = ctx.store.companies("pending")
    if only:
        needle = only.lower()
        pending = [c for c in pending if needle in c.name.lower() or needle in c.domain.lower()]
    if limit:
        pending = pending[:limit]
    if not pending:
        console.print("Nothing pending. Use [bold]enrich add[/] or [bold]enrich pull[/].")
        raise typer.Exit()
    if not free_only:
        from .providers import REGISTRY

        paid = []
        for name, cls in sorted(REGISTRY.items()):
            p = cls(ctx)
            ok, _ = p.available()
            if ok and not getattr(p, "is_free", False):
                paid.append(name)
        if paid:
            console.print(
                f"[yellow]Paid providers on:[/] {', '.join(paid)}. "
                f"Budget cap ${ctx.budget.limit_usd:.2f}. Use --free-only to skip spend.\n"
            )
    ctx.store.start_run(ctx.run_id, ctx.flags.__dict__)
    console.print(f"run [bold]{ctx.run_id}[/]: {len(pending)} companies, budget ${ctx.budget.limit_usd:.2f}\n")

    async def main():
        async with ctx:
            with Progress(
                TextColumn("[progress.description]{task.description}"), BarColumn(),
                TextColumn("{task.completed}/{task.total}"), TimeElapsedColumn(), console=console,
            ) as progress:
                task = progress.add_task("enriching", total=len(pending))

                def done(res):
                    progress.advance(task)
                    c = res.company
                    tiers = "".join(sorted({e.tier for e in res.emails if e.tier}))
                    progress.console.print(f"  {c.name:40.40s} -> {c.status:12s} tiers[{tiers}]")

                return await run_pipeline(ctx, pending, concurrency=concurrency, on_done=done)

    results = asyncio.run(main())
    ctx.store.finish_run(ctx.run_id, {"companies": len(results), "spend": ctx.budget.spent()})
    console.print()
    console.print(company_table(ctx.store, [r.company.key for r in results[:25]]))
    print_full_report(console, ctx.store, ctx.run_id)
    console.print(f"\nSpend this run: [bold]${ctx.budget.spent():.2f}[/]. Export: [bold]enrich push[/]")


@app.command()
def verify(emails: list[str] = typer.Argument(..., help="Ad-hoc verification of specific addresses")):
    """Verify addresses through the configured verifier waterfall."""
    from .pipeline import Waterfalls, _verify_email

    ctx = _ctx()

    async def main():
        async with ctx:
            wf = Waterfalls(ctx)
            for e in emails:
                res = await _verify_email(ctx, wf, e.strip().lower(), [])
                console.print(f"  {e:45s} -> [bold]{res.status}[/] ({res.source})")

    asyncio.run(main())


@app.command()
def report(
    csv: Path = typer.Option(None, "--csv", help="Also export the enriched table to this CSV path"),
):
    """Queue status, tier distribution, provider spend."""
    from .report import print_full_report
    from .sheet_io import export_enriched_csv

    ctx = _ctx()
    print_full_report(console, ctx.store)
    if csv:
        n = export_enriched_csv(ctx.store, csv)
        console.print(f"wrote {n} rows -> {csv}")


@app.command()
def push(
    gsheet: bool = typer.Option(False, "--gsheet", help="Write the Enriched tab via Sheets API"),
    out: Path = typer.Option(None, "--out", help="CSV output path (default data/output/enriched-<ts>.csv)"),
):
    """Export results: CSV by default, Google Sheet with --gsheet (needs service account)."""
    from .models import utcnow
    from .sheet_io import export_enriched_csv, push_gsheet

    ctx = _ctx()
    if gsheet:
        sheet_id = ctx.settings.key("SHEET_ID")
        sa = ctx.settings.key("GOOGLE_SERVICE_ACCOUNT_FILE")
        if not (sheet_id and sa and Path(sa).exists()):
            console.print("[red]Sheets mode needs SHEET_ID and GOOGLE_SERVICE_ACCOUNT_FILE in .env "
                          "(and the sheet shared with the service account).[/] Falling back to CSV.")
        else:
            n = push_gsheet(ctx.store, sheet_id, sa)
            console.print(f"pushed {n} rows to the Enriched tab")
            raise typer.Exit()
    path = out or ctx.settings.output_dir / f"enriched-{utcnow().replace(':', '').replace('-', '')[:13]}.csv"
    n = export_enriched_csv(ctx.store, path)
    console.print(f"wrote {n} rows -> {path}")


@app.command()
def backtest(
    sample: int = typer.Option(50, help="How many known-good companies to test against"),
    free_only: bool = typer.Option(False, "--free-only/--paid",
        help="Default uses paid providers (serp/apollo) — the real gate stack; set free-tier "
             "cost env vars to 0 to avoid spend. --free-only measures keyless mode only."),
    llm: bool = typer.Option(False, "--llm"),
):
    """SPEC §7 gate: reproduce emails the team already found by hand. Read-only, no verifier spend."""
    from .backtest import run_backtest

    ctx = _ctx(free_only=free_only, llm=llm)

    async def main():
        async with ctx:
            return await run_backtest(ctx, sample=sample)

    rep = asyncio.run(main())
    if not rep.n:
        console.print("[yellow]No learned rows in the store — run: enrich pull <sheet.csv>[/]")
        raise typer.Exit(1)
    for r in rep.rows:
        mark = "[green]HIT[/]" if r.reproduced else "[red]miss[/]"
        console.print(f"  {mark} {r.company:38.38s} people={r.people_found} candidates={r.candidates} "
                      f"reproduced={len(r.reproduced)}/{r.known}")
    share = rep.rows_with_hit / rep.n
    console.print(f"\nrows with >=1 reproduced email: [bold]{rep.rows_with_hit}/{rep.n} ({share:.0%})[/]"
                  f" | rows with >=3: {rep.rows_with_3}")
    if rep.gate():
        console.print("[green]GATE PASS[/] (>=70%) — ready for the paid backlog run")
    else:
        console.print("[red]GATE FAIL[/] (<70%) — tune the waterfall before paid backlog run")


@app.command()
def doctor(live: bool = typer.Option(False, "--live", help="Also ping each provider's cheapest endpoint")):
    """Check which providers are configured and usable."""
    from .providers import IMPORT_ERRORS, REGISTRY

    ctx = _ctx()
    console.print(f"courtesy-enrich v{__version__} | db: {ctx.settings.db_path}\n")
    for mod, err in IMPORT_ERRORS.items():
        console.print(f"  [red]import failed[/] {mod}: {err}")

    async def main():
        async with ctx:
            for name, cls in sorted(REGISTRY.items()):
                p = cls(ctx)
                ok, why = p.available()
                if not ok:
                    console.print(f"  [yellow]off[/]  {name:16s} {why}")
                    continue
                status = "ready"
                if live and hasattr(p, "doctor"):
                    try:
                        status = await p.doctor()
                    except Exception as e:  # noqa: BLE001
                        status = f"[red]{type(e).__name__}: {e}[/]"
                console.print(f"  [green]on[/]   {name:16s} {status}")

    asyncio.run(main())
    console.print("\nKeys come from .env (see .env.example). Free providers need no key.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
