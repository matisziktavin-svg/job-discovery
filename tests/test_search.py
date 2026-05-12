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

    # 5 boards × 2 locations = 10 calls
    assert len(calls) == 10
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
