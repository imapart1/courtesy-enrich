"""SQLite persistence: companies, people, emails, provider-call cache + cost ledger,
learned patterns, blocklist. Synchronous sqlite guarded by a lock (calls are tiny)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import Company, EmailRec, PersonRec, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
  key TEXT PRIMARY KEY, name TEXT, website TEXT, domain TEXT, email_domain TEXT,
  entity_name TEXT, parent_company TEXT, pattern TEXT, pattern_confidence REAL,
  catch_all INTEGER, size_flag TEXT, status TEXT, source TEXT, plaintiff TEXT,
  demand_date TEXT, notes TEXT, error TEXT, added_at TEXT, enriched_at TEXT
);
CREATE TABLE IF NOT EXISTS people (
  company_key TEXT, role TEXT, name TEXT, title TEXT, provider TEXT, url TEXT,
  confidence REAL, created_at TEXT, raw TEXT DEFAULT '{}',
  PRIMARY KEY (company_key, role, name)
);
CREATE TABLE IF NOT EXISTS emails (
  company_key TEXT, role TEXT, person_name TEXT, email TEXT, method TEXT,
  provider TEXT, verify_status TEXT, verify_provider TEXT, tier TEXT,
  confidence REAL, run_id TEXT, created_at TEXT,
  PRIMARY KEY (company_key, email)
);
CREATE TABLE IF NOT EXISTS provider_calls (
  provider TEXT, op TEXT, params_hash TEXT, params_json TEXT, response_json TEXT,
  cost_usd REAL DEFAULT 0, credits REAL DEFAULT 0, run_id TEXT, created_at TEXT,
  PRIMARY KEY (provider, op, params_hash)
);
CREATE TABLE IF NOT EXISTS domain_patterns (
  domain TEXT PRIMARY KEY, pattern TEXT, shape TEXT, confidence REAL,
  source TEXT, sample_size INTEGER, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS known_emails (         -- ground truth from the sheet (learning + backtest)
  domain TEXT, email TEXT, company_key TEXT, PRIMARY KEY (domain, email)
);
CREATE TABLE IF NOT EXISTS blocklist (
  email TEXT PRIMARY KEY, reason TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, flags TEXT, summary TEXT
);
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(people)")}
            if "raw" not in cols:  # additive migration for pre-existing DBs
                self._conn.execute("ALTER TABLE people ADD COLUMN raw TEXT DEFAULT '{}'")

    # ------------------------------------------------------------------ util
    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, args)

    def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # ------------------------------------------------------------- companies
    def upsert_company(self, c: Company) -> None:
        d = c.model_dump()
        d["catch_all"] = None if c.catch_all is None else int(c.catch_all)
        cols = ",".join(d)
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO companies ({cols}) VALUES ({','.join(':' + k for k in d)}) "
                "ON CONFLICT(key) DO UPDATE SET "
                + ",".join(f"{k}=:{k}" for k in d if k not in ("key", "added_at")),
                d,
            )

    def get_company(self, key: str) -> Company | None:
        rows = self._rows("SELECT * FROM companies WHERE key=?", (key,))
        return self._to_company(rows[0]) if rows else None

    def find_company_by_domain(self, domain: str) -> Company | None:
        rows = self._rows("SELECT * FROM companies WHERE domain=? OR email_domain=?", (domain, domain))
        return self._to_company(rows[0]) if rows else None

    def companies(self, status: str | None = None) -> list[Company]:
        if status:
            rows = self._rows("SELECT * FROM companies WHERE status=? ORDER BY added_at", (status,))
        else:
            rows = self._rows("SELECT * FROM companies ORDER BY added_at")
        return [self._to_company(r) for r in rows]

    @staticmethod
    def _to_company(r: sqlite3.Row) -> Company:
        d = dict(r)
        d["catch_all"] = None if d["catch_all"] is None else bool(d["catch_all"])
        d = {k: ("" if v is None else v) if k != "catch_all" else v for k, v in d.items()}
        return Company(**d)

    # ---------------------------------------------------------- people/emails
    def save_person(self, p: PersonRec) -> None:
        d = p.model_dump()
        d["raw"] = json.dumps(d.get("raw") or {}, default=str)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO people VALUES (:company_key,:role,:name,:title,:provider,:url,:confidence,"
                ":created_at,:raw) "
                "ON CONFLICT(company_key,role,name) DO UPDATE SET title=:title,provider=:provider,"
                "url=:url,confidence=max(confidence,:confidence),raw=:raw",
                d,
            )

    def people_for(self, company_key: str, role: str | None = None) -> list[PersonRec]:
        if role:
            rows = self._rows(
                "SELECT * FROM people WHERE company_key=? AND role=? ORDER BY confidence DESC", (company_key, role)
            )
        else:
            rows = self._rows("SELECT * FROM people WHERE company_key=? ORDER BY confidence DESC", (company_key,))
        out = []
        for r in rows:
            d = dict(r)
            d["raw"] = json.loads(d.get("raw") or "{}")
            out.append(PersonRec(**d))
        return out

    def save_email(self, e: EmailRec) -> None:
        d = e.model_dump()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO emails VALUES (:company_key,:role,:person_name,:email,:method,:provider,"
                ":verify_status,:verify_provider,:tier,:confidence,:run_id,:created_at) "
                "ON CONFLICT(company_key,email) DO UPDATE SET verify_status=:verify_status,"
                "verify_provider=:verify_provider,tier=:tier,confidence=:confidence,method=:method,"
                "provider=:provider,role=:role,person_name=:person_name",
                d,
            )

    def emails_for(self, company_key: str) -> list[EmailRec]:
        rows = self._rows("SELECT * FROM emails WHERE company_key=? ORDER BY tier, role", (company_key,))
        return [EmailRec(**dict(r)) for r in rows]

    # ------------------------------------------------------------- call cache
    @staticmethod
    def params_hash(params: dict) -> str:
        return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:32]

    def cache_get(self, provider: str, op: str, params: dict, ttl_days: int) -> dict | None:
        hit = self.cache_get_with_age(provider, op, params)
        if hit is None:
            return None
        data, age_days = hit
        return None if age_days > ttl_days else data

    def cache_get_with_age(self, provider: str, op: str, params: dict) -> tuple[dict, float] | None:
        """(cached response, age in days) — lets callers apply shorter TTLs to failures."""
        h = self.params_hash(params)
        rows = self._rows(
            "SELECT response_json, created_at FROM provider_calls WHERE provider=? AND op=? AND params_hash=?",
            (provider, op, h),
        )
        if not rows:
            return None
        created = datetime.fromisoformat(rows[0]["created_at"].replace("Z", "+00:00"))
        age = (datetime.now(UTC) - created) / timedelta(days=1)
        return json.loads(rows[0]["response_json"]), age

    def cache_put(
        self, provider: str, op: str, params: dict, response: dict,
        *, cost_usd: float = 0.0, credits: float = 0.0, run_id: str = "",
    ) -> None:
        h = self.params_hash(params)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO provider_calls VALUES (?,?,?,?,?,?,?,?,?)",
                (provider, op, h, json.dumps(params, default=str), json.dumps(response, default=str),
                 cost_usd, credits, run_id, utcnow()),
            )

    def run_spend(self, run_id: str) -> float:
        rows = self._rows("SELECT COALESCE(SUM(cost_usd),0) s FROM provider_calls WHERE run_id=?", (run_id,))
        return float(rows[0]["s"])

    def spend_by_provider(self, run_id: str | None = None) -> list[dict]:
        where, args = ("WHERE run_id=?", (run_id,)) if run_id else ("", ())
        rows = self._rows(
            f"SELECT provider, COUNT(*) calls, SUM(cost_usd) usd, SUM(credits) credits "
            f"FROM provider_calls {where} GROUP BY provider ORDER BY usd DESC", args,
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------- patterns/known
    def save_domain_pattern(
        self, domain: str, *, pattern: str = "", shape: str = "", confidence: float = 0.0,
        source: str = "", sample_size: int = 0,
    ) -> None:
        # Merge, never clobber: empty pattern/shape keeps the existing non-empty value,
        # and a lower-confidence update never downgrades the record.
        existing = self.get_domain_pattern(domain)
        if existing:
            if existing["confidence"] > confidence:
                pattern = existing["pattern"] or pattern
                shape = existing["shape"] or shape
                confidence = existing["confidence"]
                source = existing["source"]
                sample_size = existing["sample_size"]
            else:
                pattern = pattern or existing["pattern"]
                shape = shape or existing["shape"]
        self._exec(
            "INSERT OR REPLACE INTO domain_patterns VALUES (?,?,?,?,?,?,?)",
            (domain, pattern, shape, confidence, source, sample_size, utcnow()),
        )

    def get_domain_pattern(self, domain: str) -> dict | None:
        rows = self._rows("SELECT * FROM domain_patterns WHERE domain=?", (domain,))
        return dict(rows[0]) if rows else None

    def save_known_email(self, domain: str, email: str, company_key: str) -> None:
        self._exec("INSERT OR IGNORE INTO known_emails VALUES (?,?,?)", (domain, email, company_key))

    def known_emails(self, domain: str) -> list[str]:
        return [r["email"] for r in self._rows("SELECT email FROM known_emails WHERE domain=?", (domain,))]

    # ------------------------------------------------------------- blocklist
    def block_email(self, email: str, reason: str) -> None:
        self._exec("INSERT OR REPLACE INTO blocklist VALUES (?,?,?)", (email.lower(), reason, utcnow()))

    def is_blocked(self, email: str) -> bool:
        return bool(self._rows("SELECT 1 FROM blocklist WHERE email=?", (email.lower(),)))

    # ------------------------------------------------------------------ runs
    def start_run(self, run_id: str, flags: dict) -> None:
        self._exec("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)", (run_id, utcnow(), "", json.dumps(flags), ""))

    def finish_run(self, run_id: str, summary: dict) -> None:
        self._exec("UPDATE runs SET finished_at=?, summary=? WHERE run_id=?", (utcnow(), json.dumps(summary), run_id))

    def stats(self) -> dict:
        out = {}
        for status in ("pending", "enriched", "partial", "no_contacts", "anomaly", "error"):
            out[status] = self._rows("SELECT COUNT(*) c FROM companies WHERE status=?", (status,))[0]["c"]
        out["emails_by_tier"] = {
            r["tier"]: r["c"]
            for r in self._rows("SELECT tier, COUNT(*) c FROM emails WHERE tier != '' GROUP BY tier")
        }
        out["people"] = self._rows("SELECT COUNT(*) c FROM people")[0]["c"]
        return out
