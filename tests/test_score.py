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


def test_assemble_scoring_user_prompt_includes_only_listing():
    """Post-refactor: the user prompt carries ONLY the per-listing payload.
    Static context (criteria, preferences, profile) lives in the system
    prompt so it hits the prompt cache."""
    listing = {
        "title": "Mech Eng", "company": "Acme", "location": "Chicago",
        "description": "Design things.", "salary": "$80K",
    }
    prompt = score._assemble_user_prompt(listing)
    assert "Mech Eng" in prompt
    assert "Acme" in prompt
    assert "Design things" in prompt


def test_assemble_batch_user_prompt_carries_all_listings_in_array():
    """Batch path: the user prompt carries a JSON array of N listings.
    The cached system prompt covers profile/criteria/preferences as before."""
    listings = [
        {"title": "Mech Eng", "company": "Acme", "location": "Chicago",
         "description": "Design A.", "salary": "$80K"},
        {"title": "Thermal Eng", "company": "Beta", "location": "Denver",
         "description": "Design B.", "salary": "$90K"},
        {"title": "Sales Eng", "company": "Gamma", "location": "Phoenix",
         "description": "Sell.", "salary": "$50K"},
    ]
    prompt = score._assemble_batch_user_prompt(listings)
    # All three companies and descriptions must appear
    for company in ("Acme", "Beta", "Gamma"):
        assert company in prompt
    for desc in ("Design A.", "Design B.", "Sell."):
        assert desc in prompt
    # The prompt must signal array shape (so the LLM returns an array, not
    # a single object). Look for ordinal/array signal in the instructions.
    lower = prompt.lower()
    assert "array" in lower or "list" in lower


def test_assemble_batch_user_prompt_truncates_long_descriptions():
    """Same 4000-char truncation per item as single-listing path."""
    listings = [
        {"title": "Mech Eng", "company": "Acme", "location": "Chicago",
         "description": "x" * 5000, "salary": "$80K"},
    ]
    prompt = score._assemble_batch_user_prompt(listings)
    # The 5000-char body must be cut to 4000 — check by counting x's
    assert prompt.count("x") == 4000


def test_assemble_batch_user_prompt_empty_list_is_safe():
    """Edge: callers shouldn't pass [], but if they do, we should produce
    a syntactically-valid prompt rather than crashing."""
    prompt = score._assemble_batch_user_prompt([])
    # No crash; prompt is some string with an empty JSON array in it
    assert "[]" in prompt


def test_assemble_scoring_system_prompt_includes_context():
    """The system prompt carries criteria + preferences + profile so the
    cached prefix covers the parts that don't change per listing."""
    criteria = {"roles": ["Mech Eng"], "locations": ["Chicago, IL"], "weights": {}}
    preferences = {
        "learned_patterns": "Skip defense",
        "recent_pass_reasons": [{"date": "2026-05-10", "text": "too senior"}],
    }
    profile_blob = "Tavin: aerospace eng, mid-IC."
    system_prompt = score._assemble_system_prompt(
        "BASE_RULES_HERE", criteria, preferences, profile_blob,
    )
    assert "BASE_RULES_HERE" in system_prompt
    assert "Skip defense" in system_prompt
    assert "too senior" in system_prompt
    assert "aerospace eng" in system_prompt


def test_rule_score_respects_title_exclusions():
    """Bug A wiring: even rule-based fallback must give role_fit=1 to
    titles matching criteria.title_exclusions."""
    criteria_with_exclusions = {**CRITERIA_AERO, "title_exclusions": ["Senior", "Manager"]}
    listing = {
        "title": "Senior Mechanical Engineer",
        "company": "Boeing",
        "location": "Chicago, IL",
        "salary": "$120K-$160K",
        "description": "Hands-on mechanical design for aerospace propulsion.",
    }
    result = score.score_rule_based(listing, criteria_with_exclusions)
    assert result["dims"]["role_fit"] == 1


def test_assemble_scoring_system_prompt_includes_title_exclusions():
    """Bug A wiring: title_exclusions must reach the LLM scorer so it can
    enforce role_fit=1 for excluded titles. Post-refactor these live in
    the system prompt (cached), not the per-listing user prompt."""
    criteria = {
        "roles": ["Mech Eng"],
        "title_exclusions": ["Senior", "Manager"],
        "locations": ["Chicago, IL"],
        "weights": {},
    }
    system_prompt = score._assemble_system_prompt("BASE", criteria, {}, "")
    assert '"title_exclusions"' in system_prompt
    assert '"Senior"' in system_prompt
    assert '"Manager"' in system_prompt


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


# -----------------------------------------------------------------------------
# Batch response parsing: array in → list of (ScoreResult|None) out
# -----------------------------------------------------------------------------


_EQ_WEIGHTS = {"role_fit": 1, "skills_match": 1, "seniority": 1,
               "domain": 1, "location": 1, "responsibilities": 1}


def _good_score_obj(role_fit=5):
    return {
        "dims": {"role_fit": role_fit, "skills_match": 4, "seniority": 4,
                 "domain": 4, "location": 4, "responsibilities": 4},
        "one_line_take": "ok",
    }


def test_parse_batch_response_aligns_length_and_order():
    """Parser returns a list of length n in input order."""
    import json as _json
    raw = _json.dumps([
        _good_score_obj(role_fit=5),
        _good_score_obj(role_fit=2),
        _good_score_obj(role_fit=3),
    ])
    out = score._parse_batch_score_response(raw, _EQ_WEIGHTS, n=3)
    assert isinstance(out, list)
    assert len(out) == 3
    assert out[0]["dims"]["role_fit"] == 5  # type: ignore[index]
    assert out[1]["dims"]["role_fit"] == 2  # type: ignore[index]
    assert out[2]["dims"]["role_fit"] == 3  # type: ignore[index]
    # method tagged as llm
    for item in out:
        assert item is not None
        assert item["method"] == "llm"


def test_parse_batch_response_handles_markdown_fence():
    """Same fence-stripping as the single response parser."""
    import json as _json
    raw = "```json\n" + _json.dumps([_good_score_obj()]) + "\n```"
    out = score._parse_batch_score_response(raw, _EQ_WEIGHTS, n=1)
    assert len(out) == 1
    assert out[0] is not None


def test_parse_batch_response_per_item_failure_returns_none_only_for_bad_item():
    """One malformed item in the array must NOT take down the whole batch.
    Aligned positions remain None; the rest parse normally."""
    import json as _json
    raw = _json.dumps([
        _good_score_obj(role_fit=5),
        {"dims": {"role_fit": 99}},  # out of range — should None
        _good_score_obj(role_fit=3),
    ])
    out = score._parse_batch_score_response(raw, _EQ_WEIGHTS, n=3)
    assert len(out) == 3
    assert out[0] is not None
    assert out[1] is None
    assert out[2] is not None


def test_parse_batch_response_length_mismatch_returns_all_none():
    """If the array length doesn't match n, treat the whole batch as
    untrusted — caller falls back to rule-based for every position."""
    import json as _json
    raw = _json.dumps([_good_score_obj(), _good_score_obj()])  # 2 != 3
    out = score._parse_batch_score_response(raw, _EQ_WEIGHTS, n=3)
    assert out == [None, None, None]


def test_parse_batch_response_not_json_returns_all_none():
    out = score._parse_batch_score_response("not json", _EQ_WEIGHTS, n=2)
    assert out == [None, None]


def test_parse_batch_response_empty_string_returns_all_none():
    """The 'You've hit your limit' rate-limit text comes through as plain
    English. Must collapse to all-None so callers fall back cleanly."""
    out = score._parse_batch_score_response("", _EQ_WEIGHTS, n=3)
    assert out == [None, None, None]
    rate_limit_text = "You've hit your limit — resets 8:10am"
    out2 = score._parse_batch_score_response(rate_limit_text, _EQ_WEIGHTS, n=3)
    assert out2 == [None, None, None]


def test_parse_batch_response_object_not_array_returns_all_none():
    """If the model returns a single object instead of an array (LLM
    confusion or prompt drift), don't try to salvage — fall back."""
    import json as _json
    raw = _json.dumps(_good_score_obj())  # not an array
    out = score._parse_batch_score_response(raw, _EQ_WEIGHTS, n=2)
    assert out == [None, None]


# -----------------------------------------------------------------------------
# Salary handling: missing salary stays, below-floor gets soft penalty
# -----------------------------------------------------------------------------


def test_extract_salary_min_handles_common_formats():
    assert score._extract_salary_min("$70K-$90K") == 70000
    assert score._extract_salary_min("$70K+") == 70000
    assert score._extract_salary_min("$120K-$160K") == 120000
    assert score._extract_salary_min("") is None
    assert score._extract_salary_min(None) is None
    assert score._extract_salary_min("competitive") is None


def _mk_score_result(overall=4.0, take="strong fit"):
    return {
        "overall": overall,
        "dims": {"role_fit": 4, "skills_match": 4, "seniority": 4,
                 "domain": 4, "location": 4, "responsibilities": 4},
        "one_line_take": take,
        "method": "llm",
    }


def test_apply_salary_penalty_no_floor_no_change():
    """No salary_floor in criteria → no adjustment."""
    listing = {"salary": "$50K"}
    criteria = {}  # no salary_floor
    result = score.apply_salary_penalty(_mk_score_result(4.0), listing, criteria)
    assert result["overall"] == 4.0
    assert "below floor" not in result["one_line_take"]


def test_apply_salary_penalty_no_listing_salary_flagged_not_penalized():
    """Missing salary in listing → flag in one_line_take, no overall change."""
    listing = {"salary": ""}
    criteria = {"salary_floor": 60000}
    result = score.apply_salary_penalty(_mk_score_result(4.0), listing, criteria)
    assert result["overall"] == 4.0
    assert "salary not posted" in result["one_line_take"].lower()


def test_apply_salary_penalty_above_floor_no_change():
    """Listing salary >= floor → no penalty, no flag."""
    listing = {"salary": "$80K-$100K"}
    criteria = {"salary_floor": 60000}
    result = score.apply_salary_penalty(_mk_score_result(4.0), listing, criteria)
    assert result["overall"] == 4.0
    assert "below floor" not in result["one_line_take"]


def test_apply_salary_penalty_below_floor_soft_penalty():
    """Listing salary < floor → reduce overall by 0.5, append 'below floor' note."""
    listing = {"salary": "$50K-$55K"}
    criteria = {"salary_floor": 60000}
    result = score.apply_salary_penalty(_mk_score_result(4.0), listing, criteria)
    assert result["overall"] == 3.5
    assert "below floor" in result["one_line_take"].lower()
    assert "$50K" in result["one_line_take"] or "50" in result["one_line_take"]


def test_apply_salary_penalty_clamps_at_1():
    """Penalty floors overall at 1.0 even if pre-penalty is already low."""
    listing = {"salary": "$30K"}
    criteria = {"salary_floor": 60000}
    result = score.apply_salary_penalty(_mk_score_result(1.2), listing, criteria)
    assert result["overall"] == 1.0


def test_apply_salary_penalty_does_not_mutate_input():
    """apply_salary_penalty returns a new dict — input unchanged."""
    listing = {"salary": "$50K"}
    criteria = {"salary_floor": 60000}
    original = _mk_score_result(4.0)
    score.apply_salary_penalty(original, listing, criteria)
    assert original["overall"] == 4.0
    assert "below floor" not in original["one_line_take"]


# -----------------------------------------------------------------------------
# Unverified-JD handling: high blind score we could not verify gets soft-
# downranked + flagged (mirrors the salary penalty contract).
# -----------------------------------------------------------------------------


def test_apply_unverified_penalty_reduces_overall_and_flags():
    result = score.apply_unverified_penalty(_mk_score_result(4.3))
    assert result["overall"] == 3.8
    assert "unverified" in result["one_line_take"].lower()


def test_apply_unverified_penalty_clamps_at_1():
    result = score.apply_unverified_penalty(_mk_score_result(1.2))
    assert result["overall"] == 1.0


def test_apply_unverified_penalty_flag_not_double_appended():
    once = score.apply_unverified_penalty(_mk_score_result(4.3))
    twice = score.apply_unverified_penalty(once)
    assert twice["one_line_take"].lower().count("unverified") == 1


def test_apply_unverified_penalty_does_not_mutate_input():
    original = _mk_score_result(4.3)
    score.apply_unverified_penalty(original)
    assert original["overall"] == 4.3
    assert "unverified" not in original["one_line_take"].lower()


# -----------------------------------------------------------------------------
# Required-years extraction from JD text
# -----------------------------------------------------------------------------


def test_extract_required_years_single_number():
    """'3 years experience' / '3 yrs experience' → (3, 3)."""
    assert score.extract_required_years("Requires 3 years experience.") == (3, 3)
    assert score.extract_required_years("3 yrs experience required.") == (3, 3)
    assert score.extract_required_years("5 years of relevant experience") == (5, 5)


def test_extract_required_years_range():
    """'3-7 years' / '3 to 7 years' → (3, 7)."""
    assert score.extract_required_years("3-7 years of experience") == (3, 7)
    assert score.extract_required_years("3 to 7 years experience") == (3, 7)
    assert score.extract_required_years("3–7 yrs experience") == (3, 7)  # en-dash


def test_extract_required_years_plus():
    """'5+ years' → (5, None) — lower bound only, unbounded above."""
    assert score.extract_required_years("5+ years experience") == (5, None)
    assert score.extract_required_years("10+ years of experience") == (10, None)


def test_extract_required_years_minimum():
    """'minimum 2 years' / 'at least 5 years' → (N, None)."""
    assert score.extract_required_years("minimum 2 years experience") == (2, None)
    assert score.extract_required_years("at least 5 years of experience") == (5, None)
    assert score.extract_required_years("min 3 yrs") == (3, None)


def test_extract_required_years_takes_largest_max_when_multiple():
    """If a JD has multiple year phrases, take the highest signal —
    e.g., '2 years required, 5+ preferred' → effective max is 5+."""
    text = "Requires 2 years experience. 5+ years strongly preferred."
    lo, hi = score.extract_required_years(text)
    # The "5+" phrase is the more demanding bar — pick that as the effective
    assert lo == 5
    assert hi is None  # "5+" leaves hi unbounded


def test_extract_required_years_no_match_returns_none():
    assert score.extract_required_years("") == (None, None)
    assert score.extract_required_years("entry-level position") == (None, None)
    assert score.extract_required_years(
        "Looking for someone curious and motivated."
    ) == (None, None)


def test_extract_required_years_ignores_unrelated_year_numbers():
    """'30+ years in business' / 'founded in 2014' must NOT register
    as a years-of-experience requirement."""
    assert score.extract_required_years(
        "Therm-X has been delivering for 30+ years."
    ) == (None, None)
    assert score.extract_required_years(
        "Founded in 2014, the company has grown..."
    ) == (None, None)


# -----------------------------------------------------------------------------
# Experience penalty — the main fix for the years/domain scoring bug.
# Named regression cases: Auriga (hard filter), Burns (soft + domain mismatch),
# United Safety (no penalty).
# -----------------------------------------------------------------------------


CRITERIA_WITH_EXPERIENCE = {
    **CRITERIA_AERO,
    "experience": {
        "years_total": 2,
        "domains": ["aerospace", "thermal", "cryogenic",
                    "mechanical_design", "test_operations"],
        "hard_filter_years_above": 3,
    },
}


def test_apply_experience_penalty_no_profile_no_change():
    """Without an experience profile in criteria, the penalty is a no-op."""
    listing = {"title": "x", "description": "3 years required."}
    result = score.apply_experience_penalty(_mk_score_result(4.0), listing, {})
    assert result["overall"] == 4.0


def test_apply_experience_penalty_no_years_in_jd_no_change():
    """JD with no years phrase → no penalty (we can't punish what we can't see)."""
    listing = {"title": "Mech Eng", "description": "Hands-on design work."}
    result = score.apply_experience_penalty(
        _mk_score_result(4.0), listing, CRITERIA_WITH_EXPERIENCE,
    )
    assert result["overall"] == 4.0


def test_apply_experience_penalty_auriga_3_to_7_years_hard_filter():
    """Regression: Auriga Space (5/21) — '3-7 years experience' must be
    hard-filtered out of the brief. Max=7 > years_total(2) + buffer(3) = 5."""
    listing = {
        "title": "Mechanical Design Engineer",
        "company": "Auriga Space",
        "description": (
            "Aerospace startup building electromagnetic launch hardware. "
            "Requires 3-7 years of experience in mechanical design."
        ),
    }
    result = score.apply_experience_penalty(
        _mk_score_result(4.3), listing, CRITERIA_WITH_EXPERIENCE,
    )
    assert result["overall"] == 1.0  # Pushed to floor — hard filter
    assert "hard-filter" in result["one_line_take"].lower()
    assert "7" in result["one_line_take"]  # Cite the offending years


def test_apply_experience_penalty_burns_3_years_power_soft_with_domain_mismatch():
    """Regression: Burns & McDonnell (5/25) — '3 years experience in power'
    must soft-penalize. 3 yrs alone wouldn't kill it, but the POWER domain
    isn't in Tavin's profile, so the penalty steps up.
    """
    listing = {
        "title": "Mechanical Engineer - Power",
        "company": "Burns & McDonnell",
        "description": (
            "Designing mechanical systems for power generation projects. "
            "Requires 3 years experience in power, multi-discipline team."
        ),
    }
    pre = _mk_score_result(4.5)
    result = score.apply_experience_penalty(
        pre, listing, CRITERIA_WITH_EXPERIENCE,
    )
    # Hard filter should NOT fire (3 not > 5). Soft penalty should reduce
    # seniority dim and lower overall.
    assert 1.0 < result["overall"] < pre["overall"]
    assert result["dims"]["seniority"] < pre["dims"]["seniority"]
    # Domain mismatch flag should mention the years requirement and that
    # it falls outside Tavin's domains (power isn't in his profile).
    take = result["one_line_take"].lower()
    assert "yrs" in take or "years" in take
    assert "non-domain" in take


def test_apply_experience_penalty_burns_harder_than_aerospace_equivalent():
    """A 3-yr power role should be hit HARDER than a 3-yr aerospace role
    (Tavin has aerospace experience, not power). Captures the domain
    multiplier behavior Tavin asked for."""
    power_listing = {
        "title": "Mechanical Engineer",
        "company": "Burns",
        "description": "Requires 3 years experience in power generation.",
    }
    aero_listing = {
        "title": "Mechanical Engineer",
        "company": "Aero Co",
        "description": "Requires 3 years experience in aerospace propulsion.",
    }
    power_result = score.apply_experience_penalty(
        _mk_score_result(4.5), power_listing, CRITERIA_WITH_EXPERIENCE,
    )
    aero_result = score.apply_experience_penalty(
        _mk_score_result(4.5), aero_listing, CRITERIA_WITH_EXPERIENCE,
    )
    # The power listing should be penalized MORE than the aerospace one
    assert power_result["overall"] < aero_result["overall"]


def test_apply_experience_penalty_united_safety_min_2_no_penalty():
    """Regression: United Safety Level 1 — 'minimum 2 years' is exactly
    Tavin's years_total, no penalty should fire."""
    listing = {
        "title": "Level 1 Design Engineer",
        "company": "United Safety",
        "description": "Minimum 2 years experience or equivalent combination.",
    }
    pre = _mk_score_result(4.1)
    result = score.apply_experience_penalty(
        pre, listing, CRITERIA_WITH_EXPERIENCE,
    )
    assert result["overall"] == pre["overall"]
    assert result["dims"]["seniority"] == pre["dims"]["seniority"]


def test_apply_experience_penalty_does_not_mutate_input():
    """Same contract as the other penalties: return a new dict."""
    listing = {"description": "3-7 years experience"}
    original = _mk_score_result(4.5)
    score.apply_experience_penalty(original, listing, CRITERIA_WITH_EXPERIENCE)
    assert original["overall"] == 4.5
    assert "hard-filter" not in original["one_line_take"].lower()


def test_apply_experience_penalty_clamps_at_1():
    """Penalty must not produce overall < 1.0 even if pre-penalty is low."""
    listing = {"description": "10+ years experience required."}
    result = score.apply_experience_penalty(
        _mk_score_result(1.5), listing, CRITERIA_WITH_EXPERIENCE,
    )
    assert result["overall"] >= 1.0


# -----------------------------------------------------------------------------
# Integration: score_listings_batch must hard-filter pre-LLM and
# soft-penalize post-LLM. Hard-filtered listings should never reach the
# fake LLM runner.
# -----------------------------------------------------------------------------


def test_score_listings_batch_hard_filters_before_llm(monkeypatch, tmp_path):
    """A listing that triggers experience hard-filter must not be sent
    to the LLM. Saves daily Sonnet/Haiku budget on doomed listings."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    listings = [
        # Rule-based plausible (mech eng + chicago) but hard-filtered on years
        {
            "title": "Mechanical Engineer",
            "company": "AurigaLike",
            "location": "Chicago, IL",
            "salary": "$90K",
            "description": "Requires 3-7 years of experience in mechanical design.",
        },
        # Clean strong listing — should reach LLM
        _strong_listing("StrongA"),
    ]
    received = {"listings": None}

    def fake_llm(plausible, weights, *, system_prompt_path, model, batch_size):
        received["listings"] = list(plausible)
        return [_llm_payload(role_fit=5) for _ in plausible]

    out = score.score_listings_batch(
        listings, CRITERIA_WITH_EXPERIENCE, {}, "tavin-profile",
        _llm_score_fn=fake_llm,
    )
    # The hard-filtered listing must NOT have been sent to LLM
    plausible_companies = [l["company"] for l in (received["listings"] or [])]
    assert "AurigaLike" not in plausible_companies
    assert "StrongA" in plausible_companies
    # Hard-filtered listing keeps fallback method with overall pushed low
    assert out[0]["method"] == "fallback"
    assert out[0]["overall"] == 1.0
    assert "hard-filter" in out[0]["one_line_take"].lower()


def test_score_listings_batch_soft_penalty_applied_after_llm(monkeypatch, tmp_path):
    """A 3-yr-power listing that LLM optimistically scores high must still
    get the soft penalty applied — domain-mismatch is enforced even when
    the LLM was generous."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    listings = [{
        "title": "Mechanical Engineer",
        "company": "BurnsLike",
        "location": "Chicago, IL",
        "salary": "$90K",
        "description": "Requires 3 years experience in power generation.",
    }]

    def generous_llm(plausible, weights, *, system_prompt_path, model, batch_size):
        # LLM rates it 4.5, seniority=4 (the bug we're closing)
        return [{
            "overall": 4.5,
            "dims": {"role_fit": 5, "skills_match": 4, "seniority": 4,
                     "domain": 4, "location": 5, "responsibilities": 4},
            "one_line_take": "looks great",
            "method": "llm",
        }]

    out = score.score_listings_batch(
        listings, CRITERIA_WITH_EXPERIENCE, {}, "tavin-profile",
        _llm_score_fn=generous_llm,
    )
    assert len(out) == 1
    # Soft penalty must have lowered the overall and the seniority dim
    assert out[0]["overall"] < 4.5
    assert out[0]["dims"]["seniority"] < 4


# -----------------------------------------------------------------------------
# score_listings_batch — sync facade orchestrating rule-based pre-filter +
# persistent-client batched LLM scoring with rule-based fallback for failures.
# -----------------------------------------------------------------------------


def _llm_payload(role_fit=4, take="llm-scored"):
    """A fully-formed LLM-shaped ScoreResult for use in fake runners."""
    return {
        "overall": float(role_fit),
        "dims": {"role_fit": role_fit, "skills_match": 4, "seniority": 4,
                 "domain": 4, "location": 4, "responsibilities": 4},
        "one_line_take": take,
        "method": "llm",
    }


def _strong_listing(company="Acme"):
    """Listing that scores well rule-based so it clears PRE_FILTER_THRESHOLD."""
    return {
        "title": "Mechanical Design Engineer",
        "company": company,
        "location": "Chicago, IL",
        "salary": "$80K-$100K",
        "description": "Hands-on aerospace design.",
    }


def _weak_listing(company="Junk"):
    """Listing that scores poorly rule-based (sales/sell triggers low role_fit
    and responsibilities) so it stays below PRE_FILTER_THRESHOLD."""
    return {
        "title": "Sales Engineer",
        "company": company,
        "location": "Phoenix, AZ",
        "salary": "$50K",
        "description": "Sell software.",
    }


def test_score_listings_batch_calls_llm_only_for_plausible(monkeypatch, tmp_path):
    """Listings whose rule-based score < PRE_FILTER_THRESHOLD must not reach
    the LLM. Plausible ones do — the LLM result wins for those positions."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    listings = [
        _strong_listing("StrongA"),
        _weak_listing("WeakB"),
        _strong_listing("StrongC"),
    ]
    received = {"listings": None}

    def fake_llm(plausible, weights, *, system_prompt_path, model, batch_size):
        received["listings"] = list(plausible)
        # Return one LLM result per plausible listing
        return [_llm_payload(role_fit=5, take=f"llm:{l['company']}")
                for l in plausible]

    out = score.score_listings_batch(
        listings, CRITERIA_AERO, {}, "tavin-profile",
        _llm_score_fn=fake_llm,
    )

    # Plausible listings are exactly StrongA and StrongC, not WeakB
    plausible_companies = [l["company"] for l in received["listings"]]
    assert plausible_companies == ["StrongA", "StrongC"]

    # Output length aligns with input
    assert len(out) == 3
    # Positions 0 and 2 got LLM scores; position 1 stayed rule-based
    assert out[0]["method"] == "llm"
    assert out[1]["method"] == "fallback"
    assert out[2]["method"] == "llm"
    assert out[0]["one_line_take"] == "llm:StrongA"
    assert out[2]["one_line_take"] == "llm:StrongC"


def test_score_listings_batch_all_below_threshold_skips_llm_entirely(
    monkeypatch, tmp_path,
):
    """If no listing clears the pre-filter, the LLM runner is never invoked."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    listings = [_weak_listing("WeakA"), _weak_listing("WeakB")]
    calls = {"n": 0}

    def fake_llm(*args, **kwargs):
        calls["n"] += 1
        return []  # would be a bug to ever reach this

    out = score.score_listings_batch(
        listings, CRITERIA_AERO, {}, "tavin-profile",
        _llm_score_fn=fake_llm,
    )
    assert calls["n"] == 0
    assert len(out) == 2
    assert all(r["method"] == "fallback" for r in out)


def test_score_listings_batch_llm_none_falls_back_to_rule(monkeypatch, tmp_path):
    """When the LLM runner returns None for a position, that listing's
    rule-based result must be used in its place (not None, not crash)."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    listings = [_strong_listing("StrongA"), _strong_listing("StrongB")]

    def fake_llm(plausible, weights, *, system_prompt_path, model, batch_size):
        # First one parses, second one didn't
        return [_llm_payload(role_fit=5, take="llm-good"), None]

    out = score.score_listings_batch(
        listings, CRITERIA_AERO, {}, "tavin-profile",
        _llm_score_fn=fake_llm,
    )
    assert len(out) == 2
    assert out[0]["method"] == "llm"
    assert out[0]["one_line_take"] == "llm-good"
    # Position 1 fell back to rule-based
    assert out[1]["method"] == "fallback"


def test_score_listings_batch_full_llm_failure_falls_back_for_all(
    monkeypatch, tmp_path,
):
    """If the LLM runner returns all-None (rate limit, batch crash), every
    plausible listing falls back to rule-based — the scan still produces
    usable output."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    listings = [_strong_listing("A"), _strong_listing("B"), _weak_listing("C")]

    def fake_llm(plausible, weights, *, system_prompt_path, model, batch_size):
        return [None] * len(plausible)

    out = score.score_listings_batch(
        listings, CRITERIA_AERO, {}, "tavin-profile",
        _llm_score_fn=fake_llm,
    )
    assert len(out) == 3
    # All three end up rule-based
    assert all(r["method"] == "fallback" for r in out)


def test_score_listings_batch_empty_input_returns_empty(monkeypatch, tmp_path):
    """Edge: empty input → empty output, no LLM call."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    calls = {"n": 0}

    def fake_llm(*args, **kwargs):
        calls["n"] += 1
        return []

    out = score.score_listings_batch(
        [], CRITERIA_AERO, {}, "",
        _llm_score_fn=fake_llm,
    )
    assert out == []
    assert calls["n"] == 0


def test_score_listings_batch_writes_system_prompt_when_not_provided(
    monkeypatch, tmp_path,
):
    """When system_prompt_path is None, the facade calls write_scoring_system_prompt
    once so the cached prefix is on disk before the LLM runner sees it."""
    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    listings = [_strong_listing()]
    seen = {"path": None}

    def fake_llm(plausible, weights, *, system_prompt_path, model, batch_size):
        seen["path"] = system_prompt_path
        return [_llm_payload()]

    score.score_listings_batch(
        listings, CRITERIA_AERO, {}, "tavin-profile",
        _llm_score_fn=fake_llm,
    )
    assert seen["path"] is not None
    assert seen["path"].exists()
