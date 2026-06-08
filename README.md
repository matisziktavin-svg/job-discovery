# job-discovery

Daily job-discovery system for [Mizzix](https://github.com/<owner>/Mizzix). Scores postings from JobSpy-supported boards against Tavin's living preferences and surfaces top matches in the morning brief.

See [DESIGN.md](DESIGN.md) for the full design.

## Install (dev)

    pip install -e ".[dev]"

### Optional: tier-2 JD recovery (Playwright)

Tier 1 recovery uses WebFetch. For JS-rendered SPAs (Workday, Greenhouse, Lever, LinkedIn), tier 2 falls back to a headless Chromium render — install it once:

    pip install -e ".[browser]"
    playwright install chromium

If not installed, tier 2 is silently skipped and recovery behaves as tier-1-only.

## Run tests

    pytest

## Manual scan (no state mutation)

    python -m job_discovery.cli scan --dry-run
