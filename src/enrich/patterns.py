"""Email naming-convention detection, application, and learning.

Two learning modes:
- With names (backtest, provider results): detect_pattern(local, first, last) is exact.
- Without names (the 287 sheet rows): learn the *shape* of local parts per domain,
  then map shapes to an ordered list of candidate patterns to try at guess time.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from .models import is_generic_local

PATTERNS = [
    "{first}", "{first}.{last}", "{f}{last}", "{first}{last}", "{first}_{last}",
    "{f}.{last}", "{first}{l}", "{first}.{l}", "{last}", "{last}.{first}", "{f}{l}",
]

# Shape of a local part (observable without knowing the person's name) -> patterns to try, in order.
SHAPE_TO_PATTERNS: dict[str, list[str]] = {
    "first.last": ["{first}.{last}", "{last}.{first}"],
    "f.last": ["{f}.{last}", "{first}.{l}"],
    "first_last": ["{first}_{last}"],
    "single_short": ["{first}", "{f}{last}", "{f}{l}"],
    # measured on a large first-name-heavy sample: {first}@ dominates even among long tokens
    "single_long": ["{f}{last}", "{first}", "{first}{last}", "{first}{l}"],
    "unknown": ["{first}", "{first}.{last}", "{f}{last}", "{first}{last}"],
}


def norm_name(s: str) -> str:
    """ASCII-fold and strip a name token: 'José-María' -> 'josemaria'."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def split_name(full_name: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", full_name.strip()) if p and not p.startswith("(")]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return norm_name(parts[0]), ""
    return norm_name(parts[0]), norm_name(parts[-1])


def apply_pattern(pattern: str, first: str, last: str) -> str | None:
    fi, la = norm_name(first), norm_name(last)
    if not fi:
        return None
    if ("{last}" in pattern or "{l}" in pattern) and not la:
        return None
    return (
        pattern.replace("{first}", fi)
        .replace("{last}", la)
        .replace("{f}", fi[:1])
        .replace("{l}", la[:1] if la else "")
    )


def detect_pattern(local: str, first: str, last: str) -> str | None:
    """Which pattern produces this exact local part for this name?"""
    local = local.lower().strip()
    for p in PATTERNS:
        if apply_pattern(p, first, last) == local:
            return p
    return None


def classify_shape(local: str) -> str:
    """Observable shape of a local part when the owner's name is unknown."""
    local = local.lower().strip()
    if is_generic_local(local):
        return "generic"
    if "_" in local:
        return "first_last"
    if "." in local:
        head = local.split(".", 1)[0]
        return "f.last" if len(head) == 1 else "first.last"
    if not local.isalpha():
        return "unknown"
    return "single_short" if len(local) <= 6 else "single_long"


def learn_domain_shape(locals_: list[str]) -> tuple[str, float, int]:
    """Dominant shape among personal-looking locals -> (shape, confidence, sample_size)."""
    shapes = [classify_shape(x) for x in locals_ if x]
    shapes = [s for s in shapes if s not in ("generic", "unknown")]
    if not shapes:
        return "unknown", 0.0, 0
    counts = Counter(shapes)
    shape, n = counts.most_common(1)[0]
    return shape, n / len(shapes), len(shapes)


def candidate_pairs(
    first: str, last: str, *, pattern: str = "", shape: str = "", limit: int = 3
) -> list[tuple[str, str, bool]]:
    """Ordered, deduped (local, generating_pattern, informed) guesses for a person.

    informed=True means the generating pattern came from actual domain knowledge
    (the known pattern or its shape family) — not the generic fallback list. Callers
    must not label fallback guesses as pattern-backed."""
    informed_set: list[str] = []
    if pattern:
        informed_set.append(pattern)
    if shape:
        informed_set.extend(SHAPE_TO_PATTERNS.get(shape, []))
    order = informed_set + SHAPE_TO_PATTERNS["unknown"]
    seen, out = set(), []
    for i, p in enumerate(order):
        local = apply_pattern(p, first, last)
        if local and local not in seen:
            seen.add(local)
            out.append((local, p, i < len(informed_set)))
        if len(out) >= limit:
            break
    return out


def candidate_locals(first: str, last: str, *, pattern: str = "", shape: str = "", limit: int = 3) -> list[str]:
    """Ordered, deduped local-part guesses for a person, best pattern knowledge first."""
    return [local for local, _, _ in candidate_pairs(first, last, pattern=pattern, shape=shape, limit=limit)]


def pattern_matches_shape(pattern: str, shape: str) -> bool:
    return pattern in SHAPE_TO_PATTERNS.get(shape, [])
