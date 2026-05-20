"""One-off backfill: re-scrape today, record all surviving dedupe keys to
`job_scored_history.json` so the next normal scan doesn't pay to re-score
listings that the prior scan already evaluated. Zero LLM calls — pure
JobSpy + hard-gate filter + state write.

Usage:
    VAULT_PATH="..." python scripts/backfill_scored_cache.py
"""
import datetime as dt
import json
import logging
import sys

from job_discovery import cli, search, state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("backfill")


def main() -> int:
    criteria = state.read_criteria()
    if not criteria["roles"]:
        print("criteria.md empty — aborting", file=sys.stderr)
        return 1

    today = dt.date.today().isoformat()
    logger.info(
        "backfill: scraping (roles=%d, locations=%d) — no LLM calls",
        len(criteria["roles"]), len(criteria["locations"]),
    )

    raw, board_status = search.fetch_all(criteria)
    logger.info("backfill: fetched %d listings post cross-board dedupe", len(raw))

    gated = cli._apply_hard_gates(raw, criteria)
    logger.info("backfill: %d after hard gates", len(gated))

    keys = sorted({search.dedupe_key(lst) for lst in gated})
    state.append_scored_keys(keys, today)

    cache = state.load_scored_history()
    failed_boards = [k for k, v in board_status.items() if v.startswith("error")]
    print(json.dumps({
        "backfilled_keys": len(keys),
        "scored_history_size_after": len(cache),
        "failed_boards": failed_boards,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
