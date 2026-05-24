"""Live smoke test for batch scoring + persistent SDK client.

Runs `score_listings_batch` on 3 listings (covering plausible + non-plausible)
and prints the resulting ScoreResults. Validates end-to-end that the real
Claude Agent SDK path works: one subprocess, one batched query, one parsed
array of 2 scores (the third listing is pre-filtered out and never sees the LLM).

Usage:
    VAULT_PATH=<vault> .venv\\Scripts\\python.exe scripts\\smoke_batch.py
"""
import json
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Make `job_discovery` importable when run as a script
sys.path.insert(0, str(Path(__file__).parent.parent))

from job_discovery import score, state  # noqa: E402


def main() -> int:
    if "VAULT_PATH" not in os.environ:
        print("VAULT_PATH not set", file=sys.stderr)
        return 1

    criteria = state.read_criteria()
    if not criteria["roles"]:
        print("criteria.md is empty — onboard first", file=sys.stderr)
        return 1
    preferences = state.read_preferences()

    # Minimal profile blob to avoid loading the real 30KB one for smoke
    profile_blob = "Tavin: early-mid mechanical/thermal engineer, aerospace background."

    listings = [
        {
            "title": "Mechanical Design Engineer",
            "company": "SmokeAero",
            "location": "Chicago, IL",
            "salary": "$80K-$100K",
            "url": "https://example.com/smoke/1",
            "posted_date": "2026-05-24",
            "source": "linkedin",
            "description": "Hands-on aerospace propulsion design. CAD, FEA, prototype testing.",
        },
        {
            "title": "Thermal Systems Engineer",
            "company": "SmokeHRSG",
            "location": "Denver, CO",
            "salary": "$90K-$110K",
            "url": "https://example.com/smoke/2",
            "posted_date": "2026-05-24",
            "source": "linkedin",
            "description": "Heat-exchanger thermal-fluid design for industrial HRSG.",
        },
        {
            "title": "Sales Engineer",
            "company": "SmokeJunk",
            "location": "Phoenix, AZ",
            "salary": "$50K",
            "url": "https://example.com/smoke/3",
            "posted_date": "2026-05-24",
            "source": "linkedin",
            "description": "Sell our software to procurement teams.",
        },
    ]

    start = time.monotonic()
    results = score.score_listings_batch(
        listings, criteria, preferences, profile_blob,
    )
    elapsed = time.monotonic() - start

    print(f"\nElapsed: {elapsed:.1f}s for {len(listings)} listings\n")
    for listing, result in zip(listings, results):
        print(f"=== {listing['company']} — {listing['title']} ===")
        print(json.dumps(result, indent=2))
        print()

    methods = [r["method"] for r in results]
    print(f"\nMethods: {methods}")
    print("PASS" if any(m == "llm" for m in methods) else "WARN: no LLM scores returned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
