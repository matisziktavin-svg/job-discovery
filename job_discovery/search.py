"""JobSpy wrapper: fetch from 5 boards, normalize, dedupe.

Per-board failures are isolated. The orchestrator (cli.scan) is responsible
for logging which boards succeeded.
"""
import logging
import math
from typing import Iterable

logger = logging.getLogger(__name__)


def _is_real_number(v) -> bool:
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

# Higher-quality sources first — used by dedupe() to pick a winner when the
# same job appears on multiple boards.
SOURCE_QUALITY_ORDER = ["linkedin", "indeed", "glassdoor", "google", "zip_recruiter"]
ALL_BOARDS = list(SOURCE_QUALITY_ORDER)


def normalize_listing(raw: dict) -> dict:
    """Map a JobSpy row (or any board's raw output) to our match schema."""
    salary = ""
    mn = raw.get("min_amount")
    mx = raw.get("max_amount")
    if _is_real_number(mn) and _is_real_number(mx):
        salary = f"${int(mn) // 1000}K-${int(mx) // 1000}K"
    elif _is_real_number(mn):
        salary = f"${int(mn) // 1000}K+"

    posted = raw.get("date_posted")
    if posted is not None and not isinstance(posted, str):
        # JobSpy may return a datetime or pandas Timestamp
        try:
            posted = posted.strftime("%Y-%m-%d")
        except AttributeError:
            posted = str(posted)

    return {
        "title": (raw.get("title") or "").strip(),
        "company": (raw.get("company") or "").strip(),
        "location": (raw.get("location") or "").strip(),
        "url": raw.get("job_url") or raw.get("url") or "",
        "salary": salary,
        "posted_date": posted or "",
        "source": (raw.get("site") or "").lower(),
        "description": raw.get("description") or "",
    }


def dedupe_key(listing: dict) -> str:
    """Normalized key for deduping the same job across boards."""
    return "|".join([
        (listing.get("company") or "").strip().lower(),
        (listing.get("title") or "").strip().lower(),
        (listing.get("location") or "").strip().lower(),
    ])


def _source_rank(source: str) -> int:
    try:
        return SOURCE_QUALITY_ORDER.index(source.lower())
    except ValueError:
        return len(SOURCE_QUALITY_ORDER)  # unknown source ranks last


def dedupe(listings: list[dict]) -> list[dict]:
    """Collapse duplicates across boards. For each dedupe key, keep the
    listing from the highest-quality source.
    """
    by_key: dict[str, dict] = {}
    for it in listings:
        k = dedupe_key(it)
        existing = by_key.get(k)
        if existing is None or _source_rank(it["source"]) < _source_rank(existing["source"]):
            by_key[k] = it
    return list(by_key.values())


def filter_unseen(listings: list[dict], seen_keys: Iterable[str]) -> list[dict]:
    """Drop listings whose dedupe_key is in `seen_keys`."""
    seen = set(seen_keys)
    return [it for it in listings if dedupe_key(it) not in seen]


def fetch_all(criteria: dict, results_per_board: int = 50) -> tuple[list[dict], dict[str, str]]:
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

    out: list[dict] = []
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
