"""State I/O for job_matches.json and job_matches_history.json.

Mirrors Mizzix's follow_ups.py patterns:
  - atomic write via .tmp + replace()
  - corruption-tolerant load (returns [], leaves file alone)
  - never destructive on parse failure (Tavin may hand-edit)

VAULT_PATH env var must point at the Second Brain vault root.
"""
import json
import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)


def _vault() -> Path:
    return Path(os.environ["VAULT_PATH"])


def _matches_path() -> Path:
    return _vault() / ".mizzix_state" / "job_matches.json"


def _history_path() -> Path:
    return _vault() / ".mizzix_state" / "job_matches_history.json"


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


import re

_PASS_REASON_RE = re.compile(
    r"^-\s*\*\*(\d{4}-\d{2}-\d{2})\*\*\s*[—–-]\s*(.+?)\s*$",
    re.MULTILINE,
)


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


def read_criteria() -> dict:
    """Parse criteria.md into a structured dict.

    Empty defaults if file missing — caller (cli.py scan) treats empty
    criteria as a signal to trigger the onboarding interview via Mizzix.
    """
    path = _criteria_path()
    empty = {
        "roles": [],
        "locations": [],
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
    out["roles"] = _bullet_lines(sections.get("roles", ""))
    out["locations"] = _bullet_lines(sections.get("locations", ""))

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
