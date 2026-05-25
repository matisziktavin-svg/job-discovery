"""Shared TypedDict shapes for the job-discovery pipeline.

Annotation-only — runtime is plain `dict` everywhere, so existing dict
literals and `.get()` access remain valid. The types document the shape
contracts that previously lived in docstrings and made mypy give up.
"""
from typing import Literal, NotRequired, TypedDict


class Listing(TypedDict):
    """A normalized job posting from JobSpy (see `search.normalize_listing`).
    The same shape is reused for the listings the LLM scorer receives.
    """
    title: str
    company: str
    location: str
    url: str
    salary: str
    posted_date: str
    source: str
    description: str


class ExperienceProfile(TypedDict, total=False):
    """Structured representation of Tavin's years/domain experience —
    used by `apply_experience_penalty` to detect listings whose required
    years exceed his profile (hard filter) or whose required-years phrase
    sits in a domain context he lacks (soft penalty).

    Sourced from the `## Experience profile` section of criteria.md.
    All fields optional: missing section = penalty is a no-op.
    """
    years_total: int
    domains: list[str]
    hard_filter_years_above: int


class Criteria(TypedDict):
    """Parsed criteria.md — see `state.read_criteria`."""
    roles: list[str]
    locations: list[str]
    title_exclusions: list[str]
    salary_floor: int | None
    hard_gates: list[str]
    weights: dict[str, float]
    notes: str
    experience: NotRequired[ExperienceProfile]


class PassReason(TypedDict):
    date: str
    text: str


class Preferences(TypedDict):
    """Parsed preferences.md — see `state.read_preferences`."""
    learned_patterns: str
    recent_pass_reasons: list[PassReason]


class ScoreDims(TypedDict):
    role_fit: int
    skills_match: int
    seniority: int
    domain: int
    location: int
    responsibilities: int


class ScoreResult(TypedDict):
    """Output of `score.score_rule_based` / `score.score_llm` /
    `score.score_listing`. Salary penalty + unverified penalty preserve
    this shape (mutating `overall` and `one_line_take` only).
    """
    overall: float
    dims: ScoreDims
    one_line_take: str
    method: Literal["llm", "fallback"]


class MatchScore(TypedDict):
    """The nested `score` field on a Match — drops `one_line_take` since
    Match has it as a top-level field."""
    overall: float
    dims: ScoreDims
    method: Literal["llm", "fallback"]


class Match(TypedDict):
    """A scored, surfaced job match — the shape persisted in
    `.mizzix_state/job_matches.json` and `job_matches_history.json`.

    Action-conditional fields (`decoded`, `action_date`, `pass_reason`)
    are NotRequired since they're only set after the user records an
    action via the `record-action` CLI command.
    """
    id: str
    source: str
    title: str
    company: str
    location: str
    salary: str
    url: str
    posted_date: str
    surfaced_date: str
    score: MatchScore
    one_line_take: str
    status: Literal["surfaced", "applied", "passed"]
    times_carried: int
    last_brief_date: str
    decoded: NotRequired[bool]
    action_date: NotRequired[str]
    pass_reason: NotRequired[str]


class ScoredHistoryEntry(TypedDict):
    """One row in `.mizzix_state/job_scored_history.json` — the rolling
    cache that prevents re-scoring the same listing across nights."""
    key: str
    scored_date: str
