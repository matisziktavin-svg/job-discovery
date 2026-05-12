# job-discovery

Daily job-discovery system for [Mizzix](https://github.com/<owner>/Mizzix). Scores postings from JobSpy-supported boards against Tavin's living preferences and surfaces top matches in the morning brief.

See [DESIGN.md](DESIGN.md) for the full design.

## Install (dev)

    pip install -e ".[dev]"

## Run tests

    pytest

## Manual scan (no state mutation)

    python -m job_discovery.cli scan --dry-run
