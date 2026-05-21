"""CLI entrypoints for job-discovery.

Commands:
  scan                                  — run the daily pipeline (cron + ad-hoc)
  score-one <url>                       — score a single posting
  list-active                           — print current job_matches.json
  record-action <id> <action> [opts]    — update one match's status
"""
import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from job_discovery import fetch_jd, score, search, state
from job_discovery.types import Criteria, Listing, Match

logger = logging.getLogger(__name__)


def _today_iso() -> str:
    return dt.date.today().isoformat()


def _load_profile_blob() -> str:
    """Read the parts of tavin.md + Job_Search/README.md that scoring needs."""
    vault = Path(os.environ["VAULT_PATH"])
    parts = []
    for rel in ("tavin.md", "projects/Job_Search/README.md"):
        p = vault / rel
        if p.exists():
            try:
                parts.append(f"=== {rel} ===\n{p.read_text(encoding='utf-8')}")
            except Exception:
                logger.exception("could not read %s", p)
    return "\n\n".join(parts)


def _apply_hard_gates(listings: list[Listing], criteria: Criteria) -> list[Listing]:
    """Drop any listing matching a hard gate. Currently supported:
        company:<name>     — exact company match (case-insensitive)

    Unrecognized gate prefixes are silently ignored at runtime but logged
    at WARNING so users notice their gates aren't enforced (Bug D regression
    guard — earlier impl was silent and Tavin had two prose gates that did
    nothing without any indication).
    """
    gates = criteria.get("hard_gates") or []
    if not gates:
        return list(listings)
    blocked_companies: set[str] = set()
    for g in gates:
        if g.lower().startswith("company:"):
            blocked_companies.add(g.split(":", 1)[1].strip().lower())
        else:
            logger.warning(
                "_apply_hard_gates: unsupported gate %r ignored. "
                "Supported prefixes: company:<name>. "
                "Move free-text rules to ## Notes (the LLM scorer reads them).",
                g,
            )
    return [
        item for item in listings
        if (item.get("company") or "").strip().lower() not in blocked_companies
    ]


def _posted_date_sort_key(posted_date: Any) -> int:
    """Convert 'YYYY-MM-DD' to YYYYMMDD as int for sort tiebreaking.
    Returns 0 for anything malformed/missing so a single bad value never
    crashes the sort (Bug C regression guard — a NaN-derived "nan" string
    from search.normalize_listing took the whole scan down at 3 AM).
    """
    if not isinstance(posted_date, str):
        return 0
    s = posted_date.replace("-", "")
    try:
        return int(s)
    except (ValueError, TypeError):
        return 0


def _select_top_n(
    scored: list[Match], n: int = 5, threshold: float = 3.0,
) -> list[Match]:
    """Sort by overall score descending, drop anything below threshold,
    cap at N. Ties broken by posted_date desc, then id asc for stability."""
    qualified = [m for m in scored if m["score"]["overall"] >= threshold]
    qualified.sort(key=lambda m: (
        -m["score"]["overall"],
        -_posted_date_sort_key(m.get("posted_date", "")),
        m.get("id", ""),
    ))
    return qualified[:n]


def _merge_with_carryforward(
    new_matches: list[Match], today_iso: str,
) -> list[Match]:
    """Merge freshly scored matches into the existing job_matches.json,
    incrementing times_carried for items already present."""
    existing = state.load_matches()
    new_ids = {m["id"] for m in new_matches}
    out: list[Match] = []
    for old in existing:
        if old.get("status") != "surfaced":
            continue
        if old["id"] in new_ids:
            # New scoring overrides — drop the old version (the new one will
            # replace it via the loop below)
            continue
        old["times_carried"] = (old.get("times_carried") or 0) + 1
        old["last_brief_date"] = today_iso
        out.append(old)
    for m in new_matches:
        m["last_brief_date"] = today_iso
        out.append(m)
    return out


def cmd_scan(args: argparse.Namespace) -> int:
    criteria = state.read_criteria()
    if not criteria["roles"]:
        print(
            "criteria.md is empty or missing — run the onboarding interview "
            "via Mizzix (skill: job-discovery, command: onboard).",
            file=sys.stderr,
        )
        return 1

    preferences = state.read_preferences()
    profile_blob = _load_profile_blob()

    # Write the combined system prompt (rules + profile + criteria) ONCE so
    # all per-listing LLM calls reference the same stable file path and
    # benefit from Claude Code's prompt cache. Without this, the prefix is
    # rebuilt and resent per call, burning the daily Sonnet window in <2h.
    scoring_system_prompt_path = score.write_scoring_system_prompt(
        criteria, preferences, profile_blob,
    )

    today = _today_iso()
    logger.info("scan: starting (criteria roles=%d, locations=%d)",
                len(criteria["roles"]), len(criteria["locations"]))

    raw, board_status = search.fetch_all(criteria)
    logger.info("scan: fetched %d listings; board_status=%s", len(raw), board_status)
    if all(s.startswith("error") for s in board_status.values()):
        # All boards failed — preserve existing state, log error only
        logger.error("scan: all boards failed, leaving job_matches.json untouched")
        return 2

    gated = _apply_hard_gates(raw, criteria)

    # Dedupe against currently surfaced + applied/passed history + the
    # rolling "ever scored" cache. Without scored_history the same listing
    # gets re-scored every night it stays inside hours_old=72 (~3× waste).
    surfaced_keys = {search.dedupe_key(m) for m in state.load_matches()}
    history_keys = {search.dedupe_key(m) for m in state.load_history()}
    scored_keys = {e["key"] for e in state.load_scored_history() if e.get("key")}
    fresh = search.filter_unseen(
        gated, surfaced_keys | history_keys | scored_keys
    )
    logger.info(
        "scan: %d fresh after dedupe vs surfaced+history+scored_cache "
        "(surfaced=%d history=%d scored_cache=%d)",
        len(fresh), len(surfaced_keys), len(history_keys), len(scored_keys),
    )

    scored: list[Match] = []
    skipped = 0
    for listing in fresh:
        if args.dry_run:
            print(f"[dry-run] would score: {listing['company']} — {listing['title']}")
            continue
        # Per-listing isolation: a single bad row (LLM crash, unexpected
        # listing shape, salary-penalty bug) must not drop the whole batch.
        # score_listing already has internal LLM-failure handling, but the
        # outer match-dict assembly and apply_salary_penalty can still raise.
        # Bug C regression guard: pre-fix, one bad listing aborted the whole
        # scan and `state.save_matches()` never ran.
        try:
            result = score.score_listing(
                listing, criteria, preferences, profile_blob,
                system_prompt_path=scoring_system_prompt_path,
            )
            # Hybrid JD recovery: a no-description listing that still scored
            # >4 is suspect — unknown dims (esp. seniority) defaulted high.
            # Spend a WebFetch to recover the real JD and rescore. If the
            # fetch also fails, soft-downrank + flag rather than trust the
            # blind score or silently drop a wall-blocked posting.
            if (not (listing.get("description") or "").strip()
                    and result["overall"] > 4.0):
                jd = fetch_jd.fetch_job_description(listing["url"])
                if jd:
                    listing["description"] = jd
                    result = score.score_listing(
                        listing, criteria, preferences, profile_blob,
                        system_prompt_path=scoring_system_prompt_path,
                    )
                else:
                    result = score.apply_unverified_penalty(result)
            # Salary is a deterministic post-step (orchestrator-applied so both
            # LLM and rule-based paths get the same treatment): missing salary
            # gets flagged; below-floor gets soft-penalized 0.5.
            result = score.apply_salary_penalty(result, listing, criteria)
            match: Match = {
                "id": state.new_match_id(),
                "source": listing["source"],
                "title": listing["title"],
                "company": listing["company"],
                "location": listing["location"],
                "salary": listing["salary"],
                "url": listing["url"],
                "posted_date": listing["posted_date"],
                "surfaced_date": today,
                "score": {
                    "overall": result["overall"],
                    "dims": result["dims"],
                    "method": result["method"],
                },
                "one_line_take": result["one_line_take"],
                "status": "surfaced",
                "times_carried": 0,
                "last_brief_date": "",
            }
        except Exception:
            skipped += 1
            logger.exception(
                "scan: scoring crashed for company=%r title=%r; skipping listing",
                listing.get("company"), listing.get("title"),
            )
            continue
        scored.append(match)
    if skipped:
        logger.warning("scan: skipped %d listing(s) due to scoring errors", skipped)

    if args.dry_run:
        print(f"[dry-run] would have scored {len(fresh)} listings")
        return 0

    top = _select_top_n(scored, n=args.top_n, threshold=args.threshold)
    merged = _merge_with_carryforward(top, today)
    state.save_matches(merged)

    # Record every scored listing in the rolling cache so tomorrow's scan
    # doesn't re-evaluate them. Fresh-but-skipped (scoring error) listings
    # are intentionally NOT cached — they get a retry tomorrow.
    state.append_scored_keys(
        [search.dedupe_key(m) for m in scored],
        today,
    )

    print(json.dumps({
        "fresh_scored": len(scored),
        "top_n_surfaced": len(top),
        "total_active": len(merged),
        "scored_history_size": len(state.load_scored_history()),
        "board_status": board_status,
    }, indent=2))
    return 0


def cmd_list_active(args: argparse.Namespace) -> int:
    items = state.load_matches()
    items.sort(key=lambda m: (
        -m.get("score", {}).get("overall", 0.0),
        m.get("id", ""),
    ))
    if not items:
        print("(no active matches)")
        return 0
    for i, m in enumerate(items, 1):
        score_v = m.get("score", {}).get("overall", "?")
        carried = m.get("times_carried") or 0
        carried_str = f" (carried {carried}d)" if carried > 0 else ""
        print(
            f"{i}. [{m['id']}] {m['company']} — {m['title']} · "
            f"{m['location']} · {m.get('salary', '')} · score {score_v}{carried_str}"
        )
        take = m.get("one_line_take", "")
        if take:
            print(f"   {take}")
    return 0


def cmd_score_one(args: argparse.Namespace) -> int:
    # For one-off scoring, fetch the JD via JobSpy's URL-based mode if available;
    # otherwise the user pastes it. v0.1 supports URL-with-pasted-text only.
    if not args.description:
        print("score-one requires --description (paste the JD text).", file=sys.stderr)
        return 1
    listing: Listing = {
        "title": args.title or "(unknown title)",
        "company": args.company or "(unknown company)",
        "location": args.location or "",
        "url": args.url,
        "salary": "",
        "posted_date": _today_iso(),
        "source": "manual",
        "description": args.description,
    }
    criteria = state.read_criteria()
    preferences = state.read_preferences()
    profile_blob = _load_profile_blob()
    result = score.score_listing(listing, criteria, preferences, profile_blob)
    print(json.dumps(result, indent=2))
    return 0


def cmd_record_action(args: argparse.Namespace) -> int:
    items = state.load_matches()
    target = next((m for m in items if m["id"] == args.match_id), None)
    if target is None:
        print(f"no active match with id {args.match_id!r}", file=sys.stderr)
        return 1

    today = _today_iso()
    action = args.action.lower()

    if action == "applied":
        # Order matters: do the fallible markdown append BEFORE mutating
        # `target` or saving JSON state. If append_application throws, we
        # haven't half-recorded — the match stays surfaced and the user
        # can retry cleanly.
        state.append_application(
            date=today, company=target["company"], title=target["title"],
            location=target.get("location", ""), url=target.get("url", ""),
        )
        target["status"] = "applied"
        target["action_date"] = today
        state.save_history(state.load_history() + [target])
        state.save_matches([m for m in items if m["id"] != args.match_id])
        print(f"recorded applied: {target['company']} — {target['title']}")
        return 0

    if action == "pass":
        if not args.reason:
            print("pass requires --reason TEXT", file=sys.stderr)
            return 1
        # Same ordering rule as applied — fallible markdown append first.
        state.append_pass_reason(
            date=today, company=target["company"],
            location=target.get("location", ""), reason=args.reason,
        )
        target["status"] = "passed"
        target["action_date"] = today
        target["pass_reason"] = args.reason
        state.save_history(state.load_history() + [target])
        state.save_matches([m for m in items if m["id"] != args.match_id])
        print(f"recorded pass: {target['company']} — {args.reason}")
        return 0

    if action == "tomorrow":
        # No state mutation needed — natural carry-forward at next scan.
        print(f"keeping {target['company']} for tomorrow")
        return 0

    if action == "decoded":
        target["decoded"] = True
        state.save_matches(items)
        print(f"flagged {target['company']} as decoded")
        return 0

    print(f"unknown action: {action}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="job-discovery")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="run the daily pipeline")
    p_scan.add_argument("--dry-run", action="store_true",
                        help="fetch + dedupe but skip scoring + state writes")
    p_scan.add_argument("--top-n", type=int, default=3)
    p_scan.add_argument("--threshold", type=float, default=3.0)
    p_scan.set_defaults(func=cmd_scan)

    p_list = sub.add_parser("list-active", help="print job_matches.json formatted")
    p_list.set_defaults(func=cmd_list_active)

    p_score = sub.add_parser("score-one", help="score a single posting")
    p_score.add_argument("url")
    p_score.add_argument("--title", default="")
    p_score.add_argument("--company", default="")
    p_score.add_argument("--location", default="")
    p_score.add_argument("--description", required=True)
    p_score.set_defaults(func=cmd_score_one)

    p_rec = sub.add_parser("record-action", help="update a match's status")
    p_rec.add_argument("match_id")
    p_rec.add_argument("action", choices=["applied", "pass", "tomorrow", "decoded"])
    p_rec.add_argument("--reason", default="", help="required for action=pass")
    p_rec.set_defaults(func=cmd_record_action)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
