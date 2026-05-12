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
