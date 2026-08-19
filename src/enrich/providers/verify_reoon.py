"""'reoon' adapter — Reoon Email Verifier single-verification API (power mode).

Docs: https://www.reoon.com/articles/api-documentation-of-reoon-email-verifier/
(verified live 2026-08-19). Free tier: ~600 verifications/month with API access,
so this adapter is marked is_free — calls still ledger 1 credit at $0 so credit
burn is visible in reports. Override price with REOON_COST_PER_CREDIT if you
ever buy instant credits.

    GET https://emailverifier.reoon.com/api/v1/verify?email=<e>&key=<k>&mode=power
    GET https://emailverifier.reoon.com/api/v1/check-account-balance/?key=<k>

Full mapping of their ``status`` field -> our VerifyResult.status:

    valid        -> deliverable      (quick-mode vocabulary)
    safe         -> deliverable      (power-mode vocabulary)
    invalid      -> undeliverable
    disabled     -> undeliverable
    catch_all    -> catch_all
    accept_all   -> catch_all        (defensive alias; current docs say catch_all)
    disposable   -> unknown          (per spec; tiering treats unknown as risky)
    inbox_full   -> unknown
    role_account -> unknown
    spamtrap     -> unknown
    unknown      -> unknown
    anything else -> unknown
"""

from __future__ import annotations

from .. import net
from .base import ProviderBase, ProviderError, VerifyResult, register

VERIFY_URL = "https://emailverifier.reoon.com/api/v1/verify"
BALANCE_URL = "https://emailverifier.reoon.com/api/v1/check-account-balance/"

COST_PER_CREDIT = 0.0  # USD; free tier. env REOON_COST_PER_CREDIT overrides.

STATUS_MAP = {
    "valid": "deliverable",
    "safe": "deliverable",
    "invalid": "undeliverable",
    "disabled": "undeliverable",
    "catch_all": "catch_all",
    "accept_all": "catch_all",
}


@register
class Reoon(ProviderBase):
    name = "reoon"
    key_env = "REOON_API_KEY"
    is_free = True   # 600/mo recurring free tier; still ledgers credit units
    rps = 2.0

    def _cost_per_credit(self) -> float:
        raw = self.ctx.settings.key("REOON_COST_PER_CREDIT")
        return float(raw) if raw is not None else COST_PER_CREDIT

    async def verify(self, email: str) -> VerifyResult:
        async def fetch():
            data = await net.fetch_json(
                self.ctx.http,
                "GET",
                VERIFY_URL,
                params={"email": email, "key": self.api_key, "mode": "power"},
            )
            if str(data.get("status", "")).lower() == "error":
                # HTTP 200 + status=error means key/param trouble, not a lookup miss
                reason = data.get("reason") or data.get("message") or "unspecified error"
                raise ProviderError(f"reoon: {reason}")
            return data

        data = await self.cached_json(
            "verify", {"email": email, "mode": "power"}, fetch,
            cost_usd=self._cost_per_credit(), credits=1,
        )
        status = STATUS_MAP.get(str(data.get("status", "")).lower().strip(), "unknown")
        return VerifyResult(email=email, status=status, source=self.name, raw=data)

    async def doctor(self) -> str:
        async def fetch():
            return await net.fetch_json(
                self.ctx.http, "GET", BALANCE_URL, params={"key": self.api_key}
            )

        data = await self.cached_json("balance", {}, fetch, ttl_days=0)
        daily = data.get("remaining_daily_credits")
        instant = data.get("remaining_instant_credits")
        if daily is None and instant is None:
            return "ok (no quota fields in balance response)"
        return f"ok - {int(daily or 0):,} daily + {int(instant or 0):,} instant credits left"
