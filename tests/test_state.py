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


def test_load_scored_history_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    assert state.load_scored_history() == []


def test_append_scored_keys_writes_today_with_date(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    state.append_scored_keys(["acme|mech eng|chicago, il"], "2026-05-20")
    items = state.load_scored_history()
    assert items == [{"key": "acme|mech eng|chicago, il", "scored_date": "2026-05-20"}]


def test_append_scored_keys_noop_on_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    state.append_scored_keys([], "2026-05-20")
    # File should not exist — no-op means no write
    assert not (tmp_path / ".mizzix_state" / "job_scored_history.json").exists()


def test_append_scored_keys_trims_entries_older_than_retain_days(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    state.save_scored_history([
        {"key": "old|job|loc", "scored_date": "2026-04-01"},     # >14 days old
        {"key": "recent|job|loc", "scored_date": "2026-05-10"},  # within window
    ])
    state.append_scored_keys(["new|job|loc"], "2026-05-20")
    items = state.load_scored_history()
    keys = {e["key"] for e in items}
    assert "old|job|loc" not in keys
    assert "recent|job|loc" in keys
    assert "new|job|loc" in keys


def test_append_scored_keys_idempotent_within_a_day(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    state.append_scored_keys(["acme|mech eng|chicago, il"], "2026-05-20")
    state.append_scored_keys(["acme|mech eng|chicago, il"], "2026-05-20")
    items = state.load_scored_history()
    assert len(items) == 1


def test_append_scored_keys_preserves_yesterdays_same_key(tmp_path, monkeypatch):
    """A job scored yesterday and re-scored today (shouldn't normally happen
    once dedupe is wired, but defensively) keeps a record per day."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    state.append_scored_keys(["acme|mech eng|chicago, il"], "2026-05-19")
    state.append_scored_keys(["acme|mech eng|chicago, il"], "2026-05-20")
    items = state.load_scored_history()
    assert {e["scored_date"] for e in items} == {"2026-05-19", "2026-05-20"}


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


def test_read_criteria_parses_experience_profile(tmp_path, monkeypatch):
    """The ## Experience profile section produces a structured experience
    dict the scorer's apply_experience_penalty consumes."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    crit_path = tmp_path / "projects" / "Job_Search" / "discovery" / "criteria.md"
    crit_path.parent.mkdir(parents=True)
    crit_path.write_text(CRITERIA_FIXTURE + """
## Experience profile

- years_total: 2
- domains: aerospace, thermal, cryogenic, mechanical_design, test_operations
- hard_filter_years_above: 3
""", encoding="utf-8")

    crit = state.read_criteria()
    exp = crit.get("experience")
    assert exp is not None
    assert exp["years_total"] == 2
    assert exp["hard_filter_years_above"] == 3
    assert "aerospace" in exp["domains"]
    assert "mechanical_design" in exp["domains"]


def test_read_criteria_no_experience_section_omits_key(tmp_path, monkeypatch):
    """When criteria.md has no ## Experience profile section, the key is
    absent from the dict — apply_experience_penalty no-ops on this."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    crit_path = tmp_path / "projects" / "Job_Search" / "discovery" / "criteria.md"
    crit_path.parent.mkdir(parents=True)
    crit_path.write_text(CRITERIA_FIXTURE, encoding="utf-8")

    crit = state.read_criteria()
    assert "experience" not in crit or not crit.get("experience")


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


# -----------------------------------------------------------------------------
# Bug fixes from Mizzix's 2026-05-12 onboarding scan
# -----------------------------------------------------------------------------


CRITERIA_WITH_H3_SUBSECTIONS = """\
# Job Search Criteria

## Roles

### High priority titles
- Mechanical Engineer
- Aerospace Engineer

### Secondary titles
- Systems Engineer
- Test Engineer

### Title exclusions (always skip)
- Senior
- Sr.
- Manager
- Director

## Locations

### Tier 1 — top targets
- Chicago, IL
- Milwaukee, WI

### Tier 2 — major metros
- New York City, NY
- Boston, MA

### Location notes
- Hard requirement: medium to major metro area only
- Avoid Texas and Florida

## Salary floor
60000
"""


def test_read_criteria_treats_h3_subsections_as_part_of_parent_h2(tmp_path, monkeypatch):
    """H3 sub-headings under ## Roles split into roles vs title_exclusions
    based on the H3 heading text. Without this, exclusions would be parsed
    as target roles and surfaced in the search query.
    """
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    crit_path = tmp_path / "projects" / "Job_Search" / "discovery" / "criteria.md"
    crit_path.parent.mkdir(parents=True)
    crit_path.write_text(CRITERIA_WITH_H3_SUBSECTIONS, encoding="utf-8")

    crit = state.read_criteria()
    # Target roles include only the high-priority + secondary titles, NOT exclusions
    assert "Mechanical Engineer" in crit["roles"]
    assert "Aerospace Engineer" in crit["roles"]
    assert "Systems Engineer" in crit["roles"]
    assert "Test Engineer" in crit["roles"]
    assert "Senior" not in crit["roles"]
    assert "Manager" not in crit["roles"]
    assert "Director" not in crit["roles"]

    # title_exclusions surfaced as a separate field
    assert "title_exclusions" in crit
    assert "Senior" in crit["title_exclusions"]
    assert "Sr." in crit["title_exclusions"]
    assert "Manager" in crit["title_exclusions"]
    assert "Director" in crit["title_exclusions"]


def test_read_criteria_filters_non_city_bullets_from_locations(tmp_path, monkeypatch):
    """Bullets under ## Locations that don't match City, ST pattern get
    skipped — protects against prose like 'Hard requirement: medium metro
    area only' being queried as a city.
    """
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    crit_path = tmp_path / "projects" / "Job_Search" / "discovery" / "criteria.md"
    crit_path.parent.mkdir(parents=True)
    crit_path.write_text(CRITERIA_WITH_H3_SUBSECTIONS, encoding="utf-8")

    crit = state.read_criteria()
    assert "Chicago, IL" in crit["locations"]
    assert "Milwaukee, WI" in crit["locations"]
    assert "New York City, NY" in crit["locations"]
    assert "Boston, MA" in crit["locations"]
    # The prose bullets must NOT be in locations
    assert not any("Hard requirement" in loc for loc in crit["locations"])
    assert not any("Avoid Texas" in loc for loc in crit["locations"])


def test_read_criteria_title_exclusions_default_to_empty_list_when_absent(tmp_path, monkeypatch):
    """When criteria.md has no title-exclusion sub-section, the field is
    still present on the dict (as []) so downstream consumers don't KeyError.
    """
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    crit_path = tmp_path / "projects" / "Job_Search" / "discovery" / "criteria.md"
    crit_path.parent.mkdir(parents=True)
    crit_path.write_text(CRITERIA_FIXTURE, encoding="utf-8")  # the original fixture

    crit = state.read_criteria()
    assert crit["title_exclusions"] == []


def test_read_criteria_returns_empty_when_missing_includes_title_exclusions(tmp_path, monkeypatch):
    """Empty defaults must include the new field too."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    crit = state.read_criteria()
    assert crit["title_exclusions"] == []


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
