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
