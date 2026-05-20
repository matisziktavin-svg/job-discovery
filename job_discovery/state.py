"""State I/O for job_matches.json and job_matches_history.json.

Mirrors Mizzix's follow_ups.py patterns:
  - atomic write via .tmp + replace()
  - corruption-tolerant load (returns [], leaves file alone)
  - never destructive on parse failure (Tavin may hand-edit)

VAULT_PATH env var must point at the Second Brain vault root.
"""
import datetime as dt
import json
import logging
import os
import re
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)


def _vault() -> Path:
    return Path(os.environ["VAULT_PATH"])


def _matches_path() -> Path:
    return _vault() / ".mizzix_state" / "job_matches.json"


def _history_path() -> Path:
    return _vault() / ".mizzix_state" / "job_matches_history.json"


def _scored_history_path() -> Path:
    return _vault() / ".mizzix_state" / "job_scored_history.json"


def new_match_id() -> str:
    return "jm_" + secrets.token_hex(4)


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("%s corrupt — leaving file alone, returning []", path.name)
        return []
    return items if isinstance(items, list) else []


def _save(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_matches() -> list[dict]:
    return _load(_matches_path())


def save_matches(items: list[dict]) -> None:
    _save(_matches_path(), items)


def load_history() -> list[dict]:
    return _load(_history_path())


def save_history(items: list[dict]) -> None:
    _save(_history_path(), items)


# Separate from history (which is "ever applied / passed") because this is
# the much-larger "ever scored" cache — keeping them apart means
# job_scored_history can be wiped to force a re-evaluation without nuking
# the applications log. Bounded by RETAIN_DAYS so re-listed jobs eventually
# get a second look.
SCORED_HISTORY_RETAIN_DAYS = 14


def load_scored_history() -> list[dict]:
    return _load(_scored_history_path())


def save_scored_history(items: list[dict]) -> None:
    _save(_scored_history_path(), items)


def append_scored_keys(
    keys: list[str],
    today: str,
    *,
    retain_days: int = SCORED_HISTORY_RETAIN_DAYS,
) -> None:
    """Append today's dedupe keys to scored_history and trim entries older
    than retain_days. No-op on empty keys. Idempotent within a day: re-running
    a scan that produced the same keys won't duplicate."""
    if not keys:
        return
    cutoff = (dt.date.fromisoformat(today)
              - dt.timedelta(days=retain_days)).isoformat()
    existing = load_scored_history()
    # Idempotency: drop any prior entry for today with a key we're re-adding.
    today_new = set(keys)
    kept = [
        e for e in existing
        if e.get("scored_date", "") >= cutoff
        and not (e.get("scored_date") == today and e.get("key") in today_new)
    ]
    kept.extend({"key": k, "scored_date": today} for k in keys)
    save_scored_history(kept)


_PASS_REASON_RE = re.compile(
    r"^-\s*\*\*(\d{4}-\d{2}-\d{2})\*\*\s*[—–-]\s*(.+?)\s*$",
    re.MULTILINE,
)

# Match "City, ST" — letters, spaces, periods, hyphens, apostrophes, ampersands
# in the city; two-letter state. Used to filter prose bullets out of the
# Locations list (e.g., a "Hard requirement: medium metro only" bullet under
# ## Locations would otherwise be queried as a city).
_CITY_RE = re.compile(r"^[A-Za-z][\w\s.&'\-]*?,\s*[A-Z]{2}$")

# Heading text patterns indicating "this H3 sub-section under ## Roles is
# about EXCLUDING titles, not including them." Case-insensitive substring.
_EXCLUSION_H3_PATTERNS = ("exclusion", "exclude", "skip", "avoid", "filter out")


def _criteria_path() -> Path:
    return _vault() / "projects" / "Job_Search" / "discovery" / "criteria.md"


def _preferences_path() -> Path:
    return _vault() / "projects" / "Job_Search" / "discovery" / "preferences.md"


def _split_sections(md: str) -> dict[str, str]:
    """Split a markdown doc into {heading_lowercased: body_text} by ## headings."""
    sections: dict[str, str] = {}
    current_key = None
    current_lines: list[str] = []
    for line in md.splitlines():
        h = re.match(r"^##\s+(.+?)\s*$", line)
        if h:
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = h.group(1).strip().lower()
            current_lines = []
        elif current_key is not None:
            current_lines.append(line)
    if current_key is not None:
        sections[current_key] = "\n".join(current_lines).strip()
    return sections


def _bullet_lines(body: str) -> list[str]:
    """Extract bullet items (lines starting with - ) as plain strings."""
    out = []
    for line in body.splitlines():
        m = re.match(r"^-\s+(.+?)\s*$", line)
        if m:
            out.append(m.group(1))
    return out


def _split_h3_subsections(body: str) -> list[tuple[str, str]]:
    """Split an H2 section's body into [(h3_heading_or_None, sub_body), ...].

    Returned in document order. The first element's heading is None if the
    section starts with content before any H3 (those bullets are still
    "in" the H2 but not under any H3 sub-heading).
    """
    out: list[tuple[str | None, str]] = []
    current_h3: str | None = None
    current_lines: list[str] = []
    for line in body.splitlines():
        h = re.match(r"^###\s+(.+?)\s*$", line)
        if h:
            out.append((current_h3, "\n".join(current_lines).strip()))
            current_h3 = h.group(1).strip()
            current_lines = []
        else:
            current_lines.append(line)
    out.append((current_h3, "\n".join(current_lines).strip()))
    return out


def _is_exclusion_h3(heading: str | None) -> bool:
    if not heading:
        return False
    h = heading.lower()
    return any(pat in h for pat in _EXCLUSION_H3_PATTERNS)


def read_criteria() -> dict:
    """Parse criteria.md into a structured dict.

    Empty defaults if file missing — caller (cli.py scan) treats empty
    criteria as a signal to trigger the onboarding interview via Mizzix.

    Section semantics:
      `## Roles`
        H3 sub-sections are honored. Any H3 whose heading contains
        "exclusion"/"exclude"/"skip"/"avoid"/"filter out" splits its bullets
        into `title_exclusions` instead of `roles`. Bullets directly under
        ## Roles (no H3) are treated as roles.
      `## Locations`
        All bullets are candidate locations, but only those matching the
        `City, ST` pattern survive. Prose bullets (e.g. notes accidentally
        written under ## Locations) are silently dropped.
    """
    path = _criteria_path()
    empty = {
        "roles": [],
        "locations": [],
        "title_exclusions": [],
        "salary_floor": None,
        "hard_gates": [],
        "weights": {},
        "notes": "",
    }
    if not path.exists():
        return empty
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        logger.exception("read_criteria: failed to read %s", path)
        return empty

    sections = _split_sections(text)
    out = dict(empty)

    # Roles: split by H3, route exclusion sub-sections separately.
    roles: list[str] = []
    title_exclusions: list[str] = []
    for h3, sub_body in _split_h3_subsections(sections.get("roles", "")):
        bullets = _bullet_lines(sub_body)
        if _is_exclusion_h3(h3):
            title_exclusions.extend(bullets)
        else:
            roles.extend(bullets)
    out["roles"] = roles
    out["title_exclusions"] = title_exclusions

    # Locations: collect all bullets across H3 sub-sections, then keep only
    # those that look like "City, ST". Drops prose accidentally written here.
    location_bullets: list[str] = []
    for _h3, sub_body in _split_h3_subsections(sections.get("locations", "")):
        location_bullets.extend(_bullet_lines(sub_body))
    out["locations"] = [b for b in location_bullets if _CITY_RE.match(b)]

    salary_body = sections.get("salary floor", "").strip()
    if salary_body:
        try:
            out["salary_floor"] = int(re.sub(r"[^\d]", "", salary_body.split()[0]))
        except (ValueError, IndexError):
            out["salary_floor"] = None

    gates_body = sections.get("hard gates", "").strip()
    out["hard_gates"] = (
        [] if gates_body.lower() in ("(none)", "none", "") else _bullet_lines(gates_body)
    )

    weights_body = sections.get("weights", "")
    weights: dict[str, float] = {}
    for line in _bullet_lines(weights_body):
        m = re.match(r"^([\w_]+)\s*:\s*([\d.]+)\s*$", line)
        if m:
            try:
                weights[m.group(1)] = float(m.group(2))
            except ValueError:
                continue
    out["weights"] = weights

    out["notes"] = sections.get("notes", "")
    return out


def read_preferences() -> dict:
    """Parse preferences.md. Returns:
        {
          "learned_patterns": str (raw markdown body of ## Learned patterns),
          "recent_pass_reasons": [{"date": "YYYY-MM-DD", "text": str}, ...]
                                 (most recent first, capped at 30 by caller)
        }
    """
    path = _preferences_path()
    empty = {"learned_patterns": "", "recent_pass_reasons": []}
    if not path.exists():
        return empty
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        logger.exception("read_preferences: failed to read %s", path)
        return empty

    sections = _split_sections(text)
    learned = sections.get("learned patterns", "")
    raw_section = sections.get("pass reasons (raw)", "")
    reasons = [
        {"date": m.group(1), "text": m.group(2).strip()}
        for m in _PASS_REASON_RE.finditer(raw_section)
    ]
    reasons.sort(key=lambda r: r["date"], reverse=True)
    return {"learned_patterns": learned, "recent_pass_reasons": reasons}


_PREFERENCES_TEMPLATE = """\
# Preferences

*Auto-managed: pass-reasons appended by EOD check-in. Edit "Learned patterns"
section by hand or let the weekly retro distill recurring patterns.*

## Learned patterns

(none yet)

## Pass reasons (raw)

"""

_APPLICATIONS_TEMPLATE = """\
# Applications

*Auto-managed: applied jobs appended by EOD check-in. Most apps die without
response — those stay here. Active interview loops live in `../README.md`.*

| Date | Company | Title | Location | URL | Status |
|---|---|---|---|---|---|
"""


def _applications_path() -> Path:
    return _vault() / "projects" / "Job_Search" / "discovery" / "applications.md"


def append_pass_reason(date: str, company: str, location: str, reason: str) -> None:
    """Append a pass-reason entry to preferences.md, creating the file if
    needed. Never destructive — always appends to the existing
    "## Pass reasons (raw)" section.
    """
    path = _preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    location_str = f" ({location})" if location else ""
    line = f"- **{date}** — {company}{location_str} — {reason}\n"

    if not path.exists():
        path.write_text(_PREFERENCES_TEMPLATE + line, encoding="utf-8")
        return

    text = path.read_text(encoding="utf-8")
    if "## Pass reasons (raw)" not in text:
        # Section missing — append the section + entry at end of file
        sep = "" if text.endswith("\n") else "\n"
        path.write_text(text + sep + "\n## Pass reasons (raw)\n\n" + line, encoding="utf-8")
        return

    # Append to end of "## Pass reasons (raw)" section. Find where the next
    # section starts (or EOF) and insert there.
    section_start = text.index("## Pass reasons (raw)")
    rest = text[section_start:]
    next_h2 = re.search(r"\n##\s+\S", rest[len("## Pass reasons (raw)"):])
    if next_h2:
        insert_at = section_start + len("## Pass reasons (raw)") + next_h2.start()
        # Strip trailing whitespace from section before inserting
        new_text = text[:insert_at].rstrip() + "\n" + line + "\n" + text[insert_at:]
    else:
        # Section is the last one; append to EOF
        sep = "" if text.endswith("\n") else "\n"
        new_text = text + sep + line
    path.write_text(new_text, encoding="utf-8")


def append_application(
    date: str, company: str, title: str, location: str, url: str,
    status: str = "applied",
) -> None:
    """Append a row to applications.md, creating the file with header if needed."""
    path = _applications_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    row = f"| {date} | {company} | {title} | {location} | {url} | {status} |\n"
    if not path.exists():
        path.write_text(_APPLICATIONS_TEMPLATE + row, encoding="utf-8")
        return
    text = path.read_text(encoding="utf-8")
    sep = "" if text.endswith("\n") else "\n"
    path.write_text(text + sep + row, encoding="utf-8")
