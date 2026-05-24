"""JobSpy wrapper: fetch from 5 boards, normalize, dedupe.

Per-board failures are isolated. The orchestrator (cli.scan) is responsible
for logging which boards succeeded.
"""
import logging
import math
import re
from typing import Any, Iterable, Mapping

from job_discovery.types import Criteria, Listing

logger = logging.getLogger(__name__)


def _is_real_number(v: Any) -> bool:
    """True iff `v` is a number that's safely convertible to int.
    Filters None and pandas/numpy NaN (which is a float, so `is not None` is
    True but `int(nan)` raises ValueError).
    """
    if v is None:
        return False
    try:
        if isinstance(v, float) and math.isnan(v):
            return False
    except (TypeError, ValueError):
        return False
    return True


def _safe_str(v: Any) -> str:
    """Coerce a JobSpy field to a clean string. None and NaN become "".
    Non-string scalars are str()'d. Bug C regression guard: pandas returns
    NaN (a truthy float) for missing string columns, so the naive
    `(v or "").strip()` pattern crashed `.strip()` on float, and the
    `str(NaN)` fallback for `date_posted` produced the literal string "nan"
    which then crashed `int()` in cli._select_top_n's sort key.
    """
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    if isinstance(v, str):
        return v
    try:
        return str(v)
    except Exception:
        return ""

# Higher-quality sources first — used by dedupe() to pick a winner when the
# same job appears on multiple boards.
SOURCE_QUALITY_ORDER = ["linkedin", "indeed", "glassdoor", "google", "zip_recruiter"]

# Upstream JobSpy scrapers broken: Glassdoor's location lookup returns 400
# (speedyapply/JobSpy#279) and ZipRecruiter is Cloudflare-blocked with stale
# hardcoded device credentials (speedyapply/JobSpy#321). No working upstream
# fix on PyPI 1.1.82. Re-enable a board by removing it from DISABLED_BOARDS
# once upstream ships a fix (or we patch locally).
DISABLED_BOARDS = {"glassdoor", "zip_recruiter"}
ALL_BOARDS = [b for b in SOURCE_QUALITY_ORDER if b not in DISABLED_BOARDS]


def normalize_listing(raw: dict[str, Any]) -> Listing:
    """Map a JobSpy row (or any board's raw output) to our match schema."""
    salary = ""
    mn = raw.get("min_amount")
    mx = raw.get("max_amount")
    if _is_real_number(mn) and _is_real_number(mx):
        salary = f"${int(mn) // 1000}K-${int(mx) // 1000}K"  # type: ignore[arg-type]
    elif _is_real_number(mn):
        salary = f"${int(mn) // 1000}K+"  # type: ignore[arg-type]

    posted_raw = raw.get("date_posted")
    if posted_raw is None or (isinstance(posted_raw, float) and math.isnan(posted_raw)):
        posted = ""
    elif isinstance(posted_raw, str):
        posted = posted_raw
    else:
        # JobSpy may return a datetime or pandas Timestamp
        try:
            posted = posted_raw.strftime("%Y-%m-%d")
        except (AttributeError, TypeError, ValueError):
            # Never fall back to str() — that turns NaN-likes into the literal
            # string "nan" which then poisons downstream int() conversions.
            posted = ""

    return {
        "title": _safe_str(raw.get("title")).strip(),
        "company": _safe_str(raw.get("company")).strip(),
        "location": _safe_str(raw.get("location")).strip(),
        "url": _safe_str(raw.get("job_url")) or _safe_str(raw.get("url")),
        "salary": salary,
        "posted_date": posted,
        "source": _safe_str(raw.get("site")).lower(),
        "description": _safe_str(raw.get("description")),
    }


# Trailing country segment Indeed appends to US locations ("Berkeley, MO, US")
# but LinkedIn omits ("Berkeley, MO"). Without stripping, the same posting from
# two boards produced two different dedupe keys — see the 5/16-5/17 Boeing
# Berkeley dupe (jm_ca2f7f55 vs jm_7e2fc190).
_TRAILING_COUNTRY_RE = re.compile(
    r",\s*(?:us|usa|u\.s\.a?\.?|united states)\s*$", re.IGNORECASE,
)
_COMMA_SPACING_RE = re.compile(r"\s*,\s*")
_MULTISPACE_RE = re.compile(r"\s+")


def _normalize_location(loc: str | None) -> str:
    s = (loc or "").strip().lower()
    s = _TRAILING_COUNTRY_RE.sub("", s)
    s = _COMMA_SPACING_RE.sub(", ", s)
    s = _MULTISPACE_RE.sub(" ", s)
    return s.strip()


def dedupe_key(listing: Mapping[str, Any]) -> str:
    """Normalized key for deduping the same job across boards.

    Accepts either a `Listing` (pre-scoring, from search) or a `Match`
    (post-scoring, from state) — both expose the company/title/location
    fields this key needs.
    """
    return "|".join([
        (listing.get("company") or "").strip().lower(),
        (listing.get("title") or "").strip().lower(),
        _normalize_location(listing.get("location")),
    ])


def _source_rank(source: str) -> int:
    try:
        return SOURCE_QUALITY_ORDER.index(source.lower())
    except ValueError:
        return len(SOURCE_QUALITY_ORDER)  # unknown source ranks last


def dedupe(listings: list[Listing]) -> list[Listing]:
    """Collapse duplicates across boards. For each dedupe key, keep the
    listing from the highest-quality source.
    """
    by_key: dict[str, Listing] = {}
    for it in listings:
        k = dedupe_key(it)
        existing = by_key.get(k)
        if existing is None or _source_rank(it["source"]) < _source_rank(existing["source"]):
            by_key[k] = it
    return list(by_key.values())


def filter_unseen(listings: list[Listing], seen_keys: Iterable[str]) -> list[Listing]:
    """Drop listings whose dedupe_key is in `seen_keys`."""
    seen = set(seen_keys)
    return [it for it in listings if dedupe_key(it) not in seen]


def fetch_all(
    criteria: Criteria, results_per_board: int = 50,
) -> tuple[list[Listing], dict[str, str]]:
    """Run JobSpy against each (board, location) pair with criteria-derived
    params. Returns:
        (listings, board_status)
    where board_status maps "<board>@<location>" -> "ok" or error message.

    JobSpy takes one location per call, so we loop over locations × boards.
    Per-pair errors are caught and logged — the call always returns whatever
    succeeded plus the status map. Caller logs partial-success in the brief.
    """
    from jobspy import scrape_jobs  # local import — heavy module

    search_terms = " OR ".join(f'"{r}"' for r in criteria.get("roles", []) if r)
    locations = criteria.get("locations", []) or [""]

    # Cap per-pair results so total fetched is reasonable across all locations.
    # 50 results per board × 5 boards × 4 locations = 1000 candidates; tighten
    # when we have many locations to keep the daily fetch bounded.
    per_pair = max(10, results_per_board // max(1, len(locations)))

    out: list[Listing] = []
    status: dict[str, str] = {}
    for location in locations:
        for board in ALL_BOARDS:
            key = f"{board}@{location}" if location else board
            try:
                df = scrape_jobs(
                    site_name=[board],
                    search_term=search_terms or None,
                    location=location,
                    results_wanted=per_pair,
                    hours_old=72,  # only postings from the last 3 days
                    country_indeed="USA",
                )
                if df is None or df.empty:
                    status[key] = "ok (0 results)"
                    continue
                # Per-listing isolation: a single bad row (e.g. unexpected
                # types in a JobSpy field) must not drop the whole board's
                # batch. The outer try/except handles fetch-level failures;
                # this inner try/except handles per-row normalization.
                bad_rows = 0
                for _, row in df.iterrows():
                    try:
                        out.append(normalize_listing(row.to_dict()))
                    except Exception:
                        bad_rows += 1
                        logger.exception(
                            "search.fetch_all: %s skipping malformed row", key,
                        )
                status[key] = "ok" if bad_rows == 0 else f"ok ({bad_rows} bad rows skipped)"
            except Exception as e:
                logger.exception("search.fetch_all: %s failed", key)
                status[key] = f"error: {type(e).__name__}: {e}"

    return dedupe(out), status
