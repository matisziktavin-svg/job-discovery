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
import asyncio
import json
import logging
import os
import re
from pathlib import Path

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


# ---------------------------------------------------------------------------
# LLM-based scoring (primary path)
# ---------------------------------------------------------------------------

# Strip ```json ... ``` fences if the model emits them.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _strip_fence(text: str) -> str:
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text.strip()


def _assemble_user_prompt(
    listing: dict, criteria: dict, preferences: dict, profile_blob: str,
) -> str:
    payload = {
        "listing": {
            "title": listing.get("title", ""),
            "company": listing.get("company", ""),
            "location": listing.get("location", ""),
            "salary": listing.get("salary", ""),
            "description": listing.get("description", "")[:4000],  # cap to keep prompt sane
        },
        "criteria": {
            "roles": criteria.get("roles", []),
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
        "Tavin's profile (excerpt from tavin.md and Job_Search/README.md):\n\n"
        + profile_blob
        + "\n\nJob to score:\n\n```json\n"
        + json.dumps(payload, indent=2, default=str)
        + "\n```\n\nRespond with the JSON object only."
    )


def _parse_score_response(raw: str, weights: dict) -> dict | None:
    raw = _strip_fence(raw or "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("score: could not parse JSON: %s", raw[:200])
        return None
    dims = data.get("dims")
    if not isinstance(dims, dict):
        return None
    required = ["role_fit", "skills_match", "seniority", "domain", "location", "responsibilities"]
    if not all(k in dims for k in required):
        logger.warning("score: missing required dims in %s", dims)
        return None
    if not all(isinstance(dims[k], int) and 1 <= dims[k] <= 5 for k in required):
        logger.warning("score: dim out of range in %s", dims)
        return None
    take = (data.get("one_line_take") or "").strip()[:200]

    if not weights:
        weights = {k: 1.0 for k in required}
    weighted_sum = sum(dims[k] * weights.get(k, 1.0) for k in required)
    weight_total = sum(weights.get(k, 1.0) for k in required)
    overall = round(weighted_sum / weight_total, 1) if weight_total else 0.0

    return {
        "overall": overall,
        "dims": dims,
        "one_line_take": take,
        "method": "llm",
    }


async def score_llm(
    listing: dict, criteria: dict, preferences: dict, profile_blob: str,
    model: str | None = None,
) -> dict | None:
    """Score one listing via Claude Agent SDK. Returns None on failure
    (caller should fall back to score_rule_based).

    Mirrors morning_brief.py / heartbeat.py SDK setup pattern."""
    # Inherit Claude Max OAuth — same dance Mizzix's other LLM callers do.
    os.environ.pop("ANTHROPIC_API_KEY", None)

    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, TextBlock,
    )

    prompt_dir = Path(os.environ["VAULT_PATH"]) / ".mizzix_state"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    system_prompt_text = (
        Path(__file__).parent / "prompts" / "scoring_system.txt"
    ).read_text(encoding="utf-8")
    system_path = prompt_dir / "job_discovery_scoring_prompt.txt"
    system_path.write_text(system_prompt_text, encoding="utf-8")

    user_prompt = _assemble_user_prompt(listing, criteria, preferences, profile_blob)

    options = ClaudeAgentOptions(
        system_prompt={"type": "file", "path": str(system_path)},
        cwd=os.environ["VAULT_PATH"],
        allowed_tools=[],
        permission_mode="bypassPermissions",
        model=model or os.environ.get("MIZZIX_MODEL", "claude-sonnet-4-6"),
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


def score_listing(
    listing: dict, criteria: dict, preferences: dict, profile_blob: str,
    model: str | None = None,
) -> dict:
    """Synchronous facade: try LLM, fall back to rule-based on failure."""
    try:
        result = asyncio.run(score_llm(listing, criteria, preferences, profile_blob, model))
    except Exception:
        logger.exception("score_listing: LLM scoring crashed")
        result = None
    if result is None:
        result = score_rule_based(listing, criteria)
    return result
