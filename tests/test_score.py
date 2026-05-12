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


def test_assemble_scoring_user_prompt_includes_title_exclusions():
    """Bug A wiring: title_exclusions must reach the LLM scorer so it can
    enforce role_fit=1 for excluded titles."""
    listing = {"title": "T", "company": "C", "location": "L",
               "description": "d", "salary": ""}
    criteria = {
        "roles": ["Mech Eng"],
        "title_exclusions": ["Senior", "Manager"],
        "locations": ["Chicago, IL"],
        "weights": {},
    }
    prompt = score._assemble_user_prompt(listing, criteria, {}, "")
    assert '"title_exclusions"' in prompt
    assert '"Senior"' in prompt
    assert '"Manager"' in prompt


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
