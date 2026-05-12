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
