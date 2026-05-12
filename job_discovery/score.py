"""Scoring agent for job listings.

Two paths:
  - score_llm(): primary, uses Claude Agent SDK with criteria + preferences as context
  - score_rule_based(): fallback for when the API call fails. Pure keyword overlap.

Both return the same shape:
    {
      "overall": float,                 # 1.0-5.0
      "dims": {
        "role_fit": int (1-5),
        "skills_match": int,
        "seniority": int,
        "domain": int,
        "location": int,
        "responsibilities": int,
      },
      "one_line_take": str,             # ≤200 chars
      "method": "llm" | "fallback",
    }
"""
import logging

logger = logging.getLogger(__name__)

# Coarse keyword sets for the rule-based fallback. Not meant to compete with
# the LLM — meant to keep us functional when the API is down.
_AEROSPACE_KW = {"aerospace", "aircraft", "propulsion", "spacecraft", "satellite", "nasa"}
_INDUSTRIAL_KW = {"hrsg", "boiler", "heat exchanger", "energy", "power plant", "industrial"}
_HANDS_ON_KW = {"design", "build", "prototype", "fabricat", "test", "lab", "hands-on"}
_COORDINATION_KW = {"manager", "coordinator", "program", "stakeholder", "governance"}
_SENIOR_KW = {"senior", "sr.", "principal", "staff", "lead", "8+ years", "10+ years"}
_LA_KW = {"los angeles", "la, ca", "costa mesa", "santa monica", "el segundo", "torrance"}

# Words that indicate "engineering-adjacent but not target" — sales/support/etc.
# Engineering titles with these qualifiers get aggressively downscored on
# role_fit and responsibilities (e.g., "Sales Engineer," "Customer Success
# Engineer"). Verbs like "sell"/"market" in the description count too.
_NON_TARGET_QUALIFIERS = {"sales", "sell", "support", "customer", "marketing", "field rep"}

# Common words to ignore when matching role names — "engineer" appears in
# almost every engineering title, so it's not distinguishing.
_GENERIC_ROLE_WORDS = {"engineer", "engineering", "design", "designer"}


def _kw_hit(text: str, kws: set[str]) -> bool:
    t = text.lower()
    return any(k in t for k in kws)


def _score_role_fit(listing: dict, criteria: dict) -> int:
    title = (listing.get("title") or "").lower()
    if not title:
        return 1
    # Sales/Support/Customer-Success "Engineer" titles are clearly off-target
    # even though they contain "engineer". Catch them before the word match.
    if "engineer" in title and _kw_hit(title, _NON_TARGET_QUALIFIERS):
        return 1

    target_roles = [r.lower() for r in criteria.get("roles", [])]
    # Direct substring match against full target role name
    for r in target_roles:
        if r in title:
            return 5
    # Distinguishing-word match: at least one word from the role appears,
    # excluding generic words ("engineer", "design") that would over-match.
    for r in target_roles:
        distinguishing = [
            w for w in r.split()
            if len(w) > 4 and w not in _GENERIC_ROLE_WORDS
        ]
        if distinguishing and any(w in title for w in distinguishing):
            return 4
    # Engineering-adjacent but no clear target signal
    if "engineer" in title:
        if _kw_hit(title, _COORDINATION_KW):
            return 2
        return 3
    return 1


def _score_skills_match(listing: dict, criteria: dict) -> int:
    desc = (listing.get("description") or "").lower()
    if not desc:
        return 3  # no info — neutral
    target_role_words = set()
    for r in criteria.get("roles", []):
        target_role_words.update(w.lower() for w in r.split() if len(w) > 3)
    if not target_role_words:
        return 3
    hits = sum(1 for w in target_role_words if w in desc)
    if hits >= 3:
        return 5
    if hits == 2:
        return 4
    if hits == 1:
        return 3
    return 2


def _score_seniority(listing: dict) -> int:
    title = (listing.get("title") or "").lower()
    desc = (listing.get("description") or "").lower()
    if "intern" in title or "entry" in title or "i " in title or title.endswith(" i"):
        return 4
    if "principal" in title or "staff" in title:
        return 1
    if "sr." in title or "senior" in title or "lead" in title:
        return 2
    if "8+ years" in desc or "10+ years" in desc or "12+ years" in desc:
        return 2
    return 4  # mid-IC default


def _score_domain(listing: dict) -> int:
    text = (listing.get("title", "") + " " + listing.get("description", "")).lower()
    if _kw_hit(text, _AEROSPACE_KW):
        return 5
    if _kw_hit(text, _INDUSTRIAL_KW):
        return 4
    if "engineer" in text:
        return 3
    return 2


def _score_location(listing: dict, criteria: dict) -> int:
    loc = (listing.get("location") or "").lower()
    if not loc:
        return 3
    if _kw_hit(loc, _LA_KW):
        return 1  # LA — heavily downweighted, NOT a hard gate
    target_locs = [l.lower() for l in criteria.get("locations", [])]
    for tl in target_locs:
        # Match on city (first comma-separated chunk)
        city = tl.split(",")[0].strip()
        if city and city in loc:
            return 5
    # Mid-large city heuristic — no exhaustive list, so return 3 by default
    return 3


def _score_responsibilities(listing: dict) -> int:
    text = (listing.get("title", "") + " " + listing.get("description", "")).lower()
    # Sales/support/customer-facing work is the opposite of hands-on design.
    if _kw_hit(text, _NON_TARGET_QUALIFIERS):
        return 1
    hands_on = _kw_hit(text, _HANDS_ON_KW)
    coord = _kw_hit(text, _COORDINATION_KW)
    if hands_on and not coord:
        return 5
    if hands_on and coord:
        return 3
    if coord and not hands_on:
        return 1
    return 3  # no info


def score_rule_based(listing: dict, criteria: dict) -> dict:
    """Deterministic keyword-overlap scorer. Used when the LLM call fails.

    Returns the same shape as score_llm(). Marked method="fallback" so the
    caller / morning brief can flag it.
    """
    dims = {
        "role_fit": _score_role_fit(listing, criteria),
        "skills_match": _score_skills_match(listing, criteria),
        "seniority": _score_seniority(listing),
        "domain": _score_domain(listing),
        "location": _score_location(listing, criteria),
        "responsibilities": _score_responsibilities(listing),
    }

    weights = criteria.get("weights") or {}
    if not weights:
        weights = {k: 1.0 for k in dims}
    weighted_sum = sum(dims[k] * weights.get(k, 1.0) for k in dims)
    weight_total = sum(weights.get(k, 1.0) for k in dims)
    overall = round(weighted_sum / weight_total, 1) if weight_total else 0.0

    title = listing.get("title") or "(untitled)"
    company = listing.get("company") or "?"
    take = f"{title} at {company} — fallback score (rule-based, LLM unavailable)."
    if dims["role_fit"] >= 4:
        take += " Strong role fit."
    if dims["location"] == 1:
        take += " LA — flagging."

    return {
        "overall": overall,
        "dims": dims,
        "one_line_take": take[:200],
        "method": "fallback",
    }
