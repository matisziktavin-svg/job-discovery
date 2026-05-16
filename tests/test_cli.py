from unittest.mock import patch

import pytest

from job_discovery import cli, score, state, search, fetch_jd


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


def test_apply_hard_gates_warns_on_unrecognized_prefix(caplog):
    """Bug D: free-text hard_gates entries without a known prefix get
    silently no-oped at runtime. We emit a WARNING so users notice."""
    import logging as _logging
    listings = [_mk_listing("Mech Eng", "Acme")]
    crit = {
        **CRITERIA,
        "hard_gates": [
            "Military enlistment (excluded — not a job posting)",
            "Rural / no major metro area",
        ],
    }
    with caplog.at_level(_logging.WARNING, logger="job_discovery.cli"):
        out = cli._apply_hard_gates(listings, crit)
    # Listing passes through (gates aren't enforced — same behavior as before)
    assert len(out) == 1
    # But there are warnings for both unrecognized entries
    warnings = [r for r in caplog.records if r.levelno == _logging.WARNING]
    assert len(warnings) == 2
    msgs = " ".join(r.getMessage() for r in warnings)
    assert "Military enlistment" in msgs
    assert "Rural" in msgs


def test_select_top_n_robust_against_malformed_posted_date():
    """Bug C defense-in-depth: even if normalize_listing fails and a bad
    posted_date slips through ('nan', empty string, None, missing key, or
    an unparseable date), the sort must NOT crash. Bad-date items sort
    to the end via tiebreak=0 instead of taking the whole scan down."""
    scored = [
        {"id": "a", "score": {"overall": 4.5}, "posted_date": "2026-05-14"},
        {"id": "b", "score": {"overall": 4.5}, "posted_date": "nan"},  # the bug
        {"id": "c", "score": {"overall": 4.5}, "posted_date": ""},
        {"id": "d", "score": {"overall": 4.5}, "posted_date": None},
        {"id": "e", "score": {"overall": 4.5}, "posted_date": "not-a-date"},
        {"id": "f", "score": {"overall": 4.5}},  # missing key entirely
    ]
    top = cli._select_top_n(scored, n=10, threshold=3.0)  # must not raise
    # All six items survive (they all clear the threshold); the well-formed
    # date sorts first because its key is larger than 0.
    assert {m["id"] for m in top} == {"a", "b", "c", "d", "e", "f"}
    assert top[0]["id"] == "a"


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


def test_cmd_scan_isolates_per_listing_scoring_failure(tmp_path, monkeypatch, capsys):
    """Bug C regression guard: one bad listing must not abort the whole scan.
    Pre-fix, an uncaught exception in score_listing propagated up out of
    cmd_scan, so state.save_matches() never ran and job_matches.json was
    never written — exactly the failure mode that killed 2026-05-15's brief.
    """
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))

    crit_path = tmp_path / "projects" / "Job_Search" / "discovery" / "criteria.md"
    crit_path.parent.mkdir(parents=True, exist_ok=True)
    crit_path.write_text(
        "# Job Search Criteria\n\n## Roles\n- Mech Eng\n\n"
        "## Locations\n- Chicago, IL\n",
        encoding="utf-8",
    )

    listings = [
        {"title": "Good A", "company": "GoodA", "location": "Chicago, IL",
         "url": "u1", "salary": "$80K-$100K", "posted_date": "2026-05-14",
         "source": "linkedin", "description": "ok"},
        {"title": "Bad B", "company": "BadB", "location": "Chicago, IL",
         "url": "u2", "salary": "", "posted_date": "2026-05-14",
         "source": "linkedin", "description": "ok"},
        {"title": "Good C", "company": "GoodC", "location": "Chicago, IL",
         "url": "u3", "salary": "$80K-$100K", "posted_date": "2026-05-14",
         "source": "linkedin", "description": "ok"},
    ]

    monkeypatch.setattr(
        search, "fetch_all",
        lambda criteria, results_per_board=50: (listings, {"linkedin@Chicago, IL": "ok"}),
    )

    def fake_score_listing(listing, criteria, preferences, profile_blob, model=None):
        if listing["company"] == "BadB":
            raise RuntimeError("simulated scoring crash")
        return {
            "overall": 4.0,
            "dims": {"role_fit": 4, "skills_match": 4, "seniority": 4,
                     "domain": 4, "location": 4, "responsibilities": 4},
            "one_line_take": "good fit",
            "method": "llm",
        }

    monkeypatch.setattr(score, "score_listing", fake_score_listing)
    monkeypatch.setattr(score, "apply_salary_penalty", lambda r, l, c: r)

    args = _Args(dry_run=False, top_n=5, threshold=3.0)
    rc = cli.cmd_scan(args)

    assert rc == 0

    # Critical: state.save_matches() ran — the file exists with the two good
    # listings and the bad one was skipped.
    matches = state.load_matches()
    companies = {m["company"] for m in matches}
    assert "GoodA" in companies
    assert "GoodC" in companies
    assert "BadB" not in companies


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


# -----------------------------------------------------------------------------
# cmd_record_action — the action handlers
# -----------------------------------------------------------------------------


def _seed_match(monkeypatch, tmp_path, match_id="jm_test1234", **overrides):
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    base = {
        "id": match_id, "title": "Mech Eng", "company": "Acme",
        "location": "Chicago, IL", "url": "https://example.com/jobs/1",
        "salary": "$80K-$100K", "posted_date": "2026-05-12",
        "surfaced_date": "2026-05-12", "score": {"overall": 4.0, "dims": {}},
        "one_line_take": "fits", "status": "surfaced", "times_carried": 0,
        **overrides,
    }
    state.save_matches([base])
    return base


class _Args:
    """Quick stand-in for argparse.Namespace."""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_cmd_record_action_applied_moves_to_history_and_appends_application(tmp_path, monkeypatch):
    _seed_match(monkeypatch, tmp_path, match_id="jm_apply1")
    args = _Args(match_id="jm_apply1", action="applied", reason="")
    rc = cli.cmd_record_action(args)
    assert rc == 0

    # Match removed from active queue
    assert state.load_matches() == []

    # History gets the match with status=applied + action_date
    history = state.load_history()
    assert len(history) == 1
    assert history[0]["status"] == "applied"
    assert history[0]["action_date"]  # populated

    # applications.md row exists
    app_path = tmp_path / "projects" / "Job_Search" / "discovery" / "applications.md"
    assert app_path.exists()
    text = app_path.read_text(encoding="utf-8")
    assert "Acme" in text
    assert "Mech Eng" in text


def test_cmd_record_action_pass_requires_reason(tmp_path, monkeypatch):
    _seed_match(monkeypatch, tmp_path, match_id="jm_pass1")
    args = _Args(match_id="jm_pass1", action="pass", reason="")
    rc = cli.cmd_record_action(args)
    assert rc == 1
    # Match must still be active — no mutation on missing reason
    assert len(state.load_matches()) == 1


def test_cmd_record_action_pass_with_reason_writes_preference_and_history(tmp_path, monkeypatch):
    _seed_match(monkeypatch, tmp_path, match_id="jm_pass2")
    args = _Args(match_id="jm_pass2", action="pass", reason="too senior")
    rc = cli.cmd_record_action(args)
    assert rc == 0

    assert state.load_matches() == []
    history = state.load_history()
    assert len(history) == 1
    assert history[0]["status"] == "passed"
    assert history[0]["pass_reason"] == "too senior"

    pref_path = tmp_path / "projects" / "Job_Search" / "discovery" / "preferences.md"
    assert pref_path.exists()
    assert "too senior" in pref_path.read_text(encoding="utf-8")


def test_cmd_record_action_tomorrow_does_not_mutate_state(tmp_path, monkeypatch):
    seeded = _seed_match(monkeypatch, tmp_path, match_id="jm_tom1")
    args = _Args(match_id="jm_tom1", action="tomorrow", reason="")
    rc = cli.cmd_record_action(args)
    assert rc == 0
    # Match still active, unchanged
    items = state.load_matches()
    assert len(items) == 1
    assert items[0]["id"] == "jm_tom1"
    assert items[0]["status"] == "surfaced"
    # No history written
    assert state.load_history() == []


def test_cmd_record_action_decoded_flips_flag_keeps_in_queue(tmp_path, monkeypatch):
    _seed_match(monkeypatch, tmp_path, match_id="jm_dec1")
    args = _Args(match_id="jm_dec1", action="decoded", reason="")
    rc = cli.cmd_record_action(args)
    assert rc == 0
    items = state.load_matches()
    assert len(items) == 1
    assert items[0]["decoded"] is True
    assert items[0]["status"] == "surfaced"  # still in queue
    assert state.load_history() == []  # not retired


def test_cmd_record_action_unknown_match_id_errors(tmp_path, monkeypatch):
    _seed_match(monkeypatch, tmp_path, match_id="jm_real1")
    args = _Args(match_id="jm_does_not_exist", action="applied", reason="")
    rc = cli.cmd_record_action(args)
    assert rc == 1
    # No mutation
    assert len(state.load_matches()) == 1
    assert state.load_history() == []


def test_cmd_record_action_applied_failure_in_append_does_not_corrupt_state(
    tmp_path, monkeypatch,
):
    """Regression: if append_application throws after we've already mutated
    the in-memory match dict, the match must NOT have been retired from
    job_matches.json. Order: fallible append first, then state mutations.
    """
    _seed_match(monkeypatch, tmp_path, match_id="jm_fail1")

    def boom(**_kw):
        raise IOError("disk full")

    monkeypatch.setattr(state, "append_application", boom)

    args = _Args(match_id="jm_fail1", action="applied", reason="")
    with pytest.raises(IOError, match="disk full"):
        cli.cmd_record_action(args)

    # Match must still be active (not yet moved to history) AND not mutated
    items = state.load_matches()
    assert len(items) == 1
    assert items[0]["id"] == "jm_fail1"
    assert items[0]["status"] == "surfaced"  # NOT "applied"
    assert "action_date" not in items[0]
    assert state.load_history() == []


# -----------------------------------------------------------------------------
# Hybrid JD-recovery gate in cmd_scan
# -----------------------------------------------------------------------------


def _write_criteria(tmp_path):
    crit_path = tmp_path / "projects" / "Job_Search" / "discovery" / "criteria.md"
    crit_path.parent.mkdir(parents=True, exist_ok=True)
    crit_path.write_text(
        "# Job Search Criteria\n\n## Roles\n- Mech Eng\n\n"
        "## Locations\n- Chicago, IL\n",
        encoding="utf-8",
    )


def _gate_listing(url, description=""):
    return {
        "title": "Mechanical Design Engineer", "company": "Akkodis",
        "location": "Denver, CO", "url": url, "salary": "$80K-$100K",
        "posted_date": "2026-05-16", "source": "linkedin",
        "description": description,
    }


def test_cmd_scan_gate_recovers_jd_and_rescores(tmp_path, monkeypatch):
    """No description + blind score >4 → WebFetch fires, listing is rescored
    on the recovered JD (here the rescore correctly drops it)."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    _write_criteria(tmp_path)
    listing = _gate_listing("https://linkedin.com/jobs/view/1")
    monkeypatch.setattr(
        search, "fetch_all",
        lambda criteria, results_per_board=50: ([listing], {"linkedin": "ok"}),
    )

    calls = {"score": 0}

    def fake_score_listing(l, c, p, b, model=None):
        calls["score"] += 1
        if l.get("description"):  # rescore on recovered JD → correct (low)
            return {"overall": 2.5, "dims": {"role_fit": 2, "skills_match": 2,
                    "seniority": 2, "domain": 2, "location": 2,
                    "responsibilities": 2},
                    "one_line_take": "senior, not a fit", "method": "llm"}
        return {"overall": 4.3, "dims": {"role_fit": 5, "skills_match": 4,
                "seniority": 5, "domain": 4, "location": 4,
                "responsibilities": 4},
                "one_line_take": "looks great", "method": "llm"}

    monkeypatch.setattr(score, "score_listing", fake_score_listing)
    monkeypatch.setattr(score, "apply_salary_penalty", lambda r, l, c: r)

    fetch_calls = []
    monkeypatch.setattr(
        fetch_jd, "fetch_job_description",
        lambda url, timeout_s=45.0: (fetch_calls.append(url)
                                     or "7-15 yrs, senior missile defense."),
    )

    rc = cli.cmd_scan(_Args(dry_run=False, top_n=5, threshold=3.0))
    assert rc == 0
    assert fetch_calls == ["https://linkedin.com/jobs/view/1"]
    assert calls["score"] == 2  # blind score + rescore
    assert state.load_matches() == []  # rescored 2.5 < 3.0 → not surfaced


def test_cmd_scan_gate_penalizes_when_fetch_fails(tmp_path, monkeypatch):
    """No description + blind score >4 + WebFetch returns None →
    apply_unverified_penalty: -0.5 and flagged, still surfaced."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    _write_criteria(tmp_path)
    listing = _gate_listing("https://linkedin.com/jobs/view/2")
    monkeypatch.setattr(
        search, "fetch_all",
        lambda criteria, results_per_board=50: ([listing], {"linkedin": "ok"}),
    )
    monkeypatch.setattr(
        score, "score_listing",
        lambda l, c, p, b, model=None: {
            "overall": 4.4, "dims": {"role_fit": 5, "skills_match": 4,
            "seniority": 5, "domain": 4, "location": 4,
            "responsibilities": 4},
            "one_line_take": "looks great", "method": "llm"},
    )
    monkeypatch.setattr(score, "apply_salary_penalty", lambda r, l, c: r)
    monkeypatch.setattr(
        fetch_jd, "fetch_job_description", lambda url, timeout_s=45.0: None
    )

    rc = cli.cmd_scan(_Args(dry_run=False, top_n=5, threshold=3.0))
    assert rc == 0
    matches = state.load_matches()
    assert len(matches) == 1
    assert matches[0]["score"]["overall"] == 3.9  # 4.4 - 0.5
    assert "unverified" in matches[0]["one_line_take"].lower()


def test_cmd_scan_gate_skips_when_description_present(tmp_path, monkeypatch):
    """Has a description → gate never fires even if score >4."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    _write_criteria(tmp_path)
    listing = _gate_listing("u", description="Full real description here.")
    monkeypatch.setattr(
        search, "fetch_all",
        lambda criteria, results_per_board=50: ([listing], {"linkedin": "ok"}),
    )
    monkeypatch.setattr(
        score, "score_listing",
        lambda l, c, p, b, model=None: {
            "overall": 4.6, "dims": {"role_fit": 5, "skills_match": 5,
            "seniority": 4, "domain": 4, "location": 5,
            "responsibilities": 4},
            "one_line_take": "great", "method": "llm"},
    )
    monkeypatch.setattr(score, "apply_salary_penalty", lambda r, l, c: r)
    called = []
    monkeypatch.setattr(
        fetch_jd, "fetch_job_description",
        lambda url, timeout_s=45.0: called.append(url),
    )

    rc = cli.cmd_scan(_Args(dry_run=False, top_n=5, threshold=3.0))
    assert rc == 0
    assert called == []  # description present → no fetch


def test_cmd_scan_gate_skips_when_score_not_above_4(tmp_path, monkeypatch):
    """No description but score is exactly 4.0 (not >4.0) → gate must NOT
    fire. Verifies the strict > boundary."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    _write_criteria(tmp_path)
    listing = _gate_listing("u")
    monkeypatch.setattr(
        search, "fetch_all",
        lambda criteria, results_per_board=50: ([listing], {"linkedin": "ok"}),
    )
    monkeypatch.setattr(
        score, "score_listing",
        lambda l, c, p, b, model=None: {
            "overall": 4.0, "dims": {"role_fit": 4, "skills_match": 4,
            "seniority": 4, "domain": 4, "location": 4,
            "responsibilities": 4},
            "one_line_take": "ok", "method": "llm"},
    )
    monkeypatch.setattr(score, "apply_salary_penalty", lambda r, l, c: r)
    called = []
    monkeypatch.setattr(
        fetch_jd, "fetch_job_description",
        lambda url, timeout_s=45.0: called.append(url),
    )

    rc = cli.cmd_scan(_Args(dry_run=False, top_n=5, threshold=3.0))
    assert rc == 0
    assert called == []  # 4.0 is not > 4.0 → no fetch
