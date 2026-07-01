# job-discovery

Daily job-discovery system for [Mizzix](https://github.com/<owner>/Mizzix). Scores postings from JobSpy-supported boards against Tavin's living preferences and surfaces top matches in the morning brief.

See [DESIGN.md](DESIGN.md) for the full design.

## Install (dev)

    pip install -e ".[dev]"

### Tier-2 JD recovery (Firecrawl)

Tier 1 recovery uses WebFetch (free). For JS-rendered SPAs (Workday, Greenhouse, Lever, iCIMS) and bot-protected pages, tier 2 falls back to a [Firecrawl](https://www.firecrawl.dev) scrape. `firecrawl-py` is a core dependency, so no extra install — just set an API key (free tier ~1000 scrapes/month):

    export FIRECRAWL_API_KEY=fc-...

Firecrawl only runs when tier 1 fails, so a normal scan burns few credits. If the key is unset, tier 2 is silently skipped and recovery behaves as tier-1-only.

## Run tests

    pytest

## Manual scan (no state mutation)

    python -m job_discovery.cli scan --dry-run
