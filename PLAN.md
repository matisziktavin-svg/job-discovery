# job-discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `job-discovery` system end-to-end per [DESIGN.md](DESIGN.md): a Python package + Mizzix bot integration + Windows cron that surfaces top-N daily job matches in Mizzix's morning brief and learns from Tavin's pass-reasons.

**Architecture:** Python package at `C:\Users\matis\Desktop\DevProjects\job-discovery\` (own repo), invoked by Mizzix via skill pointer at `vault/skills/job-discovery/SKILL.md`. State splits between `vault/.mizzix_state/` (machine JSON) and `vault/projects/Job_Search/discovery/` (human-readable markdown). Daily 3am cron task runs the pipeline; bot reads results into the morning brief and fires an EOD check-in slot.

**Tech Stack:** Python 3.11+, `python-jobspy`, `claude-agent-sdk`, `pydantic`, `pytest`. No Apify, no Bun, no Node. Auth inherits Tavin's Claude Max OAuth.

**Scope note:** This plan covers a single cohesive deliverable — discovery + scoring + surfacing + EOD loop. It is appropriately sized for one implementation cycle and should not be decomposed further.

---

## Pre-flight reading

Before starting Task 1, the implementer should read these existing files to internalize the patterns the new code will mirror:

- `C:\Users\matis\Desktop\DevProjects\Mizzix\follow_ups.py` — closest analog for state I/O (atomic writes, corruption tolerance, validation, dedupe)
- `C:\Users\matis\Desktop\DevProjects\Mizzix\reminders.py` — same patterns, simpler shape
- `C:\Users\matis\Desktop\DevProjects\Mizzix\morning_brief.py` — the integration point for surfacing matches
- `C:\Users\matis\Desktop\DevProjects\Mizzix\heartbeat.py` — the integration point for the EOD slot (note slot pattern in `_tick()`)
- `C:\Users\matis\Desktop\Second Brain\skills\gig-finder\SKILL.md` — the pointer-skill pattern this skill will mirror
- `C:\Users\matis\Desktop\DevProjects\job-discovery\DESIGN.md` — this plan's source spec

---

## Task 1: Repo skeleton + git init

**Files:**
- Create: `C:\Users\matis\Desktop\DevProjects\job-discovery\pyproject.toml`
- Create: `C:\Users\matis\Desktop\DevProjects\job-discovery\.gitignore`
- Create: `C:\Users\matis\Desktop\DevProjects\job-discovery\README.md`
- Create: `C:\Users\matis\Desktop\DevProjects\job-discovery\job_discovery\__init__.py`
- Create: `C:\Users\matis\Desktop\DevProjects\job-discovery\tests\__init__.py`
- Create: `C:\Users\matis\Desktop\DevProjects\job-discovery\tests\fixtures\.gitkeep`

- [ ] **Step 1: Create pyproject.toml**

```toml
[project]
name = "job-discovery"
version = "0.1.0"
description = "Daily job discovery + scoring for Mizzix"
requires-python = ">=3.11"
dependencies = [
    "python-jobspy>=1.1.79",
    "claude-agent-sdk>=0.0.21",
    "pydantic>=2.5",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.4",
    "pytest-asyncio>=0.23",
]

[project.scripts]
job-discovery = "job_discovery.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["job_discovery*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Create .gitignore**

```
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.venv/
build/
dist/
*.tmp
.env
```

- [ ] **Step 3: Create README.md**

```markdown
# job-discovery

Daily job-discovery system for [Mizzix](https://github.com/<owner>/Mizzix). Scores postings from JobSpy-supported boards against Tavin's living preferences and surfaces top matches in the morning brief.

See [DESIGN.md](DESIGN.md) for the full design.

## Install (dev)

    pip install -e ".[dev]"

## Run tests

    pytest

## Manual scan (no state mutation)

    python -m job_discovery.cli scan --dry-run
```

- [ ] **Step 4: Create empty package init files**

`job_discovery/__init__.py`:
```python
"""job-discovery: daily job matching for Mizzix."""
__version__ = "0.1.0"
```

`tests/__init__.py`: (empty file)

`tests/fixtures/.gitkeep`: (empty file)

- [ ] **Step 5: Initialize git, install package, commit**

Run from `C:\Users\matis\Desktop\DevProjects\job-discovery\`:

```powershell
git init
pip install -e ".[dev]"
```

Expected: install completes without errors. Verify with:

```powershell
python -c "import job_discovery; print(job_discovery.__version__)"
```

Expected output: `0.1.0`

```powershell
git add .
git commit -m "chore: initialize job-discovery repo skeleton"
```

---

## Task 2: state.py — machine state I/O (job_matches.json + history)

**Files:**
- Create: `job_discovery\state.py`
- Create: `tests\test_state.py`

This task implements the pure JSON read/write functions that mirror Mizzix's `follow_ups.py` patterns: atomic writes via `.tmp`, corruption-tolerant load, validation. No vault-markdown reading yet (Task 3).

- [ ] **Step 1: Write failing tests for `load_matches` / `save_matches` round-trip**

Create `tests/test_state.py`:

```python
import json
from pathlib import Path

import pytest

from job_discovery import state


def test_load_matches_returns_empty_list_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    assert state.load_matches() == []


def test_save_then_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    items = [
        {"id": "jm_abc12345", "title": "Mech Eng", "company": "Acme", "status": "surfaced"},
    ]
    state.save_matches(items)
    assert state.load_matches() == items


def test_load_matches_returns_empty_on_corrupt_json(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    path = tmp_path / ".mizzix_state" / "job_matches.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert state.load_matches() == []
    # File must be left alone — Tavin may hand-edit
    assert path.read_text(encoding="utf-8") == "{not valid json"


def test_save_matches_writes_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    state.save_matches([{"id": "jm_111"}])
    state.save_matches([{"id": "jm_222"}])
    # No leftover .tmp file
    assert not (tmp_path / ".mizzix_state" / "job_matches.json.tmp").exists()
    assert state.load_matches() == [{"id": "jm_222"}]


def test_history_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    state.save_history([{"id": "jm_999", "status": "passed"}])
    assert state.load_history() == [{"id": "jm_999", "status": "passed"}]


def test_new_match_id_format():
    mid = state.new_match_id()
    assert mid.startswith("jm_")
    assert len(mid) == 11  # "jm_" + 8 hex chars
```

- [ ] **Step 2: Run tests, verify they fail with import error**

```powershell
pytest tests/test_state.py -v
```

Expected: all tests FAIL with `ImportError: cannot import name 'state' from 'job_discovery'` or similar.

- [ ] **Step 3: Implement `state.py` minimally to pass these tests**

Create `job_discovery/state.py`:

```python
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
```

- [ ] **Step 4: Re-run tests, verify they all pass**

```powershell
pytest tests/test_state.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add job_discovery/state.py tests/test_state.py
git commit -m "feat(state): JSON I/O for job_matches and history with corruption tolerance"
```

---

## Task 3: state.py — vault file readers (criteria.md, preferences.md)

**Files:**
- Modify: `job_discovery\state.py` (add reader functions)
- Modify: `tests\test_state.py` (add reader tests)

`criteria.md` is parsed for structured filters (role types, locations, salary floor, hard gates, weights). `preferences.md` is read for the scoring agent's pass-reason context. Both are markdown with conventional sections; we use a dead-simple section parser (split by `## ` headings).

- [ ] **Step 1: Write failing tests for `read_criteria` and `read_preferences`**

Append to `tests/test_state.py`:

```python
CRITERIA_FIXTURE = """\
# Job Search Criteria

## Roles
- Mechanical Design Engineer
- Thermal Engineer
- Test Engineer

## Locations
- Chicago, IL
- Milwaukee, WI
- Seattle, WA
- Denver, CO

## Salary floor
70000

## Hard gates
(none)

## Weights
- role_fit: 1.5
- domain: 1.5
- skills_match: 1.0
- seniority: 1.0
- location: 1.0
- responsibilities: 1.0

## Notes
Open to AI engineering if founding-tools-shaped.
"""


def test_read_criteria_parses_sections(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    crit_path = tmp_path / "projects" / "Job_Search" / "discovery" / "criteria.md"
    crit_path.parent.mkdir(parents=True)
    crit_path.write_text(CRITERIA_FIXTURE, encoding="utf-8")

    crit = state.read_criteria()
    assert crit["roles"] == [
        "Mechanical Design Engineer", "Thermal Engineer", "Test Engineer",
    ]
    assert "Chicago, IL" in crit["locations"]
    assert crit["salary_floor"] == 70000
    assert crit["weights"]["role_fit"] == 1.5
    assert crit["weights"]["skills_match"] == 1.0
    assert crit["hard_gates"] == []
    assert "AI engineering" in crit["notes"]


def test_read_criteria_returns_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    crit = state.read_criteria()
    # Empty defaults — caller decides what to do (likely trigger onboarding)
    assert crit["roles"] == []
    assert crit["locations"] == []
    assert crit["salary_floor"] is None
    assert crit["weights"] == {}


def test_read_preferences_returns_recent_pass_reasons(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    pref_path = tmp_path / "projects" / "Job_Search" / "discovery" / "preferences.md"
    pref_path.parent.mkdir(parents=True)
    pref_path.write_text("""\
# Preferences

## Learned patterns
- Skip startups under 50 people

## Pass reasons (raw)
- **2026-05-10** — Anduril (Costa Mesa) — too defense-heavy, not interested
- **2026-05-09** — XYZ (Phoenix) — wanted senior+, I'm mid
- **2026-05-09** — ABC (Boston) — no relocation help
""", encoding="utf-8")

    prefs = state.read_preferences()
    assert "Skip startups under 50" in prefs["learned_patterns"]
    assert len(prefs["recent_pass_reasons"]) == 3
    assert prefs["recent_pass_reasons"][0]["date"] == "2026-05-10"
    assert "defense-heavy" in prefs["recent_pass_reasons"][0]["text"]


def test_read_preferences_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    prefs = state.read_preferences()
    assert prefs == {"learned_patterns": "", "recent_pass_reasons": []}
```

- [ ] **Step 2: Run tests, verify they fail**

```powershell
pytest tests/test_state.py::test_read_criteria_parses_sections -v
```

Expected: FAIL with `AttributeError: module 'job_discovery.state' has no attribute 'read_criteria'`.

- [ ] **Step 3: Add reader functions to `state.py`**

Append to `job_discovery/state.py`:

```python
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
```

- [ ] **Step 4: Re-run all state tests, verify all pass**

```powershell
pytest tests/test_state.py -v
```

Expected: all 10 tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add job_discovery/state.py tests/test_state.py
git commit -m "feat(state): vault readers for criteria.md and preferences.md"
```

---

## Task 4: state.py — vault writers (preferences.md, applications.md append)

**Files:**
- Modify: `job_discovery\state.py` (add writer functions)
- Modify: `tests\test_state.py` (add writer tests)

When the EOD check-in records a pass, we append to `preferences.md`. When it records an application, we append to `applications.md`. Both are append-only — never destructive.

- [ ] **Step 1: Write failing tests**

Append to `tests/test_state.py`:

```python
def test_append_pass_reason_creates_file_with_section(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    state.append_pass_reason(
        date="2026-05-12",
        company="Acme",
        location="Chicago, IL",
        reason="too senior, wants 8+ yrs",
    )
    pref_path = tmp_path / "projects" / "Job_Search" / "discovery" / "preferences.md"
    text = pref_path.read_text(encoding="utf-8")
    assert "## Pass reasons (raw)" in text
    assert "**2026-05-12** — Acme (Chicago, IL) — too senior" in text


def test_append_pass_reason_preserves_existing_content(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    pref_path = tmp_path / "projects" / "Job_Search" / "discovery" / "preferences.md"
    pref_path.parent.mkdir(parents=True)
    pref_path.write_text("""\
# Preferences

## Learned patterns
- Skip defense

## Pass reasons (raw)
- **2026-05-10** — Old (Boston) — too far
""", encoding="utf-8")

    state.append_pass_reason(
        date="2026-05-12", company="New", location="Denver", reason="weak fit",
    )
    text = pref_path.read_text(encoding="utf-8")
    assert "Skip defense" in text  # preserved
    assert "Old (Boston)" in text  # preserved
    assert "**2026-05-12** — New (Denver) — weak fit" in text  # appended


def test_append_application_creates_table(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    state.append_application(
        date="2026-05-12",
        company="Acme",
        title="Mech Design Eng",
        location="Chicago, IL",
        url="https://example.com/jobs/123",
    )
    app_path = tmp_path / "projects" / "Job_Search" / "discovery" / "applications.md"
    text = app_path.read_text(encoding="utf-8")
    assert "| Date | Company | Title | Location | URL | Status |" in text
    assert "| 2026-05-12 | Acme | Mech Design Eng | Chicago, IL |" in text
    assert "| applied |" in text
```

- [ ] **Step 2: Run tests, verify they fail**

```powershell
pytest tests/test_state.py::test_append_pass_reason_creates_file_with_section -v
```

Expected: FAIL with `AttributeError`.

- [ ] **Step 3: Implement writer functions**

Append to `job_discovery/state.py`:

```python
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


def _applications_path() -> Path:
    return _vault() / "projects" / "Job_Search" / "discovery" / "applications.md"
```

- [ ] **Step 4: Re-run all state tests, verify all pass**

```powershell
pytest tests/test_state.py -v
```

Expected: all 13 tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add job_discovery/state.py tests/test_state.py
git commit -m "feat(state): append-only writers for preferences.md and applications.md"
```

---

## Task 5: search.py — JobSpy wrapper

**Files:**
- Create: `job_discovery\search.py`
- Create: `tests\test_search.py`

`search.py` wraps `python-jobspy`'s `scrape_jobs()` to fetch from all 5 boards in parallel-ish (it handles concurrency internally), normalize the dataframe to a list of dicts with our schema, and dedupe via the `(company + title + location)` key. Per-board failures are isolated.

- [ ] **Step 1: Write failing tests for normalization + dedupe**

Create `tests/test_search.py`:

```python
from job_discovery import search


def test_normalize_listing_extracts_required_fields():
    raw = {
        "title": "Mechanical Design Engineer",
        "company": "Acme Corp",
        "location": "Chicago, IL",
        "job_url": "https://example.com/jobs/123",
        "min_amount": 75000,
        "max_amount": 95000,
        "date_posted": "2026-05-10",
        "site": "linkedin",
        "description": "Design things.",
    }
    out = search.normalize_listing(raw)
    assert out["title"] == "Mechanical Design Engineer"
    assert out["company"] == "Acme Corp"
    assert out["location"] == "Chicago, IL"
    assert out["url"] == "https://example.com/jobs/123"
    assert out["salary"] == "$75K-$95K"
    assert out["posted_date"] == "2026-05-10"
    assert out["source"] == "linkedin"
    assert out["description"] == "Design things."


def test_normalize_listing_handles_missing_salary():
    raw = {
        "title": "Eng", "company": "X", "location": "Y", "job_url": "z",
        "site": "indeed",
    }
    out = search.normalize_listing(raw)
    assert out["salary"] == ""


def test_dedupe_key_is_normalized_company_title_location():
    a = {"company": "Acme Corp", "title": "Mech Eng", "location": "Chicago, IL"}
    b = {"company": "  acme corp ", "title": "MECH ENG", "location": "chicago, il"}
    assert search.dedupe_key(a) == search.dedupe_key(b)


def test_dedupe_picks_highest_quality_source():
    listings = [
        {"company": "X", "title": "T", "location": "L", "source": "indeed", "url": "i"},
        {"company": "X", "title": "T", "location": "L", "source": "linkedin", "url": "ln"},
        {"company": "X", "title": "T", "location": "L", "source": "google", "url": "g"},
    ]
    out = search.dedupe(listings)
    assert len(out) == 1
    assert out[0]["url"] == "ln"  # LinkedIn wins
    assert out[0]["source"] == "linkedin"


def test_dedupe_against_seen_keys_filters_known_jobs():
    listings = [
        {"company": "X", "title": "T", "location": "L", "source": "linkedin", "url": "u1"},
        {"company": "Y", "title": "T", "location": "L", "source": "linkedin", "url": "u2"},
    ]
    seen = {search.dedupe_key({"company": "X", "title": "T", "location": "L"})}
    out = search.filter_unseen(listings, seen)
    assert len(out) == 1
    assert out[0]["company"] == "Y"
```

- [ ] **Step 2: Run tests, verify they fail**

```powershell
pytest tests/test_search.py -v
```

Expected: all 5 tests FAIL with `ImportError: cannot import name 'search'`.

- [ ] **Step 3: Implement `search.py`**

Create `job_discovery/search.py`:

```python
"""JobSpy wrapper: fetch from 5 boards, normalize, dedupe.

Per-board failures are isolated. The orchestrator (cli.scan) is responsible
for logging which boards succeeded.
"""
import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Higher-quality sources first — used by dedupe() to pick a winner when the
# same job appears on multiple boards.
SOURCE_QUALITY_ORDER = ["linkedin", "indeed", "glassdoor", "google", "zip_recruiter"]
ALL_BOARDS = list(SOURCE_QUALITY_ORDER)


def normalize_listing(raw: dict) -> dict:
    """Map a JobSpy row (or any board's raw output) to our match schema."""
    salary = ""
    mn = raw.get("min_amount")
    mx = raw.get("max_amount")
    if mn and mx:
        salary = f"${int(mn) // 1000}K-${int(mx) // 1000}K"
    elif mn:
        salary = f"${int(mn) // 1000}K+"

    posted = raw.get("date_posted")
    if posted is not None and not isinstance(posted, str):
        # JobSpy may return a datetime or pandas Timestamp
        try:
            posted = posted.strftime("%Y-%m-%d")
        except AttributeError:
            posted = str(posted)

    return {
        "title": (raw.get("title") or "").strip(),
        "company": (raw.get("company") or "").strip(),
        "location": (raw.get("location") or "").strip(),
        "url": raw.get("job_url") or raw.get("url") or "",
        "salary": salary,
        "posted_date": posted or "",
        "source": (raw.get("site") or "").lower(),
        "description": raw.get("description") or "",
    }


def dedupe_key(listing: dict) -> str:
    """Normalized key for deduping the same job across boards."""
    return "|".join([
        (listing.get("company") or "").strip().lower(),
        (listing.get("title") or "").strip().lower(),
        (listing.get("location") or "").strip().lower(),
    ])


def _source_rank(source: str) -> int:
    try:
        return SOURCE_QUALITY_ORDER.index(source.lower())
    except ValueError:
        return len(SOURCE_QUALITY_ORDER)  # unknown source ranks last


def dedupe(listings: list[dict]) -> list[dict]:
    """Collapse duplicates across boards. For each dedupe key, keep the
    listing from the highest-quality source.
    """
    by_key: dict[str, dict] = {}
    for it in listings:
        k = dedupe_key(it)
        existing = by_key.get(k)
        if existing is None or _source_rank(it["source"]) < _source_rank(existing["source"]):
            by_key[k] = it
    return list(by_key.values())


def filter_unseen(listings: list[dict], seen_keys: Iterable[str]) -> list[dict]:
    """Drop listings whose dedupe_key is in `seen_keys`."""
    seen = set(seen_keys)
    return [it for it in listings if dedupe_key(it) not in seen]


def fetch_all(criteria: dict, results_per_board: int = 50) -> tuple[list[dict], dict[str, str]]:
    """Run JobSpy against each board with criteria-derived params. Returns:
        (listings, board_status)
    where board_status maps board_name -> "ok" or error message.

    Per-board errors are caught and logged — the call always returns whatever
    succeeded plus the status map. Caller logs partial-success in the brief.
    """
    from jobspy import scrape_jobs  # local import — heavy module

    search_terms = " OR ".join(f'"{r}"' for r in criteria.get("roles", []) if r)
    locations = criteria.get("locations", []) or [""]

    out: list[dict] = []
    status: dict[str, str] = {}
    for board in ALL_BOARDS:
        try:
            df = scrape_jobs(
                site_name=[board],
                search_term=search_terms or None,
                location=locations[0],  # JobSpy takes one location per call
                results_wanted=results_per_board,
                hours_old=72,  # only postings from the last 3 days
                country_indeed="USA",
            )
            if df is None or df.empty:
                status[board] = "ok (0 results)"
                continue
            for _, row in df.iterrows():
                out.append(normalize_listing(row.to_dict()))
            status[board] = "ok"
        except Exception as e:
            logger.exception("search.fetch_all: %s failed", board)
            status[board] = f"error: {type(e).__name__}: {e}"

    return dedupe(out), status
```

- [ ] **Step 4: Re-run search tests**

```powershell
pytest tests/test_search.py -v
```

Expected: all 5 tests PASS. (`fetch_all` is not unit-tested — it's a thin wrapper over JobSpy that we'll smoke-test manually in Task 13.)

- [ ] **Step 5: Commit**

```powershell
git add job_discovery/search.py tests/test_search.py
git commit -m "feat(search): JobSpy wrapper with per-board isolation and source-quality dedupe"
```

---

## Task 6: score.py — rule-based fallback scorer

**Files:**
- Create: `job_discovery\score.py`
- Create: `tests\test_score.py`

The fallback runs first because it has no external dependencies and lets us TDD without API calls. The LLM scorer (Task 7) becomes the primary path; this is what we fall back to when the LLM call fails.

- [ ] **Step 1: Write failing tests for `score_rule_based`**

Create `tests/test_score.py`:

```python
from job_discovery import score


CRITERIA_AERO = {
    "roles": ["Mechanical Design Engineer", "Thermal Engineer"],
    "locations": ["Chicago, IL", "Milwaukee, WI", "Denver, CO"],
    "salary_floor": 70000,
    "weights": {
        "role_fit": 1.5, "domain": 1.5, "skills_match": 1.0,
        "seniority": 1.0, "location": 1.0, "responsibilities": 1.0,
    },
    "hard_gates": [],
}


def test_rule_score_strong_match_scores_high():
    listing = {
        "title": "Mechanical Design Engineer",
        "company": "Boeing",
        "location": "Chicago, IL",
        "salary": "$80K-$100K",
        "description": "Hands-on mechanical design for aerospace propulsion.",
    }
    result = score.score_rule_based(listing, CRITERIA_AERO)
    assert result["overall"] >= 4.0
    assert result["dims"]["role_fit"] >= 4
    assert result["dims"]["location"] == 5
    assert result["method"] == "fallback"


def test_rule_score_weak_match_scores_low():
    listing = {
        "title": "Sales Engineer",
        "company": "Random",
        "location": "Phoenix, AZ",
        "salary": "$50K",
        "description": "Sell software.",
    }
    result = score.score_rule_based(listing, CRITERIA_AERO)
    assert result["overall"] <= 2.5
    assert result["dims"]["role_fit"] <= 2


def test_rule_score_la_location_is_one_not_zero():
    listing = {
        "title": "Mech Eng",
        "company": "X",
        "location": "Costa Mesa, CA",
        "description": "Mech design",
    }
    result = score.score_rule_based(listing, CRITERIA_AERO)
    # LA-area scores 1 on location (downweighted, not gated)
    assert result["dims"]["location"] == 1


def test_rule_score_one_line_take_includes_signals():
    listing = {
        "title": "Mech Eng",
        "company": "Boeing",
        "location": "Chicago, IL",
        "description": "Aerospace design.",
    }
    result = score.score_rule_based(listing, CRITERIA_AERO)
    assert result["one_line_take"]
    assert len(result["one_line_take"]) <= 200
```

- [ ] **Step 2: Run tests, verify they fail**

```powershell
pytest tests/test_score.py -v
```

Expected: all 4 tests FAIL with `ImportError`.

- [ ] **Step 3: Implement `score.py` rule-based scorer**

Create `job_discovery/score.py`:

```python
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


def _kw_hit(text: str, kws: set[str]) -> bool:
    t = text.lower()
    return any(k in t for k in kws)


def _score_role_fit(listing: dict, criteria: dict) -> int:
    title = (listing.get("title") or "").lower()
    if not title:
        return 1
    target_roles = [r.lower() for r in criteria.get("roles", [])]
    # Direct contains-match against target role names
    for r in target_roles:
        if r in title or any(word in title for word in r.split() if len(word) > 4):
            return 5
    # Engineering-adjacent but not target
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
```

- [ ] **Step 4: Re-run score tests**

```powershell
pytest tests/test_score.py -v
```

Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add job_discovery/score.py tests/test_score.py
git commit -m "feat(score): rule-based fallback scorer with 6-dimension rubric"
```

---

## Task 7: score.py — LLM-based primary scorer

**Files:**
- Modify: `job_discovery\score.py` (add `score_llm` and prompt assembly)
- Create: `job_discovery\prompts\scoring_system.txt`
- Modify: `tests\test_score.py` (add prompt-assembly tests; LLM call is integration-only)

The LLM scorer mirrors Mizzix's pattern from `morning_brief.py` and `heartbeat.py`: pop `ANTHROPIC_API_KEY`, write the system prompt to a file, use `ClaudeSDKClient` with `bypassPermissions` and `allowed_tools=[]` (no tool use; pure JSON output).

- [ ] **Step 1: Write the scoring system prompt**

Create `job_discovery/prompts/scoring_system.txt`:

```
You are a job-fit scoring agent for Tavin (early-mid-career mechanical/thermal engineer with aerospace background, currently job-hunting). Score one job listing on 6 dimensions, output JSON only.

DIMENSIONS — each scored 1-5 integer:

1. role_fit — How well the title + scope match Tavin's wants.
   Tavin wants: thermal/mechanical design engineering (5), systems/test engineering (4), AI engineering with founding/tools shape (4). Coordination-heavy / PM-ish (1-2).
2. skills_match — Overlap of JD requirements with Tavin's actual experience (read his profile in the input).
3. seniority — Calibrated for early-mid IC. "Sr." with 5+ yrs required = 2-3. "Principal/Staff" = 1. New grad = 4. Mid IC = 4-5.
4. domain — Aerospace = 5. Industrial/energy/HRSG = 4. Generic mfg = 3. Defense = follow Tavin's stated preference in criteria/preferences.
5. location — Tavin's target cities = 5. Other medium-large city = 3. Small city = 2. LA-area = 1 (downweighted, NOT gated). Tavin's exact target list is in the input.
6. responsibilities — Hands-on design/build = 5. Mixed = 3. Pure coordination = 1.

CRITICAL — read Tavin's `preferences.recent_pass_reasons` and `preferences.learned_patterns` in the input. They reflect his evolving taste. If a listing matches a recent rejection pattern (e.g. "too senior", "defense-heavy", "actually 90 min outside Denver"), score it accordingly.

OUTPUT — single JSON object, no markdown fence, no prose:

{
  "dims": {
    "role_fit": <int 1-5>,
    "skills_match": <int 1-5>,
    "seniority": <int 1-5>,
    "domain": <int 1-5>,
    "location": <int 1-5>,
    "responsibilities": <int 1-5>
  },
  "one_line_take": "<one sentence ≤200 chars summarizing fit. If the location, domain, or pass-reason patterns warrant a flag, include it (e.g. 'LA — flagging', 'small-city — flag', 'matches earlier pass on defense').>"
}

The orchestrator computes `overall` by weighted average — do NOT include it.
```

- [ ] **Step 2: Write failing tests for prompt assembly + JSON parsing**

Append to `tests/test_score.py`:

```python
def test_assemble_scoring_user_prompt_includes_listing_and_context():
    listing = {
        "title": "Mech Eng", "company": "Acme", "location": "Chicago",
        "description": "Design things.", "salary": "$80K",
    }
    criteria = {"roles": ["Mech Eng"], "locations": ["Chicago, IL"], "weights": {}}
    preferences = {
        "learned_patterns": "Skip defense",
        "recent_pass_reasons": [{"date": "2026-05-10", "text": "too senior"}],
    }
    profile_blob = "Tavin: aerospace eng, mid-IC."
    prompt = score._assemble_user_prompt(listing, criteria, preferences, profile_blob)
    assert "Mech Eng" in prompt
    assert "Acme" in prompt
    assert "Skip defense" in prompt
    assert "too senior" in prompt
    assert "aerospace eng" in prompt


def test_parse_score_response_valid_json():
    raw = '{"dims": {"role_fit": 5, "skills_match": 4, "seniority": 4, "domain": 5, "location": 5, "responsibilities": 5}, "one_line_take": "great fit"}'
    weights = {"role_fit": 1.5, "domain": 1.5, "skills_match": 1.0,
               "seniority": 1.0, "location": 1.0, "responsibilities": 1.0}
    result = score._parse_score_response(raw, weights)
    assert result is not None
    assert result["dims"]["role_fit"] == 5
    assert result["overall"] >= 4.5
    assert result["one_line_take"] == "great fit"
    assert result["method"] == "llm"


def test_parse_score_response_handles_markdown_fence():
    raw = "```json\n{\"dims\": {\"role_fit\": 3, \"skills_match\": 3, \"seniority\": 3, \"domain\": 3, \"location\": 3, \"responsibilities\": 3}, \"one_line_take\": \"meh\"}\n```"
    weights = {}  # equal weights
    result = score._parse_score_response(raw, weights)
    assert result is not None
    assert result["overall"] == 3.0


def test_parse_score_response_returns_none_on_garbage():
    assert score._parse_score_response("not json at all", {}) is None
    assert score._parse_score_response("", {}) is None
```

- [ ] **Step 3: Run tests, verify they fail**

```powershell
pytest tests/test_score.py -v -k "assemble or parse"
```

Expected: 4 tests FAIL with `AttributeError`.

- [ ] **Step 4: Add prompt assembly + parsing + LLM caller to `score.py`**

Append to `job_discovery/score.py`:

```python
import asyncio
import json
import os
import re
from pathlib import Path

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
```

- [ ] **Step 5: Re-run all score tests, verify all pass**

```powershell
pytest tests/test_score.py -v
```

Expected: all 8 tests PASS. (`score_llm` itself is not unit-tested — exercised in the smoke test, Task 13.)

- [ ] **Step 6: Commit**

```powershell
git add job_discovery/score.py job_discovery/prompts/scoring_system.txt tests/test_score.py
git commit -m "feat(score): LLM-based scoring agent with rule-based fallback"
```

---

## Task 8: cli.py — `scan` command (the daily pipeline)

**Files:**
- Create: `job_discovery\cli.py`
- Create: `tests\test_cli.py`

`scan` is the heart of the system: read criteria → fetch from boards → dedupe vs. seen → score → roll forward un-actioned matches → write top-N to `job_matches.json`. Cron calls this; Mizzix can also invoke ad-hoc.

- [ ] **Step 1: Write failing tests for the orchestration logic**

Create `tests/test_cli.py`:

```python
from unittest.mock import patch

import pytest

from job_discovery import cli, state, search


CRITERIA = {
    "roles": ["Mech Eng"],
    "locations": ["Chicago, IL"],
    "salary_floor": 70000,
    "weights": {"role_fit": 1.5, "domain": 1.5, "skills_match": 1.0,
                "seniority": 1.0, "location": 1.0, "responsibilities": 1.0},
    "hard_gates": [],
    "notes": "",
}


def _mk_listing(title, company, location="Chicago, IL", **extra):
    return {
        "title": title, "company": company, "location": location,
        "url": f"https://example.com/{company}", "salary": "$80K-$100K",
        "posted_date": "2026-05-12", "source": "linkedin",
        "description": "Mechanical design of stuff.", **extra,
    }


def test_apply_hard_gates_filters_listed_companies():
    listings = [
        _mk_listing("Mech Eng", "Allowed"),
        _mk_listing("Mech Eng", "BadCo"),
    ]
    crit = {**CRITERIA, "hard_gates": ["company:BadCo"]}
    out = cli._apply_hard_gates(listings, crit)
    assert len(out) == 1
    assert out[0]["company"] == "Allowed"


def test_apply_hard_gates_empty_list_passes_everything():
    listings = [_mk_listing("Mech Eng", "Acme"), _mk_listing("Mech Eng", "Beta")]
    out = cli._apply_hard_gates(listings, {**CRITERIA, "hard_gates": []})
    assert len(out) == 2


def test_select_top_n_respects_threshold_and_cap():
    scored = [
        {"id": "a", "score": {"overall": 4.2}},
        {"id": "b", "score": {"overall": 3.5}},
        {"id": "c", "score": {"overall": 2.8}},  # below 3.0 — drop
        {"id": "d", "score": {"overall": 4.0}},
        {"id": "e", "score": {"overall": 3.1}},
        {"id": "f", "score": {"overall": 3.9}},
        {"id": "g", "score": {"overall": 2.5}},  # below 3.0 — drop
    ]
    top = cli._select_top_n(scored, n=5, threshold=3.0)
    assert [m["id"] for m in top] == ["a", "d", "f", "b", "e"]
    assert all(m["score"]["overall"] >= 3.0 for m in top)


def test_merge_with_carryforward_preserves_unactioned_old_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    existing = [
        {"id": "old1", "title": "Old", "company": "OldCo", "status": "surfaced",
         "times_carried": 2, "score": {"overall": 4.0}},
    ]
    state.save_matches(existing)

    new_scored = [
        {"id": "new1", "title": "Fresh", "company": "NewCo", "status": "surfaced",
         "times_carried": 0, "score": {"overall": 4.5}},
    ]
    merged = cli._merge_with_carryforward(new_scored, today_iso="2026-05-12")
    ids = [m["id"] for m in merged]
    assert "old1" in ids
    assert "new1" in ids
    # times_carried for old1 was incremented
    old = next(m for m in merged if m["id"] == "old1")
    assert old["times_carried"] == 3
```

- [ ] **Step 2: Run tests, verify they fail**

```powershell
pytest tests/test_cli.py -v
```

Expected: all 4 tests FAIL with `ImportError`.

- [ ] **Step 3: Implement `cli.py` with `scan` command + helpers**

Create `job_discovery/cli.py`:

```python
"""CLI entrypoints for job-discovery.

Commands:
  scan                                  — run the daily pipeline (cron + ad-hoc)
  score-one <url>                       — score a single posting
  list-active                           — print current job_matches.json
  record-action <id> <action> [opts]    — update one match's status
"""
import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Iterable

from job_discovery import score, search, state

logger = logging.getLogger(__name__)


def _today_iso() -> str:
    return dt.date.today().isoformat()


def _load_profile_blob() -> str:
    """Read the parts of tavin.md + Job_Search/README.md that scoring needs."""
    vault = Path(os.environ["VAULT_PATH"])
    parts = []
    for rel in ("tavin.md", "projects/Job_Search/README.md"):
        p = vault / rel
        if p.exists():
            try:
                parts.append(f"=== {rel} ===\n{p.read_text(encoding='utf-8')}")
            except Exception:
                logger.exception("could not read %s", p)
    return "\n\n".join(parts)


def _apply_hard_gates(listings: list[dict], criteria: dict) -> list[dict]:
    """Drop any listing matching a hard gate. Currently supported:
        company:<name>     — exact company match (case-insensitive)
    """
    gates = criteria.get("hard_gates") or []
    if not gates:
        return list(listings)
    blocked_companies = {
        g.split(":", 1)[1].strip().lower()
        for g in gates
        if g.lower().startswith("company:")
    }
    return [
        l for l in listings
        if (l.get("company") or "").strip().lower() not in blocked_companies
    ]


def _select_top_n(scored: list[dict], n: int = 5, threshold: float = 3.0) -> list[dict]:
    """Sort by overall score descending, drop anything below threshold,
    cap at N. Ties broken by posted_date desc, then id asc for stability."""
    qualified = [m for m in scored if m["score"]["overall"] >= threshold]
    qualified.sort(key=lambda m: (
        -m["score"]["overall"],
        -(int(m.get("posted_date", "0").replace("-", "") or 0)),
        m.get("id", ""),
    ))
    return qualified[:n]


def _merge_with_carryforward(new_matches: list[dict], today_iso: str) -> list[dict]:
    """Merge freshly scored matches into the existing job_matches.json,
    incrementing times_carried for items already present."""
    existing = state.load_matches()
    new_ids = {m["id"] for m in new_matches}
    out: list[dict] = []
    for old in existing:
        if old.get("status") != "surfaced":
            continue
        if old["id"] in new_ids:
            # New scoring overrides — drop the old version (the new one will
            # replace it via the loop below)
            continue
        old["times_carried"] = (old.get("times_carried") or 0) + 1
        old["last_brief_date"] = today_iso
        out.append(old)
    for m in new_matches:
        m["last_brief_date"] = today_iso
        out.append(m)
    return out


def cmd_scan(args: argparse.Namespace) -> int:
    criteria = state.read_criteria()
    if not criteria["roles"]:
        print(
            "criteria.md is empty or missing — run the onboarding interview "
            "via Mizzix (skill: job-discovery, command: onboard).",
            file=sys.stderr,
        )
        return 1

    preferences = state.read_preferences()
    profile_blob = _load_profile_blob()

    today = _today_iso()
    logger.info("scan: starting (criteria roles=%d, locations=%d)",
                len(criteria["roles"]), len(criteria["locations"]))

    raw, board_status = search.fetch_all(criteria)
    logger.info("scan: fetched %d listings; board_status=%s", len(raw), board_status)
    if all(s.startswith("error") for s in board_status.values()):
        # All boards failed — preserve existing state, log error only
        logger.error("scan: all boards failed, leaving job_matches.json untouched")
        return 2

    gated = _apply_hard_gates(raw, criteria)

    # Dedupe against currently surfaced + history
    surfaced_keys = {search.dedupe_key(m) for m in state.load_matches()}
    history_keys = {search.dedupe_key(m) for m in state.load_history()}
    fresh = search.filter_unseen(gated, surfaced_keys | history_keys)
    logger.info("scan: %d fresh after dedupe vs surfaced+history", len(fresh))

    scored: list[dict] = []
    for listing in fresh:
        if args.dry_run:
            print(f"[dry-run] would score: {listing['company']} — {listing['title']}")
            continue
        result = score.score_listing(listing, criteria, preferences, profile_blob)
        match = {
            "id": state.new_match_id(),
            "source": listing["source"],
            "title": listing["title"],
            "company": listing["company"],
            "location": listing["location"],
            "salary": listing["salary"],
            "url": listing["url"],
            "posted_date": listing["posted_date"],
            "surfaced_date": today,
            "score": {
                "overall": result["overall"],
                "dims": result["dims"],
                "method": result["method"],
            },
            "one_line_take": result["one_line_take"],
            "status": "surfaced",
            "times_carried": 0,
        }
        scored.append(match)

    if args.dry_run:
        print(f"[dry-run] would have scored {len(fresh)} listings")
        return 0

    top = _select_top_n(scored, n=args.top_n, threshold=args.threshold)
    merged = _merge_with_carryforward(top, today)
    state.save_matches(merged)

    print(json.dumps({
        "fresh_scored": len(scored),
        "top_n_surfaced": len(top),
        "total_active": len(merged),
        "board_status": board_status,
    }, indent=2))
    return 0


def cmd_list_active(args: argparse.Namespace) -> int:
    items = state.load_matches()
    items.sort(key=lambda m: (
        -m.get("score", {}).get("overall", 0.0),
        m.get("id", ""),
    ))
    if not items:
        print("(no active matches)")
        return 0
    for i, m in enumerate(items, 1):
        score_v = m.get("score", {}).get("overall", "?")
        carried = m.get("times_carried") or 0
        carried_str = f" (carried {carried}d)" if carried > 0 else ""
        print(
            f"{i}. [{m['id']}] {m['company']} — {m['title']} · "
            f"{m['location']} · {m.get('salary', '')} · score {score_v}{carried_str}"
        )
        take = m.get("one_line_take", "")
        if take:
            print(f"   {take}")
    return 0


def cmd_score_one(args: argparse.Namespace) -> int:
    # For one-off scoring, fetch the JD via JobSpy's URL-based mode if available;
    # otherwise the user pastes it. v0.1 supports URL-with-pasted-text only.
    if not args.description:
        print("score-one requires --description (paste the JD text).", file=sys.stderr)
        return 1
    listing = {
        "title": args.title or "(unknown title)",
        "company": args.company or "(unknown company)",
        "location": args.location or "",
        "url": args.url,
        "salary": "",
        "posted_date": _today_iso(),
        "source": "manual",
        "description": args.description,
    }
    criteria = state.read_criteria()
    preferences = state.read_preferences()
    profile_blob = _load_profile_blob()
    result = score.score_listing(listing, criteria, preferences, profile_blob)
    print(json.dumps(result, indent=2))
    return 0


def cmd_record_action(args: argparse.Namespace) -> int:
    items = state.load_matches()
    target = next((m for m in items if m["id"] == args.match_id), None)
    if target is None:
        print(f"no active match with id {args.match_id!r}", file=sys.stderr)
        return 1

    today = _today_iso()
    action = args.action.lower()

    if action == "applied":
        target["status"] = "applied"
        target["action_date"] = today
        state.append_application(
            date=today, company=target["company"], title=target["title"],
            location=target.get("location", ""), url=target.get("url", ""),
        )
        history = state.load_history() + [target]
        state.save_history(history)
        items = [m for m in items if m["id"] != args.match_id]
        state.save_matches(items)
        print(f"recorded applied: {target['company']} — {target['title']}")
        return 0

    if action == "pass":
        if not args.reason:
            print("pass requires --reason TEXT", file=sys.stderr)
            return 1
        target["status"] = "passed"
        target["action_date"] = today
        target["pass_reason"] = args.reason
        state.append_pass_reason(
            date=today, company=target["company"],
            location=target.get("location", ""), reason=args.reason,
        )
        history = state.load_history() + [target]
        state.save_history(history)
        items = [m for m in items if m["id"] != args.match_id]
        state.save_matches(items)
        print(f"recorded pass: {target['company']} — {args.reason}")
        return 0

    if action == "tomorrow":
        # No state mutation needed — natural carry-forward at next scan.
        print(f"keeping {target['company']} for tomorrow")
        return 0

    if action == "decoded":
        target["decoded"] = True
        state.save_matches(items)
        print(f"flagged {target['company']} as decoded")
        return 0

    print(f"unknown action: {action}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="job-discovery")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="run the daily pipeline")
    p_scan.add_argument("--dry-run", action="store_true",
                        help="fetch + dedupe but skip scoring + state writes")
    p_scan.add_argument("--top-n", type=int, default=5)
    p_scan.add_argument("--threshold", type=float, default=3.0)
    p_scan.set_defaults(func=cmd_scan)

    p_list = sub.add_parser("list-active", help="print job_matches.json formatted")
    p_list.set_defaults(func=cmd_list_active)

    p_score = sub.add_parser("score-one", help="score a single posting")
    p_score.add_argument("url")
    p_score.add_argument("--title", default="")
    p_score.add_argument("--company", default="")
    p_score.add_argument("--location", default="")
    p_score.add_argument("--description", required=True)
    p_score.set_defaults(func=cmd_score_one)

    p_rec = sub.add_parser("record-action", help="update a match's status")
    p_rec.add_argument("match_id")
    p_rec.add_argument("action", choices=["applied", "pass", "tomorrow", "decoded"])
    p_rec.add_argument("--reason", default="", help="required for action=pass")
    p_rec.set_defaults(func=cmd_record_action)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Re-run cli tests**

```powershell
pytest tests/test_cli.py -v
```

Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add job_discovery/cli.py tests/test_cli.py
git commit -m "feat(cli): scan + list-active + score-one + record-action commands"
```

---

## Task 9: Vault skill pointer — `SKILL.md`

**Files:**
- Create: `C:\Users\matis\Desktop\Second Brain\skills\job-discovery\SKILL.md`

This is what makes Mizzix invoke the package. The frontmatter is parsed by Mizzix's skills_loader; the body is loaded on demand when Mizzix decides the skill applies.

- [ ] **Step 1: Read the gig-finder SKILL.md to mirror its shape**

```powershell
type "C:\Users\matis\Desktop\Second Brain\skills\gig-finder\SKILL.md"
```

Note: the description should be a single line that surfaces all the trigger phrases Mizzix's loader uses to decide when to invoke.

- [ ] **Step 2: Write the SKILL.md**

Create `C:\Users\matis\Desktop\Second Brain\skills\job-discovery\SKILL.md`:

```markdown
---
name: job-discovery
description: Daily job discovery and scoring for Tavin's job search. Triggers on "find me jobs", "refresh job search", "what's in my job queue", "look at #N from this morning", "I'm passing on the X one because…", "should I apply to #N", "score this posting", "let's redo my job criteria", "drop Denver", "add Houston". Also drives the 7pm EOD check-in for un-actioned matches.
---

# job-discovery

Daily job discovery system. Surfaces top matches in the morning brief, learns from pass-reasons via the EOD check-in, hands off to interview-coach when Tavin wants to act on a match.

**Repo:** `C:\Users\matis\Desktop\DevProjects\job-discovery\`
**Vault state:** `vault/.mizzix_state/job_matches.json`, `vault/.mizzix_state/job_matches_history.json`
**Vault assets:** `vault/projects/Job_Search/discovery/criteria.md`, `preferences.md`, `applications.md`

## CLI commands

All run as `python -m job_discovery.cli <command>` from any directory. Set `VAULT_PATH=C:\Users\matis\Desktop\Second Brain` if not already.

| Command | When to use |
|---|---|
| `scan` | Tavin says "refresh job search," "look again now." Also fires daily at 3am via `MizzixJobDiscovery` task. |
| `scan --dry-run` | Smoke test fetch+dedupe without writing state or burning LLM tokens. |
| `list-active` | Tavin says "what's in my job queue," "show me pending matches." |
| `score-one <url> --description "<JD text>"` | Tavin pastes a posting and asks "what do you think of this." |
| `record-action <id> <action> [--reason TEXT]` | Called by you per parsed EOD reply item. Actions: `applied`, `pass` (requires `--reason`), `tomorrow`, `decoded`. |

## Skill behaviors (you drive these — no CLI command)

### Onboarding interview (first invocation, or "let's redo my job criteria")

When `criteria.md` is missing or empty, run a conversational interview to fill it. **One question at a time.** When `tavin.md` or `projects/Job_Search/README.md` already answers a topic, frame the question as a confirmation: *"You've said X — still true? Anything to add?"* — not a cold ask. Skip entirely if the file is unambiguous and recent.

Topics to cover (in this rough order, but adapt to what's already known):

1. Defense contractors — yes / no / depends on the work
2. Public sector — NASA, national labs, federal agencies in scope?
3. Company stage — early startup ok? IPO-stage? Big established?
4. Travel willingness — % cap?
5. Specific exclusions — companies, industries, cultures Tavin won't consider
6. Visa / clearance — confirm citizenship, willingness for clearance roles
7. Compensation — confirm $70K floor, equity preference, target range
8. Title aliases — JD titles that should always trigger (e.g. "Mechanical Design Engineer", "Thermal Engineer", "ME I/II/III")
9. Title exclusions — titles to always skip (e.g. "Manager", "Sales Engineer")
10. Geography refinement — confirm Chicago/Milwaukee/Seattle/Denver, ask about cities not yet named (Boston? Austin? Phoenix? Houston?)

When done, write `vault/projects/Job_Search/discovery/criteria.md` with this structure (use the `Write` tool):

```markdown
# Job Search Criteria

*Last updated: <YYYY-MM-DD>. Generated by job-discovery onboarding interview.*

## Roles
- <title alias>
- <...>

## Locations
- <City, ST>
- <...>

## Salary floor
70000

## Hard gates
(none)

## Weights
- role_fit: 1.5
- domain: 1.5
- skills_match: 1.0
- seniority: 1.0
- location: 1.0
- responsibilities: 1.0

## Notes
<free-text Tavin context — defense stance, company stage, travel cap, etc.>
```

### Update criteria ("drop Denver", "add Houston", "raise salary floor to 80")

Edit `criteria.md` in place via the `Edit` tool. Don't re-run the full onboarding — just the targeted change. Confirm in one short line.

### EOD check-in reply parsing

When Tavin replies to a 7pm EOD check-in DM (one that lists his open matches with `applied / pass [reason] / tomorrow / decode` instructions), parse the reply and call `record-action` once per item.

Examples that should all parse:

- *"1 applied, 2 tomorrow, 3 pass too defense-heavy, 4 pass location is actually 90min from Denver, 5 decode"*
- *"applied to 1, passing on 3 and 4, the rest tomorrow"*
- *"all pass except 2"* — for each pass, ask Tavin once for a reason ("Quick why on the [Company] pass?"). One clarifying turn, not five.

For each parsed item, call:

```bash
python -m job_discovery.cli record-action <match_id> applied
python -m job_discovery.cli record-action <match_id> pass --reason "too defense-heavy"
python -m job_discovery.cli record-action <match_id> tomorrow
python -m job_discovery.cli record-action <match_id> decoded   # then invoke interview-coach decode
```

Match IDs are in the EOD DM (or use `list-active` to refresh). Keep confirmations terse: *"Logged 1 apply, 2 passes, 1 tomorrow, 1 decode."*

If a reply is genuinely ambiguous (numbers don't map to listed matches, conflicting actions), ask one specific clarifying question per the ambiguous-message rules in `CLAUDE.md`.

## Handoff to interview-coach

When Tavin says any of:

- "should I apply to #N"
- "tell me more about [company from the queue]"
- "decode #N"
- "prep me for [company]"

…invoke the `interview-coach` skill with the JD URL/text from `list-active`. From there interview-coach owns the conversation per its existing multi-step intent rules (`decode → prep → resume`). The match stays in the queue with `decoded` flag until Tavin actions it through the EOD.

## Failure modes Tavin might ask about

- *"Why isn't my morning brief showing job matches?"* → Check `vault/.mizzix_state/job_discovery.log` for last cron run. Also `list-active` — empty means no recent matches cleared the threshold.
- *"Why didn't I get an EOD check-in?"* → It only fires if `list-active` has surfaced items. Silent if the queue is empty.
- *"Show me what you're tracking."* → Run `list-active`.
```

- [ ] **Step 3: Verify Mizzix loads the skill**

After writing the file, restart Mizzix (per `feedback_restart_via_task` memory — use the Task Scheduler restart). Then in a Discord chat:

> Tavin: "What skills do you have available?"

Mizzix should mention `job-discovery` in the available list. If not, check the skill loader log for frontmatter parse warnings.

- [ ] **Step 4: Commit (vault file is in vault git, but the SKILL is referenced by repo)**

The vault has its own git history; the new SKILL.md will be picked up by the next vault commit. No commit needed in the job-discovery repo for this step. (Optionally commit a copy-for-reference to the repo if desired — not required.)

---

## Task 10: Mizzix bot integration — morning brief renderer

**Files:**
- Modify: `C:\Users\matis\Desktop\DevProjects\Mizzix\morning_brief.py`

The morning brief is generated by an LLM in `morning_brief.py`. To make it render the Job matches section, we add a `job_matches` field to the `snapshot` dict it ships to the model, and add rendering instructions to `_SYSTEM_PROMPT_HEAD`.

This is a "bot-affecting edit" — the first such edit in this implementation session triggers a Mizzix dashboard version bump per `feedback_mizzix_version_bump` memory.

- [ ] **Step 1: Read the current `morning_brief.py` to confirm exact insertion points**

```powershell
type "C:\Users\matis\Desktop\DevProjects\Mizzix\morning_brief.py"
```

Specifically locate:
- The `snapshot = {...}` dict construction (~line 217)
- The `_SYSTEM_PROMPT_HEAD` string (~line 101)

- [ ] **Step 2: Add a snapshot reader for job_matches.json**

In `morning_brief.py`, near the other small helper functions (after `_read_recent_lessons`), add:

```python
def _read_job_matches(vault: Path) -> list[dict]:
    """Read currently surfaced job matches from job_matches.json. Returns
    items sorted by overall score desc. Empty list on missing/corrupt.
    """
    path = vault / ".mizzix_state" / "job_matches.json"
    if not path.exists():
        return []
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("morning_brief: failed to read job_matches.json")
        return []
    if not isinstance(items, list):
        return []
    items.sort(
        key=lambda m: -m.get("score", {}).get("overall", 0.0),
    )
    return items
```

- [ ] **Step 3: Inject `job_matches` into the snapshot dict**

In `generate_brief()`, find the line `snapshot = {` (around line 217) and add a new key just before the closing `}`:

```python
        # Job-discovery matches surfaced by the 3am cron (and ad-hoc scans).
        # See vault/skills/job-discovery/SKILL.md.
        "job_matches": _read_job_matches(vault),
```

- [ ] **Step 4: Update `_SYSTEM_PROMPT_HEAD` with rendering rules**

In `_SYSTEM_PROMPT_HEAD`, insert this new bullet block just before the trailing `Authoritative operational state` block:

```
**Job matches** — if `job_matches` is non-empty, render a section titled `**Job matches** (N active, K new this morning)` where N = total entries and K = entries with `surfaced_date == today`. Then list each as one line:

   `🆕 N. **{title}** — {company} · {location} · {salary} · score {overall:.1f}`
       `{one_line_take}`

The 🆕 marker ONLY for items with `surfaced_date == today`; older items get no marker but get a `(carried from M/D)` stamp using the M/D/YY format Tavin reads (US ordering, no leading zeros). Use `surfaced_date` for the carry-stamp date. Sort by `score.overall` desc. Cap at 5 items in the brief; if more exist, append a footer line `(+M more — say 'show me the queue' to see all)`.

After the list, include this exact reply hint as its own line: `Reply "1 apply / 2 pass [reason] / 3 tomorrow" anytime, or wait for tonight's check-in.`

If `job_matches` is empty, omit the section entirely — do NOT write "no matches today."
```

- [ ] **Step 5: Manually smoke-test the brief**

Set up a fake job_matches.json with one entry:

```powershell
$state = "C:\Users\matis\Desktop\Second Brain\.mizzix_state\job_matches.json"
$today = (Get-Date).ToString("yyyy-MM-dd")
@"
[
  {
    "id": "jm_test1234",
    "title": "Mechanical Design Engineer",
    "company": "Test Corp",
    "location": "Chicago, IL",
    "salary": "$80K-$100K",
    "url": "https://example.com/123",
    "surfaced_date": "$today",
    "score": {"overall": 4.2, "dims": {"role_fit": 5, "skills_match": 4, "seniority": 4, "domain": 4, "location": 5, "responsibilities": 4}, "method": "llm"},
    "one_line_take": "Hands-on mech design, target city, fits the HRSG translation story.",
    "status": "surfaced",
    "times_carried": 0
  }
]
"@ | Out-File -FilePath $state -Encoding utf8
```

Then run the brief manually:

```powershell
python C:\Users\matis\Desktop\DevProjects\Mizzix\morning_brief.py
```

Expected: the printed DM includes the Job matches section with the test entry. Clean up the test data afterward by removing `job_matches.json`:

```powershell
Remove-Item $state
```

- [ ] **Step 6: Bump Mizzix dashboard version (per project convention)**

Per `feedback_mizzix_version_bump` memory: this is the first restart-triggering edit of the implementation session, so bump the dashboard's minor version. Locate the version constant in `dashboard_server.py` (search for `VERSION = `) and increment the minor digit. Subsequent edits in the same session do NOT re-bump.

```powershell
# After bumping, restart Mizzix per feedback_restart_via_task
schtasks /End /TN Mizzix
schtasks /Run /TN Mizzix
```

- [ ] **Step 7: Commit changes to Mizzix repo**

```powershell
cd C:\Users\matis\Desktop\DevProjects\Mizzix
git add morning_brief.py dashboard_server.py
git commit -m "feat(brief): render job-discovery matches in morning brief"
```

---

## Task 11: Mizzix bot integration — EOD check-in heartbeat slot

**Files:**
- Modify: `C:\Users\matis\Desktop\DevProjects\Mizzix\heartbeat.py`
- Modify: `C:\Users\matis\Desktop\DevProjects\Mizzix\config.py`

Add a new slot in `_tick()` that fires at most once per day at the configured hour, only if `job_matches.json` has surfaced entries.

- [ ] **Step 1: Add config var for EOD hour**

In `C:\Users\matis\Desktop\DevProjects\Mizzix\config.py`, near the other timing constants (look for `TODO_AFTERNOON_PING_HOUR`), add:

```python
# Job-discovery EOD check-in: hour-of-day at which the heartbeat will DM
# Tavin a list of un-actioned matches and ask applied/pass/tomorrow per item.
# 19 = 7pm. Fires at most once per day, only if job_matches.json has any
# surfaced entries.
JOB_DISCOVERY_EOD_HOUR = 19
```

- [ ] **Step 2: Add `last_job_eod_date` to `_EMPTY_STATE`**

In `heartbeat.py`, find `_EMPTY_STATE` (around line 66) and add a new key:

```python
    "last_job_eod_date": None,           # ISO — once/day at JOB_DISCOVERY_EOD_HOUR
```

- [ ] **Step 3: Add the EOD-firing function**

In `heartbeat.py`, near `_fire_due_todo_reminders`, add:

```python
def _read_active_job_matches(vault: Path) -> list[dict]:
    """Read job_matches.json; only returns items whose status is `surfaced`.
    Sorted by score.overall desc. Empty on missing/corrupt."""
    path = vault / ".mizzix_state" / "job_matches.json"
    if not path.exists():
        return []
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("heartbeat: failed to read job_matches.json")
        return []
    if not isinstance(items, list):
        return []
    out = [m for m in items if m.get("status") == "surfaced"]
    out.sort(key=lambda m: -m.get("score", {}).get("overall", 0.0))
    return out


async def _fire_job_eod_checkin(
    client: discord.Client,
    state: dict,
    now: dt.datetime,
    today: str,
) -> None:
    """Once-per-day EOD ping for un-actioned job matches.

    Fires on the first non-DND tick at-or-after JOB_DISCOVERY_EOD_HOUR.
    Skips silently if job_matches.json has zero `surfaced` entries.
    Mizzix-side parsing of Tavin's reply happens conversationally per the
    job-discovery skill — heartbeat just sends the question.
    """
    if state.get("last_job_eod_date") == today:
        return
    if now.hour < config.JOB_DISCOVERY_EOD_HOUR:
        return

    vault = Path(config.VAULT_PATH)
    matches = _read_active_job_matches(vault)
    if not matches:
        # Mark the date so we don't keep checking — silent skip is correct.
        state["last_job_eod_date"] = today
        return

    lines = [
        f"End-of-day check-in — {len(matches)} job"
        f"{'s' if len(matches) != 1 else ''} from this morning still open:",
        "",
    ]
    for i, m in enumerate(matches, 1):
        lines.append(f"{i}. [{m['id']}] {m['company']} — {m.get('location') or '?'}")
    lines.append("")
    lines.append("For each, reply: applied / pass [reason] / tomorrow / decode")
    msg = "\n".join(lines)

    try:
        await _send_dm(client, msg)
        _log_nudge_to_daily(msg)
        state["nudges_today"].append({
            "at": now.isoformat(),
            "type": "job_eod_checkin",
            "count": len(matches),
        })
        logger.info("Heartbeat: job EOD check-in sent (%d matches)", len(matches))
    except Exception:
        logger.exception("Heartbeat: job EOD check-in failed")
    state["last_job_eod_date"] = today
```

- [ ] **Step 4: Wire the new slot into `_tick()`**

In `heartbeat.py`'s `_tick()` function, after slot 1.5 (`_fire_due_todo_reminders`) and before slot 2 (the discretionary nudge), insert slot 1.6:

```python
    # 1.6. Job-discovery EOD check-in — once/day at JOB_DISCOVERY_EOD_HOUR.
    # Only DMs if there are un-actioned matches in job_matches.json.
    try:
        await _fire_job_eod_checkin(client, state, now, today)
    except Exception:
        logger.exception("Heartbeat: job EOD pass crashed")
```

- [ ] **Step 5: Smoke test by triggering the slot manually**

Set up a fake match with status=surfaced (same script as Task 10, Step 5). Then temporarily set the EOD hour to current hour minus 1 in `config.py` so the next tick fires it. Restart Mizzix:

```powershell
schtasks /End /TN Mizzix
schtasks /Run /TN Mizzix
```

Wait for the next 15-min heartbeat tick — Tavin should receive a DM matching the format above. Restore `JOB_DISCOVERY_EOD_HOUR = 19` afterward and clean up the test data.

- [ ] **Step 6: Commit**

```powershell
cd C:\Users\matis\Desktop\DevProjects\Mizzix
git add heartbeat.py config.py
git commit -m "feat(heartbeat): add EOD check-in slot for job-discovery matches"
```

---

## Task 12: Cron registration — `MizzixJobDiscovery` scheduled task

**Files:**
- Create: `C:\Users\matis\Desktop\DevProjects\job-discovery\scripts\register_task.ps1`

The cron task runs `python -m job_discovery.cli scan` daily at 3am. Output is appended to `vault/.mizzix_state/job_discovery.log`.

- [ ] **Step 1: Write the registration PowerShell script**

Create `C:\Users\matis\Desktop\DevProjects\job-discovery\scripts\register_task.ps1`:

```powershell
# Register MizzixJobDiscovery scheduled task. Run as administrator the first time.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
#
# Re-running unregisters and re-registers (safe to repeat).

$TaskName = "MizzixJobDiscovery"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..").Path
$VaultPath = "C:\Users\matis\Desktop\Second Brain"
$LogPath = "$VaultPath\.mizzix_state\job_discovery.log"

# Find python.exe — assume it's on PATH; warn if not
$Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $Python) {
    Write-Error "python.exe not found on PATH"
    exit 1
}

# Build the action: cmd.exe so we can redirect stdout/stderr
$ActionArgs = "/c set VAULT_PATH=$VaultPath && `"$Python`" -m job_discovery.cli scan >> `"$LogPath`" 2>&1"
$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $ActionArgs -WorkingDirectory $RepoRoot

# Daily at 3:00 AM
$Trigger = New-ScheduledTaskTrigger -Daily -At 3:00am

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Unregister existing if present, then register fresh
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description "Daily job-discovery scan at 3am — see C:\Users\matis\Desktop\DevProjects\job-discovery"

Write-Output "Registered $TaskName. Run with: schtasks /Run /TN $TaskName"
```

- [ ] **Step 2: Run the registration script**

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\matis\Desktop\DevProjects\job-discovery\scripts\register_task.ps1
```

Expected output: `Registered MizzixJobDiscovery. Run with: schtasks /Run /TN MizzixJobDiscovery`

- [ ] **Step 3: Trigger one manual run to verify wiring**

Pre-requisite: `criteria.md` exists (run the onboarding interview via Mizzix first if it doesn't — see SKILL.md behavior section).

```powershell
schtasks /Run /TN MizzixJobDiscovery
# Wait ~30s, then check the log
Get-Content "C:\Users\matis\Desktop\Second Brain\.mizzix_state\job_discovery.log" -Tail 30
```

Expected: log shows board fetches, scoring, and a JSON summary line. `job_matches.json` should now exist with at least the keys from the summary.

- [ ] **Step 4: Commit the script to the repo**

```powershell
cd C:\Users\matis\Desktop\DevProjects\job-discovery
git add scripts/register_task.ps1
git commit -m "chore(cron): registration script for MizzixJobDiscovery scheduled task"
```

---

## Task 13: End-to-end smoke test + GitHub push

**Files:** none new — this task validates the whole pipeline and pushes the repo.

- [ ] **Step 1: Run the full pipeline end-to-end against Tavin's actual criteria**

Pre-requisite: onboarding interview has been completed by Mizzix (criteria.md populated).

```powershell
$env:VAULT_PATH = "C:\Users\matis\Desktop\Second Brain"
python -m job_discovery.cli scan --dry-run
```

Expected: prints "would score: <company> — <title>" for each fresh listing and ends with a count line. No state files should be written. Eyeball the output — do the listings look like things Tavin would actually want to see?

If the listings look wildly off, check `criteria.md` — likely the role aliases or location list is too narrow/broad.

- [ ] **Step 2: Run a real scan (writes state)**

```powershell
python -m job_discovery.cli scan
```

Expected: writes `job_matches.json` with up to 5 entries. Verify:

```powershell
python -m job_discovery.cli list-active
```

Should print the same items, formatted.

- [ ] **Step 3: Manually fire the morning brief to verify integration**

```powershell
python C:\Users\matis\Desktop\DevProjects\Mizzix\morning_brief.py
```

Expected: printed brief includes the Job matches section with the entries from step 2.

- [ ] **Step 4: Manually exercise the EOD reply flow**

Wait for the next 7pm-or-later heartbeat tick (or temporarily lower `JOB_DISCOVERY_EOD_HOUR` for testing). Tavin DMs Mizzix with a reply like *"1 applied, 2 pass too senior, 3 tomorrow"*. Verify:

- Match 1's row appears in `applications.md`
- Match 2's reason appears in `preferences.md` under `## Pass reasons (raw)`
- Both 1 and 2 are gone from `job_matches.json` and present in `job_matches_history.json`
- Match 3 stays in `job_matches.json`

- [ ] **Step 5: Push to GitHub (private)**

Tavin creates a private repo on GitHub manually (one-time, can't be scripted without a token). Then:

```powershell
cd C:\Users\matis\Desktop\DevProjects\job-discovery
git remote add origin https://github.com/<owner>/job-discovery.git
git branch -M main
git push -u origin main
```

Expected: push succeeds, GitHub shows the repo with all commits from this implementation session.

- [ ] **Step 6: Final commit — implementation note**

Append a short "Status: live as of YYYY-MM-DD" line to `DESIGN.md` and commit:

```powershell
git add DESIGN.md
git commit -m "docs: mark design as implemented and live"
git push
```

---

## Self-review

After writing this plan, here's the cross-check against [DESIGN.md](DESIGN.md):

**Spec coverage:**

| Spec section | Plan task |
|---|---|
| Component 1 — `job-discovery` skill | Task 9 |
| Component 2 — `job_discovery` package | Tasks 1-8 |
| Component 3 — `MizzixJobDiscovery` cron | Task 12 |
| Component 4 — morning brief integration | Task 10 |
| Component 5 — EOD heartbeat trigger | Task 11 |
| File: `criteria.md` | Task 3 (reader) + Task 9 (Mizzix writes via interview) |
| File: `preferences.md` | Tasks 3, 4 |
| File: `applications.md` | Task 4 |
| File: `job_matches.json` | Task 2 |
| File: `job_matches_history.json` | Task 2 |
| Scoring: 6 dimensions, weighted | Tasks 6, 7 |
| Scoring: rule-based fallback | Task 6 |
| Pass-reason loop | Task 4 + Task 9 + Task 11 |
| Onboarding interview | Task 9 (skill body) |
| Morning brief rendering format | Task 10 |
| EOD check-in DM | Task 11 |
| Skill commands (CLI) | Task 8 |
| Skill behaviors (Mizzix-driven) | Task 9 |
| Handoff to interview-coach | Task 9 (skill body) |
| Failure-mode handling — board errors, all-fail, scoring fallback | Task 5 (per-board isolation), Task 8 (all-fail return code), Tasks 6+7 (fallback) |
| Initial setup checklist | Tasks 1, 9, 12, 13 |
| Testing strategy | TDD-driven across Tasks 2-8 |

No spec section is uncovered.

**Placeholder scan:**

No `TBD`, `TODO`, `implement later`, `add appropriate error handling`, or `similar to Task N` references. All code blocks are complete.

**Type consistency:**

- `match` dict shape consistent across state.py / cli.py / heartbeat.py / morning_brief.py: `id`, `title`, `company`, `location`, `salary`, `url`, `surfaced_date`, `score: {overall, dims, method}`, `one_line_take`, `status`, `times_carried`, `last_brief_date`.
- `score` result shape consistent between `score_rule_based` and `score_llm` / `_parse_score_response`: `overall`, `dims`, `one_line_take`, `method`.
- CLI command names match the SKILL.md table: `scan`, `list-active`, `score-one`, `record-action`.
- Action names consistent in CLI argparse choices and SKILL.md instructions: `applied`, `pass`, `tomorrow`, `decoded`.

No mismatches.

---

## Execution handoff

Plan complete. Two execution options when ready to build:

**1. Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.
