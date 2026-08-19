"""'millionverifier' adapter — MillionVerifier real-time single-verification API.

Docs: https://developer.millionverifier.com (verified live 2026-08-19).

    GET https://api.millionverifier.com/api/v3/?api=<key>&email=<email>&timeout=10
    GET https://api.millionverifier.com/api/v3/credits?api=<key>

Mapping of their ``result`` field -> our VerifyResult.status:

    ok          -> deliverable
    invalid     -> undeliverable
    disposable  -> undeliverable   (burner domains: never worth sending to)
    catch_all   -> catch_all
    unknown     -> unknown
    anything else (e.g. "unverified") -> unknown

Cost: 1 credit per verification. Default price assumes the 10k pay-as-you-go
pack (~$39, credits never expire) => $0.0039/credit; override with
MILLIONVERIFIER_COST_PER_CREDIT in .env (free-tier users set 0). NOTE: their
"Risky Email Refund" returns credits for unknown results, so the true blended
cost is somewhat LOWER than what our ledger records — the ledger is a ceiling.
"""

from __future__ import annotations

from .. import net
from .base import ProviderBase, ProviderError, VerifyResult, register

VERIFY_URL = "https://api.millionverifier.com/api/v3/"
CREDITS_URL = "https://api.millionverifier.com/api/v3/credits"

COST_PER_CREDIT = 0.0039  # USD; env MILLIONVERIFIER_COST_PER_CREDIT overrides

RESULT_MAP = {
    "ok": "deliverable",
    "invalid": "undeliverable",
    "disposable": "undeliverable",
    "catch_all": "catch_all",
    "unknown": "unknown",
}


@register
class MillionVerifier(ProviderBase):
    name = "millionverifier"
    key_env = "MILLIONVERIFIER_API_KEY"
    is_free = False
    rps = 5.0

    def _cost_per_credit(self) -> float:
        raw = self.ctx.settings.key("MILLIONVERIFIER_COST_PER_CREDIT")
        return float(raw) if raw is not None else COST_PER_CREDIT

    async def verify(self, email: str) -> VerifyResult:
        async def fetch():
            data = await net.fetch_json(
                self.ctx.http,
                "GET",
                VERIFY_URL,
                params={"api": self.api_key, "email": email, "timeout": 10},
            )
            err = str(data.get("error") or "").strip()
            if err:  # HTTP 200 with an error payload = bad key/params, not a lookup miss
                raise ProviderError(f"millionverifier: {err}")
            return data

        data = await self.cached_json(
            "verify", {"email": email}, fetch,
            cost_usd=self._cost_per_credit(), credits=1,
        )
        status = RESULT_MAP.get(str(data.get("result", "")).lower().strip(), "unknown")
        return VerifyResult(email=email, status=status, source=self.name, raw=data)

    async def doctor(self) -> str:
        async def fetch():
            return await net.fetch_json(
                self.ctx.http, "GET", CREDITS_URL, params={"api": self.api_key}
            )

        data = await self.cached_json("credits", {}, fetch, ttl_days=0)
        credits = data.get("credits")
        if credits is None:
            return "ok (credits endpoint gave no 'credits' field)"
        return f"ok - {int(credits):,} credits left"
