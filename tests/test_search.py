import datetime as dt
from unittest.mock import patch

import pandas as pd

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


def test_normalize_listing_converts_datetime_date_posted():
    raw = {
        "title": "T", "company": "X", "location": "L", "job_url": "u",
        "site": "linkedin", "date_posted": dt.date(2026, 5, 12),
    }
    out = search.normalize_listing(raw)
    assert out["posted_date"] == "2026-05-12"


def test_fetch_all_loops_over_all_locations(monkeypatch):
    """fetch_all must hit every (board, location) pair, not just the first
    location. Regression guard: earlier impl used locations[0] only.
    """
    calls = []

    def fake_scrape_jobs(**kwargs):
        calls.append({"site": kwargs["site_name"][0], "loc": kwargs["location"]})
        return pd.DataFrame()  # empty — we're checking call surface, not normalization

    import jobspy
    monkeypatch.setattr(jobspy, "scrape_jobs", fake_scrape_jobs)

    criteria = {
        "roles": ["Mech Eng"],
        "locations": ["Chicago, IL", "Denver, CO"],
    }
    listings, status = search.fetch_all(criteria, results_per_board=20)

    # len(ALL_BOARDS) × 2 locations calls (currently 3 boards × 2 = 6 with
    # glassdoor + zip_recruiter disabled upstream; see search.DISABLED_BOARDS)
    assert len(calls) == len(search.ALL_BOARDS) * 2
    locs_called = {c["loc"] for c in calls}
    assert locs_called == {"Chicago, IL", "Denver, CO"}
    boards_called = {c["site"] for c in calls}
    assert boards_called == set(search.ALL_BOARDS)
    # status keys are "<board>@<location>" when location is non-empty
    assert "linkedin@Chicago, IL" in status
    assert "linkedin@Denver, CO" in status
    assert listings == []


def test_fetch_all_isolates_per_pair_failures(monkeypatch):
    """When one (board, location) raises, others still return their data
    and the failure shows up in status with the error class.
    """
    def fake_scrape_jobs(**kwargs):
        if kwargs["site_name"][0] == "indeed":
            raise RuntimeError("indeed broke")
        return pd.DataFrame([{
            "title": "Mech Eng", "company": "Acme", "location": "Chicago, IL",
            "job_url": "https://example.com/1", "site": kwargs["site_name"][0],
        }])

    import jobspy
    monkeypatch.setattr(jobspy, "scrape_jobs", fake_scrape_jobs)

    criteria = {"roles": ["Mech Eng"], "locations": ["Chicago, IL"]}
    listings, status = search.fetch_all(criteria, results_per_board=20)

    assert "error: RuntimeError: indeed broke" in status["indeed@Chicago, IL"]
    assert status["linkedin@Chicago, IL"] == "ok"
    # 4 successful boards, 1 listing each, all dedupe to 1 (same company+title+location)
    assert len(listings) == 1
    assert listings[0]["source"] == "linkedin"  # quality winner


def test_normalize_listing_handles_nan_salary():
    """Bug B: JobSpy returns NaN (not None) for missing salary fields when
    serializing pandas DataFrames. int(NaN) raises ValueError.
    """
    nan = float("nan")
    raw = {
        "title": "Mech Eng", "company": "Acme", "location": "Chicago, IL",
        "job_url": "https://example.com/1", "site": "indeed",
        "min_amount": nan, "max_amount": nan, "date_posted": "2026-05-12",
    }
    out = search.normalize_listing(raw)  # Must not raise
    assert out["salary"] == ""


def test_normalize_listing_handles_partial_nan_salary():
    """Min set, max NaN — should still produce '+'-style salary."""
    nan = float("nan")
    raw = {
        "title": "T", "company": "C", "location": "L",
        "job_url": "u", "site": "indeed",
        "min_amount": 75000, "max_amount": nan,
    }
    out = search.normalize_listing(raw)
    assert out["salary"] == "$75K+"


def test_fetch_all_isolates_bad_listing_within_a_board(monkeypatch):
    """Bug B: a single bad listing in a DataFrame must not kill the whole
    board's batch. A NaN salary in row 1 should not drop rows 0 and 2.
    Regression: prior impl let normalize_listing's ValueError propagate up
    to the per-board try/except, dropping the entire DataFrame.
    """
    nan = float("nan")

    def fake_scrape_jobs(**kwargs):
        if kwargs["site_name"][0] == "indeed":
            return pd.DataFrame([
                {"title": "Good Eng A", "company": "A", "location": "Chicago, IL",
                 "job_url": "u1", "site": "indeed", "min_amount": 70000, "max_amount": 90000},
                {"title": "Bad Eng", "company": "B", "location": "Chicago, IL",
                 "job_url": "u2", "site": "indeed", "min_amount": nan, "max_amount": nan},
                {"title": "Good Eng C", "company": "C", "location": "Chicago, IL",
                 "job_url": "u3", "site": "indeed", "min_amount": 80000, "max_amount": 100000},
            ])
        return pd.DataFrame()

    import jobspy
    monkeypatch.setattr(jobspy, "scrape_jobs", fake_scrape_jobs)

    listings, status = search.fetch_all(
        {"roles": ["Eng"], "locations": ["Chicago, IL"]},
        results_per_board=20,
    )

    # All 3 indeed listings made it (the "bad" one with NaN gets salary="")
    indeed_listings = [l for l in listings if l["source"] == "indeed"]
    assert len(indeed_listings) == 3
    assert status["indeed@Chicago, IL"] == "ok"


def test_normalize_listing_handles_nan_posted_date():
    """Bug C regression: JobSpy returns NaN (a truthy float) for missing
    `date_posted`. Pre-fix, the strftime fallback caught AttributeError and
    did `str(NaN)` → the literal string "nan", which then crashed `int()`
    in cli._select_top_n's sort key at 3 AM and killed the whole scan
    before state.save_matches() could write. Posted_date NaN must yield "".
    """
    nan = float("nan")
    raw = {
        "title": "Mech Eng", "company": "Acme", "location": "Chicago, IL",
        "job_url": "https://example.com/1", "site": "indeed",
        "date_posted": nan,
    }
    out = search.normalize_listing(raw)
    assert out["posted_date"] == ""
    assert out["posted_date"] != "nan"  # explicit guard against the historical bug


def test_normalize_listing_handles_nan_string_fields():
    """Bug C related: any string-typed JobSpy field could in principle be NaN
    when the upstream DataFrame has a missing value. The naive
    `(raw.get("field") or "").strip()` pattern crashed on NaN floats because
    NaN is truthy. _safe_str must coerce NaN to "" before .strip()/.lower().
    """
    nan = float("nan")
    raw = {
        "title": nan, "company": nan, "location": nan,
        "job_url": nan, "site": nan, "description": nan,
    }
    out = search.normalize_listing(raw)  # Must not raise
    assert out["title"] == ""
    assert out["company"] == ""
    assert out["location"] == ""
    assert out["url"] == ""
    assert out["source"] == ""
    assert out["description"] == ""


def test_normalize_listing_handles_pandas_timestamp_posted_date():
    """JobSpy can return pandas.Timestamp for date_posted. strftime path
    must produce ISO format, not str() of the Timestamp."""
    import pandas as pd
    raw = {
        "title": "T", "company": "X", "location": "L", "job_url": "u",
        "site": "linkedin", "date_posted": pd.Timestamp("2026-05-12"),
    }
    out = search.normalize_listing(raw)
    assert out["posted_date"] == "2026-05-12"


def test_fetch_all_excludes_title_exclusions_from_search_terms(monkeypatch):
    """Bug A: criteria.title_exclusions must NOT be sent as search terms to
    JobSpy. Regression guard: earlier impl folded exclusions into roles, so
    the search query said `"Mech Eng" OR "Senior" OR "Manager"`.
    """
    captured_terms = []

    def fake_scrape_jobs(**kwargs):
        captured_terms.append(kwargs.get("search_term"))
        return pd.DataFrame()

    import jobspy
    monkeypatch.setattr(jobspy, "scrape_jobs", fake_scrape_jobs)

    criteria = {
        "roles": ["Mechanical Engineer", "Aerospace Engineer"],
        "title_exclusions": ["Senior", "Manager", "Director"],
        "locations": ["Chicago, IL"],
    }
    search.fetch_all(criteria, results_per_board=10)

    for term in captured_terms:
        assert term is not None
        assert "Mechanical Engineer" in term
        assert "Aerospace Engineer" in term
        assert "Senior" not in term
        assert "Manager" not in term
        assert "Director" not in term
