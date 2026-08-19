"""Back-test gate (SPEC §7): can the pipeline reproduce emails the team already found?

Runs identify+email stages against companies whose emails are known, without
persisting and without burning verifier credits, then scores candidates against
the known set. Purely a read-only measurement.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .context import Ctx
from .models import Company
from .pipeline import Waterfalls, stage1_resolve, stage2_identify, stage3_emails


@dataclass
class BacktestRow:
    company: str
    domain: str
    known: int
    candidates: int
    reproduced: list[str] = field(default_factory=list)
    people_found: int = 0


@dataclass
class BacktestReport:
    rows: list[BacktestRow] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.rows)

    @property
    def rows_with_hit(self) -> int:
        return sum(1 for r in self.rows if r.reproduced)

    @property
    def rows_with_3(self) -> int:
        return sum(1 for r in self.rows if len(r.reproduced) >= 3)

    def gate(self, min_share: float = 0.70) -> bool:
        return self.n > 0 and self.rows_with_hit / self.n >= min_share


# Providers that read the answer key (known_emails) — excluded from the email stage
# during backtest so the measurement isn't graded against its own crib sheet.
_ANSWER_KEY_PROVIDERS = {"sheet"}


async def run_backtest(ctx: Ctx, sample: int = 50, seed: int = 7) -> BacktestReport:
    from .models import is_generic_local

    prev_verify = ctx.flags.verify
    ctx.flags.verify = False  # never burn verifier credits on a measurement
    try:
        learned = [
            c for c in ctx.store.companies()
            if c.source == "sheet" and c.email_domain and ctx.store.known_emails(c.email_domain)
        ]
        random.Random(seed).shuffle(learned)
        wf = Waterfalls(ctx)
        # don't let the sheet provider "find" the known email by construction
        wf.email = [p for p in wf.email if p.name not in _ANSWER_KEY_PROVIDERS]
        report = BacktestReport()

        for orig in learned:
            if len(report.rows) >= sample:
                break
            c_domain = orig.email_domain
            known_all = {e.lower() for e in ctx.store.known_emails(c_domain)}
            # a row whose only known emails are generic (support@, info@) is unwinnable by design
            known = {e for e in known_all if not is_generic_local(e.split("@")[0])}
            if not known:
                continue
            c = Company(**{**orig.model_dump(), "status": "pending", "pattern": "", "pattern_confidence": 0.0})
            notes: list[str] = []
            await stage1_resolve(ctx, wf, c, notes)
            people = await stage2_identify(ctx, wf, c, notes)
            emails = await stage3_emails(ctx, wf, c, people, notes)
            candidates = {e.email for e in emails if e.method != "generic"}
            report.rows.append(
                BacktestRow(
                    company=c.name, domain=c_domain, known=len(known),
                    candidates=len(candidates),
                    reproduced=sorted(candidates & known), people_found=len(people),
                )
            )
        return report
    finally:
        ctx.flags.verify = prev_verify
