"""Scoring agent for job listings.

Public API:
  - score_listing(): synchronous facade — the entry point cli.py uses. Tries
    LLM, falls back to rule-based on any failure.
  - score_llm(): async primary path via Claude Agent SDK.
  - score_rule_based(): pure keyword overlap fallback.

All three return the same shape:
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
import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping

from job_discovery.types import (
    Criteria,
    ExperienceProfile,
    Listing,
    Preferences,
    ScoreDims,
    ScoreResult,
)

logger = logging.getLogger(__name__)

# Coarse keyword sets for the rule-based fallback. Not meant to compete with
# the LLM — meant to keep us functional when the API is down.
_AEROSPACE_KW = {"aerospace", "aircraft", "propulsion", "spacecraft", "satellite", "nasa"}
_INDUSTRIAL_KW = {"hrsg", "boiler", "heat exchanger", "energy", "power plant", "industrial"}
_HANDS_ON_KW = {"design", "build", "prototype", "fabricat", "test", "lab", "hands-on"}
_COORDINATION_KW = {"manager", "coordinator", "program", "stakeholder", "governance"}
_SENIOR_KW = {"senior", "sr.", "principal", "staff", "lead", "8+ years", "10+ years"}
_LA_KW = {"los angeles", "la, ca", "costa mesa", "santa monica", "el segundo", "torrance"}

# ---------------------------------------------------------------------------
# Shared scoring/penalty helpers
# ---------------------------------------------------------------------------


def _compute_overall_score(
    dims: Mapping[str, int], weights: Mapping[str, float],
) -> float:
    """Weight-and-average the dim scores into a single 1.0–5.0 overall.
    Falls back to equal weighting if `weights` is empty so the rule-based
    scorer can call this with `criteria.get("weights") or {}`.
    """
    if not weights:
        weights = {k: 1.0 for k in dims}
    weighted_sum = sum(dims[k] * weights.get(k, 1.0) for k in dims)
    weight_total = sum(weights.get(k, 1.0) for k in dims)
    return round(weighted_sum / weight_total, 1) if weight_total else 0.0


def _init_penalty_out(score_result: ScoreResult) -> tuple[dict, str]:
    """Shallow-copy a score result for in-place mutation without touching
    the input, and return the prepared (out_dict, current_take_text) pair.
    Used by both penalty functions to ensure consistent copy semantics.
    """
    out: dict = {**score_result, "dims": {**score_result["dims"]}}
    take = str(out.get("one_line_take") or "").strip()
    return out, take


def _append_flag(take: str, flag: str) -> str:
    """Append `flag` to `take` with the standard " — " separator, capped
    at 200 chars. Idempotent — returns `take` unchanged if the flag is
    already present.
    """
    if flag in take:
        return take
    combined = (take + " — " + flag) if take else flag
    return combined[:200]


# Words that indicate "engineering-adjacent but not target" — sales/support/etc.
# Engineering titles with these qualifiers get aggressively downscored on
# role_fit and responsibilities (e.g., "Sales Engineer," "Customer Success
# Engineer"). Verbs like "sell"/"market" in the description count too.
_NON_TARGET_QUALIFIERS = {"sales", "sell", "support", "customer", "marketing", "field rep"}

# Common words to ignore when matching role names — "engineer" appears in
# almost every engineering title, so it's not distinguishing.
_GENERIC_ROLE_WORDS = {"engineer", "engineering", "design", "designer"}

# Description keywords that indicate a Manufacturing/Production-Engineer
# role is the process/CNC/floor-ops flavor rather than mechanical-design.
# When the title says "Manufacturing Engineer" and the desc has any of
# these, role_fit caps at 2 even if the title would otherwise hit secondary.
# Reason: "company serves aerospace customers" ≠ "aerospace engineering
# role" — process work on the production floor isn't Tavin's lane.
_PROCESS_MFG_KEYWORDS = (
    "cnc", "machinist", "machining", "fixturing", "fixture",
    "production floor", "dfm", "dfa", "tooling", "lean manufacturing",
    "process engineering", "manufacturing process", "shop floor",
    "production line", "work instruction", "routing sheet",
)


def _kw_hit(text: str, kws: set[str]) -> bool:
    t = text.lower()
    return any(k in t for k in kws)


def _score_role_fit(listing: Listing, criteria: Criteria) -> int:
    title = (listing.get("title") or "").lower()
    if not title:
        return 1
    # Title exclusions from criteria.md — substring-match (case-insensitive)
    # so "Senior" matches "Senior Mechanical Engineer." Mirrors the rule the
    # LLM scorer is asked to enforce in scoring_system.txt.
    title_exclusions = [t.lower() for t in criteria.get("title_exclusions", []) if t]
    if any(excl in title for excl in title_exclusions):
        return 1
    # Sales/Support/Customer-Success "Engineer" titles are clearly off-target
    # even though they contain "engineer". Catch them before the word match.
    if "engineer" in title and _kw_hit(title, _NON_TARGET_QUALIFIERS):
        return 1

    # Tiered role match. Primary direct substring → 5; secondary direct
    # substring → 3 (per the 6/3/26 fix — "Manufacturing Engineer" was
    # scoring 5 because all titles were flat). Fall back to the flat `roles`
    # list as primary if criteria.md doesn't have tiering H3s.
    primary = [r.lower() for r in criteria.get("roles_primary", []) if r]
    secondary = [r.lower() for r in criteria.get("roles_secondary", []) if r]
    if not primary and not secondary:
        primary = [r.lower() for r in criteria.get("roles", []) if r]

    desc = (listing.get("description") or "").lower()

    # Manufacturing/Production-Engineer process-role cap. If the title hits
    # a manufacturing/production-engineer pattern AND the desc reads as
    # process/CNC work, cap at 2 regardless of tier match.
    is_mfg_title = (
        ("manufacturing engineer" in title or "production engineer" in title)
    )
    mfg_process_cap = is_mfg_title and _kw_hit(desc, set(_PROCESS_MFG_KEYWORDS))

    for r in primary:
        if r in title:
            return 2 if mfg_process_cap else 5
    for r in secondary:
        if r in title:
            return 2 if mfg_process_cap else 3

    # Distinguishing-word match: at least one word from the role appears,
    # excluding generic words ("engineer", "design") that would over-match.
    for r in primary:
        distinguishing = [
            w for w in r.split()
            if len(w) > 4 and w not in _GENERIC_ROLE_WORDS
        ]
        if distinguishing and any(w in title for w in distinguishing):
            return 2 if mfg_process_cap else 4
    for r in secondary:
        distinguishing = [
            w for w in r.split()
            if len(w) > 4 and w not in _GENERIC_ROLE_WORDS
        ]
        if distinguishing and any(w in title for w in distinguishing):
            return 2 if mfg_process_cap else 3
    # Engineering-adjacent but no clear target signal
    if "engineer" in title:
        if _kw_hit(title, _COORDINATION_KW):
            return 2
        return 3
    return 1


def _score_skills_match(listing: Listing, criteria: Criteria) -> int:
    desc = (listing.get("description") or "").lower()
    if not desc:
        return 3  # no info — neutral
    target_role_words: set[str] = set()
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


def _score_seniority(listing: Listing) -> int:
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


def _score_domain(listing: Listing) -> int:
    text = (listing.get("title", "") + " " + listing.get("description", "")).lower()
    if _kw_hit(text, _AEROSPACE_KW):
        return 5
    if _kw_hit(text, _INDUSTRIAL_KW):
        return 4
    if "engineer" in text:
        return 3
    return 2


def _score_location(listing: Listing, criteria: Criteria) -> int:
    loc = (listing.get("location") or "").lower()
    if not loc:
        return 3
    if _kw_hit(loc, _LA_KW):
        return 1  # LA — heavily downweighted, NOT a hard gate
    target_locs = [loc_str.lower() for loc_str in criteria.get("locations", [])]
    for tl in target_locs:
        # Match on city (first comma-separated chunk)
        city = tl.split(",")[0].strip()
        if city and city in loc:
            return 5
    # Mid-large city heuristic — no exhaustive list, so return 3 by default
    return 3


def _score_responsibilities(listing: Listing) -> int:
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


def score_rule_based(listing: Listing, criteria: Criteria) -> ScoreResult:
    """Deterministic keyword-overlap scorer. Used when the LLM call fails.

    Returns the same shape as score_llm(). Marked method="fallback" so the
    caller / morning brief can flag it.
    """
    dims: ScoreDims = {
        "role_fit": _score_role_fit(listing, criteria),
        "skills_match": _score_skills_match(listing, criteria),
        "seniority": _score_seniority(listing),
        "domain": _score_domain(listing),
        "location": _score_location(listing, criteria),
        "responsibilities": _score_responsibilities(listing),
    }

    # TypedDicts aren't Mapping-compatible in mypy (values are `object`),
    # but at runtime ScoreDims is just a dict[str, int].
    overall = _compute_overall_score(dims, criteria.get("weights") or {})  # type: ignore[arg-type]

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


# ---------------------------------------------------------------------------
# Salary handling — applied after scoring as a deterministic post-step.
# Both the LLM and rule-based scorers ignore salary in their dim scores;
# the orchestrator (cli.cmd_scan) calls apply_salary_penalty on the result.
# ---------------------------------------------------------------------------

# Match "$70K", "$120K", etc. — the first number-K token in a salary string.
_SALARY_K_RE = re.compile(r"\$(\d+)K", re.IGNORECASE)


def _extract_salary_min(salary_str: Any) -> int | None:
    """Pull the first '$NK' value out of a salary string and return it as
    an int (e.g. '$70K-$90K' → 70000, '$70K+' → 70000). None if no match.
    """
    if not salary_str or not isinstance(salary_str, str):
        return None
    m = _SALARY_K_RE.search(salary_str)
    if not m:
        return None
    try:
        return int(m.group(1)) * 1000
    except ValueError:
        return None


def apply_salary_penalty(
    score_result: ScoreResult, listing: Listing, criteria: Criteria,
) -> ScoreResult:
    """Post-scoring salary adjustment. Returns a NEW dict — does not mutate.

    Behavior (per Tavin 2026-05-12 design call):
      - No salary_floor in criteria → no change
      - Listing salary missing → no penalty, but flag in one_line_take
      - Listing salary >= floor → no change
      - Listing salary < floor → reduce overall by 0.5 (clamped to 1.0 min),
        append "below floor: $XK posted vs $YK floor" to one_line_take
    """
    out, take = _init_penalty_out(score_result)

    salary_floor = criteria.get("salary_floor")
    listing_salary = listing.get("salary", "")
    listing_min = _extract_salary_min(listing_salary)

    if salary_floor is None:
        return out  # type: ignore[return-value]

    if listing_min is None:
        out["one_line_take"] = _append_flag(take, "salary not posted")
        return out  # type: ignore[return-value]

    if listing_min < salary_floor:
        out["overall"] = max(1.0, round(out["overall"] - 0.5, 1))
        flag = (
            f"below floor: ${listing_min // 1000}K posted "
            f"vs ${salary_floor // 1000}K floor"
        )
        out["one_line_take"] = _append_flag(take, flag)

    return out  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Experience handling — deterministic years/domain penalty applied after
# rule-based scoring (pre-LLM gate) AND after LLM scoring (so the soft
# penalty is enforced even when the LLM was generous on the seniority dim).
#
# Closes the recurring "3+ yrs required slipped through" bug surfaced
# 2026-05-16 (Akkodis), 2026-05-21 (Auriga Space), 2026-05-25 (Burns &
# McDonnell).
# ---------------------------------------------------------------------------

_YEARS_RANGE_DASH_RE = re.compile(
    r"(\d+)\s*[-–—]\s*(\d+)\s*(?:years?|yrs?)\b", re.IGNORECASE,
)
_YEARS_RANGE_TO_RE = re.compile(
    r"(\d+)\s+to\s+(\d+)\s*(?:years?|yrs?)\b", re.IGNORECASE,
)
_YEARS_PLUS_RE = re.compile(
    r"(\d+)\s*\+\s*(?:years?|yrs?)\b", re.IGNORECASE,
)
_YEARS_MIN_RE = re.compile(
    r"(?:min(?:imum)?|at\s+least)\s+(\d+)\s*(?:years?|yrs?)\b", re.IGNORECASE,
)
_YEARS_PLAIN_RE = re.compile(
    r"(\d+)\s*(?:years?|yrs?)\b", re.IGNORECASE,
)
# "experience"-y context words that must appear within _CONTEXT_WINDOW chars
# of a year phrase for it to count as a years-of-experience requirement (vs
# "30+ years in business" or "founded 2014"). The "min/at least" pattern is
# self-evidencing and skips this check.
_EXPERIENCE_INDICATOR_RE = re.compile(
    r"\b(?:experience|exp\.?|relevant|professional|background|qualif\w+|"
    r"required|preferred|seeking)\b",
    re.IGNORECASE,
)
_CONTEXT_WINDOW = 60


# Each Tavin-domain slug → keyword list scanned against the JD text. Living
# in code (not criteria.md) so it can be versioned with the scorer and
# extended without a vault edit. Keep the keywords precise — a too-broad
# "mechanical" would match every mech eng JD and erase the mismatch signal.
_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "aerospace": (
        "aerospace", "aircraft", "aviation", "spacecraft", "satellite",
        "nasa", "propulsion", "rocket", "launch vehicle", "avionics",
        "artemis", "ksc",
    ),
    "thermal": (
        "thermal", "heat transfer", "heat exchanger", "hvac", "thermodynamic",
        "cooling system",
    ),
    "cryogenic": (
        "cryogenic", "cryogen", "lh2", "lox", "liquid nitrogen", "ln2",
        "liquid hydrogen", "tvc", "thermal vacuum",
    ),
    "mechanical_design": (
        "mechanical design", "mechanical engineering", "solidworks",
        "gd&t", "drafting", "fabrication", "cad model",
    ),
    "test_operations": (
        "test conductor", "integration test", "test engineering",
        "test campaign", "hil", "dvt", "ground support",
    ),
}

# Non-Tavin domain keywords. A JD that mentions one of these AND lacks any
# Tavin-domain keyword is treated as a domain mismatch (penalty +1). The
# Burns-McDonnell case ("power generation") is the canonical example.
_NON_DOMAIN_KEYWORDS: tuple[str, ...] = (
    "power generation", "power plant", "petroleum", "oil and gas",
    "oil & gas", "automotive", "vehicle dynamics", "medical device",
    "consumer electronics", "robotics", "mining", "agriculture",
    "food processing", "semiconductor fab",
)


def _has_experience_context(text: str, span: tuple[int, int]) -> bool:
    start = max(0, span[0] - _CONTEXT_WINDOW)
    end = min(len(text), span[1] + _CONTEXT_WINDOW)
    return bool(_EXPERIENCE_INDICATOR_RE.search(text[start:end]))


def _spans_overlap(s1: tuple[int, int], s2: tuple[int, int]) -> bool:
    return not (s1[1] <= s2[0] or s2[1] <= s1[0])


def extract_required_years(text: str) -> tuple[int | None, int | None]:
    """Extract the most demanding years-of-experience requirement from text.

    Returns (min_required, max_required).
      - Range "3-7 years"  → (3, 7)
      - Plus  "5+ years"   → (5, None)   — unbounded above
      - Min   "min 2 yrs"  → (2, None)   — unbounded above
      - Plain "3 years exp"→ (3, 3)
      - No match           → (None, None)

    For everything except `min/at least`, the year phrase must sit within
    `_CONTEXT_WINDOW` chars of an "experience"-y indicator (experience,
    relevant, professional, required, preferred, etc.). This is what
    keeps "30+ years in business" / "founded in 2014" from registering.

    If multiple phrases match, the most demanding wins: highest effective
    upper bound, with unbounded-above (`+`/`min`) preferred at ties.
    """
    if not text:
        return (None, None)

    # (lo, hi_or_None, effective_for_ranking, span)
    candidates: list[tuple[int, int | None, int, tuple[int, int]]] = []
    consumed: list[tuple[int, int]] = []

    # 1) Ranges first — they're the most specific. "3-7 years" and "3 to 7 years".
    for m in _YEARS_RANGE_DASH_RE.finditer(text):
        if _has_experience_context(text, m.span()):
            lo, hi = int(m.group(1)), int(m.group(2))
            candidates.append((lo, hi, hi, m.span()))
            consumed.append(m.span())
    for m in _YEARS_RANGE_TO_RE.finditer(text):
        if any(_spans_overlap(m.span(), s) for s in consumed):
            continue
        if _has_experience_context(text, m.span()):
            lo, hi = int(m.group(1)), int(m.group(2))
            candidates.append((lo, hi, hi, m.span()))
            consumed.append(m.span())

    # 2) Plus — "5+ years". Context required (don't trip on "30+ years in business").
    for m in _YEARS_PLUS_RE.finditer(text):
        if any(_spans_overlap(m.span(), s) for s in consumed):
            continue
        if _has_experience_context(text, m.span()):
            n = int(m.group(1))
            candidates.append((n, None, n, m.span()))
            consumed.append(m.span())

    # 3) Minimum — self-evidencing, no context check needed.
    for m in _YEARS_MIN_RE.finditer(text):
        if any(_spans_overlap(m.span(), s) for s in consumed):
            continue
        n = int(m.group(1))
        candidates.append((n, None, n, m.span()))
        consumed.append(m.span())

    # 4) Plain "N years" with context. Lowest precedence.
    for m in _YEARS_PLAIN_RE.finditer(text):
        if any(_spans_overlap(m.span(), s) for s in consumed):
            continue
        if _has_experience_context(text, m.span()):
            n = int(m.group(1))
            candidates.append((n, n, n, m.span()))
            consumed.append(m.span())

    if not candidates:
        return (None, None)

    # Most demanding: highest effective, tie-break unbounded-above first.
    candidates.sort(
        key=lambda c: (c[2], 1 if c[1] is None else 0),
        reverse=True,
    )
    lo, hi, _, _ = candidates[0]
    return (lo, hi)


def _is_domain_mismatch(text: str, tavin_domains: list[str]) -> bool:
    """True if the listing text contains a non-Tavin-domain keyword AND
    lacks any keyword for Tavin's domains. Conservative — a listing that
    mentions both 'power' and 'aerospace' is not a mismatch (overlap wins)."""
    if not tavin_domains:
        return False
    text_lower = text.lower()
    for slug in tavin_domains:
        keywords = _DOMAIN_KEYWORDS.get(
            slug, (slug.replace("_", " "),),
        )
        if any(kw in text_lower for kw in keywords):
            return False  # Tavin-domain overlap — no mismatch
    return any(kw in text_lower for kw in _NON_DOMAIN_KEYWORDS)


def _format_years_str(lo: int, hi: int | None) -> str:
    """Human-readable years string for one_line_take flags.
    (3, 7) → '3-7', (5, None) → '5+', (3, 3) → '3'."""
    if hi is None:
        return f"{lo}+"
    if hi == lo:
        return str(lo)
    return f"{lo}-{hi}"


def apply_experience_penalty(
    score_result: ScoreResult, listing: Listing, criteria: Criteria,
) -> ScoreResult:
    """Post-scoring years/domain penalty. Returns a NEW dict — does not mutate.

    Behavior (per Tavin 2026-05-25 design call):
      - No `experience` profile in criteria OR no `years_total` → no change.
      - No years phrase extracted from JD → no change.
      - effective_required >= years_total + hard_filter_years_above:
          HARD FILTER. Set overall to 1.0 and flag "hard-filter: requires Xyrs"
          so the listing drops below PRE_FILTER_THRESHOLD and bypasses LLM.
      - effective_required > years_total:
          SOFT PENALTY. Drop seniority dim by (years_above + domain_mismatch),
          recompute overall, append "Xyrs required (you have ~M)" flag.
      - effective_required <= years_total: no change.

    Idempotent: re-applying with the same input gives the same output
    (seniority is set to a target value computed from the JD, not
    decremented). Safe to call both pre- and post-LLM.
    """
    experience: ExperienceProfile | None = criteria.get("experience")  # type: ignore[assignment]
    if not experience or "years_total" not in experience:
        return score_result

    title = (listing.get("title") or "")
    desc = (listing.get("description") or "")
    listing_text = f"{title} {desc}"
    # Scan title + description together so a years-of-experience phrase in
    # the title ("Engineer III, 5+ yrs") isn't missed when desc is partial.
    lo, hi = extract_required_years(listing_text)
    if lo is None:
        return score_result

    out, take = _init_penalty_out(score_result)
    years_total = experience["years_total"]
    buffer = experience.get("hard_filter_years_above", 3)
    effective_required = hi if hi is not None else lo
    years_str = _format_years_str(lo, hi)

    if effective_required >= years_total + buffer:
        out["overall"] = 1.0
        out["one_line_take"] = _append_flag(
            take, f"hard-filter: requires {years_str}yrs",
        )
        return out  # type: ignore[return-value]

    if effective_required > years_total:
        years_above = effective_required - years_total
        mismatch = _is_domain_mismatch(
            listing_text, list(experience.get("domains", [])),
        )
        penalty_points = years_above + (1 if mismatch else 0)

        dims = dict(out["dims"])
        # Target the seniority dim. Use max(1, current - penalty) — clamps
        # at the 1.0 floor and makes the function idempotent.
        dims["seniority"] = max(1, dims["seniority"] - penalty_points)
        out["dims"] = dims  # type: ignore[typeddict-item]
        out["overall"] = max(1.0, _compute_overall_score(
            dims, criteria.get("weights") or {},
        ))

        flag = f"{years_str}yrs required (you have ~{years_total})"
        if mismatch:
            flag += " in non-domain"
        out["one_line_take"] = _append_flag(take, flag)
        return out  # type: ignore[return-value]

    return out  # type: ignore[return-value]


def apply_unverified_penalty(score_result: ScoreResult) -> ScoreResult:
    """Soft penalty for a high blind-default score we could not verify.

    Triggered when a listing had no scraped description AND scored >4.0 on
    blind defaults AND the WebFetch JD recovery also came back empty. The
    high score is suspect (unknown dims — esp. seniority — defaulted high),
    so downrank it off the top of the brief without dropping it.

    Mirrors apply_salary_penalty: returns a NEW dict, does not mutate.
      - overall reduced by 0.5 (clamped to 1.0 min)
      - append the unverified flag to one_line_take (idempotent), capped 200
    """
    out, take = _init_penalty_out(score_result)
    out["overall"] = max(1.0, round(out.get("overall", 0.0) - 0.5, 1))
    out["one_line_take"] = _append_flag(take, "⚠ unverified — JD unreadable")
    return out  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# LLM-based scoring (primary path)
# ---------------------------------------------------------------------------

# Strip ```json ... ``` fences if the model emits them.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _strip_fence(text: str) -> str:
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text.strip()


def _listing_payload(listing: Listing) -> dict[str, str]:
    """Per-listing scoring payload. Centralized so single + batch paths
    use the identical schema and 4000-char description truncation."""
    return {
        "title": listing.get("title", ""),
        "company": listing.get("company", ""),
        "location": listing.get("location", ""),
        "salary": listing.get("salary", ""),
        "description": listing.get("description", "")[:4000],
    }


def _assemble_user_prompt(listing: Listing) -> str:
    """User prompt = ONLY the per-listing payload. The profile blob,
    criteria, and preferences move into the system prompt so they hit
    the Claude Code prompt cache instead of being resent on every call.
    """
    return (
        "Job to score:\n\n```json\n"
        + json.dumps(_listing_payload(listing), indent=2, default=str)
        + "\n```\n\nRespond with the JSON object only."
    )


def _assemble_batch_user_prompt(listings: list[Listing]) -> str:
    """User prompt for batch scoring: a JSON array of listings.
    The model returns a JSON array of N score objects in the same order.
    Saves ~80-90% of system-prefix tokens vs N single-listing calls."""
    payloads = [_listing_payload(item) for item in listings]
    return (
        f"Score the following {len(payloads)} jobs. The input is a JSON "
        "array; return a JSON array of the same length, one score object "
        "per input listing, in the same order.\n\n```json\n"
        + json.dumps(payloads, indent=2, default=str)
        + "\n```\n\nRespond with the JSON array only."
    )


def _assemble_system_prompt(
    base_system_text: str,
    criteria: Criteria,
    preferences: Preferences,
    profile_blob: str,
) -> str:
    """System prompt = base scoring rules + Tavin's profile + criteria +
    preferences. All four are identical across every scoring call in a
    nightly run, so writing them once to a stable file path lets Claude
    Code's prompt cache amortize the ~5K-token prefix across the
    nightly run instead of paying the cost each time.
    """
    static = {
        "criteria": {
            "roles_primary": criteria.get("roles_primary", []),
            "roles_secondary": criteria.get("roles_secondary", []),
            "roles": criteria.get("roles", []),
            "title_exclusions": criteria.get("title_exclusions", []),
            "locations": criteria.get("locations", []),
            "salary_floor": criteria.get("salary_floor"),
            "notes": criteria.get("notes", ""),
        },
        "preferences": {
            "learned_patterns": preferences.get("learned_patterns", ""),
            "recent_pass_reasons": preferences.get("recent_pass_reasons", [])[:30],
        },
    }
    return (
        base_system_text.rstrip()
        + "\n\n---\n\nTavin's profile (excerpt from tavin.md and "
        + "Job_Search/README.md):\n\n"
        + profile_blob.rstrip()
        + "\n\n---\n\nScoring context (criteria + preferences):\n\n```json\n"
        + json.dumps(static, indent=2, default=str)
        + "\n```\n"
    )


_REQUIRED_DIMS = (
    "role_fit", "skills_match", "seniority", "domain", "location",
    "responsibilities",
)


def _validate_score_obj(
    data: Any, weights: dict[str, float],
) -> ScoreResult | None:
    """Turn one parsed JSON score object into a ScoreResult, or None if
    the shape is invalid. Shared between single + batch parsers so the
    validation rules stay in lockstep."""
    if not isinstance(data, dict):
        return None
    dims = data.get("dims")
    if not isinstance(dims, dict):
        return None
    if not all(k in dims for k in _REQUIRED_DIMS):
        logger.warning("score: missing required dims in %s", dims)
        return None
    if not all(isinstance(dims[k], int) and 1 <= dims[k] <= 5 for k in _REQUIRED_DIMS):
        logger.warning("score: dim out of range in %s", dims)
        return None
    take = (data.get("one_line_take") or "").strip()[:200]
    dims_narrowed: dict[str, int] = {k: dims[k] for k in _REQUIRED_DIMS}
    overall = _compute_overall_score(dims_narrowed, weights)
    return {
        "overall": overall,
        "dims": dims_narrowed,  # type: ignore[typeddict-item]
        "one_line_take": take,
        "method": "llm",
    }


def _parse_score_response(
    raw: str, weights: dict[str, float],
) -> ScoreResult | None:
    raw = _strip_fence(raw or "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("score: could not parse JSON: %s", raw[:200])
        return None
    return _validate_score_obj(data, weights)


def _parse_batch_score_response(
    raw: str, weights: dict[str, float], n: int,
) -> list[ScoreResult | None]:
    """Parse a batch response (JSON array of N score objects).

    Returns a list of length n where each position is either a valid
    ScoreResult or None. Failure modes:
      - raw empty / not JSON / not an array → all None
      - array length mismatch → all None (untrusted whole batch)
      - one bad item in a valid-length array → only that index is None

    The caller falls back to rule-based for None positions.
    """
    raw = _strip_fence(raw or "")
    all_none: list[ScoreResult | None] = [None] * n
    if not raw:
        return all_none
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("score: could not parse batch JSON: %s", raw[:200])
        return all_none
    if not isinstance(data, list):
        logger.warning(
            "score: batch response not a list (got %s); falling back",
            type(data).__name__,
        )
        return all_none
    if len(data) != n:
        logger.warning(
            "score: batch length mismatch — expected %d, got %d; falling back",
            n, len(data),
        )
        return all_none
    return [_validate_score_obj(item, weights) for item in data]


# Haiku is plenty for a constrained classifier task with a fixed-shape JSON
# output. Sonnet was overkill — and 5× more expensive against the daily
# Sonnet rate-limit window that the 3am scan shares with Mizzix's morning
# brief. Override via MIZZIX_MODEL env var if a future scoring change ever
# needs Sonnet's reasoning quality.
_DEFAULT_SCORING_MODEL = "claude-haiku-4-5-20251001"


def write_scoring_system_prompt(
    criteria: Criteria, preferences: Preferences, profile_blob: str,
) -> Path:
    """Write the combined system prompt (rules + profile + context) to a
    stable file path and return it. Called ONCE per scan from cli.cmd_scan
    so every per-listing call references the same file — Claude Code's
    prompt cache then amortizes the ~5K-token prefix.

    Idempotent: identical inputs produce identical file contents, so a
    re-run during the same scan is harmless.
    """
    prompt_dir = Path(os.environ["VAULT_PATH"]) / ".mizzix_state"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    base_system_text = (
        Path(__file__).parent / "prompts" / "scoring_system.txt"
    ).read_text(encoding="utf-8")
    full_text = _assemble_system_prompt(
        base_system_text, criteria, preferences, profile_blob,
    )
    system_path = prompt_dir / "job_discovery_scoring_prompt.txt"
    system_path.write_text(full_text, encoding="utf-8")
    return system_path


async def _query_batch_via_client(client: Any, listings: list[Listing]) -> str:
    """Send a batch user prompt through an already-connected SDK client
    and return the raw assistant text. Caller handles parse + retries.

    Crashes are the caller's problem — the persistent-client orchestrator
    catches and converts to all-None for the chunk."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    user_prompt = _assemble_batch_user_prompt(listings)
    await client.query(user_prompt)
    chunks: list[str] = []
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    return "".join(chunks)


async def _run_batch_llm(
    plausible_listings: list[Listing],
    weights: dict[str, float],
    *,
    system_prompt_path: Path,
    model: str | None,
    batch_size: int,
) -> list[ScoreResult | None]:
    """Open ONE SDK client, send all plausible listings through it in
    chunks of `batch_size`, return aligned list-of-(ScoreResult|None).

    The persistent client + cached system prompt means N listings cost
    `ceil(N/batch_size)` SDK round-trips instead of N. Prompt-cache hits
    on the 8.7K-token prefix amortize across the whole scan."""
    os.environ.pop("ANTHROPIC_API_KEY", None)

    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    options = ClaudeAgentOptions(
        system_prompt={"type": "file", "path": str(system_prompt_path)},
        cwd=os.environ["VAULT_PATH"],
        allowed_tools=[],
        permission_mode="bypassPermissions",
        model=model or os.environ.get("MIZZIX_MODEL", _DEFAULT_SCORING_MODEL),
    )

    out: list[ScoreResult | None] = []
    client = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        for start in range(0, len(plausible_listings), batch_size):
            chunk = plausible_listings[start:start + batch_size]
            try:
                raw = await _query_batch_via_client(client, chunk)
            except Exception:
                logger.exception(
                    "_run_batch_llm: chunk start=%d failed; falling back",
                    start,
                )
                out.extend([None] * len(chunk))
                continue
            out.extend(_parse_batch_score_response(raw, weights, n=len(chunk)))
    finally:
        try:
            await client.disconnect()
        except Exception:
            logger.exception("_run_batch_llm: client.disconnect() failed")
    return out


def _default_llm_score_fn(
    plausible_listings: list[Listing],
    weights: dict[str, float],
    *,
    system_prompt_path: Path,
    model: str | None,
    batch_size: int,
) -> list[ScoreResult | None]:
    """Sync wrapper around _run_batch_llm. Returns all-None on any
    top-level crash so the facade falls back cleanly to rule-based."""
    try:
        return asyncio.run(_run_batch_llm(
            plausible_listings, weights,
            system_prompt_path=system_prompt_path,
            model=model, batch_size=batch_size,
        ))
    except Exception:
        logger.exception(
            "_default_llm_score_fn: persistent-client run crashed; "
            "falling back to rule-based for %d listings",
            len(plausible_listings),
        )
        return [None] * len(plausible_listings)


# Default chunk size — keeps user-prompt JSON well under model context
# while still cutting SDK round-trips ~8x. Tune via env var.
_DEFAULT_BATCH_SIZE = int(os.environ.get("JOB_DISCOVERY_BATCH_SIZE", "8"))


def score_listings_batch(
    listings: list[Listing],
    criteria: Criteria,
    preferences: Preferences,
    profile_blob: str,
    *,
    system_prompt_path: Path | None = None,
    model: str | None = None,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    _llm_score_fn: Any = None,
) -> list[ScoreResult]:
    """Score a list of listings, returning an aligned list of ScoreResult.

    Pipeline:
      1. Rule-based score every listing.
      2. Listings below PRE_FILTER_THRESHOLD keep their rule-based result.
      3. Plausible listings go to the LLM through ONE persistent SDK client
         in chunks of `batch_size` — saves both per-call subprocess overhead
         and the ~8.7K-token system-prefix per call (prompt cache amortizes).
      4. Any LLM position that came back None falls back to that listing's
         rule-based score.

    `_llm_score_fn` is the test seam — production calls _default_llm_score_fn,
    which is the asyncio.run-wrapped persistent-client runner.
    """
    if not listings:
        return []

    rule_results: list[ScoreResult] = [
        score_rule_based(l, criteria) for l in listings
    ]
    # Experience penalty BEFORE the plausibility gate so years/domain hard-
    # filters drop the overall below PRE_FILTER_THRESHOLD and never reach
    # the LLM. Soft-penalty cases still go to LLM with their dims adjusted.
    rule_results = [
        apply_experience_penalty(r, l, criteria)
        for r, l in zip(rule_results, listings)
    ]
    plausible_indices = [
        i for i, r in enumerate(rule_results)
        if r["overall"] >= PRE_FILTER_THRESHOLD
    ]
    if not plausible_indices:
        return rule_results

    if system_prompt_path is None:
        system_prompt_path = write_scoring_system_prompt(
            criteria, preferences, profile_blob,
        )

    plausible_listings = [listings[i] for i in plausible_indices]
    weights = criteria.get("weights") or {}
    llm_fn = _llm_score_fn or _default_llm_score_fn

    llm_results = llm_fn(
        plausible_listings, weights,
        system_prompt_path=system_prompt_path,
        model=model,
        batch_size=batch_size,
    )

    # Belt-and-suspenders: a misbehaving fn that returned the wrong length
    # shouldn't crash the scan. Pad/truncate to plausible_indices length.
    if len(llm_results) != len(plausible_indices):
        logger.warning(
            "score_listings_batch: llm_score_fn returned %d, expected %d; "
            "falling back to rule-based for missing positions",
            len(llm_results), len(plausible_indices),
        )
        llm_results = (list(llm_results)
                       + [None] * len(plausible_indices))[:len(plausible_indices)]

    out: list[ScoreResult] = list(rule_results)
    for idx, llm_res in zip(plausible_indices, llm_results):
        if llm_res is not None:
            # Re-apply experience penalty post-LLM so the soft penalty is
            # enforced even when the LLM scored seniority optimistically.
            # Idempotent — re-applying with the same JD gives the same result.
            out[idx] = apply_experience_penalty(llm_res, listings[idx], criteria)
    return out


async def score_llm(
    listing: Listing, criteria: Criteria,
    *,
    system_prompt_path: Path,
    model: str | None = None,
) -> ScoreResult | None:
    """Score one listing via Claude Agent SDK. Returns None on failure
    (caller should fall back to score_rule_based).

    `system_prompt_path` is produced by write_scoring_system_prompt() once
    per scan — keep it stable so the prompt cache hits.

    Mirrors morning_brief.py / heartbeat.py SDK setup pattern."""
    # Force OAuth path: if ANTHROPIC_API_KEY is set in the environment, the
    # SDK uses it instead of Tavin's Claude Max OAuth. Popping it (process-
    # wide) ensures all scoring runs go through the Max subscription. Same
    # pattern Mizzix uses; fine because nothing else in this process needs
    # the key.
    os.environ.pop("ANTHROPIC_API_KEY", None)

    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock,
    )

    user_prompt = _assemble_user_prompt(listing)

    options = ClaudeAgentOptions(
        system_prompt={"type": "file", "path": str(system_prompt_path)},
        cwd=os.environ["VAULT_PATH"],
        allowed_tools=[],
        permission_mode="bypassPermissions",
        model=model or os.environ.get("MIZZIX_MODEL", _DEFAULT_SCORING_MODEL),
    )

    client = ClaudeSDKClient(options=options)
    try:
        await client.connect()
        try:
            await client.query(user_prompt)
            chunks: list[str] = []
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
            raw = "".join(chunks)
        finally:
            await client.disconnect()
    except Exception:
        logger.exception("score_llm: SDK call crashed")
        return None

    return _parse_score_response(raw, criteria.get("weights") or {})


# Listings whose rule-based score is below this gate skip the LLM call
# entirely — the rule-based result is used directly. Cuts daily LLM volume
# substantially (rule-based scoring of obvious non-fits is near-perfect).
# 2.5 is intentionally generous: anything plausible still gets the LLM
# second opinion. Tune via PRE_FILTER_THRESHOLD env var if false-negatives
# show up.
PRE_FILTER_THRESHOLD = float(os.environ.get("JOB_DISCOVERY_PREFILTER", "2.5"))


def score_listing(
    listing: Listing, criteria: Criteria, preferences: Preferences,
    profile_blob: str,
    *,
    system_prompt_path: Path | None = None,
    model: str | None = None,
) -> ScoreResult:
    """Synchronous facade: rule-based pre-filter → LLM if plausible →
    fall back to rule-based on LLM failure.

    The pre-filter gate is the per-night-volume lever: ~60-70% of fetched
    listings rule-score below PRE_FILTER_THRESHOLD and don't need an LLM
    call at all (sales/support engineer titles, wrong-domain leads, etc.).

    Must be called from a sync context (cli.py + cron). `asyncio.run()`
    will raise `RuntimeError` if invoked while another event loop is
    running — if a future caller is async (Jupyter, async CLI), they
    should call `score_llm` directly with `await` instead.

    Legacy call sites that don't pre-build the system prompt (e.g. the
    one-off `score-one` command) pay a small per-call file-write cost
    when `system_prompt_path` is None — correct, just not cache-optimal.
    """
    rule_result = score_rule_based(listing, criteria)
    if rule_result["overall"] < PRE_FILTER_THRESHOLD:
        # Rule-based is confident this is a non-fit. Skip the LLM call;
        # the rule_result already has method="fallback" so downstream
        # rendering correctly flags it.
        return rule_result

    if system_prompt_path is None:
        system_prompt_path = write_scoring_system_prompt(
            criteria, preferences, profile_blob,
        )

    try:
        result = asyncio.run(score_llm(
            listing, criteria,
            system_prompt_path=system_prompt_path, model=model,
        ))
    except Exception:
        logger.exception("score_listing: LLM scoring crashed")
        result = None
    if result is None:
        result = rule_result
    return result
